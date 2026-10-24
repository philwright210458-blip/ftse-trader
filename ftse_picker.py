"""Morning picker views for the scanner (from 26 Oct 2026; ideas register 28).

A scheduled Claude task runs at about 07:06 UK on weekdays. It reads the overnight news,
company announcements and market mood, and writes scan_data/picker/YYYY-MM-DD.csv:

    day,ticker,company,view,reason,made_at
    2026-10-26,BRBY.L,Burberry Group,Sell,"Profit warning at 07:00",2026-10-26 07:09

view is Buy, Sell or Avoid. Shares not listed are neutral.

The scanner uses the views as an extra rule (--picker filter, the default):
  * an idea against the picker's view (a buy when it says Sell, a sell when it says Buy),
    or on a share it says to Avoid, is skipped and logged with the reason;
  * the live trader takes ideas the picker agrees with first when places are limited;
  * neutral shares are traded exactly as before.
If there is no file for the day (the task didn't run), the scanner works as before.
--picker off ignores the file.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

PICKER_DIR = "picker"
VIEWS = {"Buy": "Long", "Sell": "Short"}


def picker_path(data_dir: Path, day: str) -> Path:
    return Path(data_dir) / PICKER_DIR / f"{day}.csv"


def load_views(data_dir: Path, day: str) -> dict[str, tuple[str, str]] | None:
    """ticker -> (view, reason) for the day, or None if there is no picker file for that day."""
    path = picker_path(data_dir, day)
    if not path.exists() or path.stat().st_size == 0:
        return None
    try:
        table = pd.read_csv(path, dtype=str, keep_default_na=False)
    except Exception as error:                     # a broken file must never stop a scan
        print(f"Morning picker file {path.name} could not be read ({error}); scanning without it.")
        return None
    views = {}
    for _, row in table.iterrows():
        ticker, view = str(row.get("ticker", "")).strip().upper(), str(row.get("view", "")).strip().capitalize()
        if ticker and view in ("Buy", "Sell", "Avoid"):
            views[ticker] = (view, str(row.get("reason", "")).strip())
    return views


def apply_views(setups: list, views: dict[str, tuple[str, str]]) -> int:
    """Skip ideas against the picker (as conflicts with a note); mark agreeing ones. Returns how many were skipped."""
    skipped = 0
    for setup in setups:
        setup.picker_agrees = False
        view = views.get(setup.ticker.upper())
        if not view or setup.conflict:
            continue
        word, reason = view
        if word == "Avoid" or VIEWS[word] != setup.direction:
            setup.conflict = True
            setup.conflict_note = f"Morning picker says {word}" + (f" ({reason})" if reason else "") + "; skipped."
            skipped += 1
        else:
            setup.picker_agrees = True
            setup.note = f"{setup.note} Morning picker agrees ({word}).".strip()
    return skipped
