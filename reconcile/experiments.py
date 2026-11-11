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
    for kind, tspec in truth.items():
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["mapping"])
    a = ap.parse_args()
    if a.what == "mapping":
        mapping_eval()
        print(Path("reports/mapping_eval.md").read_text())


if __name__ == "__main__":
    main()
