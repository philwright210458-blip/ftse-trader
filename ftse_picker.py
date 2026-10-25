"""Morning picker views for the scanner (from 26 Oct 2026; ideas register 28).

A scheduled Claude task runs at about 07:06 UK on weekdays. It reads the overnight news,
company announcements and market mood, and writes scan_data/picker/YYYY-MM-DD.csv:

    day,ticker,company,view,reason,made_at
    2026-10-26,BRBY.L,Burberry Group,Sell,"Profit warning at 07:00",2026-10-26 07:09

view is Buy, Sell or Avoid. Shares not listed are neutral.

Modes (scanner --picker):
  * annotate (the default from 26 Oct 2026, Phil's choice): label each idea Agrees / Opposes /
    Avoid / No view and print a count; trades are exactly as without the picker, so its value
    can be measured cleanly first (research design stage 1).
  * filter: an idea against the picker's view (a buy when it says Sell, a sell when it says Buy),
    or on a share it says to Avoid, is skipped and logged with the reason; the live trader takes
    ideas the picker agrees with first when places are limited.
  * off: ignore the file.
If there is no file for the day (the task didn't run), the scanner works as before.
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


def alignment(setup, views: dict[str, tuple[str, str]]) -> str:
    """Agrees, Opposes, Avoid or No view for one idea."""
    view = views.get(setup.ticker.upper())
    if not view:
        return "No view"
    if view[0] == "Avoid":
        return "Avoid"
    return "Agrees" if VIEWS[view[0]] == setup.direction else "Opposes"


def annotate_views(setups: list, views: dict[str, tuple[str, str]]) -> dict[str, int]:
    """Label ideas with the picker's view without changing anything else. Returns counts per label."""
    counts = {"Agrees": 0, "Opposes": 0, "Avoid": 0, "No view": 0}
    for setup in setups:
        label = alignment(setup, views)
        setup.picker_alignment = label
        counts[label] += 1
        if label != "No view":
            word, reason = views[setup.ticker.upper()]
            setup.note = f"{setup.note} Morning picker: {word} ({label.lower()})".strip() + (f": {reason}." if reason else ".")
    return counts


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
