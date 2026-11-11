"""LLM mapping proposal. The model reads column *profiles* (names, inferred types, masked samples,
category values) and proposes the field mapping and value mappings. Parsing facts that can be
measured from bytes (encoding, separator, header/footer rows, date format) come from the
deterministic profiler, not the model.

Expected cost per onboarding (4 files, ~6k tokens in, ~1.5k out): ~$0.03 on claude-sonnet-5-5,
~$0.001 on claude-haiku-5-5. Unverified in this repo's environment (no API key here); the
mapping eval reports the measured numbers when it is run with a key.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from reconcile.llm import structured_call
from reconcile.mapping.profile import discover, profile_file
from reconcile.mapping.propose_fallback import propose as propose_fallback
from reconcile.mapping.template import REQUIRED

Role = Literal["invoices", "payers", "remittances", "bank"]


class ColumnChoice(BaseModel):
    field: str = Field(description="target field name from the field list")
    columns: list[str] = Field(description="source column(s); two for a name split in last/first")


class ValueChoice(BaseModel):
    field: Literal["payer_type", "doc_type"]
    source_value: str
    canonical: Literal[
        "resident",
        "family",
        "public_aid",
        "guardian",
        "health_funder",
        "other",
        "invoice",
        "credit",
    ]


class FileChoice(BaseModel):
    file: str = Field(description="the file pattern exactly as given")
    role: Role
    bank_amount_mode: Literal["debit_credit", "signed", "not_bank"]
    columns: list[ColumnChoice]
    values: list[ValueChoice]


class MappingProposal(BaseModel):
    files: list[FileChoice]
    doubts: list[str] = Field(
        description="anything a human should check, in one short sentence each"
    )


SYSTEM = """You map a care-home group's raw finance exports onto a fixed entity model.
Roles and target fields:
- invoices: invoice_id, resident_id, resident_name (or two columns: last name, first name), facility, issue_date, due_date, period, doc_type, amount, original_invoice
- payers (who pays which share for which resident): resident_id, resident_name, payer_name, payer_type, share_pct, mandate_ref, resident_exit_date
- remittances (statement from a public payer listing the residents a transfer covers): remittance_id, date, payer_name, facility, beneficiary_name, period, amount (the amount actually paid, not the gross fee)
- bank: date, label, reference, and either debit+credit or one signed amount
payer_type values map to: resident, family, public_aid, guardian, health_funder, other.
doc_type values map to: invoice, credit.
Only map a column when the profile supports it; leave a field out rather than guess. Columns not
listed for a role (balances, sort codes, export ids, gross fees) must not be mapped."""


def _profiles(data_dir: Path) -> list[dict]:
    out = []
    for g in discover(data_dir):
        fp = profile_file(g["files"][0], g["files"][1:])
        out.append(
            {
                "file": g["pattern"],
                "periodic_export_files": len(g["files"]),
                "rows": fp.n_rows,
                "columns": [
                    {
                        "name": c.name,
                        "type": c.kind,
                        "blank_rate": c.blank_rate,
                        "distinct": c.distinct,
                        "negative_rate": c.negative_rate,
                        "samples": c.samples,
                    }
                    for c in fp.columns
                ],
            }
        )
    return out


def propose(
    data_dir: Path, client: str, model: str | None = None
) -> tuple[dict | None, list[dict], dict]:
    """Returns (template or None if the model was unavailable, review rows, call record)."""
    base, review = propose_fallback(data_dir, client)  # dialect + date formats + patterns
    profiles = _profiles(Path(data_dir))
    user = "Profiles of the exports (person names masked as <A>):\n" + json.dumps(
        profiles, ensure_ascii=False, indent=1
    )
    out, rec = structured_call(
        SYSTEM, user, MappingProposal, model=model, purpose=f"mapping:{client}"
    )
    if out is None:
        return None, review, rec
    tpl, review = _merge(base, out, profiles, rec.get("model", "llm"))
    review += [
        {"file": "(llm)", "field": "doubt", "column": d, "score": 0.5, "flag": "model doubt"}
        for d in out.doubts
    ]
    return tpl, review, rec


VALID_CANONICAL = {
    "payer_type": {"resident", "family", "public_aid", "guardian", "health_funder", "other"},
    "doc_type": {"invoice", "credit"},
}
OPTIONAL_FIELDS = (
    "original_invoice",
    "resident_name",
    "mandate_ref",
    "resident_exit_date",
    "reference",
    "facility",
    "debit",
    "credit",
    "amount",
)


def _merge(base: dict, out: MappingProposal, profiles: list[dict], model: str) -> tuple[dict, list]:
    """Apply the model's proposal onto the deterministic template, keeping only what can be
    checked: file patterns that were discovered, columns that exist in that file, canonical values
    from the entity model. Returns the template and review rows built from the *final* template,
    so the human confirms exactly what will be saved, with disagreements flagged."""
    headers = {p["file"]: {c["name"] for c in p["columns"]} for p in profiles}
    tpl = json.loads(json.dumps(base))
    tpl["proposed_by"] = model
    review: list[dict] = []
    for kind, spec in base["files"].items():
        fc = next((f for f in out.files if f.role == kind and f.file in headers), None)
        if fc is None:
            review.append(
                {
                    "file": spec["path"],
                    "field": "(role)",
                    "column": kind,
                    "score": 0.5,
                    "flag": "model did not place this file; deterministic mapping kept",
                }
            )
            continue
        known = headers[fc.file]
        cols = {}
        for cc in fc.columns:
            mappable = cc.field in REQUIRED[kind] or cc.field in OPTIONAL_FIELDS
            if mappable and cc.columns and all(c in known for c in cc.columns):
                cols[cc.field] = cc.columns[0] if len(cc.columns) == 1 else cc.columns
        new = tpl["files"][kind]
        new["path"] = fc.file
        new["columns"] = cols
        if kind == "bank":
            new["amount_mode"] = "signed" if fc.bank_amount_mode == "signed" else "debit_credit"
        vals: dict[str, dict] = {}
        for v in fc.values:
            if v.canonical in VALID_CANONICAL[v.field]:
                vals.setdefault(v.field, {})[v.source_value] = v.canonical
        if vals:
            new["values"] = vals
        else:
            new.pop("values", None)
        before = base["files"][kind]
        for f, c in cols.items():
            same = before.get("columns", {}).get(f) == c
            review.append(
                {
                    "file": fc.file,
                    "field": f,
                    "column": str(c),
                    "score": 1.0 if same else 0.5,
                    "flag": None
                    if same
                    else f"deterministic mapper chose {before.get('columns', {}).get(f)!r}",
                }
            )
        for f in set(before.get("columns", {})) - set(cols):
            review.append(
                {
                    "file": fc.file,
                    "field": f,
                    "column": "-",
                    "score": 0.5,
                    "flag": f"model left it unmapped; deterministic mapper chose {before['columns'][f]!r}",
                }
            )
        for field, mapping in vals.items():
            for src, canon in mapping.items():
                same = before.get("values", {}).get(field, {}).get(src) == canon
                review.append(
                    {
                        "file": fc.file,
                        "field": f"{field} value",
                        "column": src,
                        "proposed": canon,
                        "score": 1.0 if same else 0.5,
                        "flag": None if same else "differs from deterministic mapper",
                    }
                )
    return tpl, review
