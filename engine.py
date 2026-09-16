"""Finorix AI prototype prediction engine.

This is a rule-based, no-lookahead prototype. It is not a guarantee of future
price movement and should be backtested before any financial use.
"""
from statistics import mean


def _ema(values, period):
    if len(values) < period:
        return mean(values)
    k = 2 / (period + 1)
    value = mean(values[:period])
    for price in values[period:]:
        value = price * k + value * (1 - k)
    return value


def predict_next_candle(candles):
    if len(candles) < 3:
        return {"direction": "WAIT", "confidence": 50, "reasons": ["Need at least 3 closed candles"], "source_candle_count": len(candles)}

    closes = [float(c["close"]) for c in candles]
    last = candles[-1]
    prev = candles[-2]
    recent = candles[-min(8, len(candles)):-1]
    recent_high = max(float(c["high"]) for c in recent) if recent else float(prev["high"])
    recent_low = min(float(c["low"]) for c in recent) if recent else float(prev["low"])

    score = 0
    reasons = []

    # Candle body direction/strength.
    last_open = float(last["open"])
    last_close = float(last["close"])
    last_high = float(last["high"])
    last_low = float(last["low"])
    rng = max(last_high - last_low, 1e-9)
    body_ratio = abs(last_close - last_open) / rng
    if last_close > last_open and body_ratio >= 0.55:
        score += 2
        reasons.append("Strong bullish candle")
    elif last_close < last_open and body_ratio >= 0.55:
        score -= 2
        reasons.append("Strong bearish candle")

    # Breakout/breakdown using only candles before the signal candle.
    if last_close > recent_high:
        score += 3
        reasons.append("Closed above recent high")
    elif last_close < recent_low:
        score -= 3
        reasons.append("Closed below recent low")

    # Short momentum.
    delta = closes[-1] - closes[-3]
    if delta > 0:
        score += 1
        reasons.append("Short-term momentum up")
    elif delta < 0:
        score -= 1
        reasons.append("Short-term momentum down")

    # EMA context.
    ema_fast = _ema(closes, min(5, len(closes)))
    ema_slow = _ema(closes, min(10, len(closes)))
    if ema_fast > ema_slow and last_close > ema_fast:
        score += 1
        reasons.append("Price above bullish EMA structure")
    elif ema_fast < ema_slow and last_close < ema_fast:
        score -= 1
        reasons.append("Price below bearish EMA structure")

    # Translate score into conservative confidence. Never show fake 99% values.
    confidence = min(92, 50 + abs(score) * 7)
    if abs(score) >= 4:
        direction = "UP" if score > 0 else "DOWN"
    else:
        direction = "WAIT"
        confidence = min(confidence, 64)
        reasons.append("No sufficiently strong confluence")

    return {
        "direction": direction,
        "confidence": int(confidence),
        "reasons": reasons[-5:],
        "source_candle_count": len(candles),
        "score": score,
        "body_strength": round(body_ratio, 2),
    }
