"""FINORIX PRO - Read-only Forex candle scanner."""

import json

import os

import threading

import time

import urllib.parse

import urllib.request

from datetime import datetime, timezone

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pathlib import Path

from engine import predict_next_candle

HOST = "0.0.0.0"

PORT = int(os.getenv("PORT", "10000"))

TWELVE_KEY = os.getenv("TWELVE_DATA_API_KEY", "").strip()

OTCHARTS_KEY = os.getenv("OTCHARTS_API_KEY", "").strip()

BASE_DIR = Path(__file__).resolve().parent

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

OTC_SYMBOLS = [

    "EUR/USD (OTC)",

    "GBP/USD (OTC)",

    "USD/JPY (OTC)",

    "AUD/USD (OTC)",

]

TIMEFRAMES = {

    "1min": 60,

    "5min": 300,

}

lock = threading.RLock()

state = {

    "mode": "real",

    "symbol": "EUR/USD",

    "interval": "1min",

    "candles": [],

    "last_refresh": 0,

    "source": "Starting",

    "error": None,

}

def utc_string(timestamp):

    if timestamp is None:

        return "--"

    return datetime.fromtimestamp(

        float(timestamp), timezone.utc

    ).strftime("%Y-%m-%d %H:%M:%S UTC")

def http_json(url):

    request = urllib.request.Request(

        url,

        headers={"User-Agent": "FinorixPro/1.0"},

    )

    with urllib.request.urlopen(request, timeout=15) as response:

        return json.loads(response.read().decode("utf-8"))

def normalize_candle(row):

    raw_time = row.get("datetime", row.get("timestamp", row.get("time")))

    if raw_time is None:

        raise ValueError("Missing candle timestamp")

    if isinstance(raw_time, str) and not raw_time.isdigit():

        timestamp = datetime.strptime(

            raw_time, "%Y-%m-%d %H:%M:%S"

        ).replace(tzinfo=timezone.utc).timestamp()

    else:

        timestamp = float(raw_time)

    candle = {

        "timestamp": int(timestamp),

        "open": float(row["open"]),

        "high": float(row["high"]),

        "low": float(row["low"]),

        "close": float(row["close"]),

    }

    if (

        candle["high"] < max(candle["open"], candle["close"])

        or candle["low"] > min(candle["open"], candle["close"])

        or candle["high"] <= candle["low"]

    ):

        raise ValueError("Invalid candle OHLC values")

    return candle

def fetch_real_candles(symbol, interval, limit=100):

    if not TWELVE_KEY:

        raise RuntimeError(

            "TWELVE_DATA_API_KEY is missing in Render Environment Variables."

        )

    params = urllib.parse.urlencode({

        "symbol": symbol,

        "interval": interval,

        "outputsize": limit,

        "timezone": "UTC",

        "apikey": TWELVE_KEY,

    })

    url = "https://api.twelvedata.com/time_series?" + params

    data = http_json(url)

    if data.get("status") == "error" or "values" not in data:

        raise RuntimeError(

            data.get("message", "Twelve Data returned no candle values.")

        )

    candles = []

    for row in data["values"]:

        try:

            candles.append(normalize_candle(row))

        except (ValueError, TypeError, KeyError):

            continue

    candles.sort(key=lambda candle: candle["timestamp"])

    if len(candles) < 30:

        raise RuntimeError(

            f"Not enough valid candles received: {len(candles)}"

        )

    return candles[-limit:]

def fetch_candles(mode, symbol, interval):

    if mode == "otc":

        # Do not invent an endpoint or assume an unverified provider schema.

        if not OTCHARTS_KEY:

            raise RuntimeError(

                "OTCHARTS_API_KEY is missing in Render Environment Variables."

            )

        raise RuntimeError(

            "OTC feed is not enabled: the official OTCharts endpoint, "

            "authentication method, and candle response format must first "

            "be verified. No OTC signal is generated from real Forex data."

        )

    if symbol not in REAL_SYMBOLS:

        raise RuntimeError("Unsupported real Forex symbol.")

    return fetch_real_candles(symbol, interval)

def refresh_history(force=False):

    with lock:

        mode = state["mode"]

        symbol = state["symbol"]

        interval = state["interval"]

        last_refresh = state["last_refresh"]

    if not force and time.time() - last_refresh < 30:

        return

    try:

        candles = fetch_candles(mode, symbol, interval)

        with lock:

            # Prevent a delayed response from replacing a newly selected market.

            if (

                state["mode"],

                state["symbol"],

                state["interval"],

            ) != (mode, symbol, interval):

                return

            state["candles"] = candles

            state["last_refresh"] = time.time()

            state["source"] = (

                "Twelve Data · Real Forex"

                if mode == "real"

                else "OTC feed unavailable"

            )

            state["error"] = None

    except Exception as exc:

        with lock:

            if (

                state["mode"],

                state["symbol"],

                state["interval"],

            ) == (mode, symbol, interval):

                state["error"] = str(exc)

                state["last_refresh"] = time.time()

                state["source"] = "Feed error"

def build_state():

    refresh_history()

    with lock:

        mode = state["mode"]

        symbol = state["symbol"]

        interval = state["interval"]

        candles = [c.copy() for c in state["candles"]]

        last_refresh = state["last_refresh"]

        source = state["source"]

        error = state["error"]

    now = time.time()

    frame = TIMEFRAMES[interval]

    # Completed candles only.

    completed = [

        candle for candle in candles

        if candle["timestamp"] + frame <= now

    ]

    # Do not use stale data to generate a fresh signal.

    data_age = now - last_refresh if last_refresh else None

    data_is_fresh = data_age is not None and data_age <= 90

    if mode == "otc":

        prediction = {

            "direction": "WAIT",

            "action": "WAIT",

            "confidence": 50,

            "score": 0,

            "reasons": [

                "Verified OTC candle feed is not configured."

            ],

            "source_candle_count": 0,

        }

    elif error or not data_is_fresh:

        prediction = {

            "direction": "WAIT",

            "action": "WAIT",

            "confidence": 50,

            "score": 0,

            "reasons": [

                error or "Market data is stale; waiting for a fresh update."

            ],

            "source_candle_count": len(completed),

        }

    else:

        prediction = predict_next_candle(completed)

    current_start = int(now // frame * frame)

    current_candle = next(

        (

            candle for candle in reversed(candles)

            if candle["timestamp"] == current_start

        ),

        None,

    )

    seconds_remaining = max(

        0, int(current_start + frame - now)

    )

    return {

        "status": "error" if error else "ok",

        "mode": mode,

        "market": "OTC" if mode == "otc" else "REAL FOREX",

        "symbol": symbol,

        "interval": interval,

        "timeframe_seconds": frame,

        "source": source,

        "candles": candles[-80:],

        "current_candle": current_candle,

        "last_closed_timestamp": (

            completed[-1]["timestamp"] if completed else None

        ),

        "prediction": prediction,

        "price": (

            current_candle["close"] if current_candle else None

        ),

        "time": {

            "utc": utc_string(now),

            "seconds_remaining": seconds_remaining,

        },

        "last_refresh": last_refresh,

        "data_age_seconds": (

            round(data_age, 1) if data_age is not None else None

        ),

        "completed_candle_count": len(completed),

        "error": error,

        "note": (

            "Signals use completed candles only. "

            "Confidence is not a measured win probability."

        ),

    }

class Handler(BaseHTTPRequestHandler):

    def send_json(self, payload, status=200):

        body = json.dumps(

            payload, ensure_ascii=False

        ).encode("utf-8")

        self.send_response(status)

        self.send_header(

            "Content-Type", "application/json; charset=utf-8"

        )

        self.send_header("Content-Length", str(len(body)))

        self.send_header("Cache-Control", "no-store")

        self.end_headers()

        self.wfile.write(body)

    def do_GET(self):

        parsed = urllib.parse.urlparse(self.path)

        query = urllib.parse.parse_qs(parsed.query)

        path = parsed.path

        if path == "/":

            try:

                body = (BASE_DIR / "index.html").read_bytes()

            except OSError:

                self.send_json(

                    {"status": "error", "error": "index.html not found"},

                    500,

                )

                return

            self.send_response(200)

            self.send_header(

                "Content-Type", "text/html; charset=utf-8"

            )

            self.send_header("Content-Length", str(len(body)))

            self.send_header("Cache-Control", "no-store")

            self.end_headers()

            self.wfile.write(body)

            return

        if path in ("/api/candles", "/api/state", "/api/health"):

            mode = query.get("mode", [None])[0]

            symbol = query.get("symbol", [None])[0]

            interval = query.get("interval", [None])[0]

            changed = False

            with lock:

                if mode in ("real", "otc") and mode != state["mode"]:

                    state["mode"] = mode

                    state["symbol"] = (

                        "EUR/USD" if mode == "real"

                        else "EUR/USD (OTC)"

                    )

                    changed = True

                if interval in TIMEFRAMES and interval != state["interval"]:

                    state["interval"] = interval

                    changed = True

                allowed = REAL_SYMBOLS if state["mode"] == "real" else OTC_SYMBOLS

                if symbol in allowed and symbol != state["symbol"]:

                    state["symbol"] = symbol

                    changed = True

                if changed:

                    state["candles"] = []

                    state["last_refresh"] = 0

                    state["error"] = None

            if changed:

                refresh_history(force=True)

            if path == "/api/health":

                with lock:

                    self.send_json({

                        "status": "ok",

                        "mode": state["mode"],

                        "symbol": state["symbol"],

                        "source": state["source"],

                        "error": state["error"],

                    })

                return

            self.send_json(build_state())

            return

        if path == "/api/set-market":

            mode = query.get("mode", ["real"])[0]

            symbol = query.get(

                "symbol",

                ["EUR/USD" if mode == "real" else "EUR/USD (OTC)"],

            )[0]

            interval = query.get("interval", ["1min"])[0]

            if mode not in ("real", "otc") or interval not in TIMEFRAMES:

                self.send_json(

                    {"status": "error", "error": "Invalid market/timeframe"},

                    400,

                )

                return

            allowed = REAL_SYMBOLS if mode == "real" else OTC_SYMBOLS

            if symbol not in allowed:

                self.send_json(

                    {"status": "error", "error": "Unsupported symbol"},

                    400,

                )

                return

            with lock:

                state["mode"] = mode

                state["symbol"] = symbol

                state["interval"] = interval

                state["candles"] = []

                state["last_refresh"] = 0

                state["error"] = None

            refresh_history(force=True)

            self.send_json({

                "status": "ok",

                "mode": mode,

                "symbol": symbol,

                "interval": interval,

            })

            return

        self.send_json(

            {"status": "error", "error": "Not found"}, 404

        )

    def log_message(self, fmt, *args):

        super().log_message(fmt, *args)

def main():

    print(f"FINORIX PRO starting on port {PORT}")

    refresh_history(force=True)

    server = ThreadingHTTPServer((HOST, PORT), Handler)

    server.serve_forever()

if __name__ == "__main__":

    main()
