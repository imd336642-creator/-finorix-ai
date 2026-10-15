"""FINORIX PRO prediction engine.

Signals are probabilistic and use completed candles only. The latest candle may
still be forming; the server decides which candles are complete before calling
predict_next_candle().
"""
from statistics import mean


def ema(values, period):
    if not values:
        return None
    period = min(int(period), len(values))
    if period <= 0:
        return None
    k = 2 / (period + 1)
    result = mean(values[:period])
    for value in values[period:]:
        result = value * k + result * (1 - k)
    return result


def candle_data(candle):
    o, h, l, c = (float(candle[k]) for k in ("open", "high", "low", "close"))
    rng = max(h - l, 1e-12)
    body = abs(c - o)
    return {
        "open": o, "high": h, "low": l, "close": c, "range": rng,
        "body": body, "upper_wick": h - max(o, c),
        "lower_wick": min(o, c) - l, "body_ratio": body / rng,
        "price_position": (c - l) / rng,
    }


def predict_next_candle(candles):
    """Return a cautious UP/DOWN/WAIT signal based on completed candles."""
    clean = []
    for c in candles or []:
        try:
            item = {k: float(c[k]) for k in ("open", "high", "low", "close")}
            item["timestamp"] = int(c.get("timestamp", c.get("time", 0)) or 0)
            if item["high"] >= max(item["open"], item["close"]) and item["low"] <= min(item["open"], item["close"]):
                clean.append(item)
        except (TypeError, ValueError, KeyError):
            continue

    if len(clean) < 20:
        return {
            "direction": "WAIT", "confidence": 50, "action": "WAIT",
            "reasons": [f"Need at least 20 completed candles (currently {len(clean)})."],
            "score": 0, "source_candle_count": len(clean),
        }

    last = candle_data(clean[-1])
    prev = candle_data(clean[-2])
    closes = [c["close"] for c in clean]
    score, reasons = 0, []

    # Trend context: EMA alignment, with price confirmation.
    e5, e10, e20 = ema(closes, 5), ema(closes, 10), ema(closes, 20)
    if e5 is not None and e10 is not None and e20 is not None:
        if e5 > e10 > e20 and closes[-1] > e5:
            score += 2
            reasons.append("Bullish EMA 5/10/20 alignment")
        elif e5 < e10 < e20 and closes[-1] < e5:
            score -= 2
            reasons.append("Bearish EMA 5/10/20 alignment")
        else:
            reasons.append("EMA trend is mixed")

    # Recent closed candle body and close location.
    direction = 1 if last["close"] > last["open"] else -1 if last["close"] < last["open"] else 0
    if direction and last["body_ratio"] >= 0.60:
        score += 2 * direction
        reasons.append("Strong completed candle body")
    elif direction:
        score += direction
        reasons.append("Completed candle direction")

    if last["price_position"] >= 0.80:
        score += 1
        reasons.append("Close near candle high")
    elif last["price_position"] <= 0.20:
        score -= 1
        reasons.append("Close near candle low")

    upper_ratio = last["upper_wick"] / last["range"]
    lower_ratio = last["lower_wick"] / last["range"]
    if upper_ratio >= 0.45 and upper_ratio > lower_ratio:
        score -= 1
        reasons.append("Upper-wick rejection")
    elif lower_ratio >= 0.45 and lower_ratio > upper_ratio:
        score += 1
        reasons.append("Lower-wick rejection")

    # Momentum over three closed-candle transitions.
    if len(closes) >= 4:
        moves = [closes[i] - closes[i - 1] for i in range(len(closes) - 3, len(closes))]
        up = sum(x > 0 for x in moves)
        down = sum(x < 0 for x in moves)
        if up >= 3:
            score += 2
            reasons.append("Consistent short-term upward momentum")
        elif down >= 3:
            score -= 2
            reasons.append("Consistent short-term downward momentum")

    # A breakout is only counted when a completed close confirms it.
    if last["close"] > max(c["high"] for c in clean[-6:-1]):
        score += 2
        reasons.append("Closed above recent five-candle high")
    elif last["close"] < min(c["low"] for c in clean[-6:-1]):
        score -= 2
        reasons.append("Closed below recent five-candle low")

    if score >= 5:
        direction_out = "UP"
    elif score <= -5:
        direction_out = "DOWN"
    else:
        direction_out = "WAIT"

    # This is a score-derived strength estimate, NOT a statistically calibrated
    # probability of winning. Label it signal strength in the UI.
    strength = min(85, 50 + abs(score) * 5)
    if direction_out == "WAIT":
        strength = min(strength, 60)

    return {
        "direction": direction_out,
        "confidence": int(strength),
        "action": {"UP": "NEXT CANDLE BUY", "DOWN": "NEXT CANDLE SELL", "WAIT": "WAIT"}[direction_out],
        "score": int(score),
        "reasons": reasons[-6:] or ["No clear directional confirmation"],
        "source_candle_count": len(clean),
        "signal_candle_timestamp": clean[-1]["timestamp"],
        "ema5": round(e5, 6) if e5 is not None else None,
        "ema10": round(e10, 6) if e10 is not None else None,
        "ema20": round(e20, 6) if e20 is not None else None,
    }
