"""Profile an unfamiliar export: dialect, header position, and what each column looks like.

Everything here is inferred from the bytes and the values, never from the client's name. The
profile is also what an LLM sees (with person names masked), so it must be informative without
carrying personal data.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from reconcile.mapping.template import parse_amount
from reconcile.normalize import month_lookup, norm_text, vocab

DATE_FORMATS = ["%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%d", "%d.%m.%Y", "%d-%m-%Y", "%Y/%m/%d"]
PERIOD_FORMATS = ["%m/%Y", "%Y-%m", "%m-%Y"]
SEPS = [";", ",", "\t", "|"]
ENCODINGS = ["utf-8-sig", "cp1252"]


@dataclass
class ColumnProfile:
    name: str
    n: int
    blank_rate: float
    distinct: int
    unique_rate: float
    kind: str  # date | period | number | percent | id | name | category | text | constant | blank
    date_formats: list[str] = field(default_factory=list)
    decimal: str | None = None
    negative_rate: float = 0.0
    pattern: str | None = None
    samples: list[str] = field(default_factory=list)  # masked
    categories: list[str] = field(default_factory=list)


@dataclass
class FileProfile:
    path: str
    encoding: str
    sep: str
    skip_rows: int
    skip_footer: int
    n_rows: int
    columns: list[ColumnProfile]


def _decode(raw: bytes) -> tuple[str, str]:
    for enc in ENCODINGS:
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1"), "latin-1"


def _dialect(lines: list[str]) -> tuple[str, int, int, int]:
    """Separator, header row index, footer rows, field count: the separator whose per-line field
    count is the most consistent; preamble/footer rows are the ones that do not fit."""
    best = None
    for sep in SEPS:
        counts = [len(next(csv.reader([ln], delimiter=sep))) for ln in lines[:200] if ln.strip()]
        if not counts:
            continue
        mode, freq = Counter(counts).most_common(1)[0]
        if mode < 2:
            continue
        score = freq * mode
        if best is None or score > best[0]:
            best = (score, sep, mode)
    if best is None:
        return ",", 0, 0, 1
    _, sep, width = best
    widths = [len(next(csv.reader([ln], delimiter=sep))) if ln.strip() else 0 for ln in lines]
    header = next(i for i, w in enumerate(widths) if w == width)
    footer = 0
    for w in reversed(widths):
        if w == width:
            break
        footer += 1
    return sep, header, footer, width


def _shape(v: str) -> str:
    return re.sub(r"\d+", lambda m: f"\\d{{{len(m.group())}}}", re.escape(v.upper()))


def id_pattern(values: list[str]) -> str | None:
    """Generalise ID values to a regex: 'FA2025-000123' -> 'FA\\d{4}\\-\\d{6}'. Shapes covering
    >= 95% of values are joined as alternatives."""
    vals = [v.strip() for v in values if v and v.strip()]
    if not vals:
        return None
    shapes = Counter(_shape(v) for v in vals)
    keep, covered = [], 0
    for shape, n in shapes.most_common(5):
        keep.append(shape)
        covered += n
        if covered >= 0.95 * len(vals):
            break
    if not any("\\d" in s for s in keep):
        return None
    return "(?:" + "|".join(keep) + ")" if len(keep) > 1 else keep[0]


def _parses(values, fmt) -> float:
    ok = 0
    for v in values:
        try:
            dt.datetime.strptime(v, fmt)
            ok += 1
        except ValueError:
            pass
    return ok / len(values)


def _number_style(values: list[str]) -> tuple[str, float]:
    """Decimal separator from the values: '1 234,56' / '1.234,56' -> ','; '1,234.56' / '12.30' -> '.'."""
    comma = dot = 0
    for v in values:
        m = re.search(r"([.,])(\d{1,2})$", v.strip().rstrip("-"))
        if m:
            comma += m.group(1) == ","
            dot += m.group(1) == "."
    dec = "," if comma > dot else "."
    ok = sum(parse_amount(v, dec) is not None for v in values)
    return dec, ok / len(values)


@lru_cache(maxsize=1)
def _keep_words() -> frozenset[str]:
    """Words safe to show an LLM: generic vocabulary, months, accounting keywords, and every word
    of the mapper's header/value synonyms. Anything else alphabetic may be a person's name."""
    voc = vocab()
    syn = yaml.safe_load(Path(__file__).with_name("synonyms.yaml").read_text(encoding="utf-8"))
    words: set[str] = set()
    for group in (syn["roles"], *syn["fields"].values(), *syn["values"].values()):
        for phrases in group.values():
            for phrase in phrases:
                words |= set(norm_text(phrase).split())
    return frozenset(
        words
        | set(voc["stopwords"])
        | set(voc["titles"])
        | set(month_lookup())
        | set(voc["non_ar_keywords"])
        | set(voc["return_keywords"])
    )


def _mask_token(tok: str) -> str:
    if set(norm_text(tok).split()) <= _keep_words():
        return tok
    # Letter runs of 4+ that are not vocabulary are masked, inside codes too ("DUPONT01" ->
    # "<A>01"). Shorter runs ("FA", "INV", "TPT") are kept: they are the identifier and category
    # shapes the model needs. Residual risk: a person name of 3 letters or fewer stays visible.
    return re.sub(
        r"[A-Za-zÀ-ÿ]{4,}",
        lambda m: m.group() if norm_text(m.group()) in _keep_words() else "<A>",
        tok,
    )


def mask_value(v: str) -> str:
    """Hide anything that could be a person's name: keep vocabulary words, months, keywords and
    identifier shapes; replace other words (and long letter runs inside codes) with <A>."""
    return "".join(tok if not tok.strip() else _mask_token(tok) for tok in re.split(r"(\s+)", v))


def _form(v: str) -> str:
    """The form of a value without the value: '20-45-67' -> '99-99-99'."""
    return re.sub(r"[A-Za-zÀ-ÿ]", "A", re.sub(r"\d", "9", v))


def profile_column(name: str, values: list[str]) -> ColumnProfile:
    n = len(values)
    nonblank = [v.strip() for v in values if v and v.strip()]
    blank_rate = 1 - len(nonblank) / n if n else 1.0
    distinct = len(set(nonblank))
    cp = ColumnProfile(
        name, n, round(blank_rate, 3), distinct, round(distinct / max(1, len(nonblank)), 3), "blank"
    )
    if not nonblank:
        return cp
    if distinct == 1 and len(nonblank) >= 20:
        # account numbers, sort codes: never money, and only their shape is shown
        cp.kind, cp.samples = "constant", [_form(nonblank[0])]
        return cp
    sample = nonblank[:: max(1, len(nonblank) // 600)][
        :600
    ]  # spread over the file, not just its start
    # Typing needs >= 98% to parse; disambiguation keeps only the format(s) parsing the MOST
    # values: one "13/01/2025" is enough to rule out month-first, however rare it is.
    rates = {f: _parses(nonblank, f) for f in DATE_FORMATS}
    top = max(rates.values())
    dfmts = [f for f in DATE_FORMATS if rates[f] >= 0.98 and rates[f] >= top - 1e-9]
    pfmts = [f for f in PERIOD_FORMATS if _parses(sample, f) >= 0.98]
    dec, num_ok = _number_style(sample)
    alpha = sum(bool(re.search(r"[A-Za-zÀ-ÿ]", v)) for v in sample) / len(sample)
    digit = sum(bool(re.search(r"\d", v)) for v in sample) / len(sample)
    words = sum(len(v.split()) for v in sample) / len(sample)
    if dfmts:
        cp.kind, cp.date_formats = "date", dfmts
    elif pfmts:
        cp.kind, cp.date_formats = "period", pfmts
    elif num_ok >= 0.98 and alpha == 0:
        vals = [x for x in (parse_amount(v, dec) for v in sample) if x is not None]
        cp.decimal = dec
        cp.negative_rate = round(sum(x < 0 for x in vals) / len(vals), 3)
        small = all(0 <= x <= 10000 for x in vals)  # cents: 0..100.00
        cp.kind = "percent" if small and distinct <= 101 else "number"
        if (
            cp.kind == "number"
            and all(re.fullmatch(r"\d+", v) for v in sample)
            and cp.unique_rate > 0.2
            and max(len(v) for v in sample) >= 4
            and all(v.startswith("0") or len(v) <= 6 for v in sample[:20])
            and not any("," in v or "." in v for v in sample)
        ):
            cp.kind = "id"  # zero-padded integer codes are identifiers, not money
    elif digit >= 0.9 and words <= 1.5 and alpha > 0:
        cp.kind = "id"
    elif distinct <= max(12, 0.02 * len(nonblank)) and words <= 6:
        cp.kind = "category"
    elif alpha >= 0.9 and digit < 0.2 and words <= 4.5:
        cp.kind = "name"
    else:
        cp.kind = "text"
    if cp.kind == "id":
        cp.pattern = id_pattern(nonblank)
    if cp.kind == "category":
        cp.categories = [c for c, _ in Counter(nonblank).most_common(12)]
    # `categories` stay raw: the local mapper needs them to map values. `samples` are what an
    # LLM sees, so every value that can carry words is masked, category values included (a
    # surname column with few distinct values looks like a category).
    show = cp.categories if cp.kind == "category" else nonblank[:5]
    cp.samples = [
        v if cp.kind in ("date", "period", "number", "percent") else mask_value(v) for v in show
    ]
    return cp


def read_table(path: Path) -> tuple[FileProfile, list[str], list[list[str]]]:
    text, enc = _decode(Path(path).read_bytes())
    text = text.lstrip("\ufeff")
    lines = text.splitlines()
    sep, header, footer, width = _dialect(lines)
    body_lines = lines[header : len(lines) - footer if footer else None]
    rows = list(csv.reader(io.StringIO("\n".join(body_lines)), delimiter=sep))
    head = [h.strip() for h in rows[0]]
    body = [(r + [""] * width)[:width] for r in rows[1:] if any(c.strip() for c in r)]
    # A totals/balance footer can have the same width as a data row ("Solde au ...;;;;;1 234,56"):
    # trailing rows whose date column does not parse are footer, not data.
    date_col = _date_column(body, width)
    extra = 0
    if date_col is not None:
        while body and not _is_date(body[-1][date_col]):
            body.pop()
            extra += 1
    fp = FileProfile(str(path), enc, sep, header, footer + extra, len(body), [])
    return fp, head, body


def _is_date(v: str) -> bool:
    for f in DATE_FORMATS:
        try:
            dt.datetime.strptime(v.strip(), f)
            return True
        except ValueError:
            pass
    return False


def _date_column(body: list[list[str]], width: int) -> int | None:
    """Index of the first column where >= 90% of rows are dates."""
    if not body:
        return None
    for j in range(width):
        vals = [r[j] for r in body[:200]]
        if sum(_is_date(v) for v in vals) >= 0.9 * len(vals):
            return j
    return None


def profile_file(path: Path, extra_paths: Sequence[Path] = ()) -> FileProfile:
    fp, head, body = read_table(path)
    for p in list(extra_paths)[:3]:  # periodic exports: profile a few files together
        _, h2, b2 = read_table(p)
        if h2 == head:
            body += b2
    cols = list(zip(*body, strict=False)) if body else [[] for _ in head]
    fp.columns = [profile_column(h, list(c)) for h, c in zip(head, cols, strict=False)]
    fp.n_rows = len(body)
    return fp


def discover(data_dir: Path) -> list[dict]:
    """Find the exports: single files, and groups of periodic files (same name once digits are
    removed, e.g. bank/releve_2025-01-05.csv ...) which become one glob."""
    files = sorted(
        p
        for p in Path(data_dir).rglob("*")
        if p.is_file()
        and p.suffix.lower() in (".csv", ".txt")
        and not any(part.startswith("_") for part in p.relative_to(data_dir).parts)
    )
    groups: dict[str, list[Path]] = {}
    for f in files:
        rel = f.relative_to(data_dir)
        key = re.sub(r"\d+", "#", str(rel))
        groups.setdefault(key, []).append(f)
    out = []
    for fs in groups.values():
        rel = fs[0].relative_to(data_dir)
        if len(fs) > 1:
            parts = re.split(r"\d", str(rel), maxsplit=1)
            pattern = parts[0] + "*" + rel.suffix
        else:
            pattern = str(rel)
        out.append({"pattern": pattern, "files": fs, "periodic": len(fs) > 1})
    return out
