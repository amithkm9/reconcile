"""Thresholds and outcomes.

The auto threshold is chosen so that the *lower* 95% confidence bound on precision is >= target,
not the point estimate: 297/300 correct is "0.99" but only certifies ~0.974. If nothing can be
certified, nothing is auto-posted; the honest result is "send everything to review".
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.stats import beta


def cp_lower(k: int, n: int, alpha: float = 0.05) -> float:
    """One-sided Clopper-Pearson lower bound on a binomial proportion (k successes of n)."""
    if n == 0 or k == 0:
        return 0.0
    return float(beta.ppf(alpha, k, n - k + 1))


@dataclass
class Thresholds:
    auto: float
    review: float
    certified_lb: float
    n_at_auto: int
    k_at_auto: int
    target: float = 0.99

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def choose_thresholds(
    scores: np.ndarray, correct: np.ndarray, target: float = 0.99, review_recall: float = 0.98
) -> Thresholds:
    """`scores` / `correct` are for the candidate *chosen* per line on the threshold-selection set.

    auto  = lowest score t whose CP lower bound on precision over {score >= t} is >= target.
    review = score below which at most (1 - review_recall) of the correct choices fall.
    Scanning many thresholds and keeping the best is itself a small selection bias; the untouched
    test months are the unbiased check.
    """
    order = np.argsort(-scores)
    s, c = scores[order], correct[order].astype(int)
    k_cum = np.cumsum(c)
    best = None
    for i in range(len(s)):
        if i + 1 < len(s) and s[i + 1] == s[i]:
            continue  # only cut between distinct scores
        lb = cp_lower(int(k_cum[i]), i + 1)
        if lb >= target:
            best = (float(s[i]), lb, i + 1, int(k_cum[i]))
    correct_scores = np.sort(scores[correct.astype(bool)])
    review = (
        float(correct_scores[int((1 - review_recall) * len(correct_scores))])
        if len(correct_scores)
        else 0.5
    )
    if best is None:
        n = len(s)
        return Thresholds(float("inf"), min(review, 0.5), cp_lower(int(c.sum()), n), 0, 0, target)
    return Thresholds(best[0], min(review, best[0]), best[1], best[2], best[3], target)


def outcome(chosen_score: float | None, n_valid: int, th: Thresholds) -> str:
    """Payment outcome for a line whose best feasible candidate has `chosen_score`."""
    if chosen_score is None or chosen_score < th.review:
        return "unapplied"
    if chosen_score >= th.auto and n_valid <= 1:
        return "auto"
    return "review"
