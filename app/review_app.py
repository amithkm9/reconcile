"""Review queue and reminder approval for the finance team: `make app`.

Reads the saved weekly state (state/<client>.sqlite). Every button goes through
`review.apply_decision`, the same write path the simulated reviewer uses in the evaluation, and
is appended to the audit log.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from reconcile.audit import AuditLog  # noqa: E402
from reconcile.ledger import Ledger  # noqa: E402
from reconcile.mapping.template import load_client  # noqa: E402
from reconcile.review import apply_decision  # noqa: E402
from reconcile.state import load_state, save_state  # noqa: E402

st.set_page_config(page_title="Reconcile: review queue", layout="wide")


def money(cents: int, lang: str) -> str:
    v = f"{abs(cents) / 100:,.2f}"
    if lang == "fr":
        v = v.replace(",", " ").replace(".", ",") + " €"
    else:
        v = "£" + v
    return ("-" if cents < 0 else "") + v


@st.cache_resource
def load(client: str):
    data = load_client(ROOT / f"templates/{client}.yaml", ROOT / f"data/{client}")
    L = Ledger(data)
    meta = load_state(L, ROOT / f"state/{client}.sqlite")
    return data, L, meta


def persist(client: str, L: Ledger, meta: dict) -> None:
    save_state(
        L,
        ROOT / f"state/{client}.sqlite",
        meta
        | {
            "aliases": [[k, sorted(v)] for k, v in L.aliases.items()],
            "sig_review": dict(L.sig_review),
        },
    )


clients = [p.stem for p in sorted((ROOT / "state").glob("*.sqlite"))]
if not clients:
    st.error("No saved state. Run `make data` and `make eval CLIENT=azur` first.")
    st.stop()
client = st.sidebar.selectbox("Client", clients)
data, L, meta = load(client)
lang = data.language
audit = AuditLog(ROOT / f"reports/audit_{client}.jsonl", {"scorer": meta.get("scorer"), "ui": True})
th = meta["thresholds"]
st.sidebar.markdown(
    f"**Scorer:** {meta.get('scorer')}  \n**Auto threshold:** {th['auto']:.4f}  \n"
    f"certified >= {100 * th['certified_lb']:.1f}% (95% lower bound, n={th['n_at_auto']})"
)

queue = [d for d in L.decisions.values() if d.get("outcome") == "review" and not d.get("final")]
queue.sort(key=lambda d: L.lines[d["bank_hash"]]["date"])
last_run = L.runs[-1] if L.runs else {}
c1, c2, c3, c4 = st.columns(4)
c1.metric("Waiting for review", len(queue))
c2.metric("Auto-posted (last run)", last_run.get("auto", 0))
c3.metric("Reminder drafts (last run)", last_run.get("drafted", 0))
c4.metric("Money balances", "yes" if last_run.get("balance_ok") else "NO")

tab_q, tab_r, tab_log = st.tabs(["Review queue", "Reminders", "Weekly runs"])

with tab_q:
    if not queue:
        st.success("Queue empty.")
    else:
        labels = [
            f"{L.lines[d['bank_hash']]['date']}  {money(L.lines[d['bank_hash']]['amount_cents'], lang)}  "
            f"{L.lines[d['bank_hash']]['label']}"
            for d in queue
        ]
        i = st.selectbox(
            f"{len(queue)} lines waiting", range(len(queue)), format_func=lambda k: labels[k]
        )
        dec = queue[i]
        line = L.lines[dec["bank_hash"]]
        left, right = st.columns([2, 3])
        with left:
            st.subheader("Bank line")
            st.markdown(f"**{line['label']}**  \n{line.get('reference') or '(no reference)'}")
            st.markdown(f"Date: {line['date']}  \nAmount: **{money(line['amount_cents'], lang)}**")
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
            st.markdown("**Why**  \n" + "  \n".join(opt.get("reasons") or ["(no rule evidence)"]))
        b1, b2, b3 = st.columns(3)
        run_id = f"ui-{dt.datetime.now():%Y%m%d%H%M%S}"
        if b1.button("Accept top suggestion", type="primary"):
            ok = apply_decision(L, dec["bank_hash"], "accept", run_id, actor="human", audit=audit)
        elif b2.button(f"Accept {'Alt ' + str(pick) if pick else 'selected'}", disabled=pick == 0):
            ok = apply_decision(
                L, dec["bank_hash"], "pick", run_id, pick, actor="human", audit=audit
            )
        elif b3.button("Reject: investigate manually"):
            ok = apply_decision(L, dec["bank_hash"], "reject", run_id, actor="human", audit=audit)
        else:
            ok = None
        if ok is not None:
            audit.flush()
            if ok:
                persist(client, L, meta)
                st.rerun()
            else:
                st.error("These invoices were allocated by another decision in the meantime.")

with tab_r:
    if not L.reminders:
        st.info("No reminders yet.")
    else:
        last = max(r["as_of"] for r in L.reminders)
        rem = [r for r in L.reminders if r["as_of"] == last]
        drafted = [r for r in rem if r["status"] == "drafted"]
        held = [r for r in rem if r["status"] != "drafted"]
        st.caption(
            f"Run of {last}: {len(drafted)} drafts waiting for approval, {len(held)} held. Nothing is ever sent from here."
        )
        for k, r in enumerate(drafted[:50]):
            with st.expander(
                f"{r['invoice_id']}: {r['payer_name']} ({r['payer_type']}), "
                f"{money(r['open_cents'], lang)}, {r['days_overdue']} days overdue, level {r['level']}"
            ):
                st.text(r.get("text", ""))
                a, b = st.columns(2)
                if a.button("Approve draft", key=f"ok{k}"):
                    r["status"] = "approved"
                    audit.event("reminder_approved", invoice_id=r["invoice_id"], as_of=last)
                    audit.flush()
                    persist(client, L, meta)
                    st.rerun()
                if b.button("Reject draft", key=f"no{k}"):
                    r["status"] = "rejected"
                    audit.event("reminder_rejected", invoice_id=r["invoice_id"], as_of=last)
                    audit.flush()
                    persist(client, L, meta)
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
    st.dataframe(
        pd.DataFrame(L.runs)[
            [
                c
                for c in [
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
                if c in pd.DataFrame(L.runs).columns
            ]
        ].tail(20),
        hide_index=True,
        width="stretch",
    )
