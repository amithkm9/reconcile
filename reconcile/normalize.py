"""Text, name, reference and money normalisation.

Bank labels are upper-case ASCII, truncated and full of noise words; payer tables are written by
humans with accents and titles. Everything that compares the two goes through these functions so
the comparison is like-for-like.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from pathlib import Path

import yaml

_VOCAB_PATH = Path(__file__).with_name("vocab.yaml")


@lru_cache(maxsize=1)
def vocab() -> dict:
    return yaml.safe_load(_VOCAB_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _month_lookup() -> dict[str, int]:
    return {w: int(m) for m, words in vocab()["months"].items() for w in words}


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def norm_text(s: str | None) -> str:
    """Upper-case ASCII, punctuation to spaces, single spaces."""
    if not s:
        return ""
    s = strip_accents(str(s)).upper()
    s = re.sub(r"[^A-Z0-9]+", " ", s)
    return " ".join(s.split())


def tokens(s: str | None) -> list[str]:
    return norm_text(s).split()


def norm_name(s: str | None) -> str:
    """A person or organisation name without titles or bracketed notes: 'Mme Hélène Dupont (fille)' -> 'HELENE DUPONT'."""
    if not s:
        return ""
    s = re.sub(r"\(.*?\)", " ", str(s))
    titles = set(vocab()["titles"])
    return " ".join(t for t in tokens(s) if t not in titles)


def name_part(label: str, ref: str = "") -> str:
    """The words of a bank line that could be a name: drop titles, noise words, months, numbers."""
    drop = set(vocab()["titles"]) | set(vocab()["stopwords"]) | set(_month_lookup())
    words = [
        t for t in tokens(f"{label} {ref}") if t not in drop and not any(c.isdigit() for c in t)
    ]
    return " ".join(w for w in words if len(w) > 1)


def month_tokens(text: str) -> set[int]:
    """Month numbers mentioned as words ('MARS', 'FEB') or as MM/YYYY."""
    found = {_month_lookup()[t] for t in tokens(text) if t in _month_lookup()}
    for mm in re.findall(r"\b(0?[1-9]|1[0-2])[/-](?:20)?\d{2}\b", text or ""):
        found.add(int(mm))
    return found


def period_tokens(text: str) -> set[str]:
    """Explicit periods 'MM/YYYY' or 'YYYY-MM' -> {'YYYY-MM'}."""
    out = set()
    for mm, yyyy in re.findall(r"\b(0?[1-9]|1[0-2])[/-](20\d{2})\b", text or ""):
        out.add(f"{yyyy}-{int(mm):02d}")
    for yyyy, mm in re.findall(r"\b(20\d{2})-(0[1-9]|1[0-2])\b", text or ""):
        out.add(f"{yyyy}-{mm}")
    return out


def has_keyword(text: str, keywords: list[str]) -> bool:
    toks = set(tokens(text))
    return any(k in toks for k in keywords)


def payer_key(name: str) -> str:
    """Stable key for a payer across files: the normalised name."""
    return norm_name(name)


def find_pattern(text: str, pattern: str | None) -> list[str]:
    """All matches of a client reference pattern (from the template), normalised like the IDs are."""
    if not pattern or not text:
        return []
    return [norm_id(m) for m in re.findall(pattern, str(text).upper())]


def norm_id(s: str | None) -> str:
    """IDs lose leading zeros in Excel round-trips ('000123' -> '123'); compare on the stripped form."""
    if s is None:
        return ""
    s = str(s).strip().upper()
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return (s.lstrip("0") or "0") if s.isdigit() else s
