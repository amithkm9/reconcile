"""Features for one candidate allocation.

Every feature reads only the line itself, the candidate, and ledger state *before* this line
(open amounts and payer history come from allocations already posted), so a feature can never see
the future. `test_features_are_point_in_time` checks that.
"""

from __future__ import annotations

import statistics

from reconcile.alloc_rules import SETTLE_TOLERANCE_CENTS
from reconcile.candidates import Cand
from reconcile.ledger import Ledger
from reconcile.normalize import month_tokens

MTYPES = ["dd_return", "mandate", "reference", "remit", "bulk", "name_fifo", "name_single"]
PTYPES = ["resident", "family", "public_aid", "guardian", "health_funder", "other"]

FEATURES = (
    [f"mt_{m}" for m in MTYPES]
    + [f"pt_{p}" for p in PTYPES]
    + [
        "n_edges",
        "residual_rel",
        "residual_zero",
        "covers_full",
        "partial_ratio",
        "days_from_due_min",
        "days_from_due_max",
        "fifo_consistent",
        "skips_older_open",
        "mandate_hit",
        "inv_ref_hit",
        "month_hit",
        "name_score",
        "alias_hit",
        "resident_hit",
        "remit_backed",
        "remit_resolved",
        "remit_name_min",
        "n_valid",
        "n_excluded",
        "n_added",
        "n_cands_line",
        "n_pids_line",
        "hist_n",
        "hist_amount_match",
        "hist_dom_delta",
        "log_amount",
        "is_debit",
    ]
)


def candidate_features(c: Cand, line: dict, L: Ledger) -> dict:
    s = c.signals
    shares = [L.shares[k] for k, _ in c.edges]
    amount = int(line["amount_cents"])
    edge_sum = sum(x for _, x in c.edges)
    opens = [L.open(k) for k, _ in c.edges]
    d = line["date"]
    pids = {sh.pid for sh in shares}
    ptype = shares[0].payer_type if shares else "other"
    # FIFO consistency: did this allocation skip an older open share of the same payer?
    skipped = 0
    if len(pids) == 1 and (c.mtype.startswith("name") or c.mtype in ("reference", "mandate")):
        pid = shares[0].pid
        touched = {k for k, _ in c.edges}
        oldest_touched = min(sh.due_date for sh in shares)
        skipped = sum(
            1
            for sh in L.open_shares(pid, d)
            if sh.key not in touched and sh.due_date < oldest_touched
        )
    months = month_tokens(f"{line['label']} {line.get('reference') or ''}")
    hist = L.pid_history.get(next(iter(pids)), []) if len(pids) == 1 else []
    hist = [h for h in hist if h[0] < d]
    f: dict[str, float] = {f"mt_{m}": int(c.mtype == m) for m in MTYPES} | {
        f"pt_{p}": int(ptype == p) for p in PTYPES
    }
    f |= {
        "n_edges": len(c.edges),
        "residual_rel": c.residual / abs(amount) if amount else 0.0,
        "residual_zero": int(c.residual == 0),
        "covers_full": int(
            all(x >= o - SETTLE_TOLERANCE_CENTS for (_, x), o in zip(c.edges, opens, strict=True))
        )
        if amount > 0
        else 1,
        "partial_ratio": edge_sum / sum(opens) if amount > 0 and sum(opens) > 0 else 1.0,
        "days_from_due_min": min((d - sh.due_date).days for sh in shares) if shares else 0,
        "days_from_due_max": max((d - sh.due_date).days for sh in shares) if shares else 0,
        "fifo_consistent": int(skipped == 0),
        "skips_older_open": skipped,
        "mandate_hit": s.get("mandate_hit", 0),
        "inv_ref_hit": s.get("inv_ref_hit", 0),
        "month_hit": int(bool(months) and any(int(sh.period[5:]) in months for sh in shares)),
        "name_score": s.get("name_score", 0.0),
        "n_close_pids": s.get("n_close_pids", 0),
        "alias_hit": s.get("alias_hit", 0),
        "resident_hit": s.get("resident_hit", 0),
        "remit_backed": s.get("remit_backed", 0),
        "remit_resolved": s.get("remit_resolved", 0.0),
        "remit_name_min": s.get("remit_name_min", 0.0),
        "n_valid": s.get("n_valid", 1),
        "n_excluded": s.get("n_excluded", 0),
        "n_added": s.get("n_added", 0),
        "n_cands_line": s.get("n_cands_line", 1),
        "n_pids_line": s.get("n_pids_line", 1),
        "hist_n": len(hist),
        "hist_amount_match": int(
            any(abs(h[1] - amount) <= max(100, abs(amount) // 100) for h in hist[-3:])
        ),
        "hist_dom_delta": abs(d.day - statistics.median(h[0].day for h in hist)) if hist else 15,
        "log_amount": len(str(abs(amount))),
        "is_debit": int(amount < 0),
    }
    return f
