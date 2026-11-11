"""The receivables ledger: payer shares of each invoice, and what has been allocated to them.

An invoice is split into one *share* per payer (e.g. département 80% / resident 20%). Payments are
allocated to shares, not invoices, because each payer owes its own part and pays it on its own
schedule. Everything downstream (open amounts, candidates, reminders) reads from here.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass

from reconcile.alloc_rules import SETTLE_TOLERANCE_CENTS, payer_identity, split_shares
from reconcile.mapping.template import ClientData
from reconcile.normalize import label_signature, name_part, norm_name, payer_key

PUBLIC_TYPES = {"public_aid", "guardian", "health_funder"}


@dataclass(slots=True)
class Share:
    key: str
    invoice_id: str
    resident_id: str
    resident_name: str
    facility: str
    pid: str
    payer_key: str
    payer_name: str
    payer_type: str
    mandate: str
    cents: int
    issue_date: dt.date
    due_date: dt.date
    period: str
    exit_date: dt.date | None


def build_shares(data: ClientData) -> list[Share]:
    inv = data.invoices
    credits = (
        inv[inv.doc_type == "credit"].groupby("original_invoice")["amount_cents"].sum().to_dict()
    )
    payers_by_res: dict[str, list[dict]] = defaultdict(list)
    for p in data.payers.to_dict("records"):
        payers_by_res[p["resident_id"]].append(p)
    shares = []
    for r in inv[inv.doc_type == "invoice"].to_dict("records"):
        plist = payers_by_res.get(r["resident_id"], [])
        if not plist:
            continue
        net = r["amount_cents"] + credits.get(r["invoice_id"], 0)
        items = [(payer_key(p["payer_name"]), float(p["share_pct"])) for p in plist]
        for p, cents in zip(plist, split_shares(net, items), strict=True):
            pk = payer_key(p["payer_name"])
            pid = payer_identity(pk, p["payer_type"], r["resident_id"])
            while any(x.key == f"{r['invoice_id']}|{pid}" for x in shares[-len(plist) :]):
                pid += "#2"  # a resident and a namesake relative both paying: keep two shares
            shares.append(
                Share(
                    f"{r['invoice_id']}|{pid}",
                    r["invoice_id"],
                    r["resident_id"],
                    r["resident_name"],
                    r["facility"],
                    pid,
                    pk,
                    p["payer_name"],
                    p["payer_type"],
                    p["mandate_ref"] or "",
                    cents,
                    r["issue_date"],
                    r["due_date"],
                    r["period"],
                    p["resident_exit_date"],
                )
            )
    return shares


class Ledger:
    """In-memory state of one client's receivables; `state.py` persists it to sqlite."""

    def __init__(self, data: ClientData):
        self.data = data
        self.shares: dict[str, Share] = {s.key: s for s in build_shares(data)}
        self.by_pid: dict[str, list[Share]] = defaultdict(list)
        self.by_invoice: dict[str, list[Share]] = defaultdict(list)
        for s in sorted(self.shares.values(), key=lambda s: (s.due_date, s.invoice_id)):
            self.by_pid[s.pid].append(s)
            self.by_invoice[s.invoice_id].append(s)
        self.allocated: dict[str, int] = defaultdict(int)
        self.returned: dict[str, int] = defaultdict(int)  # cents returned (DD rejects) per share
        self.lines: dict[str, dict] = {}
        self.allocations: list[dict] = []
        self.decisions: dict[str, dict] = {}
        self.labels: list[dict] = []
        self.reminders: list[dict] = []
        self.runs: list[dict] = []
        self.aliases: dict[str, set[str]] = defaultdict(set)
        self.signature_counts: dict[str, int] = defaultdict(
            int
        )  # label layouts lettered in history
        self.sig_review: dict[str, list[int]] = defaultdict(
            lambda: [0, 0]
        )  # guard-held lines of a new layout: [reviewed, top accepted]
        self.pid_history: dict[str, list[tuple[dt.date, int]]] = defaultdict(list)

    # ---- queries ----
    def open(self, key: str) -> int:
        return self.shares[key].cents - self.allocated.get(key, 0)

    def open_shares(self, pid: str, as_of: dt.date) -> list[Share]:
        """Open shares of a payer issued by `as_of`. Residuals <= 2 EUR count as settled
        (written-off payment differences), as an AR team would treat them."""
        return [
            s
            for s in self.by_pid.get(pid, [])
            if s.issue_date <= as_of and self.open(s.key) > SETTLE_TOLERANCE_CENTS
        ]

    def invoice_open(self, invoice_id: str) -> int:
        return sum(max(0, self.open(s.key)) for s in self.by_invoice.get(invoice_id, []))

    def fits(self, edges: list[tuple[str, int]]) -> bool:
        """Can these edges be posted now without over-allocating a share (or over-returning one)?"""
        need: dict[str, int] = defaultdict(int)
        for k, c in edges:
            need[k] += c
        return all(
            (c <= self.open(k)) if c > 0 else (-c <= self.allocated.get(k, 0))
            for k, c in need.items()
        )

    # ---- updates ----
    def import_lines(self, bank_rows: list[dict]) -> int:
        """Idempotent: a line already known (same content hash) is ignored."""
        new = 0
        for r in bank_rows:
            if r["bank_hash"] not in self.lines:
                self.lines[r["bank_hash"]] = r
                new += 1
        return new

    def post(
        self,
        bank_hash: str,
        edges: list[tuple[str, int]],
        source: str,
        run_id: str,
        learn_alias: bool = False,
    ) -> None:
        """Allocate a bank line. `learn_alias`: remember how this payer's bank prints their name
        (only for transfers typed by a person, never for mandate or bulk lines)."""
        line = self.lines[bank_hash]
        for key, cents in edges:
            self.allocated[key] += cents
            if cents < 0:
                self.returned[key] += -cents
            s = self.shares[key]
            self.allocations.append(
                {
                    "bank_hash": bank_hash,
                    "share_key": key,
                    "invoice_id": s.invoice_id,
                    "cents": cents,
                    "source": source,
                    "run_id": run_id,
                    "date": line["date"],
                }
            )
        if source in ("historical", "auto"):  # layouts trusted through history, not via review
            self.signature_counts[label_signature(line["label"])] += 1
        pids = {self.shares[k].pid for k, c in edges if c > 0}
        for pid in pids:
            self.pid_history[pid].append((line["date"], line["amount_cents"]))
            if learn_alias and self.shares[edges[0][0]].payer_type not in PUBLIC_TYPES:
                alias = name_part(line["label"])
                if alias and alias != norm_name(self.shares[edges[0][0]].payer_name):
                    self.aliases[alias].add(pid)
