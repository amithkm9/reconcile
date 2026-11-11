"""Lettrage export: the allocations in a flat file the billing software can re-import, so the work
done here does not stay trapped in our UI."""

from __future__ import annotations

import csv
from pathlib import Path

from reconcile.ledger import Ledger

COLUMNS = ["invoice_id", "payer_share", "bank_line", "bank_date", "amount", "decided_by", "run_id"]


def _text_cell(v: str) -> str:
    """Text from client files can start with = + - @: a spreadsheet would run it as a formula."""
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


def export_lettrage(
    L: Ledger, path: Path, sources: tuple[str, ...] = ("auto", "review_accept", "review_pick")
) -> int:
    """Write every posted allocation from `sources` (not historical lettrage, which the billing
    software already has); returns the number of rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [a for a in L.allocations if a["source"] in sources]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        for a in rows:
            w.writerow(
                [
                    _text_cell(a["invoice_id"]),
                    _text_cell(L.shares[a["share_key"]].pid),
                    a["bank_hash"],
                    a["date"].isoformat(),
                    f"{a['cents'] / 100:.2f}",
                    a["source"],
                    a["run_id"],
                ]
            )
    return len(rows)
