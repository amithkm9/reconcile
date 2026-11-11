"""Scorers: a transparent rules baseline and a gradient-boosted model, same interface.

The rules scorer is a hand-weighted logistic over named evidence. Its hits are the plain-language
reasons shown to reviewers for *both* scorers, because "invoice number in the reference" is
something a finance clerk can check; a SHAP value is not.
"""

from __future__ import annotations

import hashlib
import math
import pickle
from pathlib import Path
from typing import Protocol

import numpy as np
from threadpoolctl import threadpool_limits

from reconcile.features import FEATURES

# (feature test, logit weight, reason shown to a reviewer)
RULES = [
    (
        lambda f: f["mt_dd_return"] and (f["mandate_hit"] or f["inv_ref_hit"]),
        7.0,
        "reverses a collected direct debit",
    ),
    (lambda f: f["mandate_hit"], 4.0, "direct-debit mandate matches"),
    (lambda f: f["inv_ref_hit"], 3.5, "invoice number in the reference"),
    (
        lambda f: f["remit_backed"] and f["remit_resolved"] >= 1.0,
        5.0,
        "every statement line matched to a resident",
    ),
    (
        lambda f: f["remit_backed"] and f["remit_resolved"] < 1.0,
        -3.0,
        "some statement lines could not be matched",
    ),
    (
        lambda f: f["mt_bulk"] and f["n_valid"] == 1 and f["n_excluded"] + f["n_added"] == 0,
        3.5,
        "transfer equals the payer's open shares for the period exactly",
    ),
    (
        lambda f: f["mt_bulk"] and f["n_valid"] == 1 and f["n_excluded"] + f["n_added"] > 0,
        1.0,
        "only one combination of open shares fits the amount",
    ),
    (
        lambda f: f["n_valid"] > 1,
        -1.0,
        "several combinations of invoices fit the amount (never auto)",
    ),
    (lambda f: f["name_score"] >= 95, 3.0, "payer name matches the payer table"),
    (lambda f: 85 <= f["name_score"] < 95, 1.5, "payer name is close (typo?)"),
    (lambda f: f["alias_hit"], 2.0, "this bank name paid this resident before"),
    (lambda f: f["resident_hit"], 1.0, "resident's surname in the label"),
    (
        lambda f: f["residual_zero"] and f["covers_full"],
        1.5,
        "amount settles the invoice share(s) exactly",
    ),
    (lambda f: not f["covers_full"], -2.0, "partial payment"),
    (lambda f: f["residual_rel"] > 0, -3.0, "leaves unallocated cash (overpayment?)"),
    (lambda f: f["fifo_consistent"] and not f["mt_bulk"], 0.5, "oldest open invoice first"),
    (lambda f: f["skips_older_open"] > 0, -1.0, "skips an older open invoice"),
    (lambda f: f["month_hit"], 0.5, "month in the label matches the invoice period"),
    (lambda f: f["n_pids_line"] > 1, -1.0, "several payers could match this line"),
    (lambda f: f["n_close_pids"] > 1, -2.5, "another payer has an equally close name"),
    (lambda f: f["hist_amount_match"], 0.7, "same amount as this payer's recent payments"),
]
BIAS = -1.5


class Scorer(Protocol):
    """What the pipeline needs from a scorer: a name, one score per candidate's features, and the
    plain-language reasons shown to a reviewer."""

    name: str

    def score(self, feats: list[dict]) -> np.ndarray: ...

    def reasons(self, f: dict) -> list[str]: ...


class RulesScorer:
    name = "rules"

    def score(self, feats: list[dict]) -> np.ndarray:
        return np.array([self._one(f)[0] for f in feats])

    def reasons(self, f: dict) -> list[str]:
        return self._one(f)[1]

    def _one(self, f: dict) -> tuple[float, list[str]]:
        logit, why = BIAS, []
        for test, w, reason in RULES:
            if test(f):
                logit += w
                why.append(("+ " if w > 0 else "- ") + reason)
        return 1 / (1 + math.exp(-logit)), why


class GBMScorer:
    """HistGradientBoosting on the same features; calibrated (sigmoid) for display only.

    Calibration is monotone, so it never changes which candidates clear the auto threshold; it
    only makes the probability shown to a reviewer mean something.
    """

    name = "model"

    def __init__(self, model=None, calibrator=None):
        self.model, self.calibrator = model, calibrator
        self._rules = RulesScorer()

    @staticmethod
    def matrix(feats: list[dict]) -> np.ndarray:
        return np.array([[float(f[k]) for k in FEATURES] for f in feats], dtype=float)

    def fit(self, feats: list[dict], y: list[int], seed: int = 0) -> GBMScorer:
        from sklearn.ensemble import HistGradientBoostingClassifier

        self.model = HistGradientBoostingClassifier(
            max_iter=300,
            learning_rate=0.08,
            max_leaf_nodes=31,
            l2_regularization=1.0,
            random_state=seed,
        )
        with threadpool_limits(2):  # small batches: more threads only add OpenMP overhead
            self.model.fit(self.matrix(feats), np.array(y))
        return self

    def calibrate(self, feats: list[dict], y: list[int]) -> None:
        from sklearn.calibration import CalibratedClassifierCV
        from sklearn.frozen import FrozenEstimator

        if len(set(y)) < 2:
            return
        self.calibrator = CalibratedClassifierCV(FrozenEstimator(self.model), method="sigmoid")
        self.calibrator.fit(self.matrix(feats), np.array(y))

    def score(self, feats: list[dict]) -> np.ndarray:
        if not feats:
            return np.array([])
        assert self.model is not None, "fit or load the model first"
        with threadpool_limits(2):
            return self.model.predict_proba(self.matrix(feats))[:, 1]

    def display_probability(self, feats: list[dict]) -> np.ndarray:
        est = self.calibrator or self.model
        assert est is not None, "fit or load the model first"
        return est.predict_proba(self.matrix(feats))[:, 1]

    def reasons(self, f: dict) -> list[str]:
        return self._rules.reasons(f)

    def save(self, path: Path) -> str:
        """Pickle the model; returns its sha256, which the caller stores next to the state."""
        path.parent.mkdir(parents=True, exist_ok=True)
        blob = pickle.dumps({"model": self.model, "calibrator": self.calibrator})
        path.write_bytes(blob)
        return hashlib.sha256(blob).hexdigest()

    @classmethod
    def load(cls, path: Path, sha256: str) -> GBMScorer:
        """Unpickling runs code, so only a file whose hash matches the saved state is loaded."""
        blob = path.read_bytes()
        if hashlib.sha256(blob).hexdigest() != sha256:
            raise ValueError(f"{path}: hash does not match the saved state; refusing to unpickle")
        d = pickle.loads(blob)  # integrity checked above
        return cls(d["model"], d["calibrator"])
