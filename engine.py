"""FINORIX PRO - Conservative next-candle signal engine."""

from statistics import mean

MIN_CANDLES = 30

def ema(values, period):

    """Calculate exponential moving average."""

    if len(values) < period:

        return None

    multiplier = 2.0 / (period + 1.0)

    result = mean(values[:period])

    for value in values[period:]:

        result = value * multiplier + result * (1.0 - multiplier)

    return result

def candle_data(candle):

    """Validate and calculate candle measurements."""

    o = float(candle["open"])

    h = float(candle["high"])

    low = float(candle["low"])

    c = float(candle["close"])

    if h < max(o, c) or low > min(o, c) or h <= low:

        raise ValueError("Invalid OHLC candle")

    candle_range = h - low

    body = abs(c - o)

    return {

        "open": o,

        "high": h,

        "low": low,

        "close": c,

        "range": candle_range,

        "body": body,

        "body_ratio": body / candle_range,

        "upper_wick": h - max(o, c),

        "lower_wick": min(o, c) - low,

    }

def wait_signal(reason, count=0):

    return {

        "direction": "WAIT",

        "action": "WAIT",

        "confidence": 50,

        "score": 0,

        "reasons": [reason],

        "source_candle_count": count,

    }

def predict_next_candle(candles):

    """

    Analyze completed candles and produce a conservative signal.

    Confidence is a rule-strength score, NOT a measured win probability.

    """

    clean = []

    for candle in candles or []:

        try:

            item = {

                "open": float(candle["open"]),

                "high": float(candle["high"]),

                "low": float(candle["low"]),

                "close": float(candle["close"]),

                "timestamp": int(

                    float(candle.get("timestamp", candle.get("time", 0)) or 0)

                ),

            }

            if (

                item["high"] >= max(item["open"], item["close"])

                and item["low"] <= min(item["open"], item["close"])

                and item["high"] > item["low"]

            ):

                clean.append(item)

        except (TypeError, ValueError, KeyError):

            continue

    # Oldest to newest.

    clean.sort(key=lambda item: item["timestamp"])

    # Remove duplicate timestamps.

    unique = []

    seen = set()

    for candle in clean:

        timestamp = candle["timestamp"]

        if timestamp not in seen:

            unique.append(candle)

            seen.add(timestamp)

    clean = unique

    if len(clean) < MIN_CANDLES:

        return wait_signal(

            f"Need at least {MIN_CANDLES} valid candles; "

            f"received {len(clean)}.",

            len(clean),

        )

    bars = [candle_data(candle) for candle in clean]

    closes = [bar["close"] for bar in bars]

    last = bars[-1]

    previous = bars[-2]

    ema5 = ema(closes, 5)

    ema10 = ema(closes, 10)

    ema20 = ema(closes, 20)

    score = 0

    reasons = []

    # 1. Trend filter.

    bullish_trend = ema5 > ema10 > ema20 and closes[-1] > ema5

    bearish_trend = ema5 < ema10 < ema20 and closes[-1] < ema5

    if bullish_trend:

        score += 3

        reasons.append("Bullish EMA alignment")

    elif bearish_trend:

        score -= 3

        reasons.append("Bearish EMA alignment")

    else:

        reasons.append("EMA trend is mixed")

    # 2. Last completed candle body strength.

    last_direction = (

        1 if last["close"] > last["open"]

        else -1 if last["close"] < last["open"]

        else 0

    )

    if last_direction and last["body_ratio"] >= 0.58:

        score += 2 * last_direction

        reasons.append("Strong completed candle")

    else:

        reasons.append("Weak candle body")

    # 3. Short-term momentum.

    moves = [

        closes[i] - closes[i - 1]

        for i in range(len(closes) - 3, len(closes))

    ]

    up_moves = sum(move > 0 for move in moves)

    down_moves = sum(move < 0 for move in moves)

    if up_moves >= 2 and down_moves == 0:

        score += 2

        reasons.append("Bullish short-term momentum")

    elif down_moves >= 2 and up_moves == 0:

        score -= 2

        reasons.append("Bearish short-term momentum")

    else:

        reasons.append("Mixed short-term momentum")

    # 4. Breakout confirmation using completed candle close.

    previous_high = max(

        candle["high"] for candle in clean[-6:-1]

    )

    previous_low = min(

        candle["low"] for candle in clean[-6:-1]

    )

    if last["close"] > previous_high:

        score += 2

        reasons.append("Close broke above previous five-candle high")

    elif last["close"] < previous_low:

        score -= 2

        reasons.append("Close broke below previous five-candle low")

    # 5. Reject strong opposition.

    if (

        last_direction < 0

        and score > 0

        and last["body_ratio"] >= 0.65

    ):

        return wait_signal(

            "Strong bearish candle conflicts with bullish setup.",

            len(clean),

        )

    if (

        last_direction > 0

        and score < 0

        and last["body_ratio"] >= 0.65

    ):

        return wait_signal(

            "Strong bullish candle conflicts with bearish setup.",

            len(clean),

        )

    # 6. Reject unusually small candles.

    typical_range = mean(

        bar["range"] for bar in bars[-14:-1]

    )

    if typical_range > 0 and last["range"] < typical_range * 0.35:

        return wait_signal(

            "Latest candle range is too small.",

            len(clean),

        )

    # 7. Final decision.

    if score >= 6:

        direction = "UP"

        action = "NEXT CANDLE BUY"

    elif score <= -6:

        direction = "DOWN"

        action = "NEXT CANDLE SELL"

    else:

        direction = "WAIT"

        action = "WAIT"

    # This is a rule score, not a win probability.

    strength = min(80, 50 + abs(score) * 4) if direction != "WAIT" else 50

    return {

        "direction": direction,

        "action": action,

        "confidence": int(strength),

        "score": int(score),

        "reasons": reasons,

        "source_candle_count": len(clean),

        "signal_candle_timestamp": clean[-1]["timestamp"],

        "ema5": round(ema5, 8),

        "ema10": round(ema10, 8),

        "ema20": round(ema20, 8),

    }
