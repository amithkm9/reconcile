"""Invariants of the weekly pipeline: idempotent import, money balance, finality of auto posts,
threshold certification, ambiguity handling, conflict handling, and point-in-time features."""

from __future__ import annotations

import copy
import datetime as dt
from pathlib import Path

import numpy as np
import pytest

from reconcile.allocate import greedy
from reconcile.candidates import Cand
from reconcile.decide import Thresholds, choose_thresholds, cp_lower, outcome
from reconcile.evaluate import evaluate_client, load_truth
from reconcile.features import candidate_features
from reconcile.generate.__main__ import generate_client
from reconcile.hardcases import build_case, load_cases
from reconcile.ledger import Ledger
from reconcile.mapping.template import _load_bank_file, load_client
from reconcile.review import apply_decision

TEMPLATE = Path("templates/azur.yaml")


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


def test_review_accept_reports_success_and_conflict(small_run):
    """The review app persists only when apply_decision says the post happened."""
    L = copy.deepcopy(small_run.ledger)
    pending = [
        h
        for h, d in sorted(L.decisions.items())
        if d.get("outcome") == "review" and not d.get("final") and d["top"]["edges"]
    ]
    assert pending
    # a line whose suggestion fits now but cannot fit twice (each edge takes over half the open share)
    h = next(
        h
        for h in pending
        if all(0 < c <= L.open(k) < 2 * c for k, c in L.decisions[h]["top"]["edges"])
    )
    n_labels = len(L.labels)
    assert apply_decision(L, h, "accept", "t") is True
    assert L.decisions[h]["final"] and len(L.labels) > n_labels
    assert [a["source"] for a in L.allocations if a["bank_hash"] == h] == ["review_accept"] * len(
        L.decisions[h]["final_edges"]
    )
    # the same invoices are now taken: a second accept must be refused, not double-posted
    L.decisions[h]["final"] = False
    before = len(L.allocations)
    assert apply_decision(L, h, "accept", "t") is False
    assert len(L.allocations) == before


def test_new_layout_needs_certified_probation():
    """A new bank-label layout is released only on the same evidence as the auto threshold."""
    from collections import defaultdict
    from types import SimpleNamespace

    from reconcile.normalize import label_signature
    from reconcile.state import _new_format

    line = {"label": "VIREMENT RECU /DE M VIDAL LUC"}
    sig = label_signature(line["label"])
    L = SimpleNamespace(
        signature_counts={"VIR SEPA <A>": 5000}, sig_review=defaultdict(lambda: [0, 0])
    )
    assert _new_format(L, line)  # never seen: held
    L.sig_review[sig] = [30, 30]
    assert _new_format(L, line)  # 30/30 certifies only ~90.5%: still held
    L.sig_review[sig] = [400, 398]
    assert _new_format(L, line)  # 99.5% observed, lower bound below 99%: still held
    L.sig_review[sig] = [299, 299]
    assert not _new_format(L, line)  # lower bound >= 99%: released
