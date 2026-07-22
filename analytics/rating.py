"""The rating engine: four analytical axes -> composite score -> grade -> signal.

Each axis produces a 0-100 sub-score. Sub-scores are stored raw so the
dashboard can recombine them under user-chosen weights without a server
round-trip. Composite, grade and signal are all derived, never stored inputs.
"""

from __future__ import annotations

from typing import Any

import config
from analytics import indicators as ind


# --- Axis 1: Momentum & Trend ---------------------------------------------

def score_momentum(candles: list[dict]) -> tuple[float | None, dict[str, Any]]:
    """RSI, MACD, EMA stacking, and range position across timeframes."""
    closes = [c["c"] for c in candles]
    if len(closes) < 60:
        return None, {}

    detail: dict[str, Any] = {}
    parts: list[tuple[float, float]] = []  # (score, weight)

    # RSI on 1h and synthetic 4h (every 4th bar).
    r1 = ind.rsi(closes, 14)
    if r1 is not None:
        detail["rsi_1h"] = round(r1, 2)
        # 50 is neutral; reward strength but penalise overbought above ~75.
        s = ind.scale(r1, 30, 70) if r1 <= 70 else ind.scale(r1, 70, 95, invert=True) * 0.6 + 40
        parts.append((ind.clamp(s), 1.0))

    closes_4h = closes[::-1][::4][::-1]  # keep the most recent bar aligned
    r4 = ind.rsi(closes_4h, 14)
    if r4 is not None:
        detail["rsi_4h"] = round(r4, 2)
        s = ind.scale(r4, 30, 70) if r4 <= 70 else ind.scale(r4, 70, 95, invert=True) * 0.6 + 40
        parts.append((ind.clamp(s), 0.8))

    # MACD: sign of the histogram plus whether it is expanding or contracting.
    m = ind.macd(closes)
    if m:
        detail["macd_hist"] = round(m["hist"], 6)
        detail["macd_slope"] = round(m["hist_slope"], 6)
        px = closes[-1] or 1
        hist_norm = m["hist"] / px * 100          # histogram as % of price
        slope_norm = m["hist_slope"] / px * 100
        parts.append((ind.scale(hist_norm, -1.5, 1.5), 1.0))
        parts.append((ind.scale(slope_norm, -0.4, 0.4), 0.6))

    # EMA stacking: 20 > 50 > 200 is a textbook uptrend.
    e20, e50, e200 = ind.ema(closes, 20), ind.ema(closes, 50), ind.ema(closes, 200)
    price = closes[-1]
    if e20 and e50:
        detail["ema20"], detail["ema50"] = round(e20, 6), round(e50, 6)
        stack = 0.0
        stack += 30 if price > e20 else 0
        stack += 30 if e20 > e50 else 0
        if e200:
            detail["ema200"] = round(e200, 6)
            stack += 40 if e50 > e200 else 0
        else:
            stack = stack / 60 * 100  # renormalise when EMA200 is unavailable
        parts.append((ind.clamp(stack), 1.2))

    # Where price sits inside its recent range.
    for periods, label, weight in ((168, "range_7d", 0.7), (720, "range_30d", 0.7)):
        rp = ind.range_position(closes, min(periods, len(closes)))
        if rp is not None:
            detail[label] = round(rp, 2)
            parts.append((rp, weight))

    if not parts:
        return None, detail

    total_w = sum(w for _, w in parts)
    score = sum(s * w for s, w in parts) / total_w

    # Agreement bonus: when short- and medium-term RSI both lean the same way,
    # the trend is more trustworthy than either reading alone.
    if r1 is not None and r4 is not None:
        if (r1 > 55 and r4 > 55) or (r1 < 45 and r4 < 45):
            score = ind.clamp(score + (4 if r1 > 55 else -4))
            detail["tf_agreement"] = True

    return ind.clamp(score), detail


# --- Axis 2: Volatility & Risk --------------------------------------------

def risk_metrics(candles: list[dict]) -> dict[str, float] | None:
    """Raw risk metrics. Scored later, relative to the basket."""
    if len(candles) < 30:
        return None
    closes = [c["c"] for c in candles]
    highs = [c["h"] for c in candles]
    lows = [c["l"] for c in candles]

    a = ind.atr(highs, lows, closes, 14)
    price = closes[-1] or 1
    window = closes[-720:] if len(closes) >= 720 else closes

    return {
        "atr_pct": (a / price * 100) if a else 0.0,
        "atr": a or 0.0,
        "realized_vol": (ind.realized_vol(window) or 0.0) * 100,
        "max_drawdown": (ind.max_drawdown(window) or 0.0) * 100,
        "sharpe": ind.sharpe(window) or 0.0,
    }


def score_risk(metrics: dict[str, float],
               basket: dict[str, dict[str, float]]) -> tuple[float, dict[str, Any]]:
    """Lower risk scores higher. Volatility and drawdown are ranked against the
    basket so scores stay meaningful regardless of the overall market regime;
    Sharpe is scored on an absolute scale since it is already normalised."""
    atr_pop = [m["atr_pct"] for m in basket.values()]
    vol_pop = [m["realized_vol"] for m in basket.values()]
    dd_pop = [m["max_drawdown"] for m in basket.values()]

    # Percentile rank, then inverted: least volatile in the basket scores best.
    s_atr = 100 - ind.pct_rank(metrics["atr_pct"], atr_pop)
    s_vol = 100 - ind.pct_rank(metrics["realized_vol"], vol_pop)
    s_dd = 100 - ind.pct_rank(metrics["max_drawdown"], dd_pop)
    s_sharpe = ind.scale(metrics["sharpe"], -2.0, 3.0)

    score = s_atr * 0.25 + s_vol * 0.25 + s_dd * 0.25 + s_sharpe * 0.25

    detail = {
        "atr_pct": round(metrics["atr_pct"], 3),
        "realized_vol_pct": round(metrics["realized_vol"], 2),
        "max_drawdown_pct": round(metrics["max_drawdown"], 2),
        "sharpe": round(metrics["sharpe"], 3),
    }
    return ind.clamp(score), detail


# --- Axis 3: Market Structure ---------------------------------------------

def structure_metrics(market: dict[str, Any], candles: list[dict]) -> dict[str, float]:
    """Raw structure metrics, scored later relative to the basket."""
    out: dict[str, float] = {}

    mcap = market.get("mcap")
    vol_usd = market.get("volume_24h_usd")
    if mcap and vol_usd:
        out["turnover"] = vol_usd / mcap * 100

    # Volume trend: last 24 bars vs the prior 7-day average.
    if len(candles) >= 192:
        vols = [c["v"] for c in candles]
        baseline = sum(vols[-192:-24]) / 168
        if baseline > 0:
            out["volume_trend"] = (sum(vols[-24:]) / 24) / baseline

    return out


def score_structure(symbol: str, market: dict[str, Any], book: dict[str, float] | None,
                    metrics: dict[str, float],
                    basket: dict[str, dict[str, float]]) -> tuple[float | None, dict[str, Any]]:
    """Market cap standing, liquidity turnover, volume trend, and spread.

    Turnover and volume trend are ranked *within the basket* rather than against
    fixed thresholds. Absolute bands proved regime-dependent -- in a quiet
    market every asset's raw turnover sits at the bottom of any fixed range, so
    the whole axis collapses toward zero and drags every composite down with it.
    Rank and spread keep absolute scales, since those are meaningful on their
    own terms (rank 1 is rank 1; a 0.1bp spread is tight in any regime).
    """
    detail: dict[str, Any] = {}
    parts: list[tuple[float, float]] = []

    rank = market.get("rank")
    if rank:
        detail["basket_rank"] = rank
        # Rank 1 -> 100, rank 15 -> ~30. Size is a proxy for durability.
        parts.append((ind.scale(rank, 1, len(config.ASSETS), invert=True) * 0.7 + 30, 1.0))

    if "turnover" in metrics:
        detail["turnover_pct"] = round(metrics["turnover"], 2)
        pop = [m["turnover"] for m in basket.values() if "turnover" in m]
        parts.append((ind.pct_rank(metrics["turnover"], pop), 1.0))

    if "volume_trend" in metrics:
        detail["volume_vs_7d"] = round(metrics["volume_trend"], 2)
        pop = [m["volume_trend"] for m in basket.values() if "volume_trend" in m]
        parts.append((ind.pct_rank(metrics["volume_trend"], pop), 0.8))

    if book:
        detail["spread_bps"] = round(book["spread_bps"], 3)
        # Under ~1bp is excellent; above ~20bp is costly to trade.
        parts.append((ind.scale(book["spread_bps"], 0.5, 20, invert=True), 0.8))

    if not parts:
        return None, detail

    total_w = sum(w for _, w in parts)
    return ind.clamp(sum(s * w for s, w in parts) / total_w), detail


# --- Axis 4: Relative Strength --------------------------------------------

def score_relative(symbol: str, candles: list[dict],
                   returns_basket: dict[str, dict[str, float]],
                   benchmark_returns: dict[str, float]) -> tuple[float | None, dict[str, Any]]:
    """Performance versus BTC and versus the rest of the basket."""
    mine = returns_basket.get(symbol)
    if not mine:
        return None, {}

    detail: dict[str, Any] = {}
    parts: list[tuple[float, float]] = []

    for horizon, weight in (("24h", 1.0), ("7d", 1.0), ("30d", 0.8)):
        mine_r = mine.get(horizon)
        if mine_r is None:
            continue
        detail[f"return_{horizon}"] = round(mine_r, 2)

        # Versus BTC. The benchmark scores neutral against itself by definition.
        bench_r = benchmark_returns.get(horizon)
        if bench_r is not None and symbol != config.BENCHMARK:
            excess = mine_r - bench_r
            detail[f"vs_btc_{horizon}"] = round(excess, 2)
            parts.append((ind.scale(excess, -20, 20), weight))
        elif symbol == config.BENCHMARK:
            parts.append((50.0, weight * 0.5))

        # Percentile within the basket.
        pop = [r[horizon] for r in returns_basket.values() if r.get(horizon) is not None]
        if len(pop) > 1:
            rank_score = ind.pct_rank(mine_r, pop)
            detail[f"basket_pct_{horizon}"] = round(rank_score, 1)
            parts.append((rank_score, weight))

    if not parts:
        return None, detail

    total_w = sum(w for _, w in parts)
    score = sum(s * w for s, w in parts) / total_w

    # Rotation flag: outperforming on short horizons but not long ones means
    # capital is rotating in right now.
    short, long = detail.get("basket_pct_24h"), detail.get("basket_pct_30d")
    if short is not None and long is not None and short - long > 20:
        detail["rotating_in"] = True
        score = ind.clamp(score + 3)

    return ind.clamp(score), detail


# --- Composition -----------------------------------------------------------

def grade_for(composite: float) -> str:
    for cutoff, letter in config.GRADE_BANDS:
        if composite >= cutoff:
            return letter
    return "F"


def composite_score(sub: dict[str, float | None],
                    weights: dict[str, float] | None = None) -> float | None:
    """Weighted blend of available sub-scores.

    Missing axes are dropped and the remaining weights renormalised, so a coin
    with incomplete data still gets a usable score rather than None.
    """
    w = weights or config.DEFAULT_WEIGHTS
    present = [(sub[k], w[k]) for k in w if sub.get(k) is not None]
    if not present:
        return None
    total_w = sum(x for _, x in present)
    if total_w == 0:
        return None
    return ind.clamp(sum(s * x for s, x in present) / total_w)


def coverage(sub: dict[str, float | None],
             weights: dict[str, float] | None = None) -> float:
    """Share of the rating weight that actually had data behind it, 0.0-1.0.

    composite_score renormalises over the axes it has, which keeps a score
    usable but erases how much of the model produced it: one surviving axis
    and all four look identical downstream. This reports that difference so
    signal_for can refuse to make a call the data does not support.
    """
    w = weights or config.DEFAULT_WEIGHTS
    total = sum(w.values())
    if total <= 0:
        return 0.0
    have = sum(w[k] for k in w if sub.get(k) is not None)
    return have / total


def signal_for(composite: float | None, momentum: float | None,
               prev_signal: str | None, holding: bool,
               axis_coverage: float = 1.0) -> str:
    """Signal with hysteresis.

    Ratings recompute every 2 minutes. A single threshold would flip constantly
    for any coin hovering near it, so entry requires crossing above
    BUY_THRESHOLD while exit requires falling all the way to EXIT_THRESHOLD.
    The band between them deliberately produces HOLD.

    `axis_coverage` gates the whole thing: below MIN_SIGNAL_COVERAGE there is
    not enough of the model left to justify telling anyone to act, so this
    reports NO DATA even though a composite exists. Defaults to 1.0 so a
    caller that has already established full coverage need not pass it.
    """
    if composite is None:
        return "NO DATA"

    # Not "NEUTRAL": neutral is a finding -- flat and uninteresting -- and this
    # is the absence of one. Saying NEUTRAL here would present missing candles
    # as a considered verdict. NO DATA is already rendered in all five
    # languages, so this needs no new string.
    if axis_coverage < config.MIN_SIGNAL_COVERAGE:
        return "NO DATA"

    if composite >= config.STRONG_BUY_THRESHOLD:
        return "STRONG BUY"
    if composite <= config.STRONG_SELL_THRESHOLD:
        return "STRONG SELL"
    if composite <= config.EXIT_THRESHOLD:
        return "SELL"

    if composite >= config.BUY_THRESHOLD:
        # Entry additionally requires momentum not to be actively deteriorating.
        if momentum is not None and momentum < 45:
            return "HOLD"
        return "BUY"

    # In the dead band. HOLD and NEUTRAL are both "do nothing", but they mean
    # different things: HOLD is a bullish stance still being carried, NEUTRAL is
    # genuinely flat and uninteresting. Anchoring HOLD to an actually-open
    # position (plus one cycle of grace for a rating that just cooled off) keeps
    # the distinction real -- feeding "HOLD" back in here would make it
    # self-sustaining and NEUTRAL unreachable.
    if holding or prev_signal in ("BUY", "STRONG BUY"):
        return "HOLD"
    return "NEUTRAL"


def build_baskets(candles_by_symbol: dict[str, list[dict]],
                  market: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Precompute the cross-sectional context every asset is scored against.

    Several axes are relative rather than absolute, so this must run over the
    whole basket before any individual asset can be rated.
    """
    risk_basket: dict[str, dict[str, float]] = {}
    structure_basket: dict[str, dict[str, float]] = {}
    returns_basket: dict[str, dict[str, float]] = {}

    for symbol, candles in candles_by_symbol.items():
        rm = risk_metrics(candles)
        if rm:
            risk_basket[symbol] = rm

        structure_basket[symbol] = structure_metrics(market.get(symbol, {}), candles)

        closes = [c["c"] for c in candles]
        returns_basket[symbol] = {
            "24h": ind.pct_change(closes, 24),
            "7d": ind.pct_change(closes, 168),
            "30d": ind.pct_change(closes, min(720, max(len(closes) - 1, 1))),
        }

    return {
        "risk": risk_basket,
        "structure": structure_basket,
        "returns": returns_basket,
        "benchmark": returns_basket.get(config.BENCHMARK, {}),
    }


def rate_asset(symbol: str, candles: list[dict], market: dict[str, Any],
               book: dict[str, float] | None, risk_basket: dict[str, dict[str, float]],
               structure_basket: dict[str, dict[str, float]],
               returns_basket: dict[str, dict[str, float]],
               benchmark_returns: dict[str, float],
               prev_signal: str | None = None,
               holding: bool = False) -> dict[str, Any]:
    """Full rating for one asset. Returns sub-scores, composite, grade, signal
    and the supporting detail the dashboard renders in the drill-down panel."""
    momentum, m_detail = score_momentum(candles)
    structure, s_detail = score_structure(
        symbol, market, book, structure_basket.get(symbol, {}), structure_basket)
    relative, r_detail = score_relative(symbol, candles, returns_basket, benchmark_returns)

    my_risk = risk_basket.get(symbol)
    if my_risk:
        risk, k_detail = score_risk(my_risk, risk_basket)
    else:
        risk, k_detail = None, {}

    sub = {"momentum": momentum, "risk": risk,
           "structure": structure, "relative": relative}
    composite = composite_score(sub)
    signal = signal_for(composite, momentum, prev_signal, holding,
                        coverage(sub))

    return {
        **{k: (round(v, 2) if v is not None else None) for k, v in sub.items()},
        "composite": round(composite, 2) if composite is not None else None,
        "grade": grade_for(composite) if composite is not None else "-",
        "signal": signal,
        "detail": {
            "momentum": m_detail,
            "risk": k_detail,
            "structure": s_detail,
            "relative": r_detail,
        },
    }
