"""The common entity model every client's exports are mapped into.

The pipeline works on DataFrames for speed; these pydantic models are the contract for what a
loaded frame must contain. `load_client` validates a sample of every file against them field by
field, and checks category values (payer type, document type) on every row, so a bad template
fails at load time, not three steps later inside matching.
"""

from __future__ import annotations

import datetime as dt
from typing import Literal

import pandas as pd
from pydantic import BaseModel, ValidationError

PayerType = Literal["resident", "family", "public_aid", "guardian", "health_funder", "other"]


class Invoice(BaseModel):
    invoice_id: str
    resident_id: str
    resident_name: str
    facility: str
    issue_date: dt.date
    due_date: dt.date
    period: str  # YYYY-MM
    doc_type: Literal["invoice", "credit"]
    amount_cents: int  # credit notes are negative
    original_invoice: str = ""


class PayerShare(BaseModel):
    resident_id: str
    payer_name: str
    payer_type: PayerType
    share_pct: float
    mandate_ref: str = ""
    resident_exit_date: dt.date | None = None


class BankLine(BaseModel):
    bank_hash: str
    date: dt.date
    label: str
    reference: str = ""
    amount_cents: int  # credits positive, debits negative


class RemittanceLine(BaseModel):
    remittance_id: str
    date: dt.date
    payer_name: str
    facility: str = ""
    beneficiary_name: str
    period: str
    amount_cents: int


ENTITY_MODELS: dict[str, type[BaseModel]] = {
    "invoices": Invoice,
    "payers": PayerShare,
    "bank": BankLine,
    "remittances": RemittanceLine,
}


class EntityValidationError(ValueError):
    pass


def validate_frame(df: pd.DataFrame, kind: str) -> None:
    """Validate every row; raise with the first few problems (not just the first)."""
    model = ENTITY_MODELS[kind]
    problems = []
    for i, row in enumerate(df.to_dict("records")):
        clean = {k: (None if isinstance(v, float) and pd.isna(v) else v) for k, v in row.items()}
        try:
            model.model_validate(clean)
        except ValidationError as e:
            problems.append(f"row {i}: {e.errors()[0]['loc']} {e.errors()[0]['msg']}")
            if len(problems) >= 5:
                break
    if problems:
        raise EntityValidationError(f"{kind}: " + "; ".join(problems))
