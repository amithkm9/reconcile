"""Run the hand-written hard cases (eval/hard_cases.yaml) through the real weekly pipeline.

Each case is a tiny ledger in the common entity model (it bypasses file mapping on purpose: these
cases test matching and reminder logic). Thresholds are fixed (auto >= 0.95, review >= 0.30) so
the cases do not depend on what a particular dataset certified.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import yaml

from reconcile.candidates import Matcher, key_of
from reconcile.decide import Thresholds
from reconcile.ids import bank_line_hash
from reconcile.ledger import Ledger
from reconcile.mapping.template import ClientData
from reconcile.scorer import RulesScorer
from reconcile.state import run_week

HARD_CASES = Path("eval/hard_cases.yaml")
FIXED = Thresholds(auto=0.95, review=0.30, certified_lb=0.0, n_at_auto=0, k_at_auto=0)


def load_cases(path: Path = HARD_CASES) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _d(s) -> dt.date:
    return s if isinstance(s, dt.date) else dt.date.fromisoformat(str(s))


def _cents(x) -> int:
    return round(float(x) * 100)


def _bank_row(ln: dict, file_end: dt.date) -> dict:
    d = _d(ln["date"])
    c = _cents(ln["amount"])
    return {
        "bank_hash": bank_line_hash(d.isoformat(), c, ln["label"], ln.get("ref", "")),
        "date": d,
        "label": ln["label"],
        "reference": ln.get("ref", ""),
        "amount_cents": c,
        "file_end": file_end,
        "source_file": "case",
    }


def build_case(base: dict, case: dict) -> tuple[ClientData, dict]:
    inv_rows, pay_rows = [], []
    for rid, r in base["residents"].items():
        exit_d = _d(r["exit"]) if r.get("exit") else None
        for m in base["months"]:
            y, mm = map(int, m.split("-"))
            start = dt.date(y, mm, 1)
            if exit_d and exit_d < start:
                continue
            prev = dt.date(y - (mm == 1), 12 if mm == 1 else mm - 1, 25)
            inv_rows.append(
                {
                    "invoice_id": f"FA{y}-00{int(rid):02d}{mm:02d}",
                    "resident_id": str(rid),
                    "resident_name": r["name"],
                    "facility": r["facility"],
                    "issue_date": prev,
                    "due_date": dt.date(y, mm, 10),
                    "period": m,
                    "doc_type": "invoice",
                    "amount_cents": _cents(r["monthly"]),
                    "original_invoice": "",
                }
            )
        for p in r["payers"]:
            pay_rows.append(
                {
                    "resident_id": str(rid),
                    "resident_name": r["name"],
                    "payer_name": p["name"],
                    "payer_type": p["type"],
                    "share_pct": float(p["pct"]),
                    "mandate_ref": p.get("mandate", ""),
                    "resident_exit_date": exit_d,
                }
            )
    rem_rows = []
    for rem in case.get("remittances", []):
        for ben, period, amount in rem["rows"]:
            rem_rows.append(
                {
                    "remittance_id": rem["id"],
                    "date": _d(rem["date"]),
                    "payer_name": rem["payer"],
                    "facility": rem.get("facility", ""),
                    "beneficiary_name": ben,
                    "period": period,
                    "amount_cents": _cents(amount),
                }
            )
    lines = list(case.get("history", [])) + ([case["line"]] if "line" in case else [])
    as_of = (
        _d(case["as_of"]) if "as_of" in case else _d(case["line"]["date"]) + dt.timedelta(days=3)
    )
    bank = pd.DataFrame(
        [_bank_row(ln, as_of) for ln in lines],
        columns=[
            "bank_hash",
            "date",
            "label",
            "reference",
            "amount_cents",
            "file_end",
            "source_file",
        ],
    )
    rem_df = pd.DataFrame(
        rem_rows,
        columns=[
            "remittance_id",
            "date",
            "payer_name",
            "facility",
            "beneficiary_name",
            "period",
            "amount_cents",
        ],
    )
    tpl = {"client": "hard_cases", "language": "fr", "patterns": base["patterns"]}
    data = ClientData(
        "hard_cases", tpl, pd.DataFrame(inv_rows), pd.DataFrame(pay_rows), rem_df, bank, "fr"
    )
    return data, {"as_of": as_of}


def run_case(base: dict, case: dict) -> dict:
    data, ctx = build_case(base, case)
    L = Ledger(data)
    for rid in case.get("jan_paid", []):  # January already lettered in the client's ledger
        for s in L.by_invoice[f"FA2025-00{int(rid):02d}01"]:
            L.allocated[s.key] += s.cents
    for ln in case.get("history", []):
        row = _bank_row(ln, ctx["as_of"])
        L.import_lines([row])
        edges = []
        for inv, amount in ln["allocation"]:
            share = (
                L.by_invoice[inv][0]
                if len(L.by_invoice[inv]) == 1
                else max(L.by_invoice[inv], key=lambda s: s.payer_type in ("resident", "family"))
            )
            edges.append((share.key, _cents(amount)))
        L.post(
            row["bank_hash"],
            edges,
            ln.get("source", "historical"),
            "history",
            learn_alias=not ln["label"].startswith(("PRLV", "REJET")),
        )
        L.decisions[row["bank_hash"]] = {
            "bank_hash": row["bank_hash"],
            "outcome": "historical",
            "final": True,
            "posted": True,
        }
    M = Matcher(L, data.template)
    run_week(
        L, M, ctx["as_of"], scorer=RulesScorer(), th=FIXED, run_id="case", mode="decide", lang="fr"
    )
    out: dict = {"id": case["id"]}
    if "line" in case:
        h = _bank_row(case["line"], ctx["as_of"])["bank_hash"]
        dec = L.decisions[h]
        top = dec.get("top") or {}
        out |= {
            "outcome": dec["outcome"],
            "allocation": top.get("key"),
            "score": top.get("score"),
            "reasons": top.get("reasons", []),
        }
    out["reminders"] = {r["invoice_id"]: r["status"] for r in L.reminders}
    return out


def check(case: dict, result: dict) -> list[str]:
    """Problems with `result` against the case's expectations (empty list = pass)."""
    exp = case.get("expect", {})
    problems = []
    if "outcome" in exp and result["outcome"] != exp["outcome"]:
        problems.append(f"outcome {result['outcome']} != {exp['outcome']}")
    if "outcome_in" in exp and result["outcome"] not in exp["outcome_in"]:
        problems.append(f"outcome {result['outcome']} not in {exp['outcome_in']}")
    if "allocation" in exp:
        want = key_of([[i, _cents(a)] for i, a in exp["allocation"]])
        if result.get("allocation") != want:
            problems.append(
                f"allocation {sorted(result.get('allocation') or [])} != {sorted(want)}"
            )
    for inv, status in case.get("reminders", {}).items():
        got = result["reminders"].get(inv, "none")
        if got != status:
            problems.append(f"reminder {inv}: {got} != {status}")
    return problems
