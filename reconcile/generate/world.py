"""Simulate one year of a care-home group: residents, invoices, payer shares, payments.

Design: simulate *payment intents* (who pays what, when, for which shares), then resolve each
intent into a bank line using the shared lettrage conventions in `alloc_rules`. Ground truth is a
by-product of construction, so it is exact. All money is integer cents.
"""

from __future__ import annotations

import calendar
import datetime as dt
import random
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from reconcile.alloc_rules import (
    SETTLE_TOLERANCE_CENTS,
    allocate_with_reference,
    payer_identity,
    split_shares,
)
from reconcile.normalize import norm_text, payer_key

_NAMES = yaml.safe_load(Path(__file__).with_name("names.yaml").read_text(encoding="utf-8"))
MONTH_WORDS = {
    "FR": [
        "JANV",
        "FEV",
        "MARS",
        "AVRIL",
        "MAI",
        "JUIN",
        "JUIL",
        "AOUT",
        "SEPT",
        "OCT",
        "NOV",
        "DEC",
    ],
    "UK": ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"],
}



@dataclass
class Payer:
    name: str
    ptype: str
    method: str
    relationship: str = ""
    gender: str = "F"
    first: str = ""
    last: str = ""
    mandate: str = ""
    married_last: str = ""  # set when the payer changes name/account in the shift period

    @property
    def key(self) -> str:
        return payer_key(self.name)


@dataclass
class Resident:
    n: int
    rid: str
    first: str
    last: str
    gender: str
    facility: dict
    region: str
    profile: str
    admit: dt.date
    exit: dt.date | None = None
    payers: list[tuple[Payer, float]] = field(default_factory=list)


@dataclass
class Invoice:
    invoice_id: str
    resident: Resident
    issue: dt.date
    due: dt.date
    period: str
    doc_type: str
    cents: int
    original: str = ""


@dataclass
class Share:
    key: str
    invoice: Invoice
    payer: Payer
    pid: str
    pct: float
    cents: int
    month_index: int


@dataclass
class Line:
    date: dt.date
    cents: int
    kind: str  # template kind: dd, dd_return, transfer, bulk_remit, bulk_noremit, guardian, apa, non_ar, refund, unknown
    ctx: dict
    true_class: str = "ar"
    match_type: str = ""
    edges: list[tuple[str, int]] = field(default_factory=list)
    unapplied: int = 0
    shift_tag: str = ""
    month_index: int = 0
    seq: int = 0
    share_edges: list[tuple[str, int]] = field(default_factory=list)


@dataclass
class Remittance:
    remittance_id: str
    date: dt.date
    payer: Payer
    facility: str
    rows: list[tuple[str, str, int, int]]  # (beneficiary, period, net cents, gross cents)
    region: str


@dataclass
class World:
    cfg: dict
    residents: list[Resident]
    invoices: list[Invoice]
    shares: list[Share]
    lines: list[Line]
    remittances: list[Remittance]
    months: list[dt.date]


def _ascii_upper(s: str) -> str:
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    return " ".join(s.upper().replace("'", " ").split())


def _month_end(d: dt.date) -> dt.date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def _add_months(d: dt.date, k: int) -> dt.date:
    m = d.month - 1 + k
    return dt.date(d.year + m // 12, m % 12 + 1, 1)


def _scaled_rates(rates: dict, noise: float) -> dict:
    """Multiply every messiness rate by `noise` (the noise sweep); probabilities are capped."""
    out = {}
    for k, v in rates.items():
        if isinstance(v, dict):
            out[k] = {kk: vv * noise for kk, vv in v.items()}
        elif k.endswith("_per_month"):
            out[k] = max(0, round(v * noise))
        elif k in ("ref_invoice",):
            out[k] = max(0.0, min(1.0, 1 - (1 - v) * noise))  # more noise = fewer clean references
        else:
            out[k] = min(0.95, v * noise)
    fam = out["family"]
    total = sum(fam.values())
    if total > 0.9:
        out["family"] = {k: v * 0.9 / total for k, v in fam.items()}
    return out


class _Gen:
    def __init__(self, cfg: dict, noise: float):
        self.cfg = cfg
        self.rng = random.Random(cfg["seed"])
        self.rates = _scaled_rates(cfg["rates"], noise)
        self.text = cfg["text"]
        self.country = self.text["country"]
        self.names = _NAMES[self.country]
        y, m = map(int, cfg["start_month"].split("-"))
        self.months = [_add_months(dt.date(y, m, 1), i) for i in range(cfg["months"])]
        self.shift_start = cfg["shift"]["start_month_index"]
        self.counters: dict[str, int] = {}
        self.residents: list[Resident] = []
        self.invoices: list[Invoice] = []
        self.shares: list[Share] = []
        self.lines: list[Line] = []
        self.remittances: list[Remittance] = []
        self.intents: list[dict] = []

    # ---------- helpers ----------
    def _next(self, kind: str) -> int:
        self.counters[kind] = self.counters.get(kind, 0) + 1
        return self.counters[kind]

    def _fmt_id(self, kind: str, n: int, **kw) -> str:
        return self.cfg["ids"][kind].format(n=n, **kw)

    def _person(self, gender: str | None = None) -> tuple[str, str, str]:
        g = gender or self.rng.choice("FM")
        return self.rng.choice(self.names[f"first_{g}"]), self.rng.choice(self.names["last"]), g

    def _bank_name(self, first: str, last: str, gender: str) -> str:
        """How the debtor's bank prints the name: title sometimes, initials in the UK, no accents."""
        cache = self.__dict__.setdefault("_bank_names", {})
        if (first, last) in cache:
            s = cache[(first, last)]
            return self._typo(s) if self.rng.random() < self.rates["typo"] else s
        style = self.text["payer_name_style"]
        title = self.text["titles"][gender] if self.rng.random() < self.text["title_rate"] else ""
        if self.rng.random() < 0.25:  # some banks print LAST FIRST without title
            style = "{LAST} {FIRST}" if self.country == "FR" else "{I} {LAST}"
        s = style.format(title=title, LAST=last, FIRST=first, I=first[0])
        s = _ascii_upper(s)
        cache[(first, last)] = s
        return self._typo(s) if self.rng.random() < self.rates["typo"] else s

    def _typo(self, s: str) -> str:
        words = s.split()
        idx = [i for i, w in enumerate(words) if len(w) >= 5]
        if not idx:
            return s
        i = self.rng.choice(idx)
        w = list(words[i])
        j = self.rng.randrange(1, len(w) - 1)
        if self.rng.random() < 0.5:
            del w[j]
        else:
            w[j], w[j + 1 if j + 1 < len(w) else j - 1] = (
                w[j + 1 if j + 1 < len(w) else j - 1],
                w[j],
            )
        words[i] = "".join(w)
        return " ".join(words)

    def _day(self, month: dt.date, lo: int, hi: int) -> dt.date:
        last = calendar.monthrange(month.year, month.month)[1]
        return month.replace(day=min(last, self.rng.randint(lo, hi)))

    # ---------- residents ----------
    def _new_resident(self, admit: dt.date) -> Resident:
        n = self._next("resident")
        first, last, g = self._person()
        fac = self.rng.choice(self.cfg["facilities"])
        region = fac["region"]
        if self.rng.random() < self.cfg["other_region_rate"]:
            region = self.rng.choice(sorted(self.cfg["public_payers"]))
        prof = self.rng.choices(
            self.cfg["profiles"], weights=[p["weight"] for p in self.cfg["profiles"]]
        )[0]
        r = Resident(
            n, self._fmt_id("resident", n), first, last, g, fac, region, prof["name"], admit
        )
        pcts: list[float] = []
        for spec in prof["shares"]:
            pct = spec["pct"]
            if pct == "rest":
                pct = 100 - sum(pcts)
            elif isinstance(pct, list):
                pct = self.rng.randint(pct[0], pct[1])
            pcts.append(float(pct))
            r.payers.append((self._payer_for(r, spec["type"], spec["method"]), float(pct)))
        return r

    def _payer_for(self, r: Resident, ptype: str, method: str) -> Payer:
        if ptype == "resident":
            p = Payer(f"{r.first} {r.last}", ptype, method, "", r.gender, r.first, r.last)
        elif ptype == "family":
            first, last, g = self._person()
            if self.rng.random() < self.text["same_surname_rate"]:
                last = r.last
            if (first, last) == (r.first, r.last):  # a daughter is not her mother's namesake
                first = next(f for f in self.names[f"first_{g}"] if f != r.first)
            p = Payer(
                f"{first} {last}",
                ptype,
                method,
                self.rng.choice(self.text["family_relations"]),
                g,
                first,
                last,
            )
        elif ptype == "public_aid":
            p = Payer(self.cfg["public_payers"][r.region], ptype, method)
        elif ptype == "guardian":
            region = r.facility["region"]
            pool = [g for g in self.cfg["guardians"] if g.endswith(region)] or self.cfg["guardians"]
            p = Payer(pool[0], ptype, method)
        elif ptype == "health_funder":
            p = Payer(self.cfg["health_funder"], ptype, method)
        else:
            raise ValueError(ptype)
        if method == "dd":
            p.mandate = self._fmt_id("mandate", r.n)
        return p

    def build_residents(self) -> None:
        start = self.months[0]
        for _ in range(self.cfg["n_residents"]):
            self.residents.append(
                self._new_resident(start - dt.timedelta(days=self.rng.randint(30, 1500)))
            )
        for month in self.months:
            for r in list(self.residents):
                if (
                    r.exit is None
                    and r.admit < month
                    and self.rng.random() < self.cfg["monthly_death_rate"]
                ):
                    r.exit = self._day(month, 1, 28)
                    self.residents.append(
                        self._new_resident(r.exit + dt.timedelta(days=self.rng.randint(3, 20)))
                    )
        # married-name / new-account shift for some family payers (decided once)
        for r in self.residents:
            for p, _ in r.payers:
                if (
                    p.ptype == "family"
                    and p.gender == "F"
                    and self.rng.random() < self.cfg["shift"]["married_name_rate"]
                ):
                    p.married_last = self.rng.choice([n for n in self.names["last"] if n != p.last])

    # ---------- invoicing ----------
    def build_invoices(self) -> None:
        daily_extra = self.cfg["extra_daily_rate"]
        for mi, month in enumerate(self.months):
            mend = _month_end(month)
            period = f"{month.year}-{month.month:02d}"
            prev = _add_months(month, -1)
            for r in self.residents:
                if r.admit > mend or (r.exit and r.exit < month):
                    continue
                start = max(month, r.admit)
                end = mend
                if r.exit and r.exit <= mend:
                    end = min(mend, r.exit + dt.timedelta(days=self.cfg["post_exit_billable_days"]))
                days = (end - start).days + 1
                if days <= 0:
                    continue
                issue = self._day(prev, self.cfg["issue_day"], self.cfg["issue_day"])
                if r.admit > issue:
                    issue = r.admit
                due = month.replace(day=self.cfg["due_day"])
                if due < issue:
                    due = issue + dt.timedelta(days=14)
                rate = r.facility["daily_rate"] + daily_extra
                cents = round(days * rate * 100)
                inv = Invoice(
                    self._fmt_id("invoice", self._next("invoice"), year=month.year),
                    r,
                    issue,
                    due,
                    period,
                    "invoice",
                    cents,
                )
                self.invoices.append(inv)
                net = cents
                if days >= 28 and self.rng.random() < self.rates["credit_note"]:
                    k = self.rng.randint(3, 10)
                    credit = -round(
                        k
                        * (r.facility["daily_rate"] - self.cfg["credit_note_deduction_per_day"])
                        * 100
                    )
                    cn = Invoice(
                        self._fmt_id("credit", self._next("credit"), year=month.year),
                        r,
                        issue,
                        due,
                        period,
                        "credit",
                        credit,
                        inv.invoice_id,
                    )
                    self.invoices.append(cn)
                    net += credit
                items = [(p.key, pct) for p, pct in r.payers]
                for (p, pct), sc in zip(r.payers, split_shares(net, items), strict=True):
                    pid = payer_identity(p.key, p.ptype, r.rid)
                    self.shares.append(Share(f"{inv.invoice_id}|{pid}", inv, p, pid, pct, sc, mi))

    # ---------- payment intents ----------
    def _intent(self, **kw) -> None:
        kw["seq"] = len(self.intents)
        self.intents.append(kw)

    def build_intents(self) -> None:
        fam_rates = self.rates["family"]
        behaviours = list(fam_rates) + ["on_time"]
        weights = list(fam_rates.values()) + [max(0.0, 1 - sum(fam_rates.values()))]
        carry: dict[str, int] = {}
        bulk_groups: dict[tuple, list[Share]] = {}
        for s in self.shares:
            inv, r, p = s.invoice, s.invoice.resident, s.payer
            month = self.months[s.month_index]
            final = r.exit is not None and r.exit <= _month_end(month)
            if p.method == "dd":
                d = month.replace(day=self.cfg["dd_day"]) + dt.timedelta(
                    days=self.rng.randint(0, 2)
                )
                if d <= inv.issue:
                    d = inv.issue + dt.timedelta(days=self.rng.randint(3, 6))
                if final and r.exit and d > r.exit:
                    if self.rng.random() < 0.6:
                        self._intent(
                            kind="transfer",
                            date=r.exit + dt.timedelta(days=self.rng.randint(30, 90)),
                            share=s,
                            cents=s.cents,
                            ref=None,
                        )
                    continue
                self._intent(kind="dd", date=d, share=s, cents=s.cents)
                if self.rng.random() < self.rates["dd_return"]:
                    rd = d + dt.timedelta(days=self.rng.randint(6, 10))
                    self._intent(kind="dd_return", date=rd, share=s, cents=-s.cents)
                    if self.rng.random() < 0.5:
                        ref = inv.invoice_id if self.rng.random() < 0.5 else None
                        self._intent(
                            kind="transfer",
                            date=rd + dt.timedelta(days=self.rng.randint(8, 25)),
                            share=s,
                            cents=s.cents,
                            ref=ref,
                        )
            elif p.method == "transfer":
                b = self.rng.choices(behaviours, weights=weights)[0]
                if final:
                    b = "late_final" if self.rng.random() < 0.6 else "unpaid"
                ref = self._choose_ref(s)
                due = inv.due
                if b == "unpaid":
                    continue
                if b == "multi_month" and s.month_index < len(self.months) - 1:
                    carry[s.pid] = carry.get(s.pid, 0) + s.cents
                    continue
                amount = s.cents + carry.pop(s.pid, 0)
                if b == "late":
                    d = due + dt.timedelta(days=self.rng.randint(15, 45))
                elif b == "late_final":
                    d = (r.exit or due) + dt.timedelta(days=self.rng.randint(30, 90))
                else:
                    d = due + dt.timedelta(days=self.rng.randint(-4, 5))
                if d < inv.issue:
                    d = inv.issue + dt.timedelta(days=2)
                if b == "partial":
                    part = max(1000, round(s.cents * self.rng.uniform(0.25, 0.75) / 1000) * 1000)
                    if part >= amount:
                        part = max(100, amount // 2)
                    self._intent(kind="transfer", date=d, share=s, cents=part, ref=ref)
                    if self.rng.random() < 0.6:
                        self._intent(
                            kind="transfer",
                            date=d + dt.timedelta(days=self.rng.randint(20, 40)),
                            share=s,
                            cents=amount - part,
                            ref=ref,
                        )
                    continue
                if b == "overpay":
                    amount += self.rng.randint(10, 50) * 100
                if b == "rounding" and amount % 100:
                    amount = amount - amount % 100 + (100 if self.rng.random() < 0.3 else 0)
                self._intent(kind="transfer", date=d, share=s, cents=amount, ref=ref)
                if self.rng.random() < self.rates["duplicate_payment"]:
                    self._intent(
                        kind="transfer",
                        date=d + dt.timedelta(days=self.rng.randint(2, 6)),
                        share=s,
                        cents=amount,
                        ref=ref,
                    )
            elif p.method in ("bulk", "guardian"):
                fac = inv.resident.facility["name"] if p.method == "bulk" else ""
                bulk_groups.setdefault((s.pid, fac, s.month_index), []).append(s)
        self._build_bulk(bulk_groups)
        self._build_noise()

    def _choose_ref(self, s: Share) -> str | None:
        u = self.rng.random()
        if u < self.rates["ref_invoice"]:
            return s.invoice.invoice_id
        if u < self.rates["ref_invoice"] + self.rates["ref_free"]:
            return "FREE"
        return None

    def _build_bulk(self, groups: dict[tuple, list[Share]]) -> None:
        carried: dict[tuple, list[Share]] = {}
        for pid, fac, mi in sorted(groups, key=lambda k: (k[2], k[0], k[1])):
            group = groups[(pid, fac, mi)]
            payer = group[0].payer
            items = carried.pop((pid, fac), []) + list(group)
            p_excl = self.rates[
                "guardian_exclusion" if payer.method == "guardian" else "bulk_exclusion"
            ]
            if len(group) > 2 and self.rng.random() < p_excl:
                for s in self.rng.sample(
                    group, self.rng.randint(1, 2 if payer.method == "bulk" else 1)
                ):
                    items.remove(s)
                    if self.rng.random() < 0.8 and mi < len(self.months) - 1:
                        carried.setdefault((pid, fac), []).append(s)
            if not items:
                continue
            month = self.months[mi]
            d = self._day(month, 15, 25)
            amount = sum(s.cents for s in items)
            rem = None
            if payer.method == "bulk" and self.rng.random() >= self.rates["remittance_missing"]:
                rd = d - dt.timedelta(days=self.rng.randint(1, 3))
                if self.rng.random() < self.rates["remittance_late"]:
                    rd = d + dt.timedelta(days=self.rng.randint(5, 20))
                region = group[0].invoice.resident.region if payer.ptype == "public_aid" else "NHS"
                rid = self._fmt_id(
                    "remittance", self._next("remittance"), region=region, year=month.year
                )
                shifted = (
                    mi >= self.shift_start
                    and region == self.cfg["shift"]["remittance_layout_region"]
                )
                rows = []
                for s in items:
                    res = s.invoice.resident
                    tmpl = self.text[
                        "remittance_beneficiary_shift" if shifted else "remittance_beneficiary"
                    ]
                    rows.append(
                        (
                            tmpl.format(
                                LAST=res.last.upper(), first=res.first, dossier=10000 + res.n
                            ),
                            s.invoice.period,
                            s.cents,
                            self._net_of(s.invoice),
                        )
                    )
                rem = Remittance(rid, rd, payer, fac, rows, region)
                self.remittances.append(rem)
            self._intent(
                kind="bulk" if payer.method == "bulk" else "guardian",
                date=d,
                shares=items,
                cents=amount,
                remittance=rem,
                facility=fac,
                month_index=mi,
            )

    def _net_of(self, inv: Invoice) -> int:
        if not hasattr(self, "_credits"):
            self._credits: dict[str, int] = {}
            for c in self.invoices:
                if c.original:
                    self._credits[c.original] = self._credits.get(c.original, 0) + c.cents
        return inv.cents + self._credits.get(inv.invoice_id, 0)

    def _build_noise(self) -> None:
        labels = self.cfg["labels"]
        for mi, month in enumerate(self.months):
            if labels.get("apa"):
                for fac in self.cfg["facilities"]:
                    payer = Payer(self.cfg["public_payers"][fac["region"]], "public_aid", "bulk")
                    self._intent(
                        kind="apa",
                        date=self._day(month, 18, 24),
                        cents=self.rng.randint(15000, 45000) * 100,
                        payer=payer,
                        facility=fac["name"],
                        month_index=mi,
                    )
            for _ in range(self.rates["non_ar_per_month"]):
                self._intent(
                    kind="non_ar",
                    date=self._day(month, 1, 28),
                    cents=self.rng.randint(500, 40000),
                    month_index=mi,
                    variant=self.rng.randrange(len(labels["non_ar"])),
                )
            for _ in range(self.rates["unknown_credits_per_month"]):
                self._intent(
                    kind="unknown",
                    date=self._day(month, 1, 28),
                    cents=self.rng.randint(50, 900) * 100,
                    month_index=mi,
                    name=self.rng.choice(self.text["unknown_payers"]),
                )
            for _ in range(self.rates["refunds_per_month"]):
                first, last, g = self._person()
                self._intent(
                    kind="refund",
                    date=self._day(month, 1, 28),
                    cents=-self.rng.randint(1500, 2500) * 100,
                    month_index=mi,
                    name=_ascii_upper(f"{last} {first}"),
                )

    # ---------- resolve intents into bank lines with truth ----------
    def resolve(self) -> None:
        open_cents = {s.key: s.cents for s in self.shares}
        by_payer: dict[str, list[Share]] = {}
        for s in sorted(self.shares, key=lambda s: (s.invoice.due, s.invoice.invoice_id)):
            by_payer.setdefault(s.pid, []).append(s)
        for it in sorted(self.intents, key=lambda i: (i["date"], i["seq"])):
            kind = it["kind"]
            mi = self._month_index(it["date"])
            if kind in ("apa", "non_ar", "unknown", "refund"):
                self.lines.append(self._noise_line(it, mi))
                continue
            edges: list[tuple[str, int]] = []
            residual = 0
            if kind == "dd":
                s = it["share"]
                take = min(it["cents"], open_cents[s.key])
                edges, residual = [(s.key, take)], it["cents"] - take
            elif kind == "dd_return":
                s = it["share"]
                edges = [(s.key, it["cents"])]
            elif kind == "transfer":
                s = it["share"]
                avail = [
                    (x.key, open_cents[x.key])
                    for x in by_payer[s.pid]
                    if x.invoice.issue <= it["date"] and open_cents[x.key] > SETTLE_TOLERANCE_CENTS
                ]
                refd = [a for a in avail if it.get("ref") and a[0].startswith(it["ref"] + "|")]
                edges, residual = allocate_with_reference(it["cents"], refd, avail)
            else:  # bulk / guardian: explicit list
                edges = [(s.key, open_cents[s.key]) for s in it["shares"]]
                residual = it["cents"] - sum(c for _, c in edges)
            for k, c in edges:
                open_cents[k] -= c
            self.lines.append(self._ar_line(it, edges, residual, mi))
        for i, ln in enumerate(self.lines):
            ln.seq = i

    def _month_index(self, d: dt.date) -> int:
        for i in range(len(self.months) - 1, -1, -1):
            if d >= self.months[i]:
                return i
        return 0

    def _ar_line(self, it: dict, edges, residual, mi) -> Line:
        kind = it["kind"]
        share_by_key = self._share_index()
        inv_edges: dict[str, int] = {}
        for k, c in edges:
            inv_edges[share_by_key[k].invoice.invoice_id] = (
                inv_edges.get(share_by_key[k].invoice.invoice_id, 0) + c
            )
        if kind == "transfer":
            s = it["share"]
            if not edges:
                mt = "unapplied"
            elif residual > 0:
                mt = "overpay"
            elif len(edges) >= 2:
                mt = "multi_month"
            elif edges[0][1] < share_by_key[edges[0][0]].cents:
                mt = "partial"
            else:
                mt = "split_share" if s.pct < 100 else "one_to_one"
        else:
            mt = {"dd": "dd", "dd_return": "dd_return", "guardian": "guardian_bulk"}.get(kind) or (
                "bulk_remit" if it.get("remittance") else "bulk_no_remit"
            )
        ctx, tkind, tag = self._context(it, mi)
        cls = "ar" if edges else "unapplied"
        line = Line(it["date"], it["cents"], tkind, ctx, cls, mt, sorted(inv_edges.items()), residual, tag, mi)
        line.share_edges = sorted(edges)
        return line

    def _share_index(self) -> dict[str, Share]:
        if not hasattr(self, "_sidx"):
            self._sidx = {s.key: s for s in self.shares}
        return self._sidx

    def _context(self, it: dict, mi: int) -> tuple[dict, str, str]:
        kind = it["kind"]
        words = MONTH_WORDS[self.country]
        month = self.months[it.get("month_index", mi)] if "month_index" in it else self.months[mi]
        ctx: dict = {
            "mm_yyyy": f"{month.month:02d}/{month.year}",
            "mon_yy": f"{words[month.month - 1]}{str(month.year)[2:]}",
        }
        tag = ""
        if "share" in it:
            s = it["share"]
            p, r = s.payer, s.invoice.resident
            smonth = self.months[s.month_index]
            last = p.last
            if p.married_last and mi >= self.shift_start:
                last, tag = p.married_last, "married_name"
            ctx.update(
                mandate=p.mandate,
                invoice=s.invoice.invoice_id,
                resident_last=_ascii_upper(r.last),
                month_word=words[smonth.month - 1],
                payer_bank_name=self._bank_name(p.first or r.first, last or r.last, p.gender),
            )
            ref = it.get("ref")
            if ref == "FREE":
                ctx["free_ref"] = self.rng.choice(self.text["free_refs"]).format(**ctx)
            elif ref:
                ctx["free_ref"] = ref
            else:
                ctx["free_ref"] = ""
            if kind == "transfer" and mi >= self.shift_start:
                return ctx, "transfer_shift", tag or "label_format"
            return ctx, kind, tag
        p = it["shares"][0].payer
        ctx.update(
            payer_upper=_ascii_upper(p.name), facility_upper=_ascii_upper(it.get("facility", ""))
        )
        if it.get("remittance"):
            ctx["remittance_id"] = it["remittance"].remittance_id
            if (
                mi >= self.shift_start
                and it["remittance"].region == self.cfg["shift"]["remittance_layout_region"]
            ):
                tag = "remit_layout"
            return ctx, "bulk_remit", tag
        return ctx, ("guardian" if kind == "guardian" else "bulk_noremit"), tag

    def _noise_line(self, it: dict, mi: int) -> Line:
        kind = it["kind"]
        words = MONTH_WORDS[self.country]
        month = self.months[it["month_index"]]
        ctx = {
            "mm_yyyy": f"{month.month:02d}/{month.year}",
            "mon_yy": f"{words[month.month - 1]}{str(month.year)[2:]}",
        }
        if kind == "apa":
            ctx.update(
                payer_upper=_ascii_upper(it["payer"].name),
                facility_upper=_ascii_upper(it["facility"]),
            )
            return Line(it["date"], it["cents"], "apa", ctx, "non_ar", "apa", month_index=mi)
        if kind == "non_ar":
            ctx["variant"] = it["variant"]
            return Line(
                it["date"], it["cents"], "non_ar", ctx, "non_ar", "unrelated", month_index=mi
            )
        if kind == "refund":
            ctx["payer_bank_name"] = it["name"]
            return Line(it["date"], it["cents"], "refund", ctx, "non_ar", "refund", month_index=mi)
        ctx.update(
            payer_bank_name=it["name"],
            free_unknown=self.rng.choice(["", "REGLEMENT", "PAIEMENT FACTURE", "PAYMENT"]),
        )
        return Line(
            it["date"], it["cents"], "unknown", ctx, "unapplied", "unapplied", month_index=mi
        )


def build_world(cfg: dict, noise: float = 1.0) -> World:
    g = _Gen(cfg, noise)
    g.build_residents()
    g.build_invoices()
    g.build_intents()
    g.resolve()
    return World(cfg, g.residents, g.invoices, g.shares, g.lines, g.remittances, g.months)


def norm_payer(name: str) -> str:
    return norm_text(name)
