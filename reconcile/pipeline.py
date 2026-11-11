"""Command line: `python -m reconcile.pipeline <command> --client <name> ...`

eval    full protocol (history -> thresholds -> 12 weekly test runs), writes reports/ and state/
run     one weekly run on the saved state (idempotent: re-running a week imports nothing new)
export  lettrage file of every posted allocation, for re-import into the billing software
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

from reconcile.audit import AuditLog
from reconcile.candidates import Matcher
from reconcile.decide import Thresholds
from reconcile.evaluate import evaluate_client
from reconcile.export import export_lettrage
from reconcile.ledger import Ledger
from reconcile.mapping.template import load_client
from reconcile.scorer import GBMScorer, RulesScorer
from reconcile.state import load_state, run_week, save_state


def _paths(client: str) -> dict:
    return {
        "template": Path(f"templates/{client}.yaml"),
        "data": Path(f"data/{client}"),
        "state": Path(f"state/{client}.sqlite"),
        "reports": Path("reports"),
    }


def cmd_eval(a) -> None:
    p = _paths(a.client)
    run = evaluate_client(
        a.client,
        p["template"],
        p["data"],
        a.scorer,
        reviewer_error=a.reviewer_error,
        out_dir=p["reports"],
        state_path=p["state"],
    )
    m = run.metrics
    print(
        json.dumps(
            {
                k: m[k]
                for k in (
                    "auto_lines",
                    "auto_precision",
                    "auto_precision_lb95",
                    "automation_rate",
                    "suggestion_recall",
                    "review_per_week",
                    "wrong_reminders",
                    "balance_ok_all_runs",
                )
            },
            indent=1,
        )
    )
    print(f"report: reports/eval_{a.client}_{run.scorer.name}.md   state: {p['state']}")


def _load(client: str):
    p = _paths(client)
    data = load_client(p["template"], p["data"])
    L = Ledger(data)
    meta = load_state(L, p["state"])
    th = Thresholds(**meta["thresholds"])
    model_path = p["state"].with_suffix(".model.pkl")
    scorer = (
        GBMScorer.load(model_path)
        if meta["scorer"] == "model" and model_path.exists()
        else RulesScorer()
    )
    return p, data, L, meta, th, scorer


def cmd_run(a) -> None:
    p, data, L, meta, th, scorer = _load(a.client)
    week = dt.date.fromisoformat(a.week)
    audit = AuditLog(
        p["reports"] / f"audit_{a.client}.jsonl", {"scorer": scorer.name, "t_auto": th.auto}
    )
    res = run_week(
        L,
        Matcher(L, data.template),
        week,
        scorer=scorer,
        th=th,
        run_id=f"manual-{week}",
        lang=data.language,
        audit=audit,
    )
    save_state(L, p["state"], meta | {"aliases": [[k, sorted(v)] for k, v in L.aliases.items()], "sig_review": dict(L.sig_review)})
    print(json.dumps(res, default=str, indent=1))


def cmd_export(a) -> None:
    p, data, L, *_ = _load(a.client)
    out = p["reports"] / f"lettrage_{a.client}.csv"
    print(f"{export_lettrage(L, out)} allocations -> {out}")


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
    a = ap.parse_args()
    {"eval": cmd_eval, "run": cmd_run, "export": cmd_export}[a.cmd](a)


if __name__ == "__main__":
    main()
