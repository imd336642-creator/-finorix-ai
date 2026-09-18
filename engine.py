"""FINORIX AI - Live Current Candle Next-Candle Predictor"""

from statistics import mean

def ema(values, period):

    if not values:

        return 0.0

    period = min(period, len(values))

    if len(values) < period:

        return mean(values)

    k = 2 / (period + 1)

    result = mean(values[:period])

    for price in values[period:]:

        result = price * k + result * (1 - k)

    return result

def candle_data(candle):

    o = float(candle["open"])

    h = float(candle["high"])

    l = float(candle["low"])

    c = float(candle["close"])

    rng = max(h - l, 1e-10)

    body = abs(c - o)

    upper_wick = h - max(o, c)

    lower_wick = min(o, c) - l

    body_ratio = body / rng

    price_position = (c - l) / rng

    return {

        "open": o,

        "high": h,

        "low": l,

        "close": c,

        "range": rng,

        "body": body,

        "upper_wick": upper_wick,

        "lower_wick": lower_wick,

        "body_ratio": body_ratio,

        "price_position": price_position,

    }

def predict_next_candle(candles):

    if len(candles) < 10:

        return {

            "direction": "WAIT",

            "confidence": 50,

            "reasons": ["Not enough candle data"],

            "source_candle_count": len(candles),

        }

    # ==========================================

    # CURRENT LIVE CANDLE

    # ==========================================

    current = candle_data(candles[-1])

    previous = candle_data(candles[-2])

    previous2 = candle_data(candles[-3])

    o = current["open"]

    h = current["high"]

    l = current["low"]

    price = current["close"]

    score = 0

    reasons = []

    # ==========================================

    # 1. CURRENT CANDLE DIRECTION

    # ==========================================

    if price > o:

        if current["body_ratio"] >= 0.60:

            score += 2

            reasons.append("Strong bullish live candle")

        else:

            score += 1

            reasons.append("Bullish live candle")

    elif price < o:

        if current["body_ratio"] >= 0.60:

            score -= 2

            reasons.append("Strong bearish live candle")

        else:

            score -= 1

            reasons.append("Bearish live candle")

    # ==========================================

    # 2. CURRENT PRICE POSITION

    # ==========================================

    position = current["price_position"]

    if position >= 0.80:

        score += 2

        reasons.append("Live price near current high")

    elif position <= 0.20:

        score -= 2

        reasons.append("Live price near current low")

    # ==========================================

    # 3. WICK / REJECTION

    # ==========================================

    upper_ratio = current["upper_wick"] / current["range"]

    lower_ratio = current["lower_wick"] / current["range"]

    if upper_ratio >= 0.35:

        score -= 1

        reasons.append("Upper wick rejection")

    if lower_ratio >= 0.35:

        score += 1

        reasons.append("Lower wick rejection")

    # ==========================================

    # 4. PREVIOUS CANDLE DIRECTION

    # ==========================================

    if previous["close"] > previous["open"]:

        score += 1

        reasons.append("Previous candle bullish")

    elif previous["close"] < previous["open"]:

        score -= 1

        reasons.append("Previous candle bearish")

    # ==========================================

    # 5. BREAK OF PREVIOUS HIGH / LOW

    # ==========================================

    if price > previous["high"]:

        score += 3

        reasons.append("Live price breaking previous high")

    elif price < previous["low"]:

        score -= 3

        reasons.append("Live price breaking previous low")

    # ==========================================

    # 6. MOMENTUM

    # ==========================================

    if price > previous2["close"]:

        score += 1

        reasons.append("Short momentum bullish")

    elif price < previous2["close"]:

        score -= 1

        reasons.append("Short momentum bearish")

    # ==========================================

    # 7. EMA TREND FILTER

    # ==========================================

    closes = [float(c["close"]) for c in candles]

    ema5 = ema(closes, 5)

    ema10 = ema(closes, 10)

    if ema5 > ema10 and price > ema5:

        score += 2

        reasons.append("Bullish EMA structure")

    elif ema5 < ema10 and price < ema5:

        score -= 2

        reasons.append("Bearish EMA structure")

    # ==========================================

    # 8. STRONG REJECTION FILTER

    # ==========================================

    # Large upper wick + price below open = bearish

    if upper_ratio >= 0.50 and price < o:

        score -= 2

        reasons.append("Strong bearish rejection")

    # Large lower wick + price above open = bullish

    if lower_ratio >= 0.50 and price > o:

        score += 2

        reasons.append("Strong bullish rejection")

    # ==========================================

    # FINAL DECISION

    # ==========================================

    if score >= 7:

        direction = "UP"

    elif score <= -7:

        direction = "DOWN"

    else:

        direction = "WAIT"

    # ==========================================

    # CONFIDENCE

    # ==========================================

    confidence = 50 + abs(score) * 5

    confidence = min(confidence, 92)

    if direction == "WAIT":

        confidence = min(confidence, 64)

    # ==========================================

    # RETURN

    # ==========================================

    return {

        "direction": direction,

        "confidence": int(confidence),

        "reasons": reasons[-7:],

        "source_candle_count": len(candles),

        "score": score,

        # Current live candle information

        "current_open": round(o, 6),

        "current_high": round(h, 6),

        "current_low": round(l, 6),

        "current_price": round(price, 6),

        "body": round(current["body"], 6),

        "body_strength": round(current["body_ratio"], 3),

        "upper_wick": round(current["upper_wick"], 6),

        "lower_wick": round(current["lower_wick"], 6),

        "price_position": round(position, 3),

        "ema5": round(ema5, 6),

        "ema10": round(ema10, 6),

    }
