"""Charts for the reports. Matplotlib is imported here only, with a file backend."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np


def fmt_pct(x: float | None) -> str:
    """0.762 -> '76.2%'; None (nothing to measure) -> 'n/a'."""
    return "n/a" if x is None else f"{100 * x:.1f}%"


def plot_sweep(rows: list[dict], path: Path, title: str) -> None:
    k = [r["noise"] for r in rows]
    fig, ax = plt.subplots(figsize=(6.4, 3.6), dpi=150)
    ax.plot(
        k,
        [100 * (r["auto_precision"] or 0) for r in rows],
        marker="o",
        color="#1f5f8b",
        label="auto precision",
    )
    ax.plot(
        k,
        [100 * (r["automation"] or 0) for r in rows],
        marker="s",
        color="#c46b2b",
        label="automation rate",
    )
    ax.plot(
        k,
        [100 * (r["review_top1"] or 0) for r in rows],
        marker="^",
        color="#5b8c5a",
        label="review queue: top suggestion right",
    )
    ax.axhline(99, color="#888", lw=0.8, ls="--")
    ax.text(k[-1], 99.3, "99% target", ha="right", va="bottom", fontsize=7, color="#666")
    ax.set_xlabel("messiness multiplier k (1 = default rates)")
    ax.set_ylabel("% (test months 10-12)")
    ax.set_ylim(0, 102)
    ax.set_title(f"Noise sweep: {title}", fontsize=10)
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(frameon=False, fontsize=8, loc="lower left")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def plot_feedback(curve: list[dict], path: Path, shift_month: str | None = "2025-10") -> None:

    x = [dt.date.fromisoformat(r["week"] + "-15") for r in curve]
    fig, ax = plt.subplots(figsize=(7.2, 3.8), dpi=150)
    ax.plot(
        x,
        [100 * r["automation"] for r in curve],
        marker="s",
        color="#c46b2b",
        label="automation rate",
    )
    ax.plot(
        x,
        [100 * (r["auto_precision"] or 0) for r in curve],
        marker="o",
        color="#1f5f8b",
        label="auto precision",
    )
    if shift_month:
        sx = float(mdates.date2num(dt.date.fromisoformat(shift_month + "-01")))
        ax.axvline(sx, color="#999", lw=0.8, ls="--")
        ax.text(sx, 8, " bank changes its\n transfer label format", fontsize=7, color="#444")
    ax.set_ylabel("% of AR lines (by month)")
    ax.set_ylim(0, 105)
    ax.set_title("Cold start, a year of weekly runs with a simulated reviewer", fontsize=10)
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(frameon=False, fontsize=8, loc="center left")
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def reliability(scores, y, path: Path, title: str) -> dict:
    """Reliability diagram + Brier score for displayed (calibrated) probabilities."""

    scores, y = np.asarray(scores), np.asarray(y)
    bins = np.linspace(0, 1, 11)
    idx = np.clip(np.digitize(scores, bins) - 1, 0, 9)
    xs, ys, ns = [], [], []
    for b in range(10):
        m = idx == b
        if m.sum() >= 20:
            xs.append(scores[m].mean())
            ys.append(y[m].mean())
            ns.append(int(m.sum()))
    fig, ax = plt.subplots(figsize=(3.8, 3.6), dpi=150)
    ax.plot([0, 1], [0, 1], color="#bbb", lw=0.8, ls="--")
    ax.plot(xs, ys, marker="o", color="#1f5f8b")
    ax.set_xlabel("displayed probability")
    ax.set_ylabel("observed share correct")
    ax.set_title(title, fontsize=9)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return {"brier": round(float(np.mean((scores - y) ** 2)), 4), "bins": len(xs)}


# Categorical slots 1-4 of the validated reference palette (adjacent CVD separation >= 9 in a
# stack). Aqua and yellow are below 3:1 contrast on white, so every segment is labelled
# directly and the README carries the same numbers as a table.
OUTCOME_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
OUTCOMES = [
    "Auto-posted: direct debits",
    "Auto-posted: other payments",
    "Sent to review",
    "Left unapplied",
]


def outcome_split(m: dict) -> list[int]:
    """Receivable test lines of one client, by what the system did with them."""
    by_type = m["by_match_type"]
    dd_auto = round(
        sum(
            by_type[t]["n"] * (by_type[t]["automation"] or 0)
            for t in ("dd", "dd_return")
            if t in by_type
        )
    )
    conf = m["confusion"]
    auto = conf.get("ar->auto", 0)
    return [dd_auto, auto - dd_auto, conf.get("ar->review", 0), conf.get("ar->unapplied", 0)]


def plot_outcomes(metrics: dict[str, dict], path: Path) -> None:
    """One 100% bar per client: where its receivable lines ended up in the test quarter."""
    names = list(metrics)
    splits = [outcome_split(metrics[n]) for n in names]
    fig, ax = plt.subplots(figsize=(8.6, 2.9), dpi=200)
    for row, counts in enumerate(splits):
        total, left = sum(counts), 0.0
        for k, c in enumerate(counts):
            share = 100 * c / total
            ax.barh(
                row,
                share,
                left=left,
                height=0.56,
                color=OUTCOME_COLORS[k],
                edgecolor="white",
                linewidth=2,
            )
            if share >= 6:
                ink = "white" if k in (0, 1) else "#0b0b0b"
                ax.text(
                    left + share / 2,
                    row,
                    f"{share:.0f}%",
                    ha="center",
                    va="center",
                    fontsize=8.5,
                    color=ink,
                    fontweight="bold",
                )
            left += share
        ax.text(101, row, f"{total:,} lines", va="center", fontsize=8, color="#52514e")
    ax.set_yticks(range(len(names)), names, fontsize=9.5)
    ax.invert_yaxis()
    ax.set_xlim(0, 112)
    ax.set_xticks(
        [0, 25, 50, 75, 100], ["0%", "25%", "50%", "75%", "100%"], fontsize=8, color="#52514e"
    )
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.spines["bottom"].set_color("#c9c9c9")
    ax.spines["bottom"].set_bounds(0, 100)
    ax.tick_params(axis="y", length=0)
    ax.tick_params(axis="x", color="#c9c9c9")
    handles = [plt.Rectangle((0, 0), 1, 1, color=c) for c in OUTCOME_COLORS]
    ax.legend(
        handles,
        OUTCOMES,
        ncol=4,
        frameon=False,
        fontsize=8,
        loc="lower left",
        bbox_to_anchor=(0, 1.02),
        handlelength=1.1,
        columnspacing=1.4,
    )
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
