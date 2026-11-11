"""Review queue and reminder approval for the finance team: `make app`.

Reads the saved weekly state (state/<client>.sqlite). Every button goes through
`review.apply_decision`, the same write path the simulated reviewer uses in the evaluation, and
is appended to the audit log. Bank labels and names come from client files, so they are shown as
plain text, never rendered as Markdown.

Single-operator tool: it listens on localhost only (.streamlit/config.toml) and has no login.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import streamlit as st

from reconcile.audit import AuditLog
from reconcile.ledger import Ledger
from reconcile.mapping.template import load_client
from reconcile.paths import REPORTS, ROOT, client_paths
from reconcile.reminders import money
from reconcile.review import apply_decision
from reconcile.state import check_balance, load_state, save_state

st.set_page_config(page_title="Reconcile: review queue", layout="wide")


@st.cache_resource
def load(client: str, mtime: float) -> tuple:  # noqa: ARG001 - mtime is the cache key
    """Keyed on the file's modification time: a `make run` in a terminal is picked up on the
    next click instead of being overwritten by this tab's older copy."""
    p = client_paths(client)
    data = load_client(p.template, p.data)
    L = Ledger(data)
    meta = load_state(L, p.state)
    return data, L, meta


def persist(client: str, L: Ledger, meta: dict) -> bool:
    """Save only a ledger whose money still balances."""
    if not check_balance(L)["ok"]:
        st.error("Money no longer balances: nothing was saved. Reload the page.")
        return False
    save_state(L, client_paths(client).state, meta)
    return True


clients = [p.stem for p in sorted((ROOT / "state").glob("*.sqlite"))]
if not clients:
    st.error("No saved state. Run `make all` (or `make data` then `make eval CLIENT=azur`) first.")
    st.stop()
client = st.sidebar.selectbox("Client", clients)
data, L, meta = load(client, client_paths(client).state.stat().st_mtime)
lang = data.language
audit = AuditLog(REPORTS / f"audit_{client}.jsonl", {"scorer": meta.get("scorer"), "ui": True})
th = meta["thresholds"]
st.sidebar.markdown(
    f"**Scorer:** {meta.get('scorer')}  \n**Auto threshold:** {th['auto']:.4f}  \n"
    f"certified >= {100 * th['certified_lb']:.1f}% (95% lower bound, n={th['n_at_auto']})"
)

queue = sorted(
    (h for h, d in L.decisions.items() if d.get("outcome") == "review" and not d.get("final")),
    key=lambda h: (L.lines[h]["date"], h),
)
recent = [r for r in L.runs if r.get("mode") == "decide"][-4:]
c1, c2, c3, c4 = st.columns(4)
c1.metric("Waiting for review", len(queue))
c2.metric("Auto-posted (last 4 runs)", sum(r.get("auto", 0) for r in recent))
c3.metric("Reminder drafts (last 4 runs)", sum(r.get("drafted", 0) for r in recent))
c4.metric("Money balances (every run)", "yes" if all(r.get("balance_ok") for r in L.runs) else "NO")

tab_q, tab_r, tab_log = st.tabs(["Review queue", "Reminders", "Weekly runs"])

with tab_q:
    if not queue:
        st.success("Queue empty.")
    else:
        # options are bank-line hashes, not positions: another tab resolving a line cannot shift
        # this tab's selection onto a different line
        h = st.selectbox(
            f"{len(queue)} lines waiting",
            queue,
            format_func=lambda x: (
                f"{L.lines[x]['date']}  {money(L.lines[x]['amount_cents'], lang)}  "
                f"{L.lines[x]['label']}"
            ),
        )
        dec, line = L.decisions[h], L.lines[h]
        left, right = st.columns([2, 3])
        with left:
            st.subheader("Bank line")
            st.text(f"{line['label']}\n{line.get('reference') or '(no reference)'}")
            st.text(f"Date: {line['date']}\nAmount: {money(line['amount_cents'], lang)}")
            if dec.get("guard") == "new_format":
                st.warning(
                    "New bank label layout: held for human review until enough lines of this "
                    "layout are confirmed (it would otherwise have been auto-posted)."
                )
            if dec.get("n_valid", 1) > 1:
                st.warning(f"{dec['n_valid']} different combinations of invoices fit this amount.")
        options = [dec["top"]] + dec.get("alts", [])
        with right:
            pick = st.radio(
                "Suggestions",
                range(len(options)),
                horizontal=True,
                format_func=lambda k: (
                    ("Top" if k == 0 else f"Alt {k}") + f" (score {options[k]['score']:.3f})"
                ),
            )
            opt = options[pick]
            rows = []
            for key, cents in opt["edges"]:
                s = L.shares[key]
                rows.append(
                    {
                        "invoice": s.invoice_id,
                        "resident": s.resident_name,
                        "payer": s.payer_name,
                        "payer type": s.payer_type,
                        "period": s.period,
                        "allocate": money(cents, lang),
                        "open now": money(L.open(key), lang),
                    }
                )
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
            if opt.get("residual"):
                st.caption(f"Left unallocated (suspense): {money(opt['residual'], lang)}")
            st.markdown("**Why**")
            st.text("\n".join(opt.get("reasons") or ["(no rule evidence)"]))
        b1, b2, b3 = st.columns(3)
        run_id = f"ui-{dt.datetime.now():%Y%m%d%H%M%S}"
        action = None
        if b1.button("Accept top suggestion", type="primary"):
            action = ("accept", 0)
        if b2.button(f"Accept {'Alt ' + str(pick) if pick else 'selected'}", disabled=pick == 0):
            action = ("pick", pick)
        if b3.button("Reject: investigate manually"):
            action = ("reject", 0)
        if action:
            ok = apply_decision(L, h, action[0], run_id, action[1], actor="human", audit=audit)
            audit.flush()
            if ok and persist(client, L, meta):
                st.rerun()
            elif not ok:
                st.error(
                    "This line was resolved elsewhere, or its invoices were allocated by another "
                    "decision in the meantime. Nothing was posted."
                )

with tab_r:
    if not L.reminders:
        st.info("No reminders yet.")
    else:
        runs = sorted({r["as_of"] for r in L.reminders}, reverse=True)
        with_drafts = [
            d
            for d in runs
            if any(r["as_of"] == d and r["status"] == "drafted" for r in L.reminders)
        ]
        last = st.selectbox(
            "Reminder run", runs, index=runs.index(with_drafts[0]) if with_drafts else 0
        )
        rem = [r for r in L.reminders if r["as_of"] == last]
        drafted = [r for r in rem if r["status"] == "drafted"]
        held = [r for r in rem if r["status"].startswith("held:")]
        st.caption(
            f"Run of {last}: {len(drafted)} drafts waiting for approval, {len(held)} held. "
            "Nothing is ever sent from here."
        )
        for r in drafted[:50]:
            with st.expander(
                f"{r['invoice_id']}: {r['payer_name']} ({r['payer_type']}), "
                f"{money(r['open_cents'], lang)}, {r['days_overdue']} days overdue, "
                f"level {r['level']}"
            ):
                st.text(r.get("text", ""))
                a, b = st.columns(2)
                for col, label, status in (
                    (a, "Approve draft", "approved"),
                    (b, "Reject draft", "rejected"),
                ):
                    if col.button(label, key=f"{status}|{r['share_key']}|{last}"):
                        r["status"] = status
                        audit.event(
                            f"reminder_{status}",
                            invoice_id=r["invoice_id"],
                            share_key=r["share_key"],
                            as_of=last,
                        )
                        audit.flush()
                        if persist(client, L, meta):
                            st.rerun()
        st.subheader("Held (not drafted) and why")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "invoice": r["invoice_id"],
                        "payer": r["payer_name"],
                        "hold": r["hold_reason"],
                        "open": money(r["open_cents"], lang),
                        "days overdue": r["days_overdue"],
                    }
                    for r in held
                ]
            ),
            hide_index=True,
            width="stretch",
        )

with tab_log:
    runs = pd.DataFrame(L.runs)
    cols = [
        "run_id",
        "as_of",
        "mode",
        "new_lines",
        "decided",
        "auto",
        "review",
        "unapplied",
        "non_ar",
        "guard_new_format",
        "drafted",
        "held",
        "balance_ok",
    ]
    st.dataframe(
        runs[[c for c in cols if c in runs.columns]].tail(20), hide_index=True, width="stretch"
    )
