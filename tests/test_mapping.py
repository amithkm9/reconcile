"""The reuse claim, tested: new clients onboard through a template with no code change, nothing
client-specific lives in Python, and a wrong template fails loudly instead of loading garbage."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path

import pytest
import yaml

from reconcile import llm
from reconcile.generate import generate_client
from reconcile.mapping import propose_llm
from reconcile.mapping.onboard import onboard
from reconcile.mapping.profile import profile_column
from reconcile.mapping.propose_fallback import propose as propose_fallback
from reconcile.mapping.template import TemplateError, load_client
from reconcile.normalize import norm_text

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def data(tmp_path_factory):
    out = tmp_path_factory.mktemp("data")
    for c in ("azur", "maple", "client3"):
        generate_client(c, out, overrides={"n_residents": 120})
    return out


def _py_status() -> dict[str, str]:
    """Hash of every Python file in the repo: works outside git and sees edits to dirty files."""
    return {
        str(f.relative_to(ROOT)): hashlib.sha256(f.read_bytes()).hexdigest()
        for d in ("reconcile", "app", "tests")
        for f in sorted((ROOT / d).rglob("*.py"))
    }


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


@pytest.mark.parametrize("client", ["azur", "maple", "client3"])
def test_llm_profiles_carry_no_person_names(data, client):
    """What would be sent to the model: no resident or private payer name (4+ letters) in it."""
    tpl = yaml.safe_load((ROOT / "templates" / f"{client}.yaml").read_text(encoding="utf-8"))
    cd = load_client(tpl, data / client)
    private = cd.payers[cd.payers.payer_type.isin(["resident", "family"])]
    names = set(cd.invoices.resident_name) | set(private.payer_name)
    words = {w for n in names for w in norm_text(n).split() if len(w) >= 4}
    sent = norm_text(json.dumps(propose_llm._profiles(data / client), ensure_ascii=False))
    leaked = sorted(w for w in words if re.search(rf"\b{re.escape(w)}\b", sent))
    assert not leaked, leaked[:10]


def test_llm_proposal_is_validated_before_it_is_saved(data):
    """A hallucinated or injected proposal cannot point outside the client folder, invent
    columns, or map values to categories that do not exist; disagreements are shown for review."""
    base, _ = propose_fallback(data / "maple", "maple")
    profiles = propose_llm._profiles(data / "maple")
    inv = base["files"]["invoices"]
    real_col = next(iter(inv["columns"].values()))
    bad = propose_llm.MappingProposal(
        files=[
            propose_llm.FileChoice(
                file="../client3/billing_export.csv",
                role="invoices",
                bank_amount_mode="not_bank",
                columns=[],
                values=[],
            ),
            propose_llm.FileChoice(
                file=inv["path"],
                role="payers",
                bank_amount_mode="not_bank",
                columns=[
                    propose_llm.ColumnChoice(field="payer_name", columns=["No Such Column"]),
                    propose_llm.ColumnChoice(field="share_pct", columns=[real_col]),
                ],
                values=[
                    propose_llm.ValueChoice(
                        field="payer_type", source_value="Family", canonical="invoice"
                    )
                ],
            ),
        ],
        doubts=[],
    )
    tpl, review = propose_llm._merge(base, bad, profiles, "test-model")
    assert tpl["files"]["invoices"] == base["files"]["invoices"]  # unknown path ignored
    payers = tpl["files"]["payers"]
    assert "payer_name" not in payers["columns"]  # column not in the file
    assert "values" not in payers  # "family -> invoice" is not a payer type
    assert any(r["flag"] for r in review)  # the human sees the disagreements
    with pytest.raises(TemplateError):
        bad_tpl = copy.deepcopy(base)
        bad_tpl["files"]["invoices"]["path"] = "../client3/billing_export.csv"
        load_client(bad_tpl, data / "maple")
