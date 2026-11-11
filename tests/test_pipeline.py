"""Invariants of the weekly pipeline: idempotent import, money balance, finality of auto posts,
threshold certification, ambiguity handling, conflict handling, and point-in-time features."""

from __future__ import annotations

import copy
import csv
import datetime as dt
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import numpy as np
import pandas as pd
import pytest

from reconcile.allocate import greedy
from reconcile.candidates import Cand, Matcher
from reconcile.decide import Thresholds, choose_thresholds, cp_lower, outcome
from reconcile.evaluate import evaluate_client, load_truth
from reconcile.export import export_lettrage
from reconcile.features import FEATURES, candidate_features
from reconcile.generate import generate_client
from reconcile.hardcases import build_case, load_cases
from reconcile.ledger import Ledger
from reconcile.mapping.template import _load_bank_file, load_client
from reconcile.normalize import label_signature
from reconcile.reminders import run_reminders
from reconcile.review import apply_decision
from reconcile.scorer import GBMScorer
from reconcile.state import _new_format, check_balance, load_state, save_state

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates" / "azur.yaml"


@pytest.fixture(scope="module")
def small_azur(tmp_path_factory):
    out = tmp_path_factory.mktemp("data")
    generate_client("azur", out, overrides={"n_residents": 300})
    return out / "azur"


@pytest.fixture(scope="module")
def small_run(small_azur):
    return evaluate_client("azur", TEMPLATE, small_azur, "rules", out_dir=None)


def test_overlapping_exports_import_once(small_azur):
    data = load_client(TEMPLATE, small_azur)
    truth = load_truth(small_azur)
    assert data.bank.bank_hash.is_unique
    # every exported line is known to the truth file exactly once
    assert set(data.bank.bank_hash) <= set(truth.keys)
    L = Ledger(data)
    rows = data.bank.to_dict("records")
    assert L.import_lines(rows) == len(rows)
    assert L.import_lines(rows) == 0


def test_identical_lines_inside_one_export_are_both_kept(tmp_path):
    f = tmp_path / "bank" / "x.csv"
    f.parent.mkdir()
    row = "05/02/2025;05/02/2025;VIR SEPA MME LEROY SOPHIE;;;2 400,00"
    f.write_text(
        "Date opération;Date valeur;Libellé;Référence;Débit;Crédit\n" + row + "\n" + row + "\n",
        encoding="cp1252",
    )
    spec = {
        "sep": ";",
        "encoding": "cp1252",
        "decimal": ",",
        "date_format": "%d/%m/%Y",
        "amount_mode": "debit_credit",
        "columns": {
            "date": "Date opération",
            "label": "Libellé",
            "reference": "Référence",
            "debit": "Débit",
            "credit": "Crédit",
        },
    }
    df = _load_bank_file(f, spec)
    assert len(df) == 2 and df.bank_hash.nunique() == 2


def test_money_balances_after_every_run(small_run):
    assert small_run.ledger.runs and all(r["balance_ok"] for r in small_run.ledger.runs)


def test_auto_posts_are_final(small_run):
    L = small_run.ledger
    auto_lines = {h for h, d in L.decisions.items() if d.get("outcome") == "auto"}
    assert auto_lines
    for h in auto_lines:
        sources = [a["source"] for a in L.allocations if a["bank_hash"] == h]
        runs = {a["run_id"] for a in L.allocations if a["bank_hash"] == h}
        assert set(sources) == {"auto"} and len(runs) == 1  # posted once, never re-decided


def test_clopper_pearson_bound():
    assert cp_lower(300, 300) == pytest.approx(0.990, abs=0.001)
    assert cp_lower(297, 300) == pytest.approx(0.974, abs=0.002)


def test_uncertifiable_set_means_no_auto():
    scores = np.linspace(0.9, 1.0, 50)
    th = choose_thresholds(scores, np.ones(50, dtype=bool))  # 50/50 correct certifies only ~0.94
    assert th.auto == float("inf")
    assert outcome(0.9999, 1, th) == "review"
    # the report says how close the best cut came: all 50 lines, 50 correct
    assert (th.n_at_auto, th.k_at_auto) == (50, 50)
    assert th.certified_lb == pytest.approx(cp_lower(50, 50))


def test_ambiguous_subset_is_never_auto():
    th = Thresholds(auto=0.9, review=0.3, certified_lb=0.99, n_at_auto=1000, k_at_auto=1000)
    assert outcome(0.9999, 1, th) == "auto"
    assert outcome(0.9999, 2, th) == "review"


def test_greedy_detects_stranded_line():
    base = load_cases()
    data, _ = build_case(
        base, {"id": "x", "line": {"date": "2025-02-11", "amount": 2400, "label": "x"}}
    )
    L = Ledger(data)
    key = "FA2025-000201|" + next(s.pid for s in L.by_invoice["FA2025-000201"])
    a = Cand("line_a", "name_single", ((key, 240000),), 0, score=0.99)
    b = Cand("line_b", "name_single", ((key, 240000),), 0, score=0.80)
    chosen, stranded = greedy([a, b], L)
    assert set(chosen) == {"line_a"} and stranded == {"line_b"}


def test_features_are_point_in_time():
    base = load_cases()
    data, _ = build_case(
        base, {"id": "x", "line": {"date": "2025-02-11", "amount": 2400, "label": "x"}}
    )
    L = Ledger(data)
    share = L.by_invoice["FA2025-000202"][0]
    line = {
        "date": dt.date(2025, 2, 11),
        "label": "VIR SEPA MME LEROY SOPHIE",
        "reference": "",
        "amount_cents": 240000,
    }
    c = Cand("h", "name_fifo", ((share.key, 240000),), 0, {"name_score": 100})
    before = candidate_features(c, line, L)
    L.pid_history[share.pid].append((dt.date(2025, 3, 11), 240000))  # a payment from the future
    assert candidate_features(c, line, L) == before


def test_review_actions_post_once_and_refuse_conflicts(small_run):
    """The review app persists only when apply_decision says the post happened; a second click
    (or a stale browser tab) and a conflicting pick are refused without touching the ledger."""
    L = copy.deepcopy(small_run.ledger)
    pending = [
        h
        for h, d in sorted(L.decisions.items())
        if d.get("outcome") == "review" and not d.get("final") and d["top"]["edges"]
    ]
    fitting = [h for h in pending if L.fits([tuple(e) for e in L.decisions[h]["top"]["edges"]])]
    assert len(fitting) >= 2
    h, other = fitting[0], fitting[1]
    n_labels = len(L.labels)
    assert apply_decision(L, h, "accept", "t") is True
    assert L.decisions[h]["final"] and len(L.labels) > n_labels
    assert [a["source"] for a in L.allocations if a["bank_hash"] == h] == ["review_accept"] * len(
        L.decisions[h]["final_edges"]
    )
    before = len(L.allocations)
    assert apply_decision(L, h, "accept", "t") is False  # already resolved: no double post
    assert apply_decision(L, h, "reject", "t") is False
    # another posting takes the invoices of `other` in the meantime: its accept must be refused
    for k, _ in L.decisions[other]["top"]["edges"]:
        L.allocated[k] = L.shares[k].cents
    assert apply_decision(L, other, "accept", "t") is False
    assert len(L.allocations) == before and not L.decisions[other].get("final")
    assert check_balance(L)["over_allocated_lines"] == 0


def test_saved_state_reloads_identically(small_azur, small_run, tmp_path):
    """`make run` on saved state must behave exactly like continuing in memory."""
    L = small_run.ledger
    save_state(
        L, tmp_path / "s.sqlite", {"scorer": "rules", "thresholds": small_run.thresholds.as_dict()}
    )
    R = Ledger(load_client(TEMPLATE, small_azur))
    load_state(R, tmp_path / "s.sqlite")
    nonzero = lambda d: {k: v for k, v in d.items() if v}  # noqa: E731
    assert nonzero(R.allocated) == nonzero(L.allocated)
    assert dict(R.signature_counts) == dict(L.signature_counts)
    assert dict(R.pid_history) == dict(L.pid_history)
    assert dict(R.sig_review) == dict(L.sig_review) and dict(R.aliases) == dict(L.aliases)
    assert R.decisions.keys() == L.decisions.keys() and len(R.labels) == len(L.labels)


def test_remittance_never_allocates_more_than_the_line(small_azur):
    """A short-paid statement: the bank line is 50 EUR less than the statement rows add up to."""
    data = load_client(TEMPLATE, small_azur)
    L = Ledger(data)
    M = Matcher(L, data.template)
    rows = next(r for r in M.remit_rows.values() if len(r) > 2)
    total = sum(int(r["amount_cents"]) for r in rows)
    text = f"{rows[0]['payer_name']} {rows[0]['remittance_id']}"
    as_of = rows[0]["date"] + dt.timedelta(days=30)
    full = M._remittance({"bank_hash": "x"}, text, total, as_of, as_of, None)
    short = M._remittance({"bank_hash": "x"}, text, total - 5000, as_of, as_of, None)
    assert full and short
    assert max(sum(c for _, c in x.edges) for x in full) == total
    assert all(sum(c for _, c in x.edges) <= total - 5000 for x in short)
    # and the ledger itself refuses a posting larger than the bank line
    line = next(r for r in data.bank.to_dict("records") if r["amount_cents"] > 0)
    L.import_lines([line])
    with pytest.raises(ValueError):
        L.post(line["bank_hash"], [(next(iter(L.shares)), line["amount_cents"] + 1)], "auto", "t")


def test_new_layout_needs_certified_probation():
    """A new bank-label layout is released only on the same evidence as the auto threshold."""
    line = {"label": "VIREMENT RECU /DE M VIDAL LUC"}
    sig = label_signature(line["label"])
    L = cast(
        Ledger,
        SimpleNamespace(
            signature_counts={"VIR SEPA <A>": 5000}, sig_review=defaultdict(lambda: [0, 0])
        ),
    )
    assert _new_format(L, line)  # never seen: held
    L.sig_review[sig] = [30, 30]
    assert _new_format(L, line)  # 30/30 certifies only ~90.5%: still held
    L.sig_review[sig] = [400, 398]
    assert _new_format(L, line)  # 99.5% observed, lower bound below 99%: still held
    L.sig_review[sig] = [299, 299]
    assert not _new_format(L, line)  # lower bound >= 99%: released


def test_credit_note_after_payment_is_a_refund_not_a_failure(small_azur):
    """A hospital-stay credit issued after the family already paid: the run must not fail."""
    data = load_client(TEMPLATE, small_azur)
    inv = data.invoices[data.invoices.doc_type == "invoice"].iloc[0]
    credit = inv.copy()
    credit["invoice_id"], credit["doc_type"] = "AV-TEST-1", "credit"
    credit["original_invoice"], credit["amount_cents"] = inv["invoice_id"], -10_000
    data.invoices = pd.concat([data.invoices, credit.to_frame().T], ignore_index=True)
    L = Ledger(data)
    for s in L.by_invoice[inv["invoice_id"]]:
        L.allocated[s.key] = s.gross_cents  # paid in full before the credit note
    res = check_balance(L)
    assert res["over_allocated_shares"] == 0 and res["refund_due_cents"] == 10_000


def test_month_name_in_december_means_next_january(small_azur):
    data = load_client(TEMPLATE, small_azur)
    M = Matcher(Ledger(data), data.template)
    assert M._period("VIR DEPARTEMENT JANVIER", dt.date(2025, 12, 29)) == "2026-01"
    assert M._period("VIR DEPARTEMENT DECEMBRE", dt.date(2026, 1, 3)) == "2025-12"
    assert M._period("VIR DEPARTEMENT MARS", dt.date(2025, 3, 10)) == "2025-03"


def test_export_writes_posted_allocations_and_defuses_formulas(small_run, tmp_path):
    L = copy.deepcopy(small_run.ledger)
    n = export_lettrage(L, tmp_path / "out.csv")
    rows = list(csv.DictReader((tmp_path / "out.csv").open(encoding="utf-8")))
    assert n == len(rows) > 0
    assert set(rows[0]) == {
        "invoice_id",
        "payer_share",
        "bank_line",
        "bank_date",
        "amount",
        "decided_by",
        "run_id",
    }
    assert {r["decided_by"] for r in rows} <= {"auto", "review_accept", "review_pick"}
    # a client invoice number that a spreadsheet would execute is written as text
    a = next(x for x in L.allocations if x["source"] == "auto")
    a["invoice_id"] = "=HYPERLINK(1)"
    export_lettrage(L, tmp_path / "out2.csv")
    assert "'=HYPERLINK(1)" in (tmp_path / "out2.csv").read_text(encoding="utf-8")


def test_balance_check_catches_an_over_allocated_line(small_run):
    L = copy.deepcopy(small_run.ledger)
    assert check_balance(L)["ok"]
    a = next(x for x in L.allocations if x["cents"] > 0)
    a["cents"] += 1  # one cent more than the bank line carried
    res = check_balance(L)
    assert not res["ok"] and res["over_allocated_lines"] == 1


def test_model_file_is_only_unpickled_when_its_hash_matches(tmp_path):
    rng = np.random.default_rng(0)
    feats = [{f: float(rng.random()) for f in FEATURES} for _ in range(200)]
    y = [int(f["name_score"] > 0.5) for f in feats]
    model = GBMScorer().fit(feats, y)
    digest = model.save(tmp_path / "m.pkl")
    again = GBMScorer.load(tmp_path / "m.pkl", digest)
    assert np.allclose(again.score(feats[:5]), model.score(feats[:5]))
    (tmp_path / "m.pkl").write_bytes((tmp_path / "m.pkl").read_bytes() + b"x")
    with pytest.raises(ValueError):
        GBMScorer.load(tmp_path / "m.pkl", digest)


def test_an_approved_reminder_is_not_redrafted_a_week_later(small_run):
    L = copy.deepcopy(small_run.ledger)
    last = max(r["as_of"] for r in L.reminders if r["status"] == "drafted")
    drafted = [r for r in L.reminders if r["as_of"] == last and r["status"] == "drafted"]
    assert drafted
    for r in drafted:
        r["status"] = "approved"
    again = run_reminders(L, last + dt.timedelta(days=7), "fr", "t")
    redrafted = {r["share_key"] for r in again if r["status"] == "drafted"}
    assert not redrafted & {r["share_key"] for r in drafted}


@pytest.fixture(scope="module")
def small_maple(tmp_path_factory):
    out = tmp_path_factory.mktemp("maple")
    generate_client("maple", out, overrides={"n_residents": 120})
    return out / "maple"


def test_truncated_dd_return_is_queued_with_every_payer_it_could_be(small_maple):
    """'UNPAID DD MCH00001' fits ten mandates; the amount narrows it to the payers just debited
    that amount. Each is offered to the reviewer; the line can never be auto-posted."""
    data = load_client(ROOT / "templates" / "maple.yaml", small_maple)
    L = Ledger(data)
    M = Matcher(L, data.template)
    groups = defaultdict(list)
    for m in M.mandate_prefixes:
        groups[m[:8]].append(m)
    prefix, mandates = next((p, ms) for p, ms in groups.items() if len(ms) >= 2)
    d = dt.date(2025, 10, 1)
    debited = []
    for i, m in enumerate(mandates[:2]):
        share = L.by_pid[M.mandate_pid[m]][0]
        col = {
            "bank_hash": f"col{i}",
            "date": d,
            "label": m,
            "reference": "",
            "amount_cents": 50_000,
        }
        L.import_lines([col])
        L.post(col["bank_hash"], [(share.key, 50_000)], "auto", "t")
        debited.append(share.key)
    ret = {
        "bank_hash": "ret",
        "date": d + dt.timedelta(days=8),
        "label": f"UNPAID DD {prefix}",
        "reference": "",
        "amount_cents": -50_000,
    }
    res = M.generate(ret, ret["date"])
    keys = sorted(c.edges[0][0] for c in res.cands)
    assert keys == sorted(debited)
    assert all(c.signals["n_valid"] == 2 for c in res.cands)
