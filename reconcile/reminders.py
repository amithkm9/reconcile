"""Collection reminders: drafts only, never sent, and never for money we may already have.

An invoice overdue past the grace period gets a draft unless a *hold* applies. Holds exist because
the costliest error in this domain is chasing a family that already paid:
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
from reconcile.ledger import PUBLIC_TYPES, Ledger
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


def _money(cents: int, lang: str) -> str:
    whole, frac = divmod(abs(cents), 100)
    if lang == "fr":
        return f"{whole:,}".replace(",", " ") + f",{frac:02d} €"
    return f"£{whole:,}.{frac:02d}"


def draft_text(lang: str, payer_type: str, level: int, ctx: dict) -> str:
    group = "public" if payer_type in PUBLIC_TYPES else "private"
    tmpls = _templates()[lang][group]
    return tmpls.get(level, tmpls[max(tmpls)]).format(**ctx)


def run_reminders(L: Ledger, as_of: dt.date, lang: str, run_id: str) -> list[dict]:
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
    last_draft: dict[str, dt.date] = {}
    for r in L.reminders:
        if r["status"] == "drafted":
            last_draft[r["invoice_id"]] = r["as_of"]
    out = []
    for inv_id, shares in L.by_invoice.items():
        s0 = shares[0]
        if s0.due_date + dt.timedelta(days=GRACE_DAYS) >= as_of or s0.issue_date > as_of:
            continue
        open_shares = [s for s in shares if L.open(s.key) > SETTLE_TOLERANCE_CENTS]
        if not open_shares:
            continue
        if inv_id in last_draft and (as_of - last_draft[inv_id]).days < REDRAFT_AFTER_DAYS:
            continue
        hold = None
        if s0.exit_date and s0.exit_date <= as_of:
            hold = "deceased"
        elif inv_id in pending_invoices:
            hold = "pending_review"
        elif any(s.key in recent_returns for s in shares):
            hold = "dd_return"
        else:
            pids = {s.pid for s in open_shares}
            resident = norm_id(s0.resident_id)
            # amounts a missed payment for this invoice could plausibly have: the share itself, or
            # the payer's 1-3 oldest open shares together (a multi-month catch-up payment)
            plausible = []
            for s in open_shares:
                if s.mandate:
                    continue
                running = 0
                for o in L.open_shares(s.pid, as_of)[:3]:
                    running += L.open(o.key)
                    plausible.append(running)
            for u in recent_unapplied:
                if (
                    u["pids"] & pids
                    or resident in u["residents"]
                    or any(abs(u["amount"] - p) <= AMOUNT_TOLERANCE * p for p in plausible)
                ):
                    hold = "unapplied_cash"
                    break
        days_over = (as_of - s0.due_date).days
        level = 1 if days_over <= 45 else 2 if days_over <= 75 else 3
        target = open_shares[0]
        rec = {
            "invoice_id": inv_id,
            "as_of": as_of,
            "run_id": run_id,
            "level": level,
            "status": "drafted" if hold is None else f"held:{hold}",
            "hold_reason": hold or "",
            "payer_name": target.payer_name,
            "payer_type": target.payer_type,
            "open_cents": sum(L.open(s.key) for s in open_shares),
            "days_overdue": days_over,
        }
        if hold is None:
            ctx = {
                "payer_name": target.payer_name,
                "resident_name": target.resident_name,
                "invoice_id": inv_id,
                "period": target.period,
                "facility": target.facility,
                "open_amount": _money(L.open(target.key), lang),
                "due_date": target.due_date.strftime("%d/%m/%Y"),
            }
            rec["text"] = draft_text(lang, target.payer_type, level, ctx)
        out.append(rec)
    L.reminders.extend(out)
    return out
