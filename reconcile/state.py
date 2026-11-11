"""Weekly runs and persistent state.

`run_week` is the only code path that decides anything. Every week it:
  1. imports the bank exports available by the week's end (idempotent: lines are content-hashed,
     so the ~9-day overlap between weekly exports never double-posts),
  2. re-decides every line that is not final (new, unapplied, pending review: a late remittance
     statement can still resolve last week's unapplied transfer),
  3. generates candidates, scores them, allocates greedily, applies thresholds,
  4. posts auto matches (final: never re-decided), queues the rest for review,
  5. drafts reminders with holds, and checks that the money balances.

The batch evaluation is literally this function called once per week, so what is measured is
what would run in production.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
from collections import defaultdict
from pathlib import Path

from reconcile.allocate import exhaustive_gap, greedy
from reconcile.audit import AuditLog
from reconcile.candidates import Cand, Matcher
from reconcile.decide import Thresholds, cp_lower, outcome
from reconcile.features import candidate_features
from reconcile.ledger import Ledger
from reconcile.normalize import label_signature
from reconcile.reminders import run_reminders
from reconcile.review import SimReviewer, apply_decision
from reconcile.scorer import Scorer

REDECIDE_DAYS = 56
NAME_TRUTH_TYPES = {"one_to_one", "split_share", "partial", "multi_month", "overpay"}


def _option(c: Cand, feats: dict) -> dict:
    return {
        "edges": [list(e) for e in c.edges],
        "key": c.key,
        "score": float(c.score),
        "mtype": c.mtype,
        "reasons": c.reasons,
        "features": feats,
        "residual": c.residual,
    }


def run_week(
    L: Ledger,
    M: Matcher,
    as_of: dt.date,
    *,
    scorer: Scorer | None,
    th: Thresholds | None,
    run_id: str,
    mode: str = "decide",
    truth: dict | None = None,
    reviewer: SimReviewer | None = None,
    audit: AuditLog | None = None,
    lang: str = "en",
    collect: list | None = None,
    do_reminders: bool = True,
) -> dict:
    """One weekly run, as of `as_of`: import the bank lines exported by then (idempotent), generate
    and score candidates for every undecided line, allocate greedily, decide auto / review /
    unapplied / non-AR, post the auto lines, let `reviewer` (if any) work the queue, draft or hold
    reminders, append to the audit log, and fail if the money does not balance.

    `mode="teacher"` posts the historical lettrage from `truth` instead of deciding (months the
    client has already reconciled); `collect` receives one training row per candidate."""
    bank = L.data.bank
    avail = bank[bank.file_end <= as_of]
    n_new = L.import_lines(avail.to_dict("records"))
    M.refresh_names()
    pending = [
        h
        for h, ln in L.lines.items()
        if ln["date"] <= as_of
        and not L.decisions.get(h, {}).get("final")
        and (as_of - ln["date"]).days <= REDECIDE_DAYS
    ]
    pending.sort(key=lambda h: (L.lines[h]["date"], h))
    results = {h: M.generate(L.lines[h], as_of) for h in pending}
    feats: dict[int, dict] = {}
    all_cands: list[Cand] = []
    for h in pending:
        for c in results[h].cands:
            feats[id(c)] = candidate_features(c, L.lines[h], L)
            all_cands.append(c)
    if scorer is not None and all_cands:
        scores = scorer.score([feats[id(c)] for c in all_cands])
        for c, s in zip(all_cands, scores, strict=True):
            c.score = float(s)
            c.reasons = scorer.reasons(feats[id(c)])
    if collect is not None:
        for h in pending:
            tkey = truth["keys"].get(h) if truth else None
            for c in results[h].cands:
                collect.append(
                    {
                        "bank_hash": h,
                        "features": feats[id(c)],
                        "y": int(c.key == tkey),
                        "score": c.score,
                        "mtype": c.mtype,
                        "n_valid": c.signals.get("n_valid", 1),
                        "date": L.lines[h]["date"],
                        "key": c.key,
                    }
                )
    stats = defaultdict(int)
    if mode == "teacher":
        _post_truth(L, pending, truth, run_id, stats, results)
    else:
        _decide(L, pending, results, feats, th, run_id, as_of, reviewer, audit, stats)
    reminders = run_reminders(L, as_of, lang, run_id) if do_reminders and mode != "teacher" else []
    if audit:
        for r in reminders:
            audit.event(
                "reminder",
                invoice_id=r["invoice_id"],
                status=r["status"],
                as_of=as_of,
                run_id=run_id,
            )
    bal = check_balance(L)
    run = {
        "run_id": run_id,
        "as_of": as_of,
        "mode": mode,
        "new_lines": n_new,
        "decided": len(pending),
        "drafted": sum(r["status"] == "drafted" for r in reminders),
        "held": sum(r["status"] != "drafted" for r in reminders),
        "balance_ok": bal["ok"],
        **dict(stats),
    }
    L.runs.append(run)
    if audit:
        audit.event("run", **run)
        audit.flush()
    if not bal["ok"]:
        raise AssertionError(f"money does not balance after {run_id}: {bal}")
    return run


def _post_truth(L, pending, truth, run_id, stats, results) -> None:
    """Teacher mode: post the historical lettrage (what a client's ledger already contains)."""
    for h in pending:
        cls = truth["class"].get(h, "unapplied")
        edges = [(k, c) for k, c in truth["share_edges"].get(h, []) if k in L.shares]
        if cls == "ar" and edges:
            L.post(
                h,
                edges,
                "historical",
                run_id,
                learn_alias=truth["match_type"].get(h) in NAME_TRUTH_TYPES,
            )
            L.decisions[h] = {
                "bank_hash": h,
                "outcome": "historical",
                "final": True,
                "posted": True,
            }
            stats["historical"] += 1
        else:
            outcome_ = "non_ar" if results[h].kind == "non_ar" or cls == "non_ar" else "unapplied"
            L.decisions[h] = {
                "bank_hash": h,
                "outcome": outcome_,
                "final": True,
                "posted": False,
                "touched_pids": sorted(results[h].touched_pids),
            }
            stats[outcome_] += 1


def _decide(L, pending, results, feats, th, run_id, as_of, reviewer, audit, stats) -> None:
    above = [c for h in pending for c in results[h].cands if c.score >= th.review]
    chosen, stranded = greedy(above, L)
    queue: list[dict] = []
    gap = exhaustive_gap(above, L)
    stats["gap_checked"] += gap["components_checked"]
    stats["gap_suboptimal"] += gap["greedy_suboptimal"]
    for h in pending:
        res = results[h]
        line = L.lines[h]
        ranked = sorted(res.cands, key=lambda c: -c.score)
        top = chosen.get(h) or (ranked[0] if ranked else None)
        alts, seen = [], {top.key} if top else set()
        for c in ranked:
            if c.key not in seen and len(alts) < 3:
                alts.append(_option(c, feats[id(c)]))
                seen.add(c.key)
        if res.kind == "non_ar":
            out = "non_ar"
        elif h in chosen:
            assert top is not None  # a chosen line always has its candidate
            out = outcome(top.score, int(top.signals.get("n_valid", 1)), th)
        else:
            out = "unapplied"
        age = (as_of - line["date"]).days
        dec = {
            "bank_hash": h,
            "outcome": out,
            "system_outcome": out,
            "final": out in ("auto", "non_ar") or (out == "unapplied" and age > REDECIDE_DAYS - 7),
            "posted": False,
            "run_id": run_id,
            "as_of": as_of,
            "top": _option(top, feats[id(top)]) if top else None,
            "alts": alts,
            "n_valid": int(top.signals.get("n_valid", 1)) if top else 0,
            "touched_pids": sorted(res.touched_pids),
            "stranded": h in stranded,
            "top_edges": [list(e) for e in top.edges] if top and out == "review" else [],
        }
        if out == "auto" and top is not None and _new_format(L, line):
            # never auto-post a label layout no human has confirmed yet: the bank changed its
            # format and nothing the scorer learned is evidence about it
            out = dec["outcome"] = dec["system_outcome"] = "review"
            dec["final"], dec["guard"] = False, "new_format"
            dec["top_edges"] = [list(e) for e in top.edges]
            stats["guard_new_format"] += 1
        L.decisions[h] = dec
        stats[out] += 1
        if out == "auto" and top is not None:
            L.post(
                h,
                list(top.edges),
                "auto",
                run_id,
                learn_alias=top.mtype.startswith("name") or top.mtype == "reference",
            )
            dec.update(posted=True, final_edges=[list(e) for e in top.edges])
        if audit:
            audit.event(
                "decision",
                bank_hash=h,
                outcome=out,
                score=top.score if top else None,
                edges=dec["top"]["edges"] if dec["top"] else None,
                reasons=top.reasons if top else [],
                run_id=run_id,
                as_of=as_of,
            )
        if out == "review":
            queue.append(dec)
    # The human queue is worked after all automatic posts of the week, as it would be in practice.
    if reviewer is not None:
        for dec in queue:
            action, idx = reviewer.act(dec)
            apply_decision(
                L, dec["bank_hash"], action, run_id, idx, actor="sim_reviewer", audit=audit
            )


GUARD_MIN_HISTORY = 200  # the guard needs a history to compare against (not active at cold start)
GUARD_MIN_CONFIRMED = 20  # confirmed lines of a layout before it may be auto-posted


# A new layout is certified by the same rule as the auto threshold: the one-sided 95% Clopper-Pearson
# lower bound on the top suggestion's accuracy, over the lines the guard held back, must reach 99%
# (about 299 reviewed in a row with no rejection). A looser gate (30 lines at >= 97%) was tried and
# released a layout that was really 91% right; see docs/decisions.md.
PROBATION_TARGET = 0.99


def _new_format(L: Ledger, line: dict) -> bool:
    """True if this line's label layout is not trusted for auto-posting yet.

    Trusted = seen in the lettered history, or passed probation: over the lines this guard held
    back (lines that would otherwise have been auto-posted), the 95% lower bound on how often
    reviewers accepted the top suggestion reaches PROBATION_TARGET. The guard lifts on the
    measured precision of exactly the posts it would release, not on a count."""
    if sum(L.signature_counts.values()) < GUARD_MIN_HISTORY:
        return False  # cold start: nothing to compare against
    sig = label_signature(line["label"])
    if L.signature_counts.get(sig, 0) >= GUARD_MIN_CONFIRMED:
        return False
    reviewed, accepted = L.sig_review.get(sig, (0, 0))
    return cp_lower(accepted, reviewed) < PROBATION_TARGET


def check_balance(L: Ledger) -> dict:
    """Every euro is accounted for: credits = allocated + unapplied (suspense) + non-AR income,
    no bank line is over-allocated, and no invoice share is allocated beyond its gross amount
    (a share paid and then reduced by a credit note is reported as refund due)."""
    posted: dict[str, int] = defaultdict(int)
    for a in L.allocations:
        posted[a["bank_hash"]] += a["cents"]
    t = defaultdict(int)
    over_lines = 0
    for h, ln in L.lines.items():
        amt, p = ln["amount_cents"], posted.get(h, 0)
        side = "credit" if amt > 0 else "debit"
        t[side] += amt
        if L.decisions.get(h, {}).get("outcome") == "non_ar":
            t[f"{side}_non_ar"] += amt
        else:
            t[f"{side}_allocated"] += p
            t[f"{side}_suspense"] += amt - p
            over_lines += int(abs(p) > abs(amt))
    # A credit note issued after the share was paid leaves the payer in credit: that is money to
    # refund, not an error. Only allocation beyond the invoice's gross amount is an error.
    over_shares = sum(1 for k, v in L.allocated.items() if v > L.shares[k].gross_cents)
    refund_due = sum(
        v - L.shares[k].cents
        for k, v in L.allocated.items()
        if L.shares[k].cents < v <= L.shares[k].gross_cents
    )
    ok = (
        over_lines == 0
        and over_shares == 0
        and t["credit"] == t["credit_allocated"] + t["credit_suspense"] + t["credit_non_ar"]
    )
    return {
        "ok": ok,
        "over_allocated_lines": over_lines,
        "over_allocated_shares": over_shares,
        "refund_due_cents": refund_due,
        **dict(t),
    }


# ---------------- persistence ----------------


def _jd(o):
    if isinstance(o, dt.date):
        return o.isoformat()
    if isinstance(o, set | frozenset):
        return sorted([list(x) if isinstance(x, tuple) else x for x in o])
    raise TypeError(type(o))


def save_state(L: Ledger, path: Path, meta: dict) -> None:
    """Write the ledger to sqlite. `meta` holds run settings (client, scorer, thresholds); learned
    aliases and new-layout review counts are added here. The file is written next to `path` and
    swapped in atomically, so a crash mid-write never leaves a half-saved state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = meta | {
        "aliases": [[k, sorted(v)] for k, v in sorted(L.aliases.items())],
        "sig_review": dict(L.sig_review),
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    con.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")
    con.execute(
        "CREATE TABLE bank_lines (bank_hash TEXT PRIMARY KEY, date TEXT, label TEXT, reference TEXT, amount_cents INTEGER, source_file TEXT)"
    )
    con.execute(
        "CREATE TABLE allocations (bank_hash TEXT, share_key TEXT, invoice_id TEXT, cents INTEGER, source TEXT, run_id TEXT, date TEXT)"
    )
    con.execute(
        "CREATE TABLE decisions (bank_hash TEXT PRIMARY KEY, outcome TEXT, final INTEGER, doc TEXT)"
    )
    con.execute(
        "CREATE TABLE labels (bank_hash TEXT, y INTEGER, source TEXT, run_id TEXT, doc TEXT)"
    )
    con.execute(
        "CREATE TABLE reminders (invoice_id TEXT, as_of TEXT, status TEXT, hold_reason TEXT, doc TEXT)"
    )
    con.execute("CREATE TABLE runs (run_id TEXT, doc TEXT)")
    con.executemany(
        "INSERT INTO meta VALUES (?, ?)", [(k, json.dumps(v, default=_jd)) for k, v in meta.items()]
    )
    con.executemany(
        "INSERT INTO bank_lines VALUES (?,?,?,?,?,?)",
        [
            (
                h,
                ln["date"].isoformat(),
                ln["label"],
                ln.get("reference") or "",
                ln["amount_cents"],
                ln.get("source_file", ""),
            )
            for h, ln in L.lines.items()
        ],
    )
    con.executemany(
        "INSERT INTO allocations VALUES (?,?,?,?,?,?,?)",
        [
            (
                a["bank_hash"],
                a["share_key"],
                a["invoice_id"],
                a["cents"],
                a["source"],
                a["run_id"],
                a["date"].isoformat(),
            )
            for a in L.allocations
        ],
    )
    con.executemany(
        "INSERT INTO decisions VALUES (?,?,?,?)",
        [
            (h, d["outcome"], int(bool(d.get("final"))), json.dumps(d, default=_jd))
            for h, d in L.decisions.items()
        ],
    )
    con.executemany(
        "INSERT INTO labels VALUES (?,?,?,?,?)",
        [
            (x["bank_hash"], x["y"], x["source"], x["run_id"], json.dumps(x, default=_jd))
            for x in L.labels
        ],
    )
    con.executemany(
        "INSERT INTO reminders VALUES (?,?,?,?,?)",
        [
            (
                r["invoice_id"],
                r["as_of"].isoformat(),
                r["status"],
                r["hold_reason"],
                json.dumps(r, default=_jd),
            )
            for r in L.reminders
        ],
    )
    con.executemany(
        "INSERT INTO runs VALUES (?,?)", [(r["run_id"], json.dumps(r, default=_jd)) for r in L.runs]
    )
    con.commit()
    con.close()
    os.replace(tmp, path)


def _undate(d: dict, keys=("date", "as_of")) -> dict:
    for k in keys:
        if isinstance(d.get(k), str):
            d[k] = dt.date.fromisoformat(d[k])
    return d


def _restore_option(o: dict | None) -> dict | None:
    if o is None:
        return None
    o["key"] = frozenset((inv, int(c)) for inv, c in o["key"])
    return o


def load_state(L: Ledger, path: Path) -> dict:
    """Restore a ledger saved by `save_state` onto a freshly built Ledger for the same data."""
    con = sqlite3.connect(path)
    meta = {k: json.loads(v) for k, v in con.execute("SELECT k, v FROM meta")}
    for h, d, lab, ref, amt, src in con.execute("SELECT * FROM bank_lines"):
        L.lines[h] = {
            "bank_hash": h,
            "date": dt.date.fromisoformat(d),
            "label": lab,
            "reference": ref,
            "amount_cents": amt,
            "source_file": src,
        }
    # Replay postings in their original order through the same code path as a live post, so
    # derived state (payer history, trusted label layouts) matches the ledger that was saved.
    posting: tuple | None = None
    edges: list[tuple[str, int]] = []
    for h, k, _inv, c, src, run, d in con.execute("SELECT * FROM allocations ORDER BY rowid"):
        if (h, src, run, d) != posting:
            if posting:
                L.record(
                    posting[0], edges, posting[1], posting[2], dt.date.fromisoformat(posting[3])
                )
            posting, edges = (h, src, run, d), []
        edges.append((k, c))
    if posting:
        L.record(posting[0], edges, posting[1], posting[2], dt.date.fromisoformat(posting[3]))
    for (doc,) in con.execute("SELECT doc FROM decisions"):
        d = _undate(json.loads(doc))
        d["top"] = _restore_option(d.get("top"))
        d["alts"] = [_restore_option(a) for a in d.get("alts", [])]
        L.decisions[d["bank_hash"]] = d
    L.labels = [_undate(json.loads(doc)) for (doc,) in con.execute("SELECT doc FROM labels")]
    L.reminders = [_undate(json.loads(doc)) for (doc,) in con.execute("SELECT doc FROM reminders")]
    L.runs = [_undate(json.loads(doc)) for (doc,) in con.execute("SELECT doc FROM runs")]
    for sig, counts in meta.get("sig_review", {}).items():
        L.sig_review[sig] = list(counts)
    for a in meta.get("aliases", []):
        L.aliases[a[0]] |= set(a[1])
    con.close()
    return meta
