"""Onboard a client from its raw exports: `python -m reconcile.mapping.onboard --client maple [--yes]`.

1. profile every export and propose a template (LLM if a key or cached response exists, else the
   deterministic mapper);
2. show the proposal with a confidence per field and flags for anything uncertain;
3. on confirmation, test-load the exports through it (drift checks included) and save it to
   templates/<client>.yaml. Later runs load the template and skip this step.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import yaml

from reconcile.mapping import propose_fallback, propose_llm
from reconcile.mapping.template import load_client


def onboard(
    client: str,
    data_dir: Path,
    out: Path,
    use_llm: bool = True,
    yes: bool = False,
    model: str | None = None,
) -> dict:
    t0 = time.time()
    tpl, review, rec = propose_llm.propose(data_dir, client, model) if use_llm else (None, [], {})
    if tpl is None:
        tpl, review = propose_fallback.propose(data_dir, client)
    print(f"\nProposed mapping for {client} (by {tpl['proposed_by']}):")
    for r in review:
        flag = f"  <-- {r['flag']}" if r.get("flag") else ""
        proposed = f" -> {r['proposed']}" if r.get("proposed") else ""
        print(
            f"  {r['file']:<28} {r['field']:<20} {str(r['column']):<28}{proposed} conf={r['score']:.2f}{flag}"
        )
    if not yes and input("\nSave this template? [y/N] ").strip().lower() != "y":
        print("Not saved. Edit the YAML by hand or re-run.")
        return {"saved": False}
    data = load_client(tpl, data_dir)  # raises TemplateError on drift / unparseable values
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml.safe_dump(tpl, allow_unicode=True, sort_keys=False), encoding="utf-8")
    secs = round(time.time() - t0, 1)
    flags = sum(1 for r in review if r.get("flag"))
    print(
        f"\nSaved {out}: {len(data.invoices)} invoices, {len(data.payers)} payer rows, "
        f"{len(data.remittances)} statement rows, {len(data.bank)} bank lines. {flags} flag(s). {secs}s."
    )
    return {
        "saved": True,
        "seconds": secs,
        "flags": flags,
        "proposed_by": tpl["proposed_by"],
        "llm": rec,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", required=True)
    ap.add_argument("--data", default=None)
    ap.add_argument("--yes", action="store_true", help="accept the proposal without asking")
    ap.add_argument("--no-llm", action="store_true")
    a = ap.parse_args()
    data = Path(a.data or f"data/{a.client}")
    onboard(a.client, data, Path(f"templates/{a.client}.yaml"), use_llm=not a.no_llm, yes=a.yes)


if __name__ == "__main__":
    main()
