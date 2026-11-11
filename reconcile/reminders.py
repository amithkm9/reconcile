"""Collection reminders: drafts only, never sent, and never for money we may already have.

Each payer's share of an invoice overdue past the grace period gets a draft unless a *hold*
applies. Holds exist because the costliest error in this domain is chasing a family that already
paid:
  - pending_review : a suggested match for this invoice is waiting for a human
  - unapplied_cash : unexplained cash arrived recently from one of this invoice's payers, or of
                     about this invoice's amount (the safety net for matches we missed)
  - dd_return      : a direct debit was just returned; it is re-presented before any letter
  - deceased       : the resident has left/died; this goes to the estate process, not a reminder
"""

from __future__ import annotations

import datetime as dt
from functools import lru_cache
from pathlib import Path

import yaml

from reconcile.alloc_rules import SETTLE_TOLERANCE_CENTS
from reconcile.ledger import PUBLIC_TYPES, Ledger, Share
from reconcile.normalize import norm_id

GRACE_DAYS = 15
UNAPPLIED_WINDOW_DAYS = 90
RETURN_WINDOW_DAYS = 35
REDRAFT_AFTER_DAYS = 28
AMOUNT_TOLERANCE = 0.02


@lru_cache(maxsize=1)
def _templates() -> dict:
    return yaml.safe_load(
        Path(__file__).with_name("reminder_templates.yaml").read_text(encoding="utf-8")
    )


def money(cents: int, lang: str) -> str:
    """1 234,56 € (fr, narrow no-break space) or £1,234.56 (en); integer cents in, never a float."""
    whole, frac = divmod(abs(cents), 100)
    sign = "-" if cents < 0 else ""
    if lang == "fr":
        return sign + f"{whole:,}".replace(",", "\u202f") + f",{frac:02d} €"
    return f"{sign}£{whole:,}.{frac:02d}"


def draft_text(lang: str, payer_type: str, level: int, ctx: dict) -> str:
    group = "public" if payer_type in PUBLIC_TYPES else "private"
    tmpls = _templates()[lang][group]
    return tmpls.get(level, tmpls[max(tmpls)]).format(**ctx)


def run_reminders(L: Ledger, as_of: dt.date, lang: str, run_id: str) -> list[dict]:
    """Draft or hold one reminder per overdue payer share; returns this run's records."""
    # --- evidence for holds ---
    pending_invoices: set[str] = set()
    recent_unapplied: list[dict] = []
    for h, dec in L.decisions.items():
        if dec["outcome"] == "review" and not dec.get("final"):
            # the top suggestion AND the alternatives shown to the reviewer: any could be right
            for opt_edges in [dec.get("top_edges", [])] + [a["edges"] for a in dec.get("alts", [])]:
                pending_invoices |= {k.split("|", 1)[0] for k, _ in opt_edges}
        if dec["outcome"] in ("unapplied", "review") and not dec.get("posted"):
            line = L.lines[h]
            if (
                line["amount_cents"] > 0
                and 0 <= (as_of - line["date"]).days <= UNAPPLIED_WINDOW_DAYS
            ):
                pids = set(dec.get("touched_pids", ()))
                recent_unapplied.append(
                    {
                        "amount": line["amount_cents"],
                        "pids": pids,
                        "residents": {p.split("@", 1)[1] for p in pids if "@" in p},
                    }
                )
    recent_returns = {
        a["share_key"]
        for a in L.allocations
        if a["cents"] < 0 and 0 <= (as_of - a["date"]).days <= RETURN_WINDOW_DAYS
    }
    # a draft that was approved or rejected still counts: no new letter for REDRAFT_AFTER_DAYS
    last_draft: dict[str, dt.date] = {}
    for r in L.reminders:
        if r["status"] in ("drafted", "approved", "rejected"):
            last_draft[r["share_key"]] = r["as_of"]
    out = []
    for inv_id, shares in L.by_invoice.items():
        s0 = shares[0]
        if s0.due_date + dt.timedelta(days=GRACE_DAYS) >= as_of or s0.issue_date > as_of:
            continue
        # one reminder per payer that still owes its share: the département and the family are
        # chased separately, and one payer's hold does not silence the other
        for s in shares:
            if L.open(s.key) <= SETTLE_TOLERANCE_CENTS:
                continue
            if s.key in last_draft and (as_of - last_draft[s.key]).days < REDRAFT_AFTER_DAYS:
                continue
            hold = _hold(L, s, as_of, pending_invoices, recent_returns, recent_unapplied)
            days_over = (as_of - s.due_date).days
            level = 1 if days_over <= 45 else 2 if days_over <= 75 else 3
            rec = {
                "invoice_id": inv_id,
                "share_key": s.key,
                "as_of": as_of,
                "run_id": run_id,
                "level": level,
                "status": "drafted" if hold is None else f"held:{hold}",
                "hold_reason": hold or "",
                "payer_name": s.payer_name,
                "payer_type": s.payer_type,
                "open_cents": L.open(s.key),
                "days_overdue": days_over,
            }
            if hold is None:
                ctx = {
                    "payer_name": s.payer_name,
                    "resident_name": s.resident_name,
                    "invoice_id": inv_id,
                    "period": s.period,
                    "facility": s.facility,
                    "open_amount": money(L.open(s.key), lang),
                    "due_date": s.due_date.strftime("%d/%m/%Y"),
                }
                rec["text"] = draft_text(lang, s.payer_type, level, ctx)
            out.append(rec)
    L.reminders.extend(out)
    return out


def _hold(
    L: Ledger,
    s: Share,
    as_of: dt.date,
    pending_invoices: set[str],
    recent_returns: set[str],
    recent_unapplied: list[dict],
) -> str | None:
    """The first reason not to chase this payer for this share now, or None."""
    if s.exit_date and s.exit_date <= as_of:
        return "deceased"
    if s.invoice_id in pending_invoices:
        return "pending_review"
    if s.key in recent_returns:
        return "dd_return"
    # amounts a missed payment for this share could plausibly have: the share itself, or the
    # payer's 1-3 oldest open shares together (a multi-month catch-up payment)
    plausible = []
    if not s.mandate:
        running = 0
        for o in L.open_shares(s.pid, as_of)[:3]:
            running += L.open(o.key)
            plausible.append(running)
    resident = norm_id(s.resident_id)
    for u in recent_unapplied:
        if (
            s.pid in u["pids"]
            or resident in u["residents"]
            or any(abs(u["amount"] - p) <= AMOUNT_TOLERANCE * p for p in plausible)
        ):
            return "unapplied_cash"
    return None
