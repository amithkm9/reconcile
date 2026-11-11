"""Bank-line identity.

Bank exports overlap (a weekly export covers the last ~16 days), so the same movement arrives several
times. A content hash makes re-import idempotent; the occurrence index keeps two genuinely identical
movements inside one export distinct.
"""

from __future__ import annotations

import hashlib


def norm_label(s: str | None) -> str:
    return " ".join(str(s or "").upper().split())


def bank_line_hash(
    date_iso: str, cents: int, label: str, ref: str = "", occurrence: int = 0
) -> str:
    raw = f"{date_iso}|{cents}|{norm_label(label)}|{norm_label(ref)}|{occurrence}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
