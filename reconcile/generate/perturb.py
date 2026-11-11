"""Derive new export schemas from a base client layout.

Client 3 and the mapping-eval variants are the same kind of business with different exports:
renamed and reordered columns, other separators, number and date conventions, a signed bank
amount instead of debit/credit, renamed category values, and a junk column.
"""

from __future__ import annotations

import copy
import random


def perturb_layout(base: dict, spec: dict, rng: random.Random) -> dict:
    layout = copy.deepcopy(base)
    for kind, fspec in layout.items():
        rename = spec.get("rename", {}).get(kind, {})
        cols = dict(fspec["columns"])
        if kind == "bank" and spec.get("bank_amount_mode") == "signed":
            cols.pop("debit", None)
            cols.pop("credit", None)
            cols["amount"] = "Amount"
            fspec["amount_mode"] = "signed"
        cols = {c: rename.get(c, h) for c, h in cols.items()}
        if spec.get("reorder"):
            items = list(cols.items())
            rng.shuffle(items)
            cols = dict(items)
        fspec["columns"] = cols
        for key in ("sep", "decimal", "thousands", "date_format"):
            if key in spec:
                fspec[key] = spec[key]
        fspec["encoding"] = spec.get("encoding", "utf-8")
        fspec.pop("preamble", None)
        fspec.pop("footer", None)
        if kind in spec.get("files", {}):
            fspec["file" if kind != "bank" else "file_pattern"] = spec["files"][kind]
        for field, vmap in spec.get("values", {}).items():
            if field in fspec.get("values", {}):
                fspec["values"][field] = dict(vmap)
    return layout


def random_variant_spec(pool: dict, rng: random.Random, base_layout: dict) -> dict:
    """A random schema variant for the mapping eval, drawn from the generator-only pool."""
    rename: dict[str, dict] = {}
    for kind, fields in pool["headers"].items():
        rename[kind] = {
            f: rng.choice(names)
            for f, names in fields.items()
            if f in base_layout[kind]["columns"] or (kind == "bank" and f == "amount")
        }
    style = rng.choice(pool["number_styles"])
    sep = rng.choice(pool["seps"])
    if sep == style["decimal"] or sep == style["thousands"]:
        sep = "\t"
    return {
        "rename": rename,
        "sep": sep,
        "decimal": style["decimal"],
        "thousands": style["thousands"],
        "date_format": rng.choice(pool["date_formats"]),
        "bank_amount_mode": rng.choice(pool["bank_amount_modes"]),
        "values": {
            "payer_type": {k: rng.choice(v) for k, v in pool["payer_type_vocab"].items()},
            "doc_type": {k: rng.choice(v) for k, v in pool["doc_type_vocab"].items()},
        },
        "reorder": True,
        "files": {
            "invoices": "export_a.csv",
            "payers": "export_b.csv",
            "remittances": "export_c.csv",
            "bank": "bank/export_d_{end}.csv",
        },
    }
