"""Synthetic care-home groups with exact ground truth.

`generate_client` simulates a year of invoices, payer shares and payments (`world.py`), renders them
in the client's own export formats (`render.py`, `perturb.py`) and writes the hidden answer key to
`data/<client>/_truth/`, which the pipeline never reads.
"""

from __future__ import annotations

import random
from pathlib import Path

import yaml

from reconcile.generate.perturb import perturb_layout, random_variant_spec
from reconcile.generate.render import write_client
from reconcile.generate.world import build_world
from reconcile.paths import ROOT

CONFIG = ROOT / "config"


def load_cfg(client: str) -> dict:
    return yaml.safe_load((CONFIG / f"{client}.yaml").read_text(encoding="utf-8"))


def generate_client(
    client: str, out: Path, noise: float = 1.0, overrides: dict | None = None
) -> dict:
    cfg = load_cfg(client)
    if "base" in cfg:  # a perturbed copy of another client's business
        spec = cfg["perturb"]
        base = load_cfg(cfg["base"])
        base["seed"] = cfg["seed"]
        base["client"] = client
        base.update(overrides or {})
        world = build_world(base, noise)
        layout = perturb_layout(base["layout"], spec, random.Random(cfg["seed"]))
        return write_client(
            world, layout, out / client, junk=spec.get("junk_column"), seed=cfg["seed"]
        )
    cfg.update(overrides or {})
    world = build_world(cfg, noise)
    return write_client(world, cfg["layout"], out / client, seed=cfg["seed"])


def generate_variants(out: Path, n: int = 12) -> list[str]:
    """Small worlds written in randomly perturbed schemas, for the mapping eval."""
    pool = yaml.safe_load((CONFIG / "variant_pool.yaml").read_text(encoding="utf-8"))
    names = []
    for i in range(n):
        base_name = "azur" if i % 2 == 0 else "maple"
        cfg: dict = {**load_cfg(base_name), "seed": 1000 + i, "n_residents": 60, "months": 2}
        rng = random.Random(1000 + i)
        layout = perturb_layout(cfg["layout"], random_variant_spec(pool, rng, cfg["layout"]), rng)
        name = f"variant_{i:02d}_{base_name}"
        write_client(build_world(cfg), layout, out / "variants" / name, junk="extract_id", seed=i)
        names.append(name)
    return names
