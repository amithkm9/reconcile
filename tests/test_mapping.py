"""The reuse claim, tested: new clients onboard through a template with no code change, nothing
client-specific lives in Python, and a wrong template fails loudly instead of loading garbage."""

from __future__ import annotations

import copy
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from reconcile import llm
from reconcile.generate.__main__ import generate_client
from reconcile.mapping import propose_llm
from reconcile.mapping.onboard import onboard
from reconcile.mapping.profile import profile_column
from reconcile.mapping.template import TemplateError, load_client

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def data(tmp_path_factory):
    out = tmp_path_factory.mktemp("data")
    for c in ("maple", "client3"):
        generate_client(c, out, overrides={"n_residents": 120})
    return out


def _py_status() -> str:
    r = subprocess.run(
        ["git", "status", "--porcelain", "--", "*.py"], cwd=ROOT, capture_output=True, text=True
    )
    return r.stdout


@pytest.mark.parametrize("client", ["maple", "client3"])
def test_new_client_onboards_without_code_change(data, tmp_path, monkeypatch, client):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    before = _py_status()
    res = onboard(client, data / client, tmp_path / f"{client}.yaml", use_llm=False, yes=True)
    assert res["saved"]
    loaded = load_client(tmp_path / f"{client}.yaml", data / client)
    assert (
        len(loaded.invoices) and len(loaded.payers) and len(loaded.remittances) and len(loaded.bank)
    )
    assert set(loaded.payers.payer_type) <= {
        "resident",
        "family",
        "public_aid",
        "guardian",
        "health_funder",
    }
    assert _py_status() == before  # onboarding wrote YAML only


def _client_headers() -> set[str]:
    headers: set[str] = set()
    canonical: set[str] = set()
    for cfg_path in (ROOT / "config").glob("*.yaml"):
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        for spec in (cfg.get("layout") or {}).values():
            canonical |= set(spec["columns"])
            headers |= set(spec["columns"].values())
        for ren in (cfg.get("perturb", {}).get("rename") or {}).values():
            headers |= set(ren.values())
        for fields in (cfg.get("headers") or {}).values():
            for names in fields.values():
                headers |= set(names)
    return {h for h in headers if h not in canonical and len(h) >= 5}


def test_no_client_header_is_hard_coded_in_python():
    src = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "reconcile").rglob("*.py"))
    found = sorted(h for h in _client_headers() if re.search(rf"[\"']{re.escape(h)}[\"']", src))
    assert not found, f"client headers found in Python: {found}"


def test_missing_column_is_drift(data):
    tpl = yaml.safe_load((ROOT / "templates/maple.yaml").read_text(encoding="utf-8"))
    bad = copy.deepcopy(tpl)
    bad["files"]["invoices"]["columns"]["amount"] = "amount_gbp"
    with pytest.raises(TemplateError, match="not found"):
        load_client(bad, data / "maple")


def test_wrong_date_format_is_drift(data):
    tpl = yaml.safe_load((ROOT / "templates/maple.yaml").read_text(encoding="utf-8"))
    bad = copy.deepcopy(tpl)
    bad["files"]["invoices"]["date_format"] = "%d/%m/%Y"
    with pytest.raises(TemplateError, match="failed to parse"):
        load_client(bad, data / "maple")


def test_constant_columns_mapped_as_money_are_rejected(data):
    tpl = yaml.safe_load((ROOT / "templates/maple.yaml").read_text(encoding="utf-8"))
    bad = copy.deepcopy(tpl)
    bad["files"]["bank"]["columns"]["credit"] = "Account Number"
    with pytest.raises(TemplateError, match="distinct amounts"):
        load_client(bad, data / "maple")


def test_day_month_order_is_inferred_from_values():
    us = profile_column("d", ["12/20/2024", "01/05/2025", "02/01/2025"] * 10)
    eu = profile_column("d", ["20/12/2024", "05/01/2025", "01/02/2025"] * 10)
    assert us.date_formats == ["%m/%d/%Y"]
    assert eu.date_formats == ["%d/%m/%Y"]


def test_llm_path_falls_back_without_key_or_cache(data, tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(llm, "CACHE", tmp_path / "empty_cache")
    monkeypatch.setattr(llm, "LOG", tmp_path / "llm.jsonl")
    tpl, _, rec = propose_llm.propose(data / "maple", "maple")
    assert tpl is None and rec["skipped"] == "no_api_key"
