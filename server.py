import os

import json

import time

import math

import threading

import urllib.parse

import urllib.request

from datetime import datetime, timezone

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import websocket

# ============================================================

# FINORIX AI — LIVE CURRENT CANDLE

# ============================================================

API_KEY = os.getenv("TWELVE_DATA_API_KEY", "").strip()

HOST = "0.0.0.0"

PORT = int(os.getenv("PORT", "10000"))

WS_URL = "wss://ws.twelvedata.com/v1/quotes/price"

REAL_SYMBOLS = [

    "EUR/USD",

    "GBP/USD",

    "USD/JPY",

    "AUD/USD",

    "USD/CAD",

    "USD/CHF",

    "NZD/USD",

    "EUR/GBP",

]

DEFAULT_SYMBOL = "EUR/USD"

state_lock = threading.Lock()

state = {

    "symbol": DEFAULT_SYMBOL,

    "price": None,

    "current": None,

    "previous": None,

    "history": [],

    "prediction": None,

    "ws_connected": False,

    "last_tick": None,

    "error": None,

}

# ============================================================

# TIME

# ============================================================

def now_utc():

    return datetime.now(timezone.utc)

def minute_start(ts):

    return ts - (ts % 60)

def format_time_utc6(ts):

    if ts is None:

        return "--"

    dt = datetime.fromtimestamp(ts, timezone.utc)

    # Bangladesh UTC+6 display

    from datetime import timedelta

    dt = dt + timedelta(hours=6)

    return dt.strftime("%Y-%m-%d %H:%M:%S")

def format_time_utc(ts):

    if ts is None:

        return "--"

    dt = datetime.fromtimestamp(ts, timezone.utc)

    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")

# ============================================================

# TWELVE DATA REST

# Used only for historical/previous candle + EMA context.

# Current candle comes from WebSocket ticks.

# ============================================================

def fetch_history(symbol):

    if not API_KEY:

        return []

    params = urllib.parse.urlencode({

        "symbol": symbol,

        "interval": "1min",

        "outputsize": 120,

        "timezone": "UTC",

        "apikey": API_KEY,

    })

    url = "https://api.twelvedata.com/time_series?" + params

    try:

        req = urllib.request.Request(

            url,

            headers={"User-Agent": "FinorixAI/1.0"}

        )

        with urllib.request.urlopen(req, timeout=15) as response:

            data = json.loads(response.read().decode("utf-8"))

        if data.get("status") == "error":

            raise RuntimeError(data.get("message", "Twelve Data error"))

        rows = data.get("values", [])

        candles = []

        for row in rows:

            try:

                dt = datetime.strptime(

                    row["datetime"],

                    "%Y-%m-%d %H:%M:%S"

                ).replace(tzinfo=timezone.utc)

                candles.append({

                    "timestamp": int(dt.timestamp()),

                    "open": float(row["open"]),

                    "high": float(row["high"]),

                    "low": float(row["low"]),

                    "close": float(row["close"]),

                })

            except Exception:

                continue

        candles.sort(key=lambda x: x["timestamp"])

        return candles

    except Exception as e:

        with state_lock:

            state["error"] = str(e)

        return []

# ============================================================

# EMA

# ============================================================

def ema(values, period):

    if not values:

        return None

    if len(values) < period:

        period = len(values)

    if period <= 0:

        return None

    multiplier = 2 / (period + 1)

    result = sum(values[:period]) / period

    for value in values[period:]:

        result = ((value - result) * multiplier) + result

    return result

# ============================================================

# CANDLE HELPERS

# ============================================================

def candle_body(c):

    return abs(c["close"] - c["open"])

def candle_range(c):

    return max(c["high"] - c["low"], 1e-12)

def candle_direction(c):

    if c["close"] > c["open"]:

        return 1

    if c["close"] < c["open"]:

        return -1

    return 0

def body_strength(c):

    return candle_body(c) / candle_range(c)

def upper_wick(c):

    return c["high"] - max(c["open"], c["close"])

def lower_wick(c):

    return min(c["open"], c["close"]) - c["low"]

# ============================================================

# LIVE CANDLE CREATION

# ============================================================

def create_live_candle(ts, price):

    start = minute_start(ts)

    return {

        "timestamp": start,

        "open": price,

        "high": price,

        "low": price,

        "close": price,

    }

def update_live_candle(ts, price):

    with state_lock:

        current = state["current"]

        if current is None:

            state["current"] = create_live_candle(ts, price)

            state["price"] = price

            state["last_tick"] = ts

            return

        current_start = current["timestamp"]

        incoming_start = minute_start(ts)

        # New minute started

        if incoming_start > current_start:

            state["previous"] = current.copy()

            # Save completed candle

            state["history"].append(current.copy())

            if len(state["history"]) > 150:

                state["history"] = state["history"][-150:]

            state["current"] = create_live_candle(ts, price)

        # Ignore old tick

        elif incoming_start < current_start:

            return

        else:

            current["high"] = max(current["high"], price)

            current["low"] = min(current["low"], price)

            current["close"] = price

        state["price"] = price

        state["last_tick"] = ts

# ============================================================

# PREDICTION ENGINE

# IMPORTANT:

# Prediction is ALWAYS for the NEXT candle.

# ============================================================

def analyze_current(symbol):

    with state_lock:

        current = state["current"].copy() if state["current"] else None

        previous = state["previous"].copy() if state["previous"] else None

        history = list(state["history"])

        price = state["price"]

    if not current or price is None:

        return {

            "direction": "WAIT",

            "confidence": 0,

            "action": "WAIT",

            "reason": "Waiting for live candle data."

        }

    # --------------------------------------------------------

    # Build close history for EMA

    # --------------------------------------------------------

    closes = [x["close"] for x in history]

    if previous:

        closes.append(previous["close"])

    closes.append(current["close"])

    ema5 = ema(closes, 5)

    ema10 = ema(closes, 10)

    ema20 = ema(closes, 20)

    # --------------------------------------------------------

    # Current candle measurements

    # --------------------------------------------------------

    body = candle_body(current)

    rng = candle_range(current)

    body_strength_value = body / rng

    uw = upper_wick(current)

    lw = lower_wick(current)

    position = (

        (price - current["low"]) /

        max(current["high"] - current["low"], 1e-12)

    )

    current_dir = candle_direction(current)

    score = 0

    reasons = []

    # --------------------------------------------------------

    # Current candle direction

    # --------------------------------------------------------

    if current_dir > 0:

        score += 2

        reasons.append("Current candle is bullish")

    elif current_dir < 0:

        score -= 2

        reasons.append("Current candle is bearish")

    # --------------------------------------------------------

    # Body strength

    # --------------------------------------------------------

    if body_strength_value >= 0.60:

        if current_dir > 0:

            score += 1

            reasons.append("Strong bullish body")

        elif current_dir < 0:

            score -= 1

            reasons.append("Strong bearish body")

    # --------------------------------------------------------

    # Current price position

    # --------------------------------------------------------

    if position >= 0.75:

        score += 1

        reasons.append("Price is near current high")

    elif position <= 0.25:

        score -= 1

        reasons.append("Price is near current low")

    # --------------------------------------------------------

    # Wick rejection

    # --------------------------------------------------------

    if uw > body * 1.2 and uw > lw:

        score -= 1

        reasons.append("Upper-wick rejection")

    if lw > body * 1.2 and lw > uw:

        score += 1

        reasons.append("Lower-wick rejection")

    # --------------------------------------------------------

    # Previous candle

    # --------------------------------------------------------

    if previous:

        prev_dir = candle_direction(previous)

        if prev_dir > 0:

            score += 1

            reasons.append("Previous candle bullish")

        elif prev_dir < 0:

            score -= 1

            reasons.append("Previous candle bearish")

        # Break previous high

        if price > previous["high"]:

            score += 2

            reasons.append("Live price broke previous high")

        # Break previous low

        elif price < previous["low"]:

            score -= 2

            reasons.append("Live price broke previous low")

    # --------------------------------------------------------

    # Short momentum

    # --------------------------------------------------------

    if len(closes) >= 4:

        recent = closes[-4:]

        up_count = sum(

            1 for i in range(1, len(recent))

            if recent[i] > recent[i - 1]

        )

        down_count = sum(

            1 for i in range(1, len(recent))

            if recent[i] < recent[i - 1]

        )

        if up_count >= 2 and down_count == 0:

            score += 2

            reasons.append("Short momentum bullish")

        elif down_count >= 2 and up_count == 0:

            score -= 2

            reasons.append("Short momentum bearish")

    # --------------------------------------------------------

    # EMA structure

    # --------------------------------------------------------

    if ema5 is not None and ema10 is not None:

        if price > ema5 > ema10:

            score += 2

            reasons.append("Bullish EMA structure")

        elif price < ema5 < ema10:

            score -= 2

            reasons.append("Bearish EMA structure")

    # EMA20 extra context

    if ema20 is not None:

        if price > ema20:

            score += 1

            reasons.append("Price above EMA20")

        elif price < ema20:

            score -= 1

            reasons.append("Price below EMA20")

    # --------------------------------------------------------

    # FINAL QUALITY FILTER

    # --------------------------------------------------------

    if score >= 7:

        direction = "UP"

    elif score <= -7:

        direction = "DOWN"

    else:

        direction = "WAIT"

    confidence = min(92, 50 + abs(score) * 5)

    if direction == "WAIT":

        confidence = min(confidence, 64)

    action = {

        "UP": "NEXT CANDLE BUY",

        "DOWN": "NEXT CANDLE SELL",

        "WAIT": "WAIT"

    }[direction]

    return {

        "direction": direction,

        "confidence": confidence,

        "action": action,

        "score": score,

        "reasons": reasons[-8:],

        "current": current,

        "previous": previous,

        "ema5": ema5,

        "ema10": ema10,

        "ema20": ema20,

        "body_strength": round(body_strength_value, 4),

        "price_position": round(position, 4),

        "upper_wick": uw,

        "lower_wick": lw,

    }

# ============================================================

# WEBSOCKET

# ============================================================

def websocket_url():

    return WS_URL + "?apikey=" + urllib.parse.quote(API_KEY)

def subscribe(ws):

    message = {

        "action": "subscribe",

        "params": {

            "symbols": ",".join(REAL_SYMBOLS)

        }

    }

    ws.send(json.dumps(message))

def websocket_worker():

    while True:

        if not API_KEY:

            with state_lock:

                state["ws_connected"] = False

                state["error"] = "TWELVE_DATA_API_KEY is missing."

            time.sleep(10)

            continue

        try:

            def on_open(ws):

                with state_lock:

                    state["ws_connected"] = True

                    state["error"] = None

                subscribe(ws)

            def on_message(ws, message):

                try:

                    data = json.loads(message)

                except Exception:

                    return

                if data.get("event") != "price":

                    return

                symbol = data.get("symbol")

                price_value = data.get("price")

                timestamp = data.get("timestamp")

                if symbol not in REAL_SYMBOLS:

                    return

                try:

                    price = float(price_value)

                except Exception:

                    return

                try:

                    ts = float(timestamp)

                except Exception:

                    ts = time.time()

                # Only update currently selected symbol

                with state_lock:

                    selected = state["symbol"]

                if symbol != selected:

                    return

                update_live_candle(ts, price)

            def on_error(ws, error):

                with state_lock:

                    state["ws_connected"] = False

                    state["error"] = str(error)

            def on_close(ws, close_status_code, close_msg):

                with state_lock:

                    state["ws_connected"] = False

            ws = websocket.WebSocketApp(

                websocket_url(),

                on_open=on_open,

                on_message=on_message,

                on_error=on_error,

                on_close=on_close,

            )

            ws.run_forever(

                ping_interval=20,

                ping_timeout=10

            )

        except Exception as e:

            with state_lock:

                state["ws_connected"] = False

                state["error"] = str(e)

        time.sleep(5)

# ============================================================

# LOAD HISTORICAL DATA

# ============================================================

def load_symbol(symbol):

    candles = fetch_history(symbol)

    if not candles:

        return

    now_ts = time.time()

    current_start = minute_start(now_ts)

    previous = None

    current_seed = None

    history = []

    for candle in candles:

        if candle["timestamp"] < current_start:

            history.append(candle)

            if (

                previous is None or

                candle["timestamp"] > previous["timestamp"]

            ):

                previous = candle

        elif candle["timestamp"] == current_start:

            current_seed = candle

    # Keep historical candles

    history = history[-150:]

    with state_lock:

        state["symbol"] = symbol

        state["history"] = history

        state["previous"] = previous

        # If REST already has current candle,

        # use it only as initial seed until live ticks arrive.

        if current_seed:

            state["current"] = current_seed.copy()

            state["price"] = current_seed["close"]

        else:

            state["current"] = None

            state["price"] = None

        state["prediction"] = None

# ============================================================

# API

# ============================================================

def api_response():

    with state_lock:

        symbol = state["symbol"]

        current = state["current"].copy() if state["current"] else None

        previous = state["previous"].copy() if state["previous"] else None

        price = state["price"]

        connected = state["ws_connected"]

        last_tick = state["last_tick"]

        error = state["error"]

    prediction = analyze_current(symbol)

    now_ts = time.time()

    if current:

        candle_end = current["timestamp"] + 60

        remaining = max(0, int(candle_end - now_ts))

    else:

        candle_end = None

        remaining = None

    return {

        "status": "ok",

        "mode": "CURRENT-CANDLE LIVE",

        "symbol": symbol,

        "market": "REAL FOREX",

        "time": {

            "utc": format_time_utc(now_ts),

            "utc6": format_time_utc6(now_ts),

            "current_candle": (

                format_time_utc6(current["timestamp"])

                if current else "--"

            ),

            "candle_end": (

                format_time_utc6(candle_end)

                if candle_end else "--"

            ),

            "seconds_remaining": remaining,

        },

        "websocket": {

            "connected": connected,

            "last_tick": (

                format_time_utc(last_tick)

                if last_tick else "--"

            ),

        },

        "current_candle": current,

        "previous_candle": previous,

        "price": price,

        "prediction": prediction,

        "error": error,

    }

# ============================================================

# WEB PAGE

# ============================================================

HTML = r"""

<!DOCTYPE html>

<html>

<head>

<meta name="viewport" content="width=device-width, initial-scale=1">

<title>FINORIX AI</title>

<style>

body {

    margin: 0;

    background: #07111f;

    color: white;

    font-family: Arial, sans-serif;

}

.container {

    max-width: 650px;

    margin: auto;

    padding: 18px;

}

.card {

    background: #0d1b2d;

    border: 1px solid #20344e;

    border-radius: 16px;

    padding: 18px;

    margin-bottom: 14px;

}

.title {

    font-size: 28px;

    font-weight: bold;

}

.mode {

    margin-top: 7px;

    color: #55d6ff;

    font-weight: bold;

}

.row {

    display: flex;

    justify-content: space-between;

    padding: 8px 0;

    border-bottom: 1px solid #1b2d43;

}

.big {

    font-size: 42px;

    font-weight: bold;

    text-align: center;

    margin: 18px 0;

}

.up {

    color: #39e58c;

}

.down {

    color: #ff6577;

}

.wait {

    color: #ffc857;

}

button, select {

    width: 100%;

    padding: 13px;

    border-radius: 10px;

    border: 0;

    margin-top: 8px;

    font-size: 16px;

}

button {

    background: #1d8cff;

    color: white;

    font-weight: bold;

}

.status {

    text-align: center;

    margin-top: 10px;

}

.reason {

    margin: 7px 0;

    padding: 9px;

    background: #102238;

    border-radius: 8px;

}

</style>

</head>

<body>

<div class="container">

    <div class="card">

        <div class="title">FINORIX AI</div>

        <div class="mode">MODE: CURRENT-CANDLE LIVE</div>

        <div>REAL FOREX MARKET — NO OTC</div>

        <select id="symbol">

            <option>EUR/USD</option>

            <option>GBP/USD</option>

            <option>USD/JPY</option>

            <option>AUD/USD</option>

            <option>USD/CAD</option>

            <option>USD/CHF</option>

            <option>NZD/USD</option>

            <option>EUR/GBP</option>

        </select>

        <button onclick="changeSymbol()">CHANGE MARKET</button>

    </div>

    <div class="card">

        <div class="row">

            <span>Market</span>

            <b id="market">--</b>

        </div>

        <div class="row">

            <span>Market Time UTC+6</span>

            <b id="time">--</b>

        </div>

        <div class="row">

            <span>Current Candle</span>

            <b id="candle">--</b>

        </div>

        <div class="row">

            <span>Time Remaining</span>

            <b id="remaining">--</b>

        </div>

        <div class="row">

            <span>Live Price</span>

            <b id="price">--</b>

        </div>

        <div class="status" id="connection">Connecting...</div>

    </div>

    <div class="card">

        <div style="text-align:center">

            NEXT CANDLE PREDICTION

        </div>

        <div id="prediction" class="big wait">

            WAIT

        </div>

        <div class="row">

            <span>Confidence</span>

            <b id="confidence">--</b>

        </div>

        <div class="row">

            <span>Score</span>

            <b id="score">--</b>

        </div>

        <div class="row">

            <span>Action</span>

            <b id="action">WAIT</b>

        </div>

    </div>

    <div class="card">

        <b>Current Candle</b>

        <div class="row">

            <span>Open</span>

            <b id="open">--</b>

        </div>

        <div class="row">

            <span>High</span>

            <b id="high">--</b>

        </div>

        <div class="row">

            <span>Low</span>

            <b id="low">--</b>

        </div>

        <div class="row">

            <span>Close / Live Price</span>

            <b id="close">--</b>

        </div>

    </div>

    <div class="card">

        <b>Analysis</b>

        <div id="reasons"></div>

    </div>

</div>

<script>

async function loadData() {

    try {

        const response = await fetch("/api/candles");

        const data = await response.json();

        document.getElementById("market").innerText =

            data.symbol + " — REAL FOREX";

        document.getElementById("time").innerText =

            data.time.utc6;

        document.getElementById("candle").innerText =

            data.time.current_candle;

        document.getElementById("remaining").innerText =

            data.time.seconds_remaining === null

                ? "--"

                : data.time.seconds_remaining + " sec";

        document.getElementById("price").innerText =

            data.price === null

                ? "--"

                : Number(data.price).toFixed(5);

        const connected = data.websocket.connected;

        document.getElementById("connection").innerText =

            connected

                ? "🟢 LIVE PRICE CONNECTED"

                : "🔴 LIVE PRICE DISCONNECTED";

        const p = data.prediction;

        const prediction = document.getElementById("prediction");

        prediction.innerText = p.direction;

        prediction.className = "big " +

            (

                p.direction === "UP"

                    ? "up"

                    : p.direction === "DOWN"

                        ? "down"

                        : "wait"

            );

        document.getElementById("confidence").innerText =

            p.confidence + "%";

        document.getElementById("score").innerText =

            p.score ?? "--";

        document.getElementById("action").innerText =

            p.action;

        const c = data.current_candle;

        if (c) {

            document.getElementById("open").innerText =

                Number(c.open).toFixed(5);

            document.getElementById("high").innerText =

                Number(c.high).toFixed(5);

            document.getElementById("low").innerText =

                Number(c.low).toFixed(5);

            document.getElementById("close").innerText =

                Number(c.close).toFixed(5);

        }

        const reasons =

            document.getElementById("reasons");

        reasons.innerHTML = "";

        if (p.reasons) {

            p.reasons.forEach(function(reason) {

                const div = document.createElement("div");

                div.className = "reason";

                div.innerText = "• " + reason;

                reasons.appendChild(div);

            });

        }

        document.getElementById("symbol").value =

            data.symbol;

    }

    catch (error) {

        document.getElementById("connection").innerText =

            "🔴 API ERROR";

    }

}

async function changeSymbol() {

    const symbol =

        document.getElementById("symbol").value;

    await fetch(

        "/api/set-symbol?symbol=" +

        encodeURIComponent(symbol)

    );

    loadData();

}

loadData();

setInterval(loadData, 1000);

</script>

</body>

</html>

"""

# ============================================================

# HTTP SERVER

# ============================================================

class Handler(BaseHTTPRequestHandler):

    def send_json(self, data):

        body = json.dumps(

            data,

            ensure_ascii=False

        ).encode("utf-8")

        self.send_response(200)

        self.send_header(

            "Content-Type",

            "application/json; charset=utf-8"

        )

        self.send_header(

            "Content-Length",

            str(len(body))

        )

        self.send_header(

            "Cache-Control",

            "no-store"

        )

        self.end_headers()

        self.wfile.write(body)

    def do_GET(self):

        parsed = urllib.parse.urlparse(

            self.path

        )

        path = parsed.path

        query = urllib.parse.parse_qs(

            parsed.query

        )

        if path == "/":

            body = HTML.encode("utf-8")

            self.send_response(200)

            self.send_header(

                "Content-Type",

                "text/html; charset=utf-8"

            )

            self.send_header(

                "Content-Length",

                str(len(body))

            )

            self.end_headers()

            self.wfile.write(body)

            return

        if path == "/api/candles":

            self.send_json(api_response())

            return

        if path == "/api/health":

            with state_lock:

                connected = state["ws_connected"]

            self.send_json({

                "status": "ok",

                "mode": "CURRENT-CANDLE LIVE",

                "websocket": connected

            })

            return

        if path == "/api/set-symbol":

            requested = query.get(

                "symbol",

                [DEFAULT_SYMBOL]

            )[0]

            if requested not in REAL_SYMBOLS:

                self.send_json({

                    "status": "error",

                    "message": "Symbol not allowed."

                })

                return

            load_symbol(requested)

            self.send_json({

                "status": "ok",

                "symbol": requested

            })

            return

        self.send_response(404)

        self.end_headers()

# ============================================================

# START

# ============================================================

def main():

    if not API_KEY:

        print(

            "WARNING: TWELVE_DATA_API_KEY is not configured."

        )

    # Seed historical data

    load_symbol(DEFAULT_SYMBOL)

    # Start live WebSocket

    thread = threading.Thread(

        target=websocket_worker,

        daemon=True

    )

    thread.start()

    # Start web server

    server = ThreadingHTTPServer(

        (HOST, PORT),

        Handler

    )

    print(

        f"FINORIX AI running on port {PORT}"

    )

    print(

        "MODE: CURRENT-CANDLE LIVE"

    )

    print(

        "MARKET: REAL FOREX"

    )

    server.serve_forever()

if __name__ == "__main__":

    main()
