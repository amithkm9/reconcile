"""Human review decisions: one write path for the Streamlit app and the simulated reviewer.

Accept / Pick other post the allocation; Reject sends the line to manual investigation. Every
decision also becomes training labels (the accepted candidate positive, the rejected ones
negative), which is how review work feeds the next model.
"""

from __future__ import annotations

import random

from reconcile.audit import AuditLog
from reconcile.ledger import Ledger
from reconcile.normalize import label_signature


def apply_decision(
    L: Ledger,
    bank_hash: str,
    action: str,
    run_id: str,
    alt_index: int = 0,
    actor: str = "human",
    audit: AuditLog | None = None,
) -> bool:
    """Apply one review action. Returns False, posting nothing, if the picked invoices no longer fit."""
    dec = L.decisions[bank_hash]
    options = [dec["top"]] + dec.get("alts", [])
    if action == "accept":
        picked = options[0]
    elif action == "pick":
        picked = options[alt_index]
    elif action == "reject":
        picked = None
    else:
        raise ValueError(action)
    if picked is not None and not L.fits([tuple(e) for e in picked["edges"]]):
        # Another decision already used these invoices: refuse, keep the line in the queue.
        dec.update(resolution="conflict")
        if audit:
            audit.event(
                "review_conflict", bank_hash=bank_hash, action=action, actor=actor, run_id=run_id
            )
        return False
    for i, opt in enumerate(options):
        y = int(picked is not None and opt is picked)
        if picked is None and i > 0:
            continue  # a reject only tells us the top suggestion was wrong
        L.labels.append(
            {
                "bank_hash": bank_hash,
                "features": opt["features"],
                "y": y,
                "source": f"review_{action}",
                "run_id": run_id,
                "date": L.lines[bank_hash]["date"],
            }
        )
    if dec.get("guard") == "new_format":
        # Probation evidence: a line the system would have auto-posted but for its new layout.
        # Only these count; the rest of the queue (partials, multi-month...) would be reviewed in
        # any layout and says nothing about whether this layout is safe to auto-post.
        sig = label_signature(L.lines[bank_hash]["label"])
        L.sig_review[sig][0] += 1
        L.sig_review[sig][1] += int(action == "accept")
    if picked is not None:
        edges = [tuple(e) for e in picked["edges"]]
        L.post(
            bank_hash,
            edges,
            f"review_{action}",
            run_id,
            learn_alias=picked["mtype"].startswith("name") or picked["mtype"] == "reference",
        )
        dec.update(final=True, posted=True, resolution=action, final_edges=edges)
    else:
        dec.update(final=True, resolution="reject")
    if audit:
        audit.event(
            "review_decision",
            bank_hash=bank_hash,
            action=action,
            actor=actor,
            run_id=run_id,
            edges=picked["edges"] if picked else None,
        )
    return True


class SimReviewer:
    """Stands in for the finance team in experiments: it knows the truth, but can only choose among
    the suggestions it is shown (it never invents an allocation), and it errs `error_rate` of the time."""

    def __init__(self, truth_keys: dict, error_rate: float = 0.0, seed: int = 0):
        self.truth, self.error_rate, self.rng = truth_keys, error_rate, random.Random(seed)

    def act(self, dec: dict) -> tuple[str, int]:
        options = [dec["top"]] + dec.get("alts", [])
        truth = self.truth.get(dec["bank_hash"])
        right = next((i for i, o in enumerate(options) if o["key"] == truth), None)
        if (
            self.rng.random() < self.error_rate
        ):  # a tired reviewer: accept the top or reject at random
            return ("accept", 0) if right != 0 else ("reject", 0)
        if right is None:
            return "reject", 0
        return ("accept", 0) if right == 0 else ("pick", right)
