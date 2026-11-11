"""Calendar helpers shared by the generator and the weekly pipeline."""

from __future__ import annotations

import datetime as dt


def week_ends(start: dt.date, end: dt.date) -> list[dt.date]:
    """Sundays from `start` to `end`, plus `end` itself: the dates of the weekly bank exports/runs."""
    d = start + dt.timedelta(days=(6 - start.weekday()) % 7)
    out = []
    while d <= end:
        out.append(d)
        d += dt.timedelta(days=7)
    if not out or out[-1] != end:
        out.append(end)
    return out


def month_end(d: dt.date) -> dt.date:
    nxt = dt.date(d.year + d.month // 12, d.month % 12 + 1, 1)
    return nxt - dt.timedelta(days=1)
