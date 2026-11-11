"""The generator is the source of truth for every metric, so its invariants are tested first."""

from __future__ import annotations

import filecmp
from pathlib import Path

import pytest

from reconcile.generate import generate_client, load_cfg
from reconcile.generate.world import build_world

SMALL = {"n_residents": 60, "months": 12}


def _dir_equal(a: Path, b: Path) -> bool:
    files_a = sorted(p.relative_to(a) for p in a.rglob("*") if p.is_file())
    files_b = sorted(p.relative_to(b) for p in b.rglob("*") if p.is_file())
    return files_a == files_b and all(filecmp.cmp(a / f, b / f, shallow=False) for f in files_a)


@pytest.mark.parametrize("client", ["azur", "maple", "client3"])
def test_generator_is_deterministic(tmp_path, client):
    generate_client(client, tmp_path / "a", overrides=SMALL)
    generate_client(client, tmp_path / "b", overrides=SMALL)
    assert _dir_equal(tmp_path / "a" / client, tmp_path / "b" / client)


@pytest.fixture(scope="module", params=["azur", "maple"])
def world(request):
    return build_world(load_cfg(request.param) | SMALL)


def test_truth_balances(world):
    """Every bank line's allocation + unapplied residual equals its amount; no invoice is over-allocated."""
    net: dict[str, int] = {}
    for inv in world.invoices:
        key = inv.original or inv.invoice_id
        net[key] = net.get(key, 0) + inv.cents
    paid: dict[str, int] = {}
    for ln in world.lines:
        if ln.true_class == "ar":
            assert sum(c for _, c in ln.edges) + ln.unapplied == ln.cents
            for inv_id, c in ln.edges:
                paid[inv_id] = paid.get(inv_id, 0) + c
        else:
            assert ln.edges == []
    for inv_id, p in paid.items():
        assert p <= net[inv_id], inv_id


def test_shift_only_in_test_months(world):
    start = world.cfg["shift"]["start_month_index"]
    tagged = [ln for ln in world.lines if ln.shift_tag]
    assert tagged, "shift corruption should exist"
    assert all(ln.month_index >= start for ln in tagged)


def test_every_outcome_class_is_present(world):
    classes = {ln.true_class for ln in world.lines}
    assert classes == {"ar", "unapplied", "non_ar"}
    kinds = {ln.match_type for ln in world.lines}
    assert {"dd", "dd_return", "partial", "multi_month", "bulk_remit"} <= kinds
