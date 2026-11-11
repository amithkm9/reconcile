"""`python -m reconcile.generate` writes synthetic exports + ground truth for every client."""

from __future__ import annotations

import argparse
from pathlib import Path

from reconcile.generate import generate_client, generate_variants
from reconcile.paths import ROOT


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", default="all")
    ap.add_argument("--out", default=str(ROOT / "data"))
    ap.add_argument("--noise", type=float, default=1.0)
    ap.add_argument("--variants", action="store_true")
    a = ap.parse_args()
    out = Path(a.out)
    clients = ["azur", "maple", "client3"] if a.client == "all" else [a.client]
    for c in clients:
        print(c, generate_client(c, out, a.noise))
    if a.variants:
        print("variants", generate_variants(out))


if __name__ == "__main__":
    main()
