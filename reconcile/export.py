"""Lettrage export: the allocations in a flat file the billing software can re-import, so the work
done here does not stay trapped in our UI."""

from __future__ import annotations

import csv
from pathlib import Path

from reconcile.ledger import Ledger


def export_lettrage(
    L: Ledger, path: Path, sources: tuple[str, ...] = ("auto", "review_accept", "review_pick")
) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [a for a in L.allocations if a["source"] in sources]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "invoice_id",
                "payer_share",
                "bank_line",
                "bank_date",
                "amount",
                "decided_by",
                "run_id",
            ]
        )
        for a in rows:
            w.writerow(
                [
                    a["invoice_id"],
                    a["share_key"].split("|", 1)[1],
                    a["bank_hash"],
                    a["date"].isoformat(),
                    f"{a['cents'] / 100:.2f}",
                    a["source"],
                    a["run_id"],
                ]
            )
    return len(rows)
