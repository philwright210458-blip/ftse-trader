"""A made-up share price with one textbook impulse, pullback and breakout (for the structure tests)."""

import pandas as pd


def candles(path):
    """path: list of (open, close) pairs; highs/lows sit just beyond the body."""
    rows = [(o, max(o, c) + 0.15, min(o, c) - 0.15, c) for o, c in path]
    days = pd.bdate_range("2023-01-02", periods=len(rows))
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=days)


def textbook():
    path = []
    for k in range(20):                                      # quiet start, with one dip to make an earlier swing low
        if k in (8, 9):
            path.append((100.0, 99.0) if k == 8 else (99.0, 100.0))
        else:
            path.append((100.0, 100.3) if k % 2 == 0 else (100.3, 100.0))
    for o, c in [(100.0, 100.6), (100.6, 101.2), (101.2, 101.8), (101.8, 102.4),     # small wave up: swing high
                 (102.4, 101.8), (101.8, 101.2), (101.2, 100.6), (100.6, 100.4),     # down: swing low (higher than start)
                 (100.4, 100.6), (100.6, 100.5), (100.5, 100.8)]:
        path.append((o, c))
    price = 100.8
    for _ in range(6):                                       # impulse: big green candles closing at the highs
        path.append((price, price + 1.7))
        price += 1.7
    for o, c in [(price, price - 0.6), (price - 0.6, price - 1.6), (price - 1.6, price - 2.4),
                 (price - 2.4, price - 3.4), (price - 3.4, price - 4.4), (price - 4.4, price - 4.2),
                 (price - 4.2, price - 4.9), (price - 4.9, price - 4.6)]:
        path.append((o, c))                                  # pullback of about half
    low_close = path[-1][1]
    path.append((low_close, low_close + 1.6))                # strong breakout day
    price = low_close + 1.6
    for _ in range(14):
        path.append((price, price + 0.9))
        price += 0.9
    for k in range(80):
        path.append((price, price + 0.1) if k % 2 == 0 else (price + 0.1, price))
    return candles(path)
