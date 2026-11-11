"""Experiments beyond the main evaluation: the mapping eval, the noise sweep, and the feedback loop.

Each writes a markdown report (and, where useful, a PNG) to reports/.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from reconcile import llm
from reconcile.candidates import Matcher
from reconcile.dates import month_end, week_ends
from reconcile.decide import Thresholds, choose_thresholds, cp_lower
from reconcile.evaluate import evaluate_client, load_truth
from reconcile.generate import generate_client
from reconcile.ledger import Ledger
from reconcile.mapping import propose_fallback, propose_llm
from reconcile.mapping.profile import read_table
from reconcile.mapping.template import TemplateError, load_client
from reconcile.paths import REPORTS, ROOT, client_paths
from reconcile.plots import fmt_pct, plot_feedback, plot_outcomes, plot_sweep
from reconcile.review import SimReviewer
from reconcile.scorer import GBMScorer, RulesScorer
from reconcile.state import run_week

log = logging.getLogger(__name__)

ENTITY_FIELDS = {
    "invoices": [
        "invoice_id",
        "issue_date",
        "due_date",
        "resident_id",
        "resident_name",
        "facility",
        "period",
        "doc_type",
        "amount",
        "original_invoice",
    ],
    "payers": [
        "resident_id",
        "resident_name",
        "payer_name",
        "payer_type",
        "share_pct",
        "mandate_ref",
        "resident_exit_date",
    ],
    "remittances": [
        "remittance_id",
        "date",
        "payer_name",
        "facility",
        "beneficiary_name",
        "period",
        "amount",
    ],
    "bank": ["date", "label", "reference", "debit", "credit", "amount"],
}


def _truth_fields(kind: str, tspec: dict) -> dict:
    cols = tspec["columns"]
    out = {}
    for f in ENTITY_FIELDS[kind]:
        if f == "resident_name" and kind == "invoices" and "resident_name" not in cols:
            if "resident_last" in cols:
                out[f] = sorted([cols["resident_last"], cols["resident_first"]])
            continue
        if kind == "bank" and f in ("debit", "credit") and tspec.get("amount_mode") == "signed":
            continue
        if kind == "bank" and f == "amount" and tspec.get("amount_mode") != "signed":
            continue
        if f in cols:
            out[f] = cols[f]
    return out


def _present_values(data_dir: Path, truth: dict) -> dict:
    """Category values that actually occur in the exports (per field), from the true mapping."""

    out: dict[str, set] = {}
    for tspec in truth.values():
        for field in tspec.get("values") or {}:
            header = tspec["columns"].get(field)
            path = next(
                iter(sorted(Path(data_dir).glob(str(tspec["file"]).replace("{end}", "*")))), None
            )
            if header and path:
                _, head, body = read_table(path)
                if header in head:
                    i = head.index(header)
                    out[field] = {r[i].strip() for r in body}
    return out


def score_template(tpl: dict, truth: dict, present: dict | None = None) -> dict:
    """Per-decision correctness of a proposed template against the generator's true mapping."""
    checks: list[tuple[str, bool]] = []
    for kind, tspec in truth.items():
        pspec = tpl["files"].get(kind)
        if pspec is None:
            checks.append((f"{kind}.role", False))
            continue
        tfile = str(tspec["file"]).replace("{end}", "")
        checks.append((f"{kind}.role", Path(tfile).stem.rstrip("_") in pspec["path"]))
        for f, want in _truth_fields(kind, tspec).items():
            got = pspec["columns"].get(f)
            got = sorted(got) if isinstance(got, list) else got
            checks.append((f"{kind}.{f}", got == want))
        checks.append((f"{kind}.date_format", pspec.get("date_format") == tspec["date_format"]))
        if (
            kind != "payers"
        ):  # the payer file holds whole percentages: its decimal mark is not observable
            checks.append((f"{kind}.decimal", pspec.get("decimal") == tspec.get("decimal", ".")))
        if kind == "bank":
            checks.append(
                ("bank.amount_mode", pspec.get("amount_mode") == tspec.get("amount_mode"))
            )
        for field, canon_to_src in (tspec.get("values") or {}).items():
            pvals = (pspec.get("values") or {}).get(field, {})
            for canon, src in canon_to_src.items():
                if present is not None and src not in present.get(field, set()):
                    continue  # this category never occurs in the client's data: nothing to map
                checks.append((f"{kind}.{field}={canon}", pvals.get(src) == canon))
    ok = sum(c for _, c in checks)
    return {
        "decisions": len(checks),
        "correct": ok,
        "accuracy": round(ok / len(checks), 4),
        "wrong": [n for n, c in checks if not c],
    }


def mapping_eval(
    data_root: Path = ROOT / "data",
    out_dir: Path = REPORTS,
    models: tuple[str, ...] | None = None,
) -> dict:
    """Propose a template for every schema (deterministic mapper, and each configured model when
    a key or cached responses exist), score it against the generator's true mapping, and write
    reports/mapping_eval.md."""
    models = models or llm.configured_models()
    targets = [(c, data_root / c) for c in ("azur", "maple", "client3")]
    targets += [(p.name, p) for p in sorted((data_root / "variants").glob("variant_*"))]
    results = []
    llm_available = bool(os.environ.get("ANTHROPIC_API_KEY")) or any(llm.CACHE.glob("*.json"))
    for name, d in targets:
        truth = yaml.safe_load((d / "_truth" / "mapping.yaml").read_text(encoding="utf-8"))
        t0 = time.time()
        tpl, review = propose_fallback.propose(d, name)
        secs = time.time() - t0
        present = _present_values(d, truth)
        row = {
            "target": name,
            "fallback": score_template(tpl, truth, present),
            "fallback_seconds": round(secs, 2),
            "fallback_flags": sum(1 for r in review if r.get("flag")),
        }
        try:
            load_client(tpl, d)
            row["fallback_loads"] = True
        except (TemplateError, ValueError, KeyError) as e:
            row["fallback_loads"] = False
            row["fallback_load_error"] = str(e)[:160]
        for model in models:
            if not llm_available:
                row[model] = "not run (no API key, no cached responses)"
                continue
            ltpl, _, rec = propose_llm.propose(d, name, model)
            row[model] = (
                score_template(ltpl, truth, present)
                | {"cost_usd": rec.get("cost_usd"), "cache": rec.get("cache")}
                if ltpl
                else f"failed: {rec}"
            )
        results.append(row)
    write_mapping_report(results, out_dir)
    return {"results": results}


def write_mapping_report(results: list[dict], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    total = sum(r["fallback"]["decisions"] for r in results)
    ok = sum(r["fallback"]["correct"] for r in results)
    loads = sum(r["fallback_loads"] for r in results)
    perfect = sum(r["fallback"]["accuracy"] == 1.0 for r in results)
    wrong_counts: dict[str, int] = {}
    for r in results:
        for w in r["fallback"]["wrong"]:
            key = w.split(".", 1)[1] if "." in w else w
            wrong_counts[key] = wrong_counts.get(key, 0) + 1
    lines = [
        "# Mapping eval: proposed templates vs the true mapping",
        "",
        f"{len(results)} schemas: the 3 clients plus 12 generated variants (renamed/reordered columns, other separators, number and date conventions, signed vs debit/credit bank amounts, renamed category values, a junk column). Variant headers come from a generator-only pool, disjoint from the mapper's vocabulary.",
        "",
        "Each *decision* is one thing the template must get right: a file's role, a field's column, a file's date format or decimal separator, the bank amount mode, or one category value mapping.",
        "",
        "| Mapper | Decisions correct | Schemas fully correct | Templates that load and pass the data-quality gates (wrong ones that still load) |",
        "|---|---|---|---|",
        f"| deterministic fallback | {ok}/{total} ({100 * ok / total:.1f}%) | {perfect}/{len(results)} | {loads}/{len(results)} ({sum(r['fallback_loads'] and r['fallback']['accuracy'] < 1 for r in results)} silently wrong) |",
    ]
    for model in [k for k in results[0] if k.startswith("claude")]:
        vals = [r[model] for r in results]
        if all(isinstance(v, dict) for v in vals):
            mt = sum(v["decisions"] for v in vals)
            mo = sum(v["correct"] for v in vals)
            cost = sum(v.get("cost_usd") or 0 for v in vals)
            lines.append(
                f"| {model} | {mo}/{mt} ({100 * mo / mt:.1f}%) | {sum(v['accuracy'] == 1 for v in vals)}/{len(vals)} | (cost ${cost:.3f}) |"
            )
        else:
            lines.append(f"| {model} | {vals[0]} | | |")
    var = [r["fallback"] for r in results if r["target"].startswith("variant")]
    v_ok, v_total = sum(v["correct"] for v in var), sum(v["decisions"] for v in var)
    lines += [
        "",
        f"On the {len(var)} unseen-vocabulary variants alone, the fallback gets {v_ok}/{v_total} "
        f"decisions right ({100 * v_ok / v_total:.1f}%).",
        "",
        "## Per schema (fallback)",
        "",
        "| schema | accuracy | loads | flags raised | seconds | wrong decisions |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        f = r["fallback"]
        lines.append(
            f"| {r['target']} | {100 * f['accuracy']:.1f}% | {r['fallback_loads']} | {r['fallback_flags']} | {r['fallback_seconds']} | {', '.join(f['wrong']) or '-'} |"
        )
    lines += ["", "## Most frequent mistakes", ""]
    lines += [
        f"- {k}: {v} schema(s)" for k, v in sorted(wrong_counts.items(), key=lambda kv: -kv[1])
    ] or ["- none"]
    (out_dir / "mapping_eval.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------- noise sweep ----------------


def noise_sweep(
    levels=(0.5, 1.0, 1.5, 2.0, 3.0),
    client: str = "azur",
    scorer: str = "rules",
    out_dir: Path = REPORTS,
) -> list[dict]:
    """Regenerate the client at k x every messiness rate (same seed) and rerun the full protocol.
    Answers: how fast does it degrade, and does precision hold while automation falls?"""

    rows = []
    for k in levels:
        d = ROOT / "data" / "sweep" / f"k{k}"
        generate_client(client, d, noise=k)
        m = evaluate_client(
            client, client_paths(client).template, d / client, scorer, out_dir=None
        ).metrics
        rows.append(
            {
                "noise": k,
                "auto_precision": m["auto_precision"],
                "lb95": m["auto_precision_lb95"],
                "automation": m["automation_rate"],
                "suggestion_recall": m["suggestion_recall"],
                "review_top1": m["review_top1_accuracy"],
                "review_per_week": m["review_per_week"],
                "wrong_reminders": m["wrong_reminders"],
                "t_auto": m["thresholds"]["auto"],
            }
        )
        log.info("%s", rows[-1])
    plot_sweep(rows, out_dir / f"noise_sweep_{client}_{scorer}.png", f"{client}, {scorer} scorer")
    lines = [
        f"# Noise sweep: {client}, {scorer} scorer",
        "",
        "Every messiness rate (typos, missing references, partial and multi-month payments, missing statements, unknown credits...) multiplied by k; same seed; full protocol each time (thresholds re-chosen on history, reported on months 10-12).",
        "",
        "| k | auto precision | 95% lower bound | automation | review: top suggestion right | review / week | wrong reminders |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['noise']} | {fmt_pct(r['auto_precision'])} | {fmt_pct(r['lb95'])} | {fmt_pct(r['automation'])} | {fmt_pct(r['review_top1'])} | {r['review_per_week']} | {r['wrong_reminders']} |"
        )
    (out_dir / f"noise_sweep_{client}_{scorer}.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    return rows


# ---------------- feedback loop ----------------


def feedback_loop(
    client: str = "azur",
    error_rate: float = 0.03,
    retrain_every: int = 4,
    out_dir: Path = REPORTS,
) -> list[dict]:
    """Cold start, no historical lettrage: hand-tuned rules with a conservative auto threshold, a
    simulated reviewer (3% error) works the queue each week, and every `retrain_every` weeks a
    model is retrained on the review labels accumulated so far. Reports live automation and
    precision per calendar month of bank lines, with the labels collected so far.

    Caveat: review labels are a biased sample (only what reached the queue), and the threshold is
    chosen on the most recent labels from that same biased stream."""

    d = client_paths(client).data
    data = load_client(client_paths(client).template, d)
    truth = load_truth(d)
    months = [dt.date.fromisoformat(m) for m in truth.meta["months"]]
    L = Ledger(data)
    M = Matcher(L, data.template)
    reviewer = SimReviewer(truth.keys, error_rate, seed=1)
    scorer, th = (
        RulesScorer(),
        Thresholds(auto=0.999, review=0.3, certified_lb=0.0, n_at_auto=0, k_at_auto=0),
    )
    weeks = week_ends(months[0], month_end(months[-1]))
    # Reported per calendar month of the bank line, not per 4-week window: monthly direct-debit
    # runs dominate the auto count, and a window that catches only part of one looks like a dip.
    month_state: dict[str, dict] = {}
    for i, w in enumerate(weeks):
        res = run_week(
            L,
            M,
            w,
            scorer=scorer,
            th=th,
            run_id=f"fb-{w}",
            reviewer=reviewer,
            lang=data.language,
            do_reminders=False,
        )
        ms = month_state.setdefault(f"{w:%Y-%m}", {"guard": 0})
        ms["guard"] += res.get("guard_new_format", 0) or 0
        ms["labels"], ms["scorer"] = len(L.labels), scorer.name
        if (i + 1) % retrain_every == 0:
            labels = L.labels
            if len(labels) >= 300 and len({x["y"] for x in labels}) == 2:
                cut = int(0.75 * len(labels))
                model = GBMScorer().fit(
                    [x["features"] for x in labels[:cut]], [x["y"] for x in labels[:cut]]
                )
                recent = labels[cut:]
                new_th = choose_thresholds(
                    model.score([x["features"] for x in recent]),
                    np.array([x["y"] for x in recent], dtype=bool),
                )
                if new_th.auto != float("inf"):  # only switch once the model can certify something
                    scorer, th = model, new_th
    by_month: dict[str, list[str]] = {}
    for h in L.decisions:
        if truth.cls.get(h) == "ar":
            by_month.setdefault(str(L.lines[h]["date"])[:7], []).append(h)
    curve: list[dict[str, Any]] = []
    for m in sorted(by_month):
        hs = by_month[m]
        auto = [h for h in hs if L.decisions[h].get("system_outcome") == "auto"]
        ok = sum(L.decisions[h]["top"]["key"] == truth.keys[h] for h in auto)
        st = month_state.get(m, {})
        curve.append(
            {
                "week": m,
                "labels": st.get("labels", len(L.labels)),
                "scorer": st.get("scorer", scorer.name),
                "automation": round(len(auto) / max(1, len(hs)), 4),
                "auto_precision": round(ok / len(auto), 4) if auto else None,
                "lb95": round(cp_lower(ok, len(auto)), 4),
                "guard": st.get("guard", 0),
            }
        )
        log.info("%s", curve[-1])
    plot_feedback(curve, out_dir / f"feedback_{client}.png")
    lines = [
        f"# Feedback loop: {client}, cold start, simulated reviewer ({100 * error_rate:.0f}% error)",
        "",
        "No historical lettrage. Week 1 runs hand-tuned rules with a conservative threshold; the reviewer's decisions become labels; every "
        f"{retrain_every} weeks a model is retrained on all labels so far and its threshold chosen on the most recent quarter of them. Each row is one calendar month of bank lines (by line date). From October the bank's transfer labels use a new layout; the last column counts auto-posts the new-format guard held back for review.",
        "",
        "Caveat: review labels are a biased sample (only lines that reached the queue), so the threshold is chosen on a stream that over-represents hard cases.",
        "",
        "| month | labels so far | scorer in use | automation | auto precision | 95% lower bound | held by format guard |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in curve:
        lines.append(
            f"| {r['week']} | {r['labels']} | {r['scorer']} | {fmt_pct(r['automation'])} | {fmt_pct(r['auto_precision'])} | {fmt_pct(r['lb95'])} | {r['guard']} |"
        )
    (out_dir / f"feedback_{client}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return curve


def _parse_pct(v: str) -> float | None:
    return None if v == "n/a" else float(v.rstrip("%")) / 100


def replot_feedback(client: str = "azur", out_dir: Path = REPORTS) -> None:
    """Rebuild the feedback plot from its markdown table (the experiment takes minutes)."""
    curve = []
    for line in (out_dir / f"feedback_{client}.md").read_text().splitlines():
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) == 7 and cells[0][:2] == "20":
            curve.append(
                {
                    "week": cells[0],
                    "labels": int(cells[1]),
                    "scorer": cells[2],
                    "automation": _parse_pct(cells[3]),
                    "auto_precision": _parse_pct(cells[4]),
                    "guard": int(cells[6]),
                }
            )
    plot_feedback(curve, out_dir / f"feedback_{client}.png")


def write_outcomes_chart(out_dir: Path = REPORTS) -> Path:
    """reports/outcomes.png from the three clients' rules metrics (run after `make eval`)."""
    labels = {"azur": "Azur (FR)", "maple": "Maple (UK)", "client3": "Client 3 (UK)"}
    metrics = {
        label: json.loads((out_dir / f"metrics_{c}_rules.json").read_text())
        for c, label in labels.items()
    }
    path = out_dir / "outcomes.png"
    plot_outcomes(metrics, path)
    return path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["mapping", "sweep", "feedback", "replot", "outcomes"])
    ap.add_argument("--client", default="azur")
    ap.add_argument("--scorer", default="rules")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if a.what == "mapping":
        mapping_eval()
        print((REPORTS / "mapping_eval.md").read_text())
    elif a.what == "sweep":
        noise_sweep(client=a.client, scorer=a.scorer)
    elif a.what == "replot":
        replot_feedback(a.client)
    elif a.what == "outcomes":
        write_outcomes_chart()
    else:
        feedback_loop(client=a.client)


if __name__ == "__main__":
    main()
