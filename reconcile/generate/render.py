"""Render a simulated World into one client's export files (and the hidden ground truth).

The layout (file names, headers, separators, number/date formats, preamble rows, encodings) comes
entirely from the client config, so the same world can be written as Azur, Maple or any perturbed
schema. Ground truth goes to `_truth/`, which the pipeline never reads.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import random
import unicodedata
from collections.abc import Sequence
from pathlib import Path

import yaml

from reconcile.dates import month_end, week_ends
from reconcile.generate.world import World
from reconcile.ids import bank_line_hash


def _ascii_upper(s: str) -> str:
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    return " ".join(s.upper().split())


def fmt_amount(cents: int | None, decimal: str, thousands: str) -> str:
    if cents is None or cents == "":
        return ""
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(int(cents)), 100)
    w = f"{whole:,}".replace(",", thousands)
    return f"{sign}{w}{decimal}{frac:02d}"


def fmt_pct(pct: float, decimal: str) -> str:
    s = f"{pct:.2f}".rstrip("0").rstrip(".")
    return s.replace(".", decimal)


def _fmt_period(d: dt.date, spec: dict) -> str:
    pf = spec.get("period_format", "%Y-%m")
    return d.strftime(spec["date_format"] if pf == "date" else pf)


def render_label(cfg: dict, kind: str, ctx: dict) -> tuple[str, str]:
    """Bank label and reference for a movement, from the client's templates."""
    tmpl = cfg["labels"][kind]
    if kind == "non_ar":
        tmpl = tmpl[ctx.get("variant", 0)]
    label = _ascii_upper(tmpl["label"].format_map(_Default(ctx)))
    ref = _ascii_upper(tmpl.get("ref", "").format_map(_Default(ctx)))
    limit = cfg.get("max_label_len")
    if limit:  # one description column, truncated by the payment scheme (UK: 18 chars)
        label = " ".join(f"{label} {ref}".split())[:limit].rstrip()
        ref = ""
    return label, ref


class _Default(dict):
    def __missing__(self, key: str) -> str:
        return ""


# ---------------- logical tables (canonical field -> python value) ----------------


def logical_tables(world: World, rng: random.Random) -> dict[str, list[dict]]:
    cfg = world.cfg
    inv_rows = []
    for inv in world.invoices:
        r = inv.resident
        month = dt.date(int(inv.period[:4]), int(inv.period[5:]), 1)
        inv_rows.append(
            {
                "invoice_id": inv.invoice_id,
                "issue_date": inv.issue,
                "due_date": inv.due,
                "resident_id": r.rid,
                "resident_last": r.last.upper(),
                "resident_first": r.first,
                "resident_name": f"{r.first} {r.last}",
                "facility": r.facility["name"],
                "period": month,
                "period_end": month_end(month),
                "doc_type": inv.doc_type,
                "amount": inv.cents,
                "original_invoice": inv.original,
            }
        )
    pay_rows = []
    strip_rate = cfg["layout"]["payers"].get("strip_id_zeros_rate", 0.0)
    for r in world.residents:
        for p, pct in r.payers:
            rid = r.rid.lstrip("0") if rng.random() < strip_rate else r.rid
            pay_rows.append(
                {
                    "resident_id": rid,
                    "resident_name": f"{r.last.upper()} {r.first}",
                    "payer_name": p.name,
                    "payer_type": p.ptype,
                    "relationship": p.relationship,
                    "share_pct": pct,
                    "mandate_ref": p.mandate,
                    "region": r.region,
                    "resident_exit_date": r.exit,
                }
            )
    rem_rows = []
    for rem in world.remittances:
        for ben, period, net, gross in rem.rows:
            month = dt.date(int(period[:4]), int(period[5:]), 1)
            rem_rows.append(
                {
                    "remittance_id": rem.remittance_id,
                    "date": rem.date,
                    "payer_name": rem.payer.name,
                    "facility": rem.facility,
                    "beneficiary_name": ben,
                    "period": month,
                    "amount": net,
                    "gross": gross,
                    "contribution": gross - net,
                }
            )
    rem_rows.sort(key=lambda x: (x["date"], x["remittance_id"]))
    bank_rows = []
    balance = 1_250_000_00
    types = cfg.get("bank_types", {})
    for ln in sorted(world.lines, key=lambda x: (x.date, x.seq)):
        label, ref = render_label(cfg, ln.kind, ln.ctx)
        balance += ln.cents
        bank_rows.append(
            {
                "date": ln.date,
                "value_date": ln.date + dt.timedelta(days=rng.choice([0, 0, 1, 2])),
                "label": label,
                "reference": ref,
                "amount": ln.cents,
                "type": types.get(ln.kind, ""),
                "balance": balance,
                "_line": ln,
            }
        )
    return {"invoices": inv_rows, "payers": pay_rows, "remittances": rem_rows, "bank": bank_rows}


# ---------------- writing ----------------


def _cell(field: str, value, spec: dict, values_map: dict) -> str:
    if value is None or value == "":
        return ""
    if field in values_map:
        return values_map[field].get(value, str(value))
    if field in ("amount", "gross", "contribution", "balance", "debit", "credit"):
        return fmt_amount(value, spec.get("decimal", "."), spec.get("thousands", ""))
    if field == "share_pct":
        return fmt_pct(value, spec.get("decimal", "."))
    if field == "period":
        return _fmt_period(value, spec)
    if isinstance(value, dt.date):
        return value.strftime(spec["date_format"])
    return str(value)


def _bank_row_cells(row: dict, spec: dict) -> dict:
    """Bank amounts: either Debit/Credit columns or one signed column."""
    out = dict(row)
    if spec.get("amount_mode", "debit_credit") == "debit_credit":
        out["debit"] = -row["amount"] if row["amount"] < 0 else None
        out["credit"] = row["amount"] if row["amount"] > 0 else None
        out["amount"] = None
    for k, v in spec.get("constants", {}).items():
        out[k] = v
    return out


def write_table(
    rows: list[dict],
    spec: dict,
    path: Path,
    junk: str | None,
    rng: random.Random,
    preamble: Sequence[str] = (),
    footer: str | None = None,
) -> None:
    cols = spec["columns"]
    values_map = spec.get("values", {})
    headers = list(cols.values()) + ([junk] if junk else [])
    buf = io.StringIO()
    for line in preamble:
        buf.write(line + "\n")
    w = csv.writer(buf, delimiter=spec["sep"], lineterminator="\n")
    w.writerow(headers)
    for row in rows:
        if "label" in cols:  # bank statement
            row = _bank_row_cells(row, spec)
        cells = [_cell(f, row.get(f), spec, values_map) for f in cols]
        if junk:
            cells.append(f"B{rng.randint(1000, 9999)}")
        w.writerow(cells)
    if footer:
        buf.write(footer + "\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(buf.getvalue(), encoding=spec.get("encoding", "utf-8"))


def write_client(
    world: World,
    layout: dict,
    out: Path,
    junk: str | None = None,
    seed: int = 0,
    export_window_days: int = 16,
) -> dict:
    """Write all files for one client; returns summary counts."""
    rng = random.Random(seed)
    tables = logical_tables(world, rng)
    out.mkdir(parents=True, exist_ok=True)
    for kind in ("invoices", "payers", "remittances"):
        spec = layout[kind]
        write_table(tables[kind], spec, out / spec["file"], junk, rng)
    spec = layout["bank"]
    bank = tables["bank"]
    first, last = world.months[0], month_end(world.months[-1])
    n_files = 0
    for we in week_ends(first, last):
        lo = we - dt.timedelta(days=export_window_days)
        rows = [r for r in bank if lo <= r["date"] <= we]
        if not rows:
            continue
        fmt = spec["date_format"]
        ctx = {
            "start": lo.strftime(fmt),
            "end": we.strftime(fmt),
            "balance": fmt_amount(
                rows[-1]["balance"], spec.get("decimal", "."), spec.get("thousands", "")
            ),
        }
        pre = [p.format(**ctx) for p in spec.get("preamble", [])]
        foot = spec["footer"].format(**ctx) if spec.get("footer") else None
        write_table(
            rows, spec, out / spec["file_pattern"].format(end=we.isoformat()), junk, rng, pre, foot
        )
        n_files += 1
    _write_truth(world, tables, layout, out / "_truth")
    return {
        "invoices": len(tables["invoices"]),
        "payers": len(tables["payers"]),
        "remittance_rows": len(tables["remittances"]),
        "bank_lines": len(bank),
        "bank_files": n_files,
    }


def _write_truth(world: World, tables: dict, layout: dict, tdir: Path) -> None:
    tdir.mkdir(parents=True, exist_ok=True)
    with (tdir / "ground_truth.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "bank_hash",
                "date",
                "amount_cents",
                "label",
                "reference",
                "true_class",
                "match_type",
                "allocation_json",
                "share_alloc_json",
                "unapplied_cents",
                "shift_tag",
                "month_index",
            ]
        )
        seen: set[str] = set()
        for r in tables["bank"]:
            ln = r["_line"]
            h = bank_line_hash(r["date"].isoformat(), r["amount"], r["label"], r["reference"])
            assert h not in seen, (
                f"generator produced two identical bank lines: {r['label']} {r['date']}"
            )
            seen.add(h)
            w.writerow(
                [
                    h,
                    r["date"].isoformat(),
                    r["amount"],
                    r["label"],
                    r["reference"],
                    ln.true_class,
                    ln.match_type,
                    json.dumps(ln.edges),
                    json.dumps(ln.share_edges),
                    ln.unapplied,
                    ln.shift_tag,
                    ln.month_index,
                ]
            )
    # the true mapping (canonical field -> header) for the mapping eval
    truth = {
        k: {
            "file": v.get("file") or v.get("file_pattern"),
            "columns": v["columns"],
            "date_format": v["date_format"],
            "decimal": v.get("decimal", "."),
            "amount_mode": v.get("amount_mode"),
            "values": v.get("values", {}),
        }
        for k, v in layout.items()
    }
    (tdir / "mapping.yaml").write_text(
        yaml.safe_dump(truth, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    meta = {
        "months": [m.isoformat() for m in world.months],
        "shift_start_month_index": world.cfg["shift"]["start_month_index"],
    }
    (tdir / "meta.yaml").write_text(yaml.safe_dump(meta), encoding="utf-8")
