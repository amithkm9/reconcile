"""Where a client's files live. One place, anchored on the repository root, so commands behave the
same from any working directory and a client name can never point outside the project."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
_CLIENT = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


@dataclass(frozen=True)
class ClientPaths:
    template: Path
    data: Path
    state: Path
    model: Path


def client_paths(client: str) -> ClientPaths:
    if not _CLIENT.fullmatch(client):
        raise ValueError(f"invalid client name {client!r}: use lowercase letters, digits, - and _")
    state = ROOT / "state" / f"{client}.sqlite"
    return ClientPaths(
        template=ROOT / "templates" / f"{client}.yaml",
        data=ROOT / "data" / client,
        state=state,
        model=state.with_suffix(".model.pkl"),
    )
