"""Accounting conventions shared by the generator and the matcher.

These are *conventions* (how a French/UK AR team letters a payment), not ground truth. The generator
uses them to write the truth, and the matcher uses them to build candidate allocations, so an exact
match between the two means the matcher picked the right invoices, not that it did arithmetic the
same way by luck.
"""

from __future__ import annotations

from collections.abc import Hashable, Sequence
from typing import TypeVar

from reconcile.normalize import norm_id

PRIVATE_PAYER_TYPES = {"resident", "family", "other"}


K = TypeVar("K", bound=Hashable)  # whatever identifies an open item (a share key)


def payer_identity(pkey: str, ptype: str, resident_id: str) -> str:
    """Private payers are scoped to their resident (two 'Marie Martin' are two people);
    public bodies, guardians and health funders are one payer across many residents."""
    return f"{pkey}@{norm_id(resident_id)}" if ptype in PRIVATE_PAYER_TYPES else pkey


# An open balance at or below this is a payment difference (écart de règlement), treated as settled.
SETTLE_TOLERANCE_CENTS = 200


def share_order(items: Sequence[tuple[str, float]]) -> list[int]:
    """Deterministic order of (payer_key, pct) shares: biggest share first, then by name."""
    return sorted(range(len(items)), key=lambda i: (-items[i][1], items[i][0]))


def split_shares(net_cents: int, items: Sequence[tuple[str, float]]) -> list[int]:
    """Split an invoice's net amount into per-payer shares (pct in 0..100).

    Each share is rounded to the cent; the last share in `share_order` takes the remainder so the
    shares always add up to the invoice exactly.
    """
    order = share_order(items)
    out = [0] * len(items)
    total_pct = sum(p for _, p in items) or 100.0
    running = 0
    for pos, i in enumerate(order):
        if pos == len(order) - 1:
            out[i] = net_cents - running
        else:
            out[i] = round(net_cents * items[i][1] / total_pct)
            running += out[i]
    return out


def fifo_allocate(
    amount: int, open_items: Sequence[tuple[K, int]]
) -> tuple[list[tuple[K, int]], int]:
    """Allocate `amount` to open items oldest first (items must already be sorted by due date).

    Returns (edges, unapplied_residual). Standard lettrage convention when a payment carries no
    usable reference.
    """
    edges = []
    left = amount
    for key, open_cents in open_items:
        if left <= 0:
            break
        if open_cents <= 0:
            continue
        take = min(left, open_cents)
        edges.append((key, take))
        left -= take
    return edges, left


def allocate_with_reference(
    amount: int, referenced: Sequence[tuple[K, int]], others: Sequence[tuple[K, int]]
) -> tuple[list[tuple[K, int]], int]:
    """Referenced invoices first (in order), then the rest oldest first."""
    edges, left = fifo_allocate(amount, referenced)
    more, left = fifo_allocate(left, [o for o in others if o[0] not in {k for k, _ in edges}])
    return edges + more, left
