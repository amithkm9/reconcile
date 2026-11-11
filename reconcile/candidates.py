"""Candidate allocations for one bank line.

Blocking strategy, cheapest and most reliable first:
  1. returned direct debit  -> the collected DD it reverses
  2. mandate / invoice number in the label -> that payer's / invoice's share
  3. remittance statement (avis de paiement) -> one edge per statement row        [primary bulk path]
  4. bulk transfer without a statement -> payer's open shares for the period, with a bounded
     subset search (<= MAX_EXCL exclusions of current shares, <= MAX_ADD arrears added)
  5. payer / resident name -> each open share, and oldest-first (FIFO) across the payer's shares

Every candidate has the same shape (edges to shares + unapplied residual + signals), so a single
scorer can rank a DD and a 40-line département transfer on one scale.
"""

from __future__ import annotations

import datetime as dt
import itertools
from collections import defaultdict
from dataclasses import dataclass, field

from rapidfuzz import fuzz, process

from reconcile.alloc_rules import allocate_with_reference, fifo_allocate
from reconcile.ledger import PUBLIC_TYPES, Ledger, Share
from reconcile.normalize import (
    find_pattern,
    has_keyword,
    month_tokens,
    name_part,
    norm_id,
    norm_name,
    norm_text,
    period_tokens,
    tokens,
    vocab,
)

MAX_EXCL = 2  # exclusions searched among the period's shares (3 when the set is small)
MAX_ADD = 2  # arrears from earlier periods that may be added
SMALL_SET = 25
MAX_VALID = 20  # stop counting valid subsets beyond this: "many" is enough to block auto
NAME_CUTOFF = 72
MAX_CANDS = 60


@dataclass
class Cand:
    bank_hash: str
    mtype: str  # dd_return | mandate | reference | remit | bulk | name_fifo | name_single
    edges: tuple[tuple[str, int], ...]
    residual: int
    signals: dict = field(default_factory=dict)
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)

    @property
    def key(self) -> frozenset:
        agg: dict[str, int] = defaultdict(int)
        for k, c in self.edges:
            agg[k.split("|", 1)[0]] += c
        return frozenset(agg.items())


def key_of(alloc: list) -> frozenset:
    """Truth allocation [[invoice, cents], ...] -> comparable key."""
    agg: dict[str, int] = defaultdict(int)
    for inv, c in alloc:
        agg[inv] += int(c)
    return frozenset(agg.items())


@dataclass
class LineResult:
    kind: str  # 'ar' (has candidates or not) | 'non_ar'
    cands: list[Cand]
    touched_pids: set[str]


class Matcher:
    def __init__(self, ledger: Ledger, template: dict):
        self.L = ledger
        pats = template.get("patterns", {})
        self.p_invoice, self.p_mandate, self.p_remit = (
            pats.get("invoice"),
            pats.get("mandate"),
            pats.get("remittance"),
        )
        self.extra_non_ar = [k.upper() for k in template.get("non_ar_keywords", [])]
        self.mandate_pid: dict[str, str] = {}
        self.public: dict[str, str] = {}  # normalised display -> pid (payer_key)
        self.private_names: dict[str, set[str]] = defaultdict(set)
        self.surname_pids: dict[str, set[str]] = defaultdict(set)
        self.res_by_payer_fac: dict[tuple[str, str], dict[str, str]] = defaultdict(dict)
        self.facilities: dict[str, str] = {}
        for s in ledger.shares.values():
            if s.mandate:
                self.mandate_pid[norm_id(s.mandate)] = s.pid
            self.facilities[norm_text(s.facility)] = s.facility
            if s.payer_type in PUBLIC_TYPES:
                self.public[norm_text(s.payer_name)] = s.pid
                self.res_by_payer_fac[(s.pid, norm_text(s.facility))][s.resident_id] = norm_name(
                    s.resident_name
                )
                self.res_by_payer_fac[(s.pid, "")][s.resident_id] = norm_name(s.resident_name)
            else:
                self.private_names[norm_name(s.payer_name)].add(s.pid)
                for t in tokens(norm_name(s.resident_name)):
                    if len(t) >= 3:
                        self.surname_pids[t].add(s.pid)
        rem = ledger.data.remittances
        self.remit_rows: dict[str, list[dict]] = defaultdict(list)
        for r in rem.to_dict("records"):
            self.remit_rows[norm_id(r["remittance_id"])].append(r)
        self.remit_groups = [
            (rid, rows[0]["payer_name"], rows[0]["date"], sum(x["amount_cents"] for x in rows))
            for rid, rows in self.remit_rows.items()
        ]
        self.refresh_names()

    def refresh_names(self) -> None:
        """Payer names from the payer table plus aliases learned from posted history."""
        names: dict[str, set[str]] = defaultdict(set)
        for n, pids in self.private_names.items():
            names[n] |= pids
        for alias, pids in self.L.aliases.items():
            names[alias] |= pids
        self.name_choices = list(names)
        self.name_pids = [names[n] for n in self.name_choices]
        self.alias_set = set(self.L.aliases)

    # ------------------------------------------------------------------
    def generate(self, line: dict, as_of: dt.date) -> LineResult:
        text = f"{line['label']} {line.get('reference') or ''}"
        amount = int(line["amount_cents"])
        d = line["date"]
        voc = vocab()
        if amount < 0:
            if has_keyword(text, voc["return_keywords"]):
                return LineResult("ar", self._dd_return(line, text), set())
            return LineResult("non_ar", [], set())
        if has_keyword(text, voc["non_ar_keywords"] + self.extra_non_ar):
            return LineResult("non_ar", [], set())
        cands: list[Cand] = []
        touched: set[str] = set()
        cands += self._reference(line, text, amount, d)
        public_pid = self._public_payer(text)
        cands += self._remittance(line, text, amount, d, as_of, public_pid)
        if public_pid:
            touched.add(public_pid)
            cands += self._bulk(line, text, amount, d, public_pid)
        else:
            name_cands, pids = self._names(line, text, amount, d)
            cands += name_cands
            touched |= pids
        touched |= {self.L.shares[k].pid for c in cands for k, _ in c.edges}
        uniq: dict[frozenset, Cand] = {}
        for c in cands:
            if c.edges and (c.key not in uniq or _prio(c) < _prio(uniq[c.key])):
                uniq[c.key] = c
        out = list(uniq.values())[:MAX_CANDS]
        for c in out:
            c.signals["n_cands_line"] = len(out)
            c.signals["n_pids_line"] = len({self.L.shares[k].pid for x in out for k, _ in x.edges})
        return LineResult("ar", out, touched)

    # ---- 1. returned direct debits ----
    def _dd_return(self, line, text) -> list[Cand]:
        amount = int(line["amount_cents"])
        pids = {
            self.mandate_pid[m] for m in find_pattern(text, self.p_mandate) if m in self.mandate_pid
        }
        invs = set(find_pattern(text, self.p_invoice))
        out = []
        for s in self._shares_for(pids, invs):
            if self.L.allocated.get(s.key, 0) >= -amount:
                out.append(
                    Cand(
                        line["bank_hash"],
                        "dd_return",
                        ((s.key, amount),),
                        0,
                        {
                            "mandate_hit": int(s.pid in pids),
                            "inv_ref_hit": int(s.invoice_id in invs),
                        },
                    )
                )
        return out

    def _shares_for(self, pids: set[str], invs: set[str]) -> list[Share]:
        if invs:
            shares = [s for i in sorted(invs) for s in self.L.by_invoice.get(i, [])]
            return [s for s in shares if not pids or s.pid in pids]
        return [s for p in sorted(pids) for s in self.L.by_pid.get(p, [])]

    # ---- 2. mandate / invoice number ----
    def _reference(self, line, text, amount, d) -> list[Cand]:
        mand = [
            self.mandate_pid[m] for m in find_pattern(text, self.p_mandate) if m in self.mandate_pid
        ]
        invs = [i for i in find_pattern(text, self.p_invoice) if i in self.L.by_invoice]
        out = []
        if mand:
            pid = mand[0]
            avail = [(s.key, self.L.open(s.key)) for s in self.L.open_shares(pid, d)]
            refd = [a for a in avail if a[0].split("|")[0] in invs]
            edges, left = allocate_with_reference(amount, refd, avail)
            if edges:
                out.append(
                    Cand(
                        line["bank_hash"],
                        "mandate",
                        tuple(edges),
                        left,
                        {"mandate_hit": 1, "inv_ref_hit": int(bool(refd))},
                    )
                )
        for inv in invs:
            for s in self.L.by_invoice[inv]:
                if s.issue_date > d:
                    continue
                avail = [(x.key, self.L.open(x.key)) for x in self.L.open_shares(s.pid, d)]
                refd = [a for a in avail if a[0] == s.key]
                if not refd:
                    continue
                edges, left = allocate_with_reference(amount, refd, avail)
                out.append(
                    Cand(
                        line["bank_hash"],
                        "reference",
                        tuple(edges),
                        left,
                        {"inv_ref_hit": 1, "mandate_hit": int(s.pid in mand)},
                    )
                )
        return out

    # ---- 3. remittance statements ----
    def _public_payer(self, text: str) -> str | None:
        if not self.public:
            return None
        best = process.extractOne(
            norm_text(text), list(self.public), scorer=fuzz.token_set_ratio, score_cutoff=90
        )
        return self.public[best[0]] if best else None

    def _remittance(self, line, text, amount, d, as_of, public_pid) -> list[Cand]:
        ids = [r for r in find_pattern(text, self.p_remit) if r in self.remit_rows]
        groups = []
        for rid in ids:
            rows = self.remit_rows[rid]
            if rows[0]["date"] <= as_of:
                groups.append(rows)
        if not groups and public_pid:
            for rid, payer, rdate, total in self.remit_groups:
                if (
                    abs(total - amount) <= 100
                    and rdate <= as_of
                    and -25 <= (rdate - d).days <= 10
                    and self.public.get(norm_text(payer)) == public_pid
                ):
                    groups.append(self.remit_rows[rid])
        out = []
        for rows in groups:
            pid = self.public.get(norm_text(rows[0]["payer_name"]))
            if not pid:
                continue
            edges, scores, unresolved = [], [], 0
            used: set[str] = set()
            for r in rows:
                pool = self.res_by_payer_fac.get(
                    (pid, norm_text(r["facility"])), self.res_by_payer_fac[(pid, "")]
                )
                hit = _best_name(norm_name(r["beneficiary_name"]), pool)
                share = None
                if hit:
                    rid_, sc = hit
                    share = next(
                        (
                            s
                            for s in self.L.by_pid[pid]
                            if s.resident_id == rid_ and s.period == r["period"]
                        ),
                        None,
                    )
                    scores.append(sc)
                if share is None or share.key in used or self.L.open(share.key) <= 0:
                    unresolved += 1
                    continue
                used.add(share.key)
                edges.append((share.key, min(int(r["amount_cents"]), self.L.open(share.key))))
            if edges:
                left = amount - sum(c for _, c in edges)
                out.append(
                    Cand(
                        line["bank_hash"],
                        "remit",
                        tuple(sorted(edges)),
                        left,
                        {
                            "remit_backed": 1,
                            "remit_rows": len(rows),
                            "remit_resolved": 1 - unresolved / len(rows),
                            "remit_name_min": min(scores) if scores else 0,
                            "remit_by_id": int(bool(ids)),
                        },
                    )
                )
        return out

    # ---- 4. bulk transfer without statement ----
    def _period(self, text: str, d: dt.date) -> str:
        ps = period_tokens(text)
        if len(ps) == 1:
            return ps.pop()
        ms = month_tokens(text)
        if len(ms) == 1:
            m = ms.pop()
            y = d.year - 1 if m > d.month + 1 else d.year
            return f"{y}-{m:02d}"
        return f"{d.year}-{d.month:02d}"

    def _bulk(self, line, text, amount, d, pid) -> list[Cand]:
        period = self._period(text, d)
        toks = set(tokens(text))
        facs = [f for n, f in self.facilities.items() if n and set(n.split()) <= toks]
        pool = [
            s
            for s in self.L.by_pid.get(pid, [])
            if s.issue_date <= d and self.L.open(s.key) > 0 and (not facs or s.facility in facs)
        ]
        # Public bodies usually pay per facility; when the label names none, try each facility
        # separately as well as the whole pool, and count valid subsets across all of them.
        groups = [pool]
        if not facs:
            by_fac: dict[str, list[Share]] = defaultdict(list)
            for sh in pool:
                by_fac[sh.facility].append(sh)
            if len(by_fac) > 1:
                groups += list(by_fac.values())
        found, n_valid = [], 0
        for g in groups:
            cur = [sh for sh in g if sh.period == period]
            arr = [sh for sh in g if sh.period < period][-12:]
            if not cur and not arr:
                continue
            res = _subset_search(cur, arr, amount, self.L.open)
            found += res["found"]
            n_valid += res["n_valid"]
        out, seen = [], set()
        for subset, n_ex, n_add in found:
            edges = tuple(sorted((sh.key, self.L.open(sh.key)) for sh in subset))
            if edges in seen:
                n_valid -= 1  # the same subset found via the whole pool and via its facility
                continue
            seen.add(edges)
            out.append((edges, n_ex, n_add, len(subset)))
        sig = {"n_valid": max(1, n_valid), "facility_hit": int(bool(facs))}
        return [
            Cand(line["bank_hash"], "bulk", e, 0, sig | {"n_excluded": x, "n_added": a, "bulk_size": n})
            for e, x, a, n in out[:5]
        ]

    # ---- 5. names ----
    def _names(self, line, text, amount, d) -> tuple[list[Cand], set[str]]:
        npart = name_part(line["label"], line.get("reference") or "")
        pid_score: dict[str, float] = {}
        alias_hit: set[str] = set()
        if npart:
            for choice, score, idx in process.extract(
                npart,
                self.name_choices,
                scorer=fuzz.token_set_ratio,
                limit=8,
                score_cutoff=NAME_CUTOFF,
            ):
                for pid in sorted(self.name_pids[idx]):
                    pid_score[pid] = max(pid_score.get(pid, 0), score)
                    if choice in self.alias_set:
                        alias_hit.add(pid)
        res_hit: set[str] = set()
        for t in sorted(set(tokens(text))):
            for pid in sorted(self.surname_pids.get(t, ())):
                res_hit.add(pid)
                pid_score.setdefault(pid, 0.0)
        if len(pid_score) > 12:  # too generic a name (e.g. a common surname): keep the best
            pid_score = dict(sorted(pid_score.items(), key=lambda kv: (-kv[1], kv[0]))[:12])
        months = month_tokens(text)
        out = []
        for pid, score in pid_score.items():
            opens = self.L.open_shares(pid, d)
            if not opens:
                continue
            sig = {
                "name_score": score,
                "alias_hit": int(pid in alias_hit),
                "resident_hit": int(pid in res_hit),
                "month_hit_any": int(bool(months)),
            }
            edges, left = fifo_allocate(amount, [(s.key, self.L.open(s.key)) for s in opens])
            out.append(Cand(line["bank_hash"], "name_fifo", tuple(edges), left, dict(sig)))
            for s in opens[-4:]:
                take = min(amount, self.L.open(s.key))
                out.append(
                    Cand(
                        line["bank_hash"], "name_single", ((s.key, take),), amount - take, dict(sig)
                    )
                )
        return out, set(pid_score)


def _prio(c: Cand) -> int:
    return ["dd_return", "mandate", "remit", "reference", "bulk", "name_fifo", "name_single"].index(
        c.mtype
    )


def _best_name(target: str, pool: dict[str, str]) -> tuple[str, float] | None:
    if not pool or not target:
        return None
    ids, names = list(pool), list(pool.values())
    res = process.extract(target, names, scorer=fuzz.token_set_ratio, limit=2)
    if not res or res[0][1] < 80:
        return None
    if len(res) > 1 and res[1][1] >= res[0][1] - 3 and names[res[1][2]] != names[res[0][2]]:
        return None  # ambiguous beneficiary name
    return ids[res[0][2]], res[0][1]


def _subset_search(cur: list[Share], arr: list[Share], amount: int, open_fn) -> dict:
    """Which shares does a bulk amount pay? Start from all of the period's open shares, then
    exclude up to MAX_EXCL of them (3 if the set is small) and/or add up to MAX_ADD arrears.

    Simplest explanation first: solutions are searched by number of changes (exclusions +
    additions), and only the first level that has any solution counts. An exact match of the
    period's shares is not made "ambiguous" by a swap with an equal-amount arrear; two different
    single exclusions that both fit *are* ambiguous, and then the line can never be auto-posted.

    Bounds: C(|cur|, <=3) * C(|arr|<=12, <=2) sums, ~ 2.3k * 79 worst case; arrears sums are
    pre-indexed per size, so each exclusion set is one dict lookup. Counting stops at MAX_VALID.
    """
    total = sum(open_fn(s.key) for s in cur)
    diff = total - amount
    k = 3 if len(cur) <= SMALL_SET else (MAX_EXCL if len(cur) <= 60 else 1)
    adds: list[dict[int, list[tuple]]] = []
    for n in range(0, MAX_ADD + 1):
        by_sum: dict[int, list[tuple]] = defaultdict(list)
        for combo in itertools.combinations(arr, n):
            by_sum[sum(open_fn(s.key) for s in combo)].append(combo)
        adds.append(by_sum)
    for level in range(0, k + MAX_ADD + 1):
        found, n_valid = [], 0
        for n_ex in range(0, min(k, level) + 1):
            n_add = level - n_ex
            if n_add > MAX_ADD:
                continue
            for excl in itertools.combinations(cur, n_ex):
                need = sum(open_fn(s.key) for s in excl) - diff
                for add in adds[n_add].get(need, []):
                    keep = [s for s in cur if s not in excl] + list(add)
                    if not keep:
                        continue
                    n_valid += 1
                    if len(found) < 5:
                        found.append((keep, n_ex, n_add))
                    if n_valid >= MAX_VALID:
                        return {"found": found, "n_valid": n_valid}
        if n_valid:
            return {"found": found, "n_valid": n_valid}
    return {"found": [], "n_valid": 0}
