"""Experiments beyond the main evaluation: the mapping eval, the noise sweep, and the feedback loop.

Each writes a markdown report (and, where useful, a PNG) to reports/.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import yaml

from reconcile.mapping import propose_fallback, propose_llm
from reconcile.mapping.template import TemplateError, load_client

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
    from reconcile.mapping.profile import read_table

    out: dict[str, set] = {}
    for _kind, tspec in truth.items():
        for field in (tspec.get("values") or {}):
            header = tspec["columns"].get(field)
            path = next(iter(sorted(Path(data_dir).glob(str(tspec["file"]).replace("{end}", "*")))), None)
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
        if kind != "payers":  # the payer file holds whole percentages: its decimal mark is not observable
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
    data_root: Path = Path("data"),
    out_dir: Path = Path("reports"),
    models=("claude-sonnet-5-5", "claude-haiku-5-5"),
) -> dict:
    targets = [(c, data_root / c) for c in ("azur", "maple", "client3")]
    targets += [(p.name, p) for p in sorted((data_root / "variants").glob("variant_*"))]
    results = []
    llm_available = bool(os.environ.get("ANTHROPIC_API_KEY")) or any(
        Path("llm_cache").glob("*.json")
    )
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
    (out_dir / "mapping_eval.json").write_text(json.dumps(results, indent=1, default=str))
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
    lines += [
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


def noise_sweep(levels=(0.5, 1.0, 1.5, 2.0, 3.0), client: str = "azur", scorer: str = "rules",
                out_dir: Path = Path("reports")) -> list[dict]:
    """Regenerate the client at k x every messiness rate (same seed) and rerun the full protocol.
    Answers: how fast does it degrade, and does precision hold while automation falls?"""
    from reconcile.evaluate import evaluate_client
    from reconcile.generate.__main__ import generate_client

    rows = []
    for k in levels:
        d = Path("data/sweep") / f"k{k}"
        generate_client(client, d, noise=k)
        m = evaluate_client(client, Path(f"templates/{client}.yaml"), d / client, scorer, out_dir=None).metrics
        rows.append({"noise": k, "auto_precision": m["auto_precision"], "lb95": m["auto_precision_lb95"],
                     "automation": m["automation_rate"], "suggestion_recall": m["suggestion_recall"],
                     "review_per_week": m["review_per_week"], "wrong_reminders": m["wrong_reminders"],
                     "t_auto": m["thresholds"]["auto"]})
        print(rows[-1])
    _plot_sweep(rows, out_dir / f"noise_sweep_{client}_{scorer}.png", f"{client}, {scorer} scorer")
    lines = [f"# Noise sweep: {client}, {scorer} scorer", "",
             "Every messiness rate (typos, missing references, partial and multi-month payments, missing statements, unknown credits...) multiplied by k; same seed; full protocol each time (thresholds re-chosen on history, reported on months 10-12).", "",
             "| k | auto precision | 95% lower bound | automation | suggestion recall | review / week | wrong reminders |",
             "|---|---|---|---|---|---|---|"]
    fmt = lambda x: "n/a" if x is None else f"{100 * x:.1f}%"  # noqa: E731
    for r in rows:
        lines.append(f"| {r['noise']} | {fmt(r['auto_precision'])} | {fmt(r['lb95'])} | {fmt(r['automation'])} | {fmt(r['suggestion_recall'])} | {r['review_per_week']} | {r['wrong_reminders']} |")
    (out_dir / f"noise_sweep_{client}_{scorer}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return rows


def _plot_sweep(rows: list[dict], path: Path, title: str) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    k = [r["noise"] for r in rows]
    fig, ax = plt.subplots(figsize=(6.4, 3.6), dpi=150)
    ax.plot(k, [100 * (r["auto_precision"] or 0) for r in rows], marker="o", color="#1f5f8b", label="auto precision")
    ax.plot(k, [100 * (r["automation"] or 0) for r in rows], marker="s", color="#c46b2b", label="automation rate")
    ax.plot(k, [100 * (r["suggestion_recall"] or 0) for r in rows], marker="^", color="#5b8c5a", label="suggestion recall")
    ax.axhline(99, color="#888", lw=0.8, ls="--")
    ax.text(k[-1], 99.3, "99% target", ha="right", va="bottom", fontsize=7, color="#666")
    ax.set_xlabel("messiness multiplier k (1 = default rates)")
    ax.set_ylabel("% (test months 10-12)")
    ax.set_ylim(0, 102)
    ax.set_title(f"Noise sweep: {title}", fontsize=10)
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(frameon=False, fontsize=8, loc="lower left")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


# ---------------- feedback loop ----------------


def feedback_loop(client: str = "azur", error_rate: float = 0.03, retrain_every: int = 4,
                  out_dir: Path = Path("reports")) -> list[dict]:
    """Cold start, no historical lettrage: hand-tuned rules with a conservative auto threshold, a
    simulated reviewer (3% error) works the queue each week, and every `retrain_every` weeks a
    model is retrained on the review labels accumulated so far. Tracks live automation/precision
    per 4-week window against the number of labels.

    Caveat: review labels are a biased sample (only what reached the queue), and the threshold is
    chosen on the most recent labels from that same biased stream."""
    import numpy as np

    from reconcile.candidates import Matcher
    from reconcile.dates import month_end, week_ends
    from reconcile.decide import Thresholds, choose_thresholds, cp_lower
    from reconcile.evaluate import load_truth
    from reconcile.ledger import Ledger
    from reconcile.review import SimReviewer
    from reconcile.scorer import GBMScorer, RulesScorer
    from reconcile.state import run_week

    d = Path(f"data/{client}")
    data = load_client(Path(f"templates/{client}.yaml"), d)
    truth = load_truth(d)
    months = [__import__("datetime").date.fromisoformat(m) for m in truth.meta["months"]]
    L = Ledger(data)
    M = Matcher(L, data.template)
    reviewer = SimReviewer(truth.keys, error_rate, seed=1)
    scorer, th = RulesScorer(), Thresholds(auto=0.999, review=0.3, certified_lb=0.0, n_at_auto=0, k_at_auto=0)
    weeks = week_ends(months[0], month_end(months[-1]))
    curve, window = [], []
    for i, w in enumerate(weeks):
        before = set(L.decisions)
        run_week(L, M, w, scorer=scorer, th=th, run_id=f"fb-{w}", reviewer=reviewer, lang=data.language,
                 do_reminders=False)
        window += [h for h in L.decisions if h not in before or L.decisions[h].get("run_id") == f"fb-{w}"]
        if (i + 1) % retrain_every == 0:
            hs = {h for h in window if truth.cls.get(h) == "ar"}
            auto = [h for h in hs if L.decisions[h].get("system_outcome") == "auto"]
            ok = sum(L.decisions[h]["top"]["key"] == truth.keys[h] for h in auto)
            curve.append({"week": w.isoformat(), "labels": len(L.labels), "scorer": scorer.name,
                          "automation": round(len(auto) / max(1, len(hs)), 4),
                          "auto_precision": round(ok / len(auto), 4) if auto else None,
                          "lb95": round(cp_lower(ok, len(auto)), 4)})
            print(curve[-1])
            window = []
            labels = L.labels
            if len(labels) >= 300 and len({x["y"] for x in labels}) == 2:
                cut = int(0.75 * len(labels))
                model = GBMScorer().fit([x["features"] for x in labels[:cut]], [x["y"] for x in labels[:cut]])
                recent = labels[cut:]
                new_th = choose_thresholds(model.score([x["features"] for x in recent]),
                                           np.array([x["y"] for x in recent], dtype=bool))
                if new_th.auto != float("inf"):  # only switch once the model can certify something
                    scorer, th = model, new_th
    _plot_feedback(curve, out_dir / f"feedback_{client}.png")
    lines = [f"# Feedback loop: {client}, cold start, simulated reviewer ({100 * error_rate:.0f}% error)", "",
             "No historical lettrage. Week 1 runs hand-tuned rules with a conservative threshold; the reviewer's decisions become labels; every "
             f"{retrain_every} weeks a model is retrained on all labels so far and its threshold chosen on the most recent quarter of them. Each row is one 4-week window of live decisions.", "",
             "Caveat: review labels are a biased sample (only lines that reached the queue), so the threshold is chosen on a stream that over-represents hard cases.", "",
             "| window ending | labels so far | scorer in use | automation | auto precision | 95% lower bound |", "|---|---|---|---|---|---|"]
    fmt = lambda x: "n/a" if x is None else f"{100 * x:.1f}%"  # noqa: E731
    for r in curve:
        lines.append(f"| {r['week']} | {r['labels']} | {r['scorer']} | {fmt(r['automation'])} | {fmt(r['auto_precision'])} | {fmt(r['lb95'])} |")
    (out_dir / f"feedback_{client}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return curve


def _plot_feedback(curve: list[dict], path: Path, shift_week: str | None = "2025-10-05") -> None:
    import datetime as dt

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    x = [dt.date.fromisoformat(r["week"]) for r in curve]
    fig, ax = plt.subplots(figsize=(7.2, 3.8), dpi=150)
    ax.plot(x, [100 * r["automation"] for r in curve], marker="s", color="#c46b2b", label="automation rate")
    ax.plot(x, [100 * (r["auto_precision"] or 0) for r in curve], marker="o", color="#1f5f8b", label="auto precision")
    if shift_week:
        sx = dt.date.fromisoformat(shift_week)
        ax.axvline(sx, color="#999", lw=0.8, ls="--")
        ax.annotate("bank changes its label format:\nnew layout routed to review until\n30 reviewed lines at >=97% accepted",
                    xy=(sx, 55), xytext=(x[2], 30), fontsize=7, color="#444",
                    arrowprops={"arrowstyle": "->", "color": "#999", "lw": 0.8})
    ax.set_ylabel("% of AR lines (4-week window)")
    ax.set_ylim(0, 105)
    ax.set_title("Cold start, a year of weekly runs with a simulated reviewer", fontsize=10)
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(frameon=False, fontsize=8, loc="lower right")
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def replot_feedback(client: str = "azur", out_dir: Path = Path("reports")) -> None:
    """Rebuild the feedback plot from its markdown table (the experiment takes minutes)."""
    curve = []
    for line in (out_dir / f"feedback_{client}.md").read_text().splitlines():
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) == 6 and cells[0][:2] == "20":
            pct = lambda v: None if v == "n/a" else float(v.rstrip("%")) / 100  # noqa: E731
            curve.append({"week": cells[0], "labels": int(cells[1]), "scorer": cells[2],
                          "automation": pct(cells[3]), "auto_precision": pct(cells[4])})
    _plot_feedback(curve, out_dir / f"feedback_{client}.png")


def reliability(scores, y, path: Path, title: str) -> dict:
    """Reliability diagram + Brier score for displayed (calibrated) probabilities."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    scores, y = np.asarray(scores), np.asarray(y)
    bins = np.linspace(0, 1, 11)
    idx = np.clip(np.digitize(scores, bins) - 1, 0, 9)
    xs, ys, ns = [], [], []
    for b in range(10):
        m = idx == b
        if m.sum() >= 20:
            xs.append(scores[m].mean())
            ys.append(y[m].mean())
            ns.append(int(m.sum()))
    fig, ax = plt.subplots(figsize=(3.8, 3.6), dpi=150)
    ax.plot([0, 1], [0, 1], color="#bbb", lw=0.8, ls="--")
    ax.plot(xs, ys, marker="o", color="#1f5f8b")
    ax.set_xlabel("displayed probability")
    ax.set_ylabel("observed share correct")
    ax.set_title(title, fontsize=9)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return {"brier": round(float(np.mean((scores - y) ** 2)), 4), "bins": len(xs)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["mapping", "sweep", "feedback", "replot"])
    ap.add_argument("--client", default="azur")
    ap.add_argument("--scorer", default="rules")
    a = ap.parse_args()
    if a.what == "mapping":
        mapping_eval()
        print(Path("reports/mapping_eval.md").read_text())
    elif a.what == "sweep":
        noise_sweep(client=a.client, scorer=a.scorer)
    elif a.what == "replot":
        replot_feedback(a.client)
    else:
        feedback_loop(client=a.client)


if __name__ == "__main__":
    main()
