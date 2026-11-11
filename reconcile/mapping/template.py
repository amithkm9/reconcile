"""Load a client's raw exports through its mapping template into the common entity model.

A template is plain YAML: file paths, CSV dialect, column -> field mapping, parsing rules, value
mappings and reference patterns. Onboarding a client means producing this file; nothing in this
module knows any client's headers.

On load we check for drift: a missing column or a parse-failure rate above MAX_PARSE_FAILURE
raises TemplateError, because silently loading half-parsed exports is how wrong reminders happen.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import yaml

from reconcile.entities import validate_frame
from reconcile.ids import bank_line_hash
from reconcile.normalize import norm_id, norm_text

MAX_PARSE_FAILURE = 0.02
REQUIRED = {
    "invoices": [
        "invoice_id",
        "resident_id",
        "facility",
        "issue_date",
        "due_date",
        "period",
        "doc_type",
        "amount",
    ],
    "payers": ["resident_id", "payer_name", "payer_type", "share_pct"],
    "remittances": ["remittance_id", "date", "payer_name", "beneficiary_name", "period", "amount"],
    "bank": ["date", "label"],
}


class TemplateError(ValueError):
    pass


@dataclass
class ClientData:
    client: str
    template: dict
    invoices: pd.DataFrame
    payers: pd.DataFrame
    remittances: pd.DataFrame
    bank: pd.DataFrame
    language: str


def load_template(path: Path) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


# ---------------- raw reading ----------------


def read_raw(path: Path, spec: dict) -> pd.DataFrame:
    """Read one CSV as strings, skipping preamble/footer rows the template declares."""
    text = path.read_bytes().decode(spec.get("encoding", "utf-8"))
    text = text.lstrip("﻿")
    lines = text.splitlines()
    skip_top, skip_bottom = spec.get("skip_rows", 0), spec.get("skip_footer", 0)
    lines = lines[skip_top : len(lines) - skip_bottom if skip_bottom else None]
    rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=spec["sep"]))
    if not rows:
        raise TemplateError(f"{path.name}: empty")
    header, body = [h.strip() for h in rows[0]], [r for r in rows[1:] if any(c.strip() for c in r)]
    width = len(header)
    body = [(r + [""] * width)[:width] for r in body]
    return pd.DataFrame(body, columns=header, dtype=str)


def _col(df: pd.DataFrame, spec: dict, field: str, path: Path) -> pd.Series | None:
    src = spec["columns"].get(field)
    if src is None:
        return None
    srcs = src if isinstance(src, list) else [src]
    missing = [s for s in srcs if s not in df.columns]
    if missing:
        raise TemplateError(
            f"{path.name}: column(s) {missing} for field '{field}' not found; "
            f"file has {list(df.columns)}. The export format changed: update the template."
        )
    if len(srcs) == 1:
        return df[srcs[0]].fillna("").str.strip()
    return df[srcs].fillna("").agg(" ".join, axis=1).str.strip()


# ---------------- value parsing ----------------


def parse_amount(s: str, decimal: str) -> int | None:
    """'1 234,56' / '1.234,56' / '1,234.56' / '-12.30' -> cents. None if unparseable."""
    s = (s or "").strip()
    if not s:
        return None
    neg = s.startswith("-") or s.endswith("-") or (s.startswith("(") and s.endswith(")"))
    s = re.sub(r"[\s  ()+\-]", "", s)
    if decimal == ",":
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", "")
    try:
        cents = round(float(s) * 100)
    except ValueError:
        return None
    return -cents if neg else cents


def parse_date(s: str, fmt: str) -> dt.date | None:
    try:
        return dt.datetime.strptime((s or "").strip(), fmt).date()
    except ValueError:
        return None


def parse_period(s: str, fmt: str, date_fmt: str) -> str | None:
    d = parse_date(s, date_fmt if fmt == "date" else fmt)
    return f"{d.year}-{d.month:02d}" if d else None


def _check_failures(
    path: Path, field: str, raw: pd.Series, parsed: list, allow_blank: bool = False
) -> None:
    mask = raw.str.len() > 0 if allow_blank else pd.Series(True, index=raw.index)
    n = int(mask.sum())
    bad = sum(1 for ok, p in zip(mask, parsed, strict=True) if ok and p is None)
    if n and bad / n > MAX_PARSE_FAILURE:
        example = raw[[ok and p is None for ok, p in zip(mask, parsed, strict=True)]].iloc[0]
        raise TemplateError(
            f"{path.name}: {bad}/{n} values of '{field}' failed to parse (e.g. {example!r}). "
            "Date/number format in the template is wrong or the export changed."
        )


def _dates(df, spec, field, path, allow_blank=False):
    raw = _col(df, spec, field, path)
    if raw is None:
        return [None] * len(df)
    parsed = [parse_date(v, spec["date_format"]) if v else None for v in raw]
    _check_failures(path, field, raw, parsed, allow_blank)
    return parsed


def _amounts(df, spec, field, path):
    raw = _col(df, spec, field, path)
    parsed = [parse_amount(v, spec.get("decimal", ".")) for v in raw]
    _check_failures(path, field, raw, parsed)
    return parsed


def _values(series: pd.Series | None, mapping: dict | None, default: str, n: int) -> list[str]:
    if series is None:
        return [default] * n
    m = {norm_text(k): v for k, v in (mapping or {}).items()}
    return [m.get(norm_text(v), default) for v in series]


def _check_required(kind: str, spec: dict, path: Path) -> None:
    cols = spec["columns"]
    need = list(REQUIRED[kind])
    if kind == "invoices" and "resident_name" not in cols:
        raise TemplateError(f"{path.name}: template maps no resident name column")
    if kind == "bank":
        mode = spec.get("amount_mode", "debit_credit")
        need += ["debit", "credit"] if mode == "debit_credit" else ["amount"]
    missing = [f for f in need if f not in cols]
    if missing:
        raise TemplateError(f"{kind}: template does not map required field(s) {missing}")


# ---------------- per-entity loaders ----------------


def _load_invoices(path: Path, spec: dict) -> pd.DataFrame:
    _check_required("invoices", spec, path)
    df = read_raw(path, spec)
    n = len(df)
    pf = spec.get("period_format", "date")
    period_raw = _col(df, spec, "period", path)
    periods = [parse_period(v, pf, spec["date_format"]) for v in period_raw]
    _check_failures(path, "period", period_raw, periods)
    orig = _col(df, spec, "original_invoice", path)
    out = pd.DataFrame(
        {
            "invoice_id": _col(df, spec, "invoice_id", path).str.upper(),
            "resident_id": [norm_id(v) for v in _col(df, spec, "resident_id", path)],
            "resident_name": _col(df, spec, "resident_name", path),
            "facility": _col(df, spec, "facility", path),
            "issue_date": _dates(df, spec, "issue_date", path),
            "due_date": _dates(df, spec, "due_date", path),
            "period": periods,
            "doc_type": _values(
                _col(df, spec, "doc_type", path),
                spec.get("values", {}).get("doc_type"),
                "invoice",
                n,
            ),
            "amount_cents": _amounts(df, spec, "amount", path),
            "original_invoice": orig.str.upper() if orig is not None else [""] * n,
        }
    )
    # credit notes are negative whatever sign convention the export uses
    credit = out["doc_type"] == "credit"
    out.loc[credit, "amount_cents"] = -out.loc[credit, "amount_cents"].abs()
    return out


def _load_payers(path: Path, spec: dict) -> pd.DataFrame:
    _check_required("payers", spec, path)
    df = read_raw(path, spec)
    n = len(df)
    pct_raw = _col(df, spec, "share_pct", path)
    pcts = [parse_amount(v, spec.get("decimal", ".")) for v in pct_raw]
    _check_failures(path, "share_pct", pct_raw, pcts)
    mandate = _col(df, spec, "mandate_ref", path)
    rname = _col(df, spec, "resident_name", path)
    return pd.DataFrame(
        {
            "resident_id": [norm_id(v) for v in _col(df, spec, "resident_id", path)],
            "resident_name": rname if rname is not None else [""] * n,
            "payer_name": _col(df, spec, "payer_name", path),
            "payer_type": _values(
                _col(df, spec, "payer_type", path),
                spec.get("values", {}).get("payer_type"),
                "other",
                n,
            ),
            "share_pct": [p / 100 if p is not None else None for p in pcts],
            "mandate_ref": mandate.str.upper() if mandate is not None else [""] * n,
            "resident_exit_date": _dates(df, spec, "resident_exit_date", path, allow_blank=True),
        }
    )


def _load_remittances(path: Path, spec: dict) -> pd.DataFrame:
    _check_required("remittances", spec, path)
    df = read_raw(path, spec)
    n = len(df)
    pf = spec.get("period_format", "date")
    period_raw = _col(df, spec, "period", path)
    periods = [parse_period(v, pf, spec["date_format"]) for v in period_raw]
    _check_failures(path, "period", period_raw, periods)
    fac = _col(df, spec, "facility", path)
    return pd.DataFrame(
        {
            "remittance_id": _col(df, spec, "remittance_id", path).str.upper(),
            "date": _dates(df, spec, "date", path),
            "payer_name": _col(df, spec, "payer_name", path),
            "facility": fac if fac is not None else [""] * n,
            "beneficiary_name": _col(df, spec, "beneficiary_name", path),
            "period": periods,
            "amount_cents": _amounts(df, spec, "amount", path),
        }
    )


def _load_bank_file(path: Path, spec: dict) -> pd.DataFrame:
    df = read_raw(path, spec)
    n = len(df)
    dates = _dates(df, spec, "date", path)
    if spec.get("amount_mode", "debit_credit") == "debit_credit":
        deb = [parse_amount(v, spec.get("decimal", ".")) for v in _col(df, spec, "debit", path)]
        cre = [parse_amount(v, spec.get("decimal", ".")) for v in _col(df, spec, "credit", path)]
        amounts = [
            (c or 0) - abs(d or 0) if (c is not None or d is not None) else None
            for d, c in zip(deb, cre, strict=True)
        ]
        _check_failures(path, "debit/credit", pd.Series(["x"] * n), amounts)
    else:
        amounts = _amounts(df, spec, "amount", path)
    labels = _col(df, spec, "label", path)
    ref = _col(df, spec, "reference", path)
    refs = ref if ref is not None else pd.Series([""] * n)
    out = pd.DataFrame(
        {"date": dates, "label": labels.values, "reference": refs.values, "amount_cents": amounts}
    )
    seen: dict[tuple, int] = {}
    hashes = []
    for d, c, lab, r in zip(
        out["date"], out["amount_cents"], out["label"], out["reference"], strict=True
    ):
        k = (d, c, lab, r)
        occ = seen.get(k, 0)
        seen[k] = occ + 1
        hashes.append(bank_line_hash(d.isoformat(), c, lab, r, occ))
    out["bank_hash"] = hashes
    out["file_end"] = max(d for d in dates if d)
    out["source_file"] = path.name
    return out


def _sanity(payers: pd.DataFrame, bank: pd.DataFrame) -> None:
    """Data-quality gates: a template can parse every value and still be wrong (e.g. a constant
    'sort code' column mapped as the amount). These checks catch mappings that make no sense."""
    if len(bank) >= 50 and bank["amount_cents"].nunique() < 0.05 * len(bank):
        raise TemplateError(
            f"bank: only {bank['amount_cents'].nunique()} distinct amounts in {len(bank)} lines; "
            "the amount column(s) are probably mapped wrong"
        )
    sums = payers.groupby("resident_id")["share_pct"].sum()
    bad = ((sums - 100).abs() > 1).mean() if len(sums) else 0
    if bad > 0.05:
        raise TemplateError(
            f"payers: shares do not add up to 100% for {bad:.0%} of residents; "
            "share_pct (or resident_id) is probably mapped wrong"
        )


def load_client(template: dict | Path, data_dir: Path, validate: bool = True) -> ClientData:
    tpl = load_template(template) if isinstance(template, Path | str) else template
    files = tpl["files"]
    data_dir = Path(data_dir)

    def one(kind: str) -> Path:
        p = data_dir / files[kind]["path"]
        if not p.exists():
            raise TemplateError(f"{kind}: expected file {p} not found")
        return p

    invoices = _load_invoices(one("invoices"), files["invoices"])
    payers = _load_payers(one("payers"), files["payers"])
    remittances = _load_remittances(one("remittances"), files["remittances"])
    _check_required("bank", files["bank"], data_dir)
    bank_files = sorted(data_dir.glob(files["bank"]["path"]))
    if not bank_files:
        raise TemplateError(f"bank: no files match {files['bank']['path']}")
    bank = pd.concat([_load_bank_file(p, files["bank"]) for p in bank_files], ignore_index=True)
    bank = (
        bank.sort_values(["file_end", "date"])
        .drop_duplicates("bank_hash", keep="first")
        .reset_index(drop=True)
    )
    _sanity(payers, bank)
    if validate:  # contract check on a sample: fast, and enough to catch a wrong template
        for kind, frame in (
            ("invoices", invoices),
            ("payers", payers),
            ("remittances", remittances),
        ):
            validate_frame(frame.head(300), kind)
        validate_frame(
            bank[["bank_hash", "date", "label", "reference", "amount_cents"]].head(300), "bank"
        )
    return ClientData(
        tpl["client"], tpl, invoices, payers, remittances, bank, tpl.get("language", "en")
    )
