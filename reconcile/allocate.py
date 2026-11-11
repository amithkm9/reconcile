"""Turn scored candidates into at most one allocation per bank line.

Greedy by score: take the best remaining candidate, accept it if its line is still free and every
share it touches still has enough open balance (or, for a DD return, enough collected cash).
Greedy is a 1/2-approximation of the best global assignment in theory; `exhaustive_gap` measures
how far it is from optimal on our real conflict components.
"""

from __future__ import annotations

import itertools
from collections import defaultdict

from reconcile.candidates import Cand
from reconcile.ledger import Ledger


def _fits(c: Cand, remaining: dict[str, int], collected: dict[str, int]) -> bool:
    for k, cents in c.edges:
        if cents > 0 and remaining[k] < cents:
            return False
        if cents < 0 and collected[k] < -cents:
            return False
    return True


def greedy(cands: list[Cand], L: Ledger) -> tuple[dict[str, Cand], set[str]]:
    """Returns (chosen candidate per bank line, lines left stranded by conflicts)."""
    remaining: dict[str, int] = defaultdict(int)
    collected: dict[str, int] = defaultdict(int)
    for c in cands:
        for k, _ in c.edges:
            remaining[k] = L.open(k)
            collected[k] = L.allocated.get(k, 0)
    chosen: dict[str, Cand] = {}
    lines_with_cands = {c.bank_hash for c in cands}
    for c in sorted(cands, key=lambda c: (-c.score, c.bank_hash, sorted(c.edges))):
        if c.bank_hash in chosen or not _fits(c, remaining, collected):
            continue
        chosen[c.bank_hash] = c
        for k, cents in c.edges:
            remaining[k] -= cents
            collected[k] += cents
    return chosen, lines_with_cands - set(chosen)


def exhaustive_gap(cands: list[Cand], L: Ledger, max_cands: int = 8) -> dict:
    """Compare greedy with the best feasible assignment on small conflict components.

    A component = candidates linked by sharing a line or a share. Returns how many components
    were checked and how many had a strictly better total score than greedy found.
    """
    parent: dict[int, int] = {}

    def find(i: int) -> int:
        while parent.setdefault(i, i) != i:
            i = parent[i]
        return i

    by_res: dict[str, list[int]] = defaultdict(list)
    for i, c in enumerate(cands):
        by_res["L" + c.bank_hash].append(i)
        for k, _ in c.edges:
            by_res["S" + k].append(i)
    for idx in by_res.values():
        for j in idx[1:]:
            parent[find(j)] = find(idx[0])
    comps: dict[int, list[int]] = defaultdict(list)
    for i in range(len(cands)):
        comps[find(i)].append(i)
    checked = worse = 0
    for comp in comps.values():
        if len(comp) < 2 or len(comp) > max_cands:
            continue
        sub = [cands[i] for i in comp]
        g, _ = greedy(sub, L)
        g_total = sum(c.score for c in g.values())
        best = g_total
        for r in range(1, len(sub) + 1):
            for combo in itertools.combinations(sub, r):
                if len({c.bank_hash for c in combo}) < r:
                    continue
                chosen, _ = greedy(list(combo), L)
                if len(chosen) == r:
                    best = max(best, sum(c.score for c in combo))
        checked += 1
        worse += int(best > g_total + 1e-9)
    return {"components_checked": checked, "greedy_suboptimal": worse}
