"""Command line: `python -m reconcile.pipeline <command> --client <name> ...`

eval     full protocol (history -> thresholds -> 14 weekly test runs), writes reports/ and state/
run      one weekly run on the saved state (idempotent: re-running a week imports nothing new)
export   lettrage file of every posted allocation, for re-import into the billing software
explain  why one bank line was auto-posted, queued or left unapplied
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from dataclasses import dataclass

from reconcile.audit import AuditLog
from reconcile.candidates import Matcher
from reconcile.decide import Thresholds
from reconcile.evaluate import evaluate_client
from reconcile.export import export_lettrage
from reconcile.ledger import Ledger
from reconcile.mapping.template import ClientData, load_client
from reconcile.paths import REPORTS, ClientPaths, client_paths
from reconcile.scorer import GBMScorer, RulesScorer, Scorer
from reconcile.state import load_state, run_week, save_state

HEADLINE = (
    "auto_lines",
    "auto_precision",
    "auto_precision_lb95",
    "automation_rate",
    "review_top1_accuracy",
    "review_per_week",
    "wrong_reminders",
    "balance_ok_all_runs",
)


@dataclass
class Loaded:
    paths: ClientPaths
    data: ClientData
    ledger: Ledger
    meta: dict
    thresholds: Thresholds
    scorer: Scorer


def load(client: str) -> Loaded:
    """Saved state for a client, with the exact scorer its thresholds were certified for."""
    p = client_paths(client)
    data = load_client(p.template, p.data)
    L = Ledger(data)
    meta = load_state(L, p.state)
    scorer = (
        GBMScorer.load(p.model, meta["model_sha256"])
        if meta["scorer"] == "model"
        else RulesScorer()
    )
    return Loaded(p, data, L, meta, Thresholds(**meta["thresholds"]), scorer)


def cmd_eval(a: argparse.Namespace) -> None:
    p = client_paths(a.client)
    run = evaluate_client(
        a.client,
        p.template,
        p.data,
        a.scorer,
        reviewer_error=a.reviewer_error,
        out_dir=REPORTS,
        state_path=p.state,
    )
    print(json.dumps({k: run.metrics[k] for k in HEADLINE}, indent=1))
    print(f"report: reports/eval_{a.client}_{run.scorer.name}.md   state: {p.state}")


def cmd_run(a: argparse.Namespace) -> None:
    s = load(a.client)
    week = dt.date.fromisoformat(a.week)
    audit = AuditLog(
        REPORTS / f"audit_{a.client}.jsonl", {"scorer": s.scorer.name, "t_auto": s.thresholds.auto}
    )
    res = run_week(
        s.ledger,
        Matcher(s.ledger, s.data.template),
        week,
        scorer=s.scorer,
        th=s.thresholds,
        run_id=f"manual-{week}",
        lang=s.data.language,
        audit=audit,
    )
    save_state(s.ledger, s.paths.state, s.meta)
    print(json.dumps(res, default=str, indent=1))


def cmd_export(a: argparse.Namespace) -> None:
    s = load(a.client)
    out = REPORTS / f"lettrage_{a.client}.csv"
    print(f"{export_lettrage(s.ledger, out)} allocations -> {out}")


def cmd_explain(a: argparse.Namespace) -> None:
    s = load(a.client)
    L = s.ledger
    hits = [h for h in L.lines if h.startswith(a.line)]
    if len(hits) != 1:
        raise SystemExit(f"{len(hits)} bank lines start with {a.line!r}; give a longer prefix")
    h = hits[0]
    line, dec = L.lines[h], L.decisions.get(h, {})
    print(f"bank line {h}  {line['date']}  {line['amount_cents'] / 100:,.2f}")
    print(f"  label:     {line['label']}")
    print(f"  reference: {line.get('reference') or '-'}")
    print(
        f"outcome: {dec.get('outcome', 'not decided yet')}"
        + (" (final)" if dec.get("final") else "")
    )
    if dec.get("guard"):
        print(f"  held by the {dec['guard']} guard (would otherwise have been auto-posted)")
    print(f"thresholds: auto >= {s.thresholds.auto:.4f}, review >= {s.thresholds.review:.4f}")
    for i, opt in enumerate([dec.get("top")] + dec.get("alts", [])):
        if not opt:
            continue
        print(f"{'top' if i == 0 else f'alt {i}'}: {opt['mtype']}  score {opt['score']:.4f}")
        for key, cents in opt["edges"]:
            sh = L.shares[key]
            print(f"    {sh.invoice_id}  {sh.payer_name} ({sh.payer_type})  {cents / 100:,.2f}")
        for reason in opt.get("reasons", []):
            print(f"    {reason}")
    posted = [x for x in L.allocations if x["bank_hash"] == h]
    for x in posted:
        print(
            f"posted: {x['invoice_id']}  {x['cents'] / 100:,.2f}  by {x['source']} in {x['run_id']}"
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("eval")
    e.add_argument("--client", required=True)
    e.add_argument("--scorer", default="model", choices=["rules", "model"])
    e.add_argument("--reviewer-error", type=float, default=0.0)
    r = sub.add_parser("run")
    r.add_argument("--client", required=True)
    r.add_argument("--week", required=True, help="week end date, YYYY-MM-DD")
    x = sub.add_parser("export")
    x.add_argument("--client", required=True)
    w = sub.add_parser("explain")
    w.add_argument("--client", required=True)
    w.add_argument("--line", required=True, help="bank line hash, or a unique prefix of it")
    a = ap.parse_args()
    {"eval": cmd_eval, "run": cmd_run, "export": cmd_export, "explain": cmd_explain}[a.cmd](a)


if __name__ == "__main__":
    main()
