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
