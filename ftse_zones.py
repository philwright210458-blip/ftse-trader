"""Zone building with optional "fewer, stronger" rules (plan step 2, zone overhaul, idea 25; 2 Nov 2026).

build_zones(visible, cfg) with no rules is exactly what the live scanner does today. The overhaul rules, set in
advance before any results:

  recent_days  only swing points from the last N trading days (old levels drop out)
  merge_atr    zones closer together than this many typical 15-minute candle ranges become one zone
  move_atr     a re-test only counts if price moved away by at least this many typical candle ranges in between
               (today's rule: 0.3% of the price)

Research only: the live scanner does not use these rules.
"""

from __future__ import annotations

import pandas as pd

import ftse_trade_scanner as sc

NEAR_PCT = 1.2          # "zones near the price": within 1.2% (same as the trade notes)


def meaningful_touches(zone: sc.Zone, df: pd.DataFrame, cfg: sc.IndexConfig, move_atr: float | None,
                       typical: float) -> list[sc.SwingPoint]:
    """As sc.meaningful_touches, but the move away must also be at least move_atr typical candle ranges."""
    if move_atr is None:
        return sc.meaningful_touches(zone, df, cfg)
    candidates = sorted(zone.touches, key=lambda point: point.ts)
    if not candidates:
        return []
    accepted = [candidates[0]]
    for point in candidates[1:]:
        previous = accepted[-1]
        between = df.loc[(df.index > previous.ts) & (df.index <= point.ts)]
        if len(between) < cfg.min_touch_gap_bars or between.empty:
            continue
        if previous.kind == "low":
            need = max(zone.high * (1 + cfg.rearm_move_pct), zone.high + move_atr * typical)
            rearmed = float(between["high"].max()) >= need
        else:
            need = min(zone.low * (1 - cfg.rearm_move_pct), zone.low - move_atr * typical)
            rearmed = float(between["low"].min()) <= need
        if rearmed:
            accepted.append(point)
    return accepted


def merge_close(zones: list[sc.Zone], gap: float) -> list[sc.Zone]:
    """Zones whose edges are closer than `gap` (in price) become one zone covering both."""
    merged: list[sc.Zone] = []
    for zone in sorted(zones, key=lambda z: z.low):
        if merged and zone.low - merged[-1].high < gap:
            last = merged[-1]
            touches = sorted(last.touches + zone.touches, key=lambda p: p.ts)
            merged[-1] = sc.Zone(min(last.low, zone.low), max(last.high, zone.high), touches)
        else:
            merged.append(zone)
    return merged


def build_zones(visible: pd.DataFrame, cfg: sc.IndexConfig, rules: dict | None = None) -> list[sc.Zone]:
    if not rules:
        return sc.qualify_zones(sc.cluster_zones(sc.find_swing_points(visible, cfg.swing_lookback),
                                                 cfg.zone_tolerance_pct), visible, cfg)
    typical = sc.typical_true_range(visible)
    points = sc.find_swing_points(visible, cfg.swing_lookback)
    recent = rules.get("recent_days")
    if recent:
        days = sorted({stamp.normalize() for stamp in visible.index})
        first = days[-recent] if len(days) >= recent else days[0]
        points = [p for p in points if p.ts >= first]
    zones = []
    for zone in sc.cluster_zones(points, cfg.zone_tolerance_pct):
        touches = meaningful_touches(zone, visible, cfg, rules.get("move_atr"), typical)
        if len(touches) >= cfg.min_touches:
            prices = [p.price for p in touches]
            zones.append(sc.Zone(min(prices), max(prices), touches))
    if rules.get("merge_atr"):
        zones = merge_close(zones, rules["merge_atr"] * typical)
    return zones


def zones_near(zones: list[sc.Zone], price: float, pct: float = NEAR_PCT) -> int:
    band = price * pct / 100
    return sum(1 for z in zones if z.low - band <= price <= z.high + band)
