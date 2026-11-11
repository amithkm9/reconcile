"""End-to-end evaluation against ground truth.

Protocol (by time, never shuffled):
  months 1-7  : historical lettrage posted as-is (what a client's ledger already contains);
                candidates generated against that point-in-time state become training rows
  months 8-9  : same, used only to choose thresholds (and fit display calibration)
  months 10-12: the system decides, week by week, through `run_week`; a simulated reviewer
                handles the review queue; reminders are drafted. These months also contain
                corruption types never seen before (bank label change, married names, new
                remittance layout). Only these months are reported.
"""

from __future__ import annotations

import datetime as dt
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from reconcile.alloc_rules import SETTLE_TOLERANCE_CENTS
from reconcile.audit import AuditLog
from reconcile.candidates import Matcher, key_of
from reconcile.dates import month_end, week_ends
from reconcile.decide import Thresholds, choose_thresholds, cp_lower
from reconcile.ledger import Ledger
from reconcile.mapping.template import ClientData, load_client
from reconcile.review import SimReviewer
from reconcile.scorer import GBMScorer, RulesScorer
from reconcile.state import run_week, save_state

TRAIN_MONTHS, THRESHOLD_MONTHS = 7, 2


@dataclass
class Truth:
    keys: dict
    share_edges: dict
    cls: dict
    match_type: dict
    shift_tag: dict
    month_index: dict
    date: dict
    amount: dict
    meta: dict

    def as_run_truth(self) -> dict:
        return {
            "keys": self.keys,
            "share_edges": self.share_edges,
            "class": self.cls,
            "match_type": self.match_type,
        }


def load_truth(data_dir: Path) -> Truth:
    t = pd.read_csv(Path(data_dir) / "_truth" / "ground_truth.csv", keep_default_na=False)
    meta = yaml.safe_load((Path(data_dir) / "_truth" / "meta.yaml").read_text())
    h = t.bank_hash.tolist()
    return Truth(
        keys={k: key_of(json.loads(a)) for k, a in zip(h, t.allocation_json, strict=True)},
        share_edges={
            k: [tuple(e) for e in json.loads(a)] for k, a in zip(h, t.share_alloc_json, strict=True)
        },
        cls=dict(zip(h, t.true_class, strict=True)),
        match_type=dict(zip(h, t.match_type, strict=True)),
        shift_tag=dict(zip(h, t.shift_tag, strict=True)),
        month_index=dict(zip(h, t.month_index.astype(int), strict=True)),
        date={k: dt.date.fromisoformat(d) for k, d in zip(h, t.date, strict=True)},
        amount=dict(zip(h, t.amount_cents.astype(int), strict=True)),
        meta=meta,
    )


def chosen_per_line(
    rows: list[dict], scores: np.ndarray
) -> tuple[np.ndarray, np.ndarray, list[dict]]:
    """Best candidate per line (what allocation would pick, ignoring rare within-week conflicts)."""
    best: dict[str, tuple[float, dict]] = {}
    for r, s in zip(rows, scores, strict=True):
        if r["bank_hash"] not in best or s > best[r["bank_hash"]][0]:
            best[r["bank_hash"]] = (s, r)
    vals = list(best.values())
    return np.array([v[0] for v in vals]), np.array([v[1]["y"] for v in vals]), [v[1] for v in vals]


@dataclass
class EvalRun:
    client: str
    data: ClientData
    truth: Truth
    ledger: Ledger
    scorer: object
    thresholds: Thresholds
    train_rows: list
    thr_rows: list
    test_rows: list
    test_weeks: list
    metrics: dict


def prepare_history(data: ClientData, truth: Truth, teacher_months: int = TRAIN_MONTHS + THRESHOLD_MONTHS):
    """Teacher-forced pass: post the historical lettrage for the first `teacher_months` months and
    collect the candidate rows generated against that point-in-time state."""
    months = [dt.date.fromisoformat(m) for m in truth.meta["months"]]
    test_start = months[TRAIN_MONTHS + THRESHOLD_MONTHS]
    until = months[teacher_months]
    L = Ledger(data)
    M = Matcher(L, data.template)
    rows: list[dict] = []
    for w in [w for w in week_ends(months[0], month_end(months[-1])) if w < until]:
        run_week(L, M, w, scorer=None, th=None, run_id=f"hist-{w}", mode="teacher",
                 truth=truth.as_run_truth(), collect=rows, do_reminders=False)
    thr_start = months[TRAIN_MONTHS]
    train = [r for r in rows if r["date"] < thr_start]
    thr = [r for r in rows if thr_start <= r["date"] < test_start]
    return L, M, train, thr, months, test_start


def fit_scorer(kind: str, train: list[dict], seed: int = 0):
    if kind == "rules":
        return RulesScorer()
    return GBMScorer().fit([r["features"] for r in train], [r["y"] for r in train], seed=seed)


def _shadow_thresholds(L: Ledger, M: Matcher, truth: Truth, scorer, months, test_start, reviewer, lang):
    """Months 8-9 as a *shadow run*: the trained model decides week by week on live state (with
    the review queue worked), and the auto threshold is chosen on what it actually decided.

    Why: the model is trained on historical lettrage, where every earlier payment is perfectly
    lettered. Live state is not (lines wait in review, some are rejected), so signals like "oldest
    open invoice first" are less reliable than in training. Thresholding on the shadow run prices
    that train/serve skew in; thresholding on teacher-forced rows hid it (98.2% on test for a
    threshold that certified 99.0%).
    """
    from reconcile.decide import Thresholds as Th

    provisional = Th(auto=float("inf"), review=0.3, certified_lb=0.0, n_at_auto=0, k_at_auto=0)
    rows: list[dict] = []
    for w in [w for w in week_ends(months[0], month_end(months[-1])) if months[TRAIN_MONTHS] <= w < test_start]:
        run_week(L, M, w, scorer=scorer, th=provisional, run_id=f"shadow-{w}", mode="decide",
                 truth=truth.as_run_truth(), reviewer=reviewer, lang=lang, collect=rows, do_reminders=False)
    scores, correct = [], []
    for h, d in L.decisions.items():
        if d.get("run_id", "").startswith("shadow-") and d.get("top") and truth.month_index.get(h, -1) in (7, 8):
            scores.append(d["top"]["score"])
            correct.append(truth.cls[h] == "ar" and d["top"]["key"] == truth.keys[h])
    th = choose_thresholds(np.array(scores), np.array(correct, dtype=bool))
    scorer.calibrate([r["features"] for r in rows], [r["y"] for r in rows])
    return th, rows, len(scores), int(sum(correct))


def evaluate_client(client: str, template: Path, data_dir: Path, scorer_kind: str = "rules",
                    reviewer_error: float = 0.0, out_dir: Path | None = Path("reports"),
                    state_path: Path | None = None, keep_last_queue: bool = True) -> EvalRun:
    """Rules: months 1-9 are historical lettrage and all nine are out-of-sample for an untrained
    scorer, so its threshold uses them all. Model: months 1-7 historical lettrage for training,
    months 8-9 a shadow run for the threshold (see _shadow_thresholds). Both report months 10-12."""
    data = load_client(template, data_dir)
    truth = load_truth(data_dir)
    reviewer = SimReviewer(truth.keys, reviewer_error)
    if scorer_kind == "rules":
        L, M, train, thr, months, test_start = prepare_history(data, truth)
        scorer = fit_scorer("rules", train)
        s_thr, c_thr, _ = chosen_per_line(train + thr, scorer.score([r["features"] for r in train + thr]))
        th = choose_thresholds(s_thr, c_thr)
        thr_info = {"lines": int(len(s_thr)), "correct_chosen": int(c_thr.sum()), "months": "1-9 (historical lettrage)"}
    else:
        L, M, train, _, months, test_start = prepare_history(data, truth, teacher_months=TRAIN_MONTHS)
        scorer = fit_scorer("model", train)
        th, thr, n, k = _shadow_thresholds(L, M, truth, scorer, months, test_start, reviewer, data.language)
        thr_info = {"lines": n, "correct_chosen": k, "months": "8-9 (shadow run)"}
    audit = AuditLog(
        out_dir / f"audit_{client}.jsonl" if out_dir else None,
        {"template": data.template.get("version", 1), "scorer": scorer.name, "t_auto": th.auto},
    )
    if audit.path and audit.path.exists():
        audit.path.unlink()
    weeks = [w for w in week_ends(months[0], month_end(months[-1])) if w >= test_start]
    test_rows: list[dict] = []
    for i, w in enumerate(weeks):
        last = i == len(weeks) - 1
        run_week(
            L,
            M,
            w,
            scorer=scorer,
            th=th,
            run_id=f"run-{w}",
            mode="decide",
            truth=truth.as_run_truth(),
            reviewer=None if (last and keep_last_queue) else reviewer,
            audit=audit,
            lang=data.language,
            collect=test_rows,
        )
    metrics = compute_metrics(L, truth, th, test_start, train + thr, test_rows, weeks)
    metrics |= {
        "client": client,
        "scorer": scorer.name,
        "thresholds": th.as_dict(),
        "n_train_rows": len(train),
        "n_threshold_rows": len(thr),
        "threshold_set": thr_info,
    }
    if isinstance(scorer, GBMScorer) and out_dir and test_rows:
        from reconcile.experiments import reliability

        probs = scorer.display_probability([r["features"] for r in test_rows])
        metrics["calibration_test"] = reliability(probs, [r["y"] for r in test_rows],
                                                  out_dir / f"reliability_{client}.png",
                                                  f"{client}: calibrated model, test months")
    run = EvalRun(client, data, truth, L, scorer, th, train, thr, test_rows, weeks, metrics)
    if out_dir:
        write_report(run, out_dir)
    if state_path:
        save_state(
            L,
            state_path,
            {
                "client": client,
                "scorer": scorer.name,
                "thresholds": th.as_dict(),
                "aliases": [[a, sorted(p)] for a, p in L.aliases.items()],
                "sig_review": dict(L.sig_review),
            },
        )
        if isinstance(scorer, GBMScorer):
            scorer.save(state_path.with_suffix(".model.pkl"))
    return run


# ---------------- metrics ----------------


def _rate(a: int, b: int) -> float | None:
    return round(a / b, 4) if b else None


def compute_metrics(
    L: Ledger, truth: Truth, th: Thresholds, test_start: dt.date, hist_rows, test_rows, weeks
) -> dict:
    test = [h for h in L.lines if truth.month_index.get(h, -1) >= TRAIN_MONTHS + THRESHOLD_MONTHS]
    ar = [h for h in test if truth.cls[h] == "ar"]
    per = {}
    for h in test:
        d = L.decisions.get(h, {})
        top = d.get("top") or {}
        per[h] = {
            "out": d.get("system_outcome", d.get("outcome", "undecided")),
            "key": top.get("key"),
            "res": d.get("resolution"),
        }
    auto = [h for h in test if per[h]["out"] == "auto"]
    auto_ok = [h for h in auto if truth.cls[h] == "ar" and per[h]["key"] == truth.keys[h]]
    edges_pred = sum(len(per[h]["key"]) for h in auto)
    edges_ok = sum(len(per[h]["key"] & truth.keys[h]) for h in auto)
    amt = lambda hs: sum(abs(L.lines[h]["amount_cents"]) for h in hs)  # noqa: E731
    sugg_ok = [
        h for h in ar if per[h]["key"] == truth.keys[h] and per[h]["out"] in ("auto", "review")
    ]
    review = [h for h in test if per[h]["out"] == "review"]
    # candidate recall: is the true allocation among the candidates at all?
    in_cands_hist = defaultdict(int)
    for r in hist_rows:
        in_cands_hist[r["bank_hash"]] |= r["y"]
    in_cands_test = defaultdict(int)
    for r in test_rows:
        in_cands_test[r["bank_hash"]] |= r["y"]
    hist_ar = [h for h in L.lines if truth.cls.get(h) == "ar" and truth.month_index.get(h, 99) < 9]
    by_type_recall = defaultdict(lambda: [0, 0])
    for h in hist_ar + ar:
        src = in_cands_hist if h in in_cands_hist or truth.month_index[h] < 9 else in_cands_test
        by_type_recall[truth.match_type[h]][0] += src.get(h, 0)
        by_type_recall[truth.match_type[h]][1] += 1

    # breakdowns
    def breakdown(group_of) -> dict:
        g = defaultdict(list)
        for h in ar:
            g[group_of(h)].append(h)
        out = {}
        for name, hs in sorted(g.items()):
            a = [h for h in hs if per[h]["out"] == "auto"]
            ok = [h for h in a if per[h]["key"] == truth.keys[h]]
            out[name] = {
                "n": len(hs),
                "automation": _rate(len(a), len(hs)),
                "auto_precision": _rate(len(ok), len(a)),
                "suggestion_recall": _rate(
                    sum(per[h]["key"] == truth.keys[h] for h in hs), len(hs)
                ),
            }
        return out

    confusion = Counter((truth.cls[h], per[h]["out"]) for h in test)
    # reminders
    rem_test = [r for r in L.reminders if r["as_of"] >= test_start]
    paid_by = _truth_paid_by(truth)
    net = defaultdict(int)
    for s in L.shares.values():
        net[s.invoice_id] += s.cents
    wrong = [
        r
        for r in rem_test
        if r["status"] == "drafted"
        and _paid_as_of(paid_by.get(r["invoice_id"], []), r["as_of"])
        >= net[r["invoice_id"]] - SETTLE_TOLERANCE_CENTS
    ]
    last = weeks[-1]
    reminded = {r["invoice_id"] for r in rem_test}
    missed = [
        inv
        for inv, shares in L.by_invoice.items()
        if shares[0].due_date + dt.timedelta(days=15) < last - dt.timedelta(days=28)
        and shares[0].due_date >= test_start - dt.timedelta(days=60)
        and net[inv] - _paid_as_of(paid_by.get(inv, []), last) > SETTLE_TOLERANCE_CENTS
        and inv not in reminded
    ]
    errors = []
    for h in auto:
        if per[h]["key"] != truth.keys[h]:
            ln = L.lines[h]
            errors.append(
                {
                    "date": ln["date"].isoformat(),
                    "label": ln["label"],
                    "reference": ln.get("reference", ""),
                    "amount": ln["amount_cents"] / 100,
                    "truth_type": truth.match_type[h],
                    "shift": truth.shift_tag[h],
                    "predicted": sorted(per[h]["key"] or []),
                    "truth": sorted(truth.keys[h]),
                }
            )
    n_weeks = len(weeks)
    return {
        "test_lines": len(test),
        "test_ar_lines": len(ar),
        "test_weeks": n_weeks,
        "auto_lines": len(auto),
        "auto_correct": len(auto_ok),
        "auto_precision": _rate(len(auto_ok), len(auto)),
        "auto_precision_lb95": round(cp_lower(len(auto_ok), len(auto)), 4),
        "edge_precision": _rate(edges_ok, edges_pred),
        "amount_weighted_precision": _rate(amt(auto_ok), amt(auto)),
        "automation_rate": _rate(len([h for h in auto if truth.cls[h] == "ar"]), len(ar)),
        "automation_rate_eur": _rate(amt([h for h in auto if truth.cls[h] == "ar"]), amt(ar)),
        "suggestion_recall": _rate(len(sugg_ok), len(ar)),
        "review_lines": len(review),
        "review_per_week": round(len(review) / n_weeks, 1),
        "candidate_recall_all_months": _rate(
            sum(v[0] for v in by_type_recall.values()), sum(v[1] for v in by_type_recall.values())
        ),
        "candidate_recall_by_type": {k: _rate(*v) for k, v in sorted(by_type_recall.items())},
        "confusion": {f"{a}->{b}": n for (a, b), n in sorted(confusion.items())},
        "by_match_type": breakdown(lambda h: truth.match_type[h]),
        "by_shift": breakdown(lambda h: truth.shift_tag[h] or "none"),
        "reminders_drafted": sum(r["status"] == "drafted" for r in rem_test),
        # distinct invoices: a held invoice is re-checked (and counted) every week otherwise
        "reminders_held": {
            reason: len({r["invoice_id"] for r in rem_test if r["hold_reason"] == reason})
            for reason in sorted({r["hold_reason"] for r in rem_test if r["hold_reason"]})
        },
        # the price of the safety holds: invoices really unpaid whose reminder a hold delayed
        "reminders_delayed_by_holds": len(
            {
                r["invoice_id"]
                for r in rem_test
                if r["hold_reason"] in ("unapplied_cash", "pending_review")
                and _paid_as_of(paid_by.get(r["invoice_id"], []), r["as_of"])
                < net[r["invoice_id"]] - SETTLE_TOLERANCE_CENTS
            }
        ),
        "wrong_reminders": len(wrong),
        "wrong_reminder_examples": [r["invoice_id"] for r in wrong[:10]],
        "missed_reminders": len(missed),
        "balance_ok_all_runs": all(r["balance_ok"] for r in L.runs),
        "greedy_components_checked": sum(r.get("gap_checked", 0) for r in L.runs),
        "greedy_suboptimal": sum(r.get("gap_suboptimal", 0) for r in L.runs),
        "auto_blocked_by_new_format_guard": sum(1 for h in test if L.decisions.get(h, {}).get("guard") == "new_format"),
        "stranded_lines": sum(1 for h in test if L.decisions.get(h, {}).get("stranded")),
        "errors": errors,
    }


def _truth_paid_by(truth: Truth) -> dict[str, list[tuple[dt.date, int]]]:
    out: dict[str, list] = defaultdict(list)
    for h, key in truth.keys.items():
        for inv, c in key:
            out[inv].append((truth.date[h], c))
    return out


def _paid_as_of(events: list[tuple[dt.date, int]], as_of: dt.date) -> int:
    return sum(c for d, c in events if d <= as_of)


# ---------------- report ----------------


def _pct(x) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}%"


def write_report(run: EvalRun, out_dir: Path) -> Path:
    m, th = run.metrics, run.thresholds
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"metrics_{run.client}_{run.scorer.name}.json").write_text(
        json.dumps(m, indent=2, default=str)
    )
    lines = [
        f"# Evaluation: {run.client} ({run.scorer.name} scorer)",
        "",
        "Generated by `reconcile.evaluate`. Test = months 10-12, decided week by week; every number below is computed against ground truth.",
        "",
        "## Headline",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Test bank lines (AR lines) | {m['test_lines']} ({m['test_ar_lines']}) |",
        f"| Auto-matched lines | {m['auto_lines']} |",
        f"| Auto precision (exact allocation) | {_pct(m['auto_precision'])} |",
        f"| 95% lower bound on auto precision (Clopper-Pearson, n={m['auto_lines']}) | {_pct(m['auto_precision_lb95'])} |",
        f"| Edge precision / amount-weighted precision | {_pct(m['edge_precision'])} / {_pct(m['amount_weighted_precision'])} |",
        f"| Automation rate (count / EUR) | {_pct(m['automation_rate'])} / {_pct(m['automation_rate_eur'])} |",
        f"| Suggestion recall (auto + correct top review suggestion) | {_pct(m['suggestion_recall'])} |",
        f"| Review queue | {m['review_lines']} lines, {m['review_per_week']} / week |",
        f"| Wrong reminders (drafted for an invoice already paid) | {m['wrong_reminders']} |",
        f"| Missed reminders (overdue, unpaid, never drafted or held) | {m['missed_reminders']} |",
        f"| Reminder drafts / invoices held, by reason | {m['reminders_drafted']} / {m['reminders_held']} |",
        f"| Reminders delayed by a safety hold although the invoice was really unpaid | {m['reminders_delayed_by_holds']} |",
        f"| Auto-posts held back by the new-label-format guard | {m['auto_blocked_by_new_format_guard']} |",
        f"| Money balances after every weekly run | {m['balance_ok_all_runs']} |",
        f"| Candidate recall (truth among candidates, all months) | {_pct(m['candidate_recall_all_months'])} |",
        "",
        f"## Thresholds (chosen on months {m['threshold_set']['months']} only; never on test months)",
        "",
        f"- auto >= {th.auto:.4f}: CP lower bound {_pct(th.certified_lb)} on {th.n_at_auto} chosen candidates ({th.k_at_auto} correct)",
        f"- review >= {th.review:.4f}",
        f"- threshold-selection set: {m['threshold_set']['lines']} lines; training rows: {m['n_train_rows']}",
        "",
        "## By match type (AR lines, test months)",
        "",
        "| Type | n | Automation | Auto precision | Suggestion recall | Candidate recall |",
        "|---|---|---|---|---|---|",
    ]
    for k, v in m["by_match_type"].items():
        lines.append(
            f"| {k} | {v['n']} | {_pct(v['automation'])} | {_pct(v['auto_precision'])} | {_pct(v['suggestion_recall'])} | {_pct(m['candidate_recall_by_type'].get(k))} |"
        )
    lines += [
        "",
        "## Held-out corruption (never seen before month 10)",
        "",
        "| Shift | n | Automation | Auto precision | Suggestion recall |",
        "|---|---|---|---|---|",
    ]
    for k, v in m["by_shift"].items():
        lines.append(
            f"| {k} | {v['n']} | {_pct(v['automation'])} | {_pct(v['auto_precision'])} | {_pct(v['suggestion_recall'])} |"
        )
    lines += [
        "",
        "## Outcome confusion (truth class -> system outcome)",
        "",
        "| truth -> outcome | lines |",
        "|---|---|",
    ]
    lines += [f"| {k} | {v} |" for k, v in m["confusion"].items()]
    lines += [
        "",
        "## Allocation",
        "",
        f"- greedy vs exhaustive on small conflict components: {m['greedy_suboptimal']} suboptimal of {m['greedy_components_checked']} checked",
        f"- lines stranded by conflicts: {m['stranded_lines']}",
        "",
        f"## Every auto-match error on the test set ({len(m['errors'])})",
        "",
    ]
    if m["errors"]:
        lines += [
            "| date | label | ref | amount | truth type | shift | predicted | truth |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for e in m["errors"]:
            lines.append(
                f"| {e['date']} | {e['label']} | {e['reference']} | {e['amount']:.2f} | {e['truth_type']} | {e['shift'] or '-'} | {e['predicted']} | {e['truth']} |"
            )
    else:
        lines.append("None.")
    p = out_dir / f"eval_{run.client}_{run.scorer.name}.md"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p
