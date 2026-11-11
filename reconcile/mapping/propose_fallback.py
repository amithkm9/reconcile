"""Deterministic mapping proposal: header similarity x value-type compatibility, then optimal
assignment (Hungarian) of columns to fields, file by file.

No client header appears here. The vocabulary is in synonyms.yaml, and where words are not
enough the data decides: the date format is the one every date column parses with, a document
type whose amounts are negative is a credit note, a payer that pays for many residents is a
public body.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path

import numpy as np
import yaml
from rapidfuzz import fuzz
from scipy.optimize import linear_sum_assignment

from reconcile.mapping.profile import DATE_FORMATS, FileProfile, discover, profile_file, read_table
from reconcile.mapping.template import REQUIRED, parse_amount
from reconcile.normalize import norm_text

MIN_SCORE = 0.42
FLAG_BELOW = 0.62

FIELD_KINDS = {
    "invoice_id": {"id": 1.0},
    "remittance_id": {"id": 1.0, "category": 0.3},
    "resident_id": {"id": 1.0, "number": 0.5},
    "mandate_ref": {"id": 1.0},
    "original_invoice": {"id": 1.0, "blank": 0.5},
    "issue_date": {"date": 1.0},
    "due_date": {"date": 1.0},
    "date": {"date": 1.0},
    "resident_exit_date": {"date": 1.0, "blank": 0.6},
    "period": {"period": 1.0, "date": 0.8},
    "amount": {"number": 1.0},
    "debit": {"number": 1.0, "blank": 0.3},
    "credit": {"number": 1.0},
    "share_pct": {"percent": 1.0, "number": 0.3},
    "resident_name": {"name": 1.0, "category": 0.6, "text": 0.4},
    "payer_name": {"name": 1.0, "category": 0.8, "text": 0.4},
    "beneficiary_name": {"name": 1.0, "text": 0.5, "category": 0.4},
    "resident_last": {"name": 1.0, "category": 0.6},
    "resident_first": {"name": 1.0, "category": 0.6},
    "facility": {"category": 1.0, "name": 0.5},
    "doc_type": {"category": 1.0},
    "payer_type": {"category": 1.0},
    "label": {"text": 1.0, "name": 0.6},
    "reference": {"text": 1.0, "name": 0.6, "category": 0.5, "id": 0.4, "blank": 0.3},
}
OPTIONAL = {
    "invoices": ["original_invoice", "resident_name", "resident_last", "resident_first"],
    "payers": ["resident_name", "mandate_ref", "resident_exit_date"],
    "remittances": ["facility"],
    "bank": ["reference", "debit", "credit", "amount"],
}


@lru_cache(maxsize=1)
def synonyms() -> dict:
    return yaml.safe_load(Path(__file__).with_name("synonyms.yaml").read_text(encoding="utf-8"))


def header_score(header: str, words: list[str]) -> float:
    h = norm_text(header).lower()
    if not h:
        return 0.0
    best = 0.0
    for w in words:
        w = norm_text(w).lower()
        s = max(
            fuzz.ratio(h, w),
            0.9 * fuzz.token_sort_ratio(h, w),
            0.8 * fuzz.partial_ratio(h, w) if min(len(h), len(w)) >= 4 else 0,
        )
        best = max(best, s)
    return best / 100


def _fields(kind: str) -> list[str]:
    req = [f for f in REQUIRED[kind] if f not in ("debit", "credit")]
    return list(dict.fromkeys(req + OPTIONAL[kind]))


def assign_columns(fp: FileProfile, kind: str) -> dict[str, tuple[str, float]]:
    """field -> (column, score) by optimal assignment over header x type scores."""
    fields = _fields(kind)
    syn = synonyms()["fields"][kind]
    cols = fp.columns
    S = np.zeros((len(fields), len(cols)))
    for i, f in enumerate(fields):
        for j, c in enumerate(cols):
            compat = FIELD_KINDS[f].get(c.kind, 0.0)
            if compat == 0:
                continue
            hs = header_score(c.name, syn.get(f, [f]))
            if f in OPTIONAL[kind] and hs < 0.55:
                continue  # an optional field is only mapped on real header evidence, never on type alone
            S[i, j] = 0.6 * hs + 0.4 * compat
            if f == "amount" and kind == "bank" and c.negative_rate > 0:
                S[i, j] += 0.1  # a signed amount column has negatives
    rows, cs = linear_sum_assignment(-S)
    return {
        fields[i]: (cols[j].name, float(S[i, j]))
        for i, j in zip(rows, cs, strict=True)
        if S[i, j] >= MIN_SCORE
    }


def _role_scores(fp: FileProfile, group: dict) -> dict[str, float]:
    out = {}
    name_toks = set(norm_text(group["pattern"]).lower().split())
    for role in REQUIRED:
        asg = assign_columns(fp, role)
        req = [f for f in REQUIRED[role] if f not in ("debit", "credit")]
        score = sum(asg.get(f, ("", 0))[1] for f in req) / len(req)
        if any(w in name_toks for w in synonyms()["roles"][role]):
            score += 0.25
        if role == "bank":
            score += 0.4 if group["periodic"] else -0.4
        out[role] = score
    return out


def _date_format(fp: FileProfile, cols: list[str]) -> tuple[str, bool]:
    """The format every date column of the file parses with (month-first only if some value proves it)."""
    sets = [set(c.date_formats) for c in fp.columns if c.name in cols and c.kind == "date"]
    common = set.intersection(*sets) if sets else set()
    ordered = [f for f in DATE_FORMATS if f in common]
    if not ordered:
        return "%d/%m/%Y", True
    return ordered[0], len(ordered) > 1


def _value_map(field: str, categories: list[str]) -> tuple[dict, list[str]]:
    vocab = synonyms()["values"][field]
    out, unresolved = {}, []
    for cat in categories:
        best, score = None, 0.0
        for canon, words in vocab.items():
            s = header_score(cat, words)
            single = {norm_text(w) for w in words if " " not in w.strip()}
            if set(norm_text(cat).split()) & single:  # 'ICB-FNC' contains the word 'ICB'
                s = max(s, 0.95)
            if s > score:
                best, score = canon, s
        if score >= 0.8:
            out[cat] = best
        else:
            unresolved.append(cat)
    return out, unresolved


def _infer_doc_types(
    path: Path, fp: FileProfile, type_col: str, amount_col: str, decimal: str, known: dict
) -> dict:
    """Unknown document types: the one whose amounts are mostly negative is the credit note."""
    _, head, body = read_table(path)
    ti, ai = head.index(type_col), head.index(amount_col)
    neg = defaultdict(list)
    for r in body:
        a = parse_amount(r[ai], decimal)
        if a is not None:
            neg[r[ti].strip()].append(a < 0)
    out = dict(known)
    for cat, flags in neg.items():
        if cat not in out:
            out[cat] = "credit" if flags and sum(flags) / len(flags) > 0.5 else "invoice"
    return out


def _infer_payer_types(path: Path, cols: dict, known: dict) -> dict:
    """Unknown payer types, from behaviour: a payer named like the resident is the resident; a payer
    paying for many residents is a public body; otherwise a family member."""
    _, head, body = read_table(path)
    ti, pi = head.index(cols["payer_type"]), head.index(cols["payer_name"])
    ri = head.index(cols["resident_id"])
    ni = head.index(cols["resident_name"]) if cols.get("resident_name") else None
    residents_per_payer = defaultdict(set)
    for r in body:
        residents_per_payer[norm_text(r[pi])].add(r[ri])
    votes: dict[str, Counter] = defaultdict(Counter)
    for r in body:
        cat = r[ti].strip()
        if cat in known:
            continue
        if len(residents_per_payer[norm_text(r[pi])]) >= 3:
            votes[cat]["public_aid"] += 1
        elif ni is not None and fuzz.token_set_ratio(norm_text(r[pi]), norm_text(r[ni])) >= 90:
            votes[cat]["resident"] += 1
        else:
            votes[cat]["family"] += 1
    out = dict(known)
    for cat, c in votes.items():
        out[cat] = c.most_common(1)[0][0]
    return out


def propose(data_dir: Path, client: str) -> tuple[dict, list[dict]]:
    """Returns (template, review rows). Review rows carry a confidence and a flag for the human."""
    data_dir = Path(data_dir)
    groups = discover(data_dir)
    profiles = [profile_file(g["files"][0], g["files"][1:]) for g in groups]
    roles = list(REQUIRED)
    M = np.array(
        [[_role_scores(fp, g)[r] for r in roles] for fp, g in zip(profiles, groups, strict=True)]
    )
    gi, ri = linear_sum_assignment(-M)
    review: list[dict] = []
    files: dict = {}
    lang_votes = Counter()
    for g_idx, r_idx in zip(gi, ri, strict=True):
        kind, fp, group = roles[r_idx], profiles[g_idx], groups[g_idx]
        asg = assign_columns(fp, kind)
        review.append(
            {
                "file": group["pattern"],
                "field": "(role)",
                "column": kind,
                "score": round(float(M[g_idx, r_idx]), 2),
            }
        )
        cols = {f: c for f, (c, _) in asg.items()}
        if kind == "invoices" and "resident_name" in cols and "resident_last" in cols:
            if asg["resident_last"][1] > asg["resident_name"][1]:
                cols.pop("resident_name")
        if kind == "invoices" and "resident_name" not in cols and "resident_last" in cols:
            cols["resident_name"] = [cols["resident_last"]] + (
                [cols["resident_first"]] if "resident_first" in cols else []
            )
        cols.pop("resident_last", None)
        cols.pop("resident_first", None)
        spec: dict = {"path": group["pattern"], "encoding": fp.encoding, "sep": fp.sep}
        if fp.skip_rows:
            spec["skip_rows"] = fp.skip_rows
        if fp.skip_footer:
            spec["skip_footer"] = fp.skip_footer
        date_cols = [
            c for f, c in cols.items() if isinstance(c, str) and FIELD_KINDS.get(f, {}).get("date")
        ]
        dfmt, ambiguous = _date_format(fp, date_cols)
        spec["date_format"] = dfmt
        num_cols = [c for f, c in cols.items() if f in ("amount", "debit", "credit", "share_pct")]
        decs = [c.decimal for c in fp.columns if c.name in num_cols and c.decimal]
        spec["decimal"] = Counter(decs).most_common(1)[0][0] if decs else "."
        if "period" in cols:
            pc = next(c for c in fp.columns if c.name == cols["period"])
            spec["period_format"] = pc.date_formats[0] if pc.kind == "period" else "date"
        if kind == "bank":
            if "debit" in cols and "credit" in cols:
                spec["amount_mode"] = "debit_credit"
                cols.pop("amount", None)
            else:
                spec["amount_mode"] = "signed"
                cols.pop("debit", None)
                cols.pop("credit", None)
        spec["columns"] = cols
        for field in ("doc_type", "payer_type"):
            if field in cols:
                col = next(c for c in fp.columns if c.name == cols[field])
                vm, unresolved = _value_map(field, col.categories)
                if unresolved and field == "doc_type" and "amount" in cols:
                    vm = _infer_doc_types(
                        group["files"][0], fp, cols["doc_type"], cols["amount"], spec["decimal"], vm
                    )
                if unresolved and field == "payer_type":
                    vm = _infer_payer_types(group["files"][0], cols, vm)
                spec.setdefault("values", {})[field] = vm
                for u in unresolved:
                    review.append(
                        {
                            "file": group["pattern"],
                            "field": f"{field} value",
                            "column": u,
                            "score": 0.5,
                            "proposed": vm.get(u),
                            "flag": "inferred from data, please check",
                        }
                    )
        for f, (c, s) in asg.items():
            if (
                f in ("resident_last", "resident_first")
                and "resident_name" in cols
                and isinstance(cols["resident_name"], str)
            ):
                continue
            review.append(
                {
                    "file": group["pattern"],
                    "field": f,
                    "column": c,
                    "score": round(s, 2),
                    **({"flag": "low confidence"} if s < FLAG_BELOW else {}),
                }
            )
        missing = [
            f for f in REQUIRED[kind] if f not in cols and f not in ("debit", "credit", "amount")
        ]
        if kind == "invoices" and "resident_name" not in cols:
            missing.append("resident_name")
        for f in missing:
            review.append(
                {
                    "file": group["pattern"],
                    "field": f,
                    "column": None,
                    "score": 0.0,
                    "flag": "NOT FOUND",
                }
            )
        if ambiguous:
            review.append(
                {
                    "file": group["pattern"],
                    "field": "date_format",
                    "column": dfmt,
                    "score": 0.5,
                    "flag": "day/month order not provable from values",
                }
            )
        files[kind] = spec
        lang_votes.update(
            "fr" if any(ch in "éèàçù" for c in fp.columns for ch in c.name.lower()) else "en"
            for _ in [0]
        )
    patterns = {}
    pmap = {
        "invoice": ("invoices", "invoice_id"),
        "mandate": ("payers", "mandate_ref"),
        "remittance": ("remittances", "remittance_id"),
    }
    for name, (kind, field) in pmap.items():
        col = files.get(kind, {}).get("columns", {}).get(field)
        prof = next(
            (
                fp
                for fp, g in zip(profiles, groups, strict=True)
                if g["pattern"] == files.get(kind, {}).get("path")
            ),
            None,
        )
        cp = (
            next((c for c in prof.columns if c.name == col), None)
            if prof and isinstance(col, str)
            else None
        )
        if cp is not None and cp.pattern:
            patterns[name] = cp.pattern
    template = {
        "client": client,
        "language": lang_votes.most_common(1)[0][0] if lang_votes else "en",
        "proposed_by": "fallback",
        "files": files,
        "patterns": patterns,
    }
    return template, review
