"""Append-only action log. Every automatic post, human decision and reminder decision is one JSON
line, stamped with the template, scorer and threshold versions that produced it, so any action
can be replayed and explained later."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path


def _default(o):
    if isinstance(o, dt.date | dt.datetime):
        return o.isoformat()
    if isinstance(o, set | frozenset):
        return sorted(o)
    return str(o)


class AuditLog:
    def __init__(self, path: Path | None, versions: dict):
        self.path = path
        self.versions = versions
        self.buffer: list[str] = []
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)

    def event(self, kind: str, **payload) -> None:
        rec = {"event": kind, **payload, "versions": self.versions}
        self.buffer.append(json.dumps(rec, default=_default, ensure_ascii=False))

    def flush(self) -> None:
        if self.path and self.buffer:
            with self.path.open("a", encoding="utf-8") as f:
                f.write("\n".join(self.buffer) + "\n")
        self.buffer.clear()
