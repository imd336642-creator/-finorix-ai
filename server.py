"""FINORIX PRO — live/near-live read-only market scanner.

Providers:
- OTCharts Quotex venue for OTC symbols (OTCHARTS_API_KEY)
- Twelve Data for real forex symbols (TWELVE_DATA_API_KEY)

The app never places trades. Signals are calculated from completed candles only.
"""
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from engine import predict_next_candle

HOST = "0.0.0.0"
PORT = int(os.getenv("PORT", "10000"))
TWELVE_KEY = os.getenv("TWELVE_DATA_API_KEY", "").strip()
OTCHARTS_KEY = os.getenv("OTCHARTS_API_KEY", "").strip()
BASE_DIR = Path(__file__).resolve().parent

REAL_SYMBOLS = ["EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "USD/CAD", "USD/CHF", "NZD/USD", "EUR/GBP"]
OTC_SYMBOLS = {
    "EUR/USD (OTC)": "EURUSD_otc",
    "GBP/USD (OTC)": "GBPUSD_otc",
    "USD/JPY (OTC)": "USDJPY_otc",
    "AUD/USD (OTC)": "AUDUSD_otc",
    "BTC/USD (OTC)": "BTCUSD_otc",
}
TIMEFRAMES = {"1min": 60, "5min": 300}

lock = threading.RLock()
state = {
    "mode": "otc" if OTCHARTS_KEY else "real",
    "symbol": "EUR/USD (OTC)" if OTCHARTS_KEY else "EUR/USD",
    "interval": "1min",
    "candles": [],
    "live_price": None,
    "last_tick": None,
    "last_refresh": 0,
    "source": "Starting",
    "error": None,
    "otc_stream_connected": False,
    "real_ws_connected": False,
}
real_ws = None


def utc_string(ts):
    if ts is None:
        return "--"
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def local_string(ts):
    if ts is None:
        return "--"
    # Bangladesh time (UTC+6) as requested by the original prototype.
    return (datetime.fromtimestamp(float(ts), timezone.utc) + timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")


def http_json(url, headers=None, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "FinorixPro/1.0", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def normalize_candle(row, timestamp_key="timestamp"):
    ts = row.get(timestamp_key, row.get("time", row.get("datetime", 0)))
    if isinstance(ts, str):
        try:
            ts = int(datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            ts = int(float(ts))
    return {
        "timestamp": int(float(ts)),
        "open": float(row["open"]), "high": float(row["high"]),
        "low": float(row["low"]), "close": float(row["close"]),
    }


def fetch_twelve_candles(symbol, interval, limit=100):
    if not TWELVE_KEY:
        raise RuntimeError("TWELVE_DATA_API_KEY is missing in Render Environment Variables.")
    params = urllib.parse.urlencode({
        "symbol": symbol, "interval": interval, "outputsize": limit,
        "timezone": "UTC", "apikey": TWELVE_KEY,
    })
    data = http_json("https://api.twelvedata.com/time_series?" + params)
    if data.get("status") == "error" or "values" not in data:
        raise RuntimeError(data.get("message", "Twelve Data returned no candle values."))
    candles = []
    for row in data["values"]:
        try:
            candles.append(normalize_candle(row, "datetime"))
        except (ValueError, TypeError, KeyError):
            continue
    return sorted(candles, key=lambda x: x["timestamp"])[-limit:]


def fetch_otc_candles(symbol, interval, limit=100):
    if not OTCHARTS_KEY:
        raise RuntimeError("OTCHARTS_API_KEY is missing in Render Environment Variables.")
    otc_id = OTC_SYMBOLS.get(symbol)
    if not otc_id:
        raise RuntimeError("This OTC symbol is not mapped. Choose a listed OTC symbol.")
    tf = TIMEFRAMES[interval]
    params = urllib.parse.urlencode({"venue": "quotex", "symbol": otc_id, "tf": tf, "limit": limit})
    data = http_json("https://otcharts.com/v1/candles?" + params, {"Authorization": "Bearer " + OTCHARTS_KEY})
    if "candles" not in data:
        raise RuntimeError(data.get("error", "OTCharts returned no candles."))
    candles = []
    for row in data["candles"]:
        try:
            candles.append(normalize_candle(row, "time"))
        except (ValueError, TypeError, KeyError):
            continue
    return sorted(candles, key=lambda x: x["timestamp"])[-limit:]


def fetch_candles(mode, symbol, interval):
    if mode == "otc":
        return fetch_otc_candles(symbol, interval)
    return fetch_twelve_candles(symbol, interval)


def current_interval_start(ts, interval):
    size = TIMEFRAMES[interval]
    return int(ts // size * size)


def refresh_history(force=False):
    with lock:
        mode, symbol, interval = state["mode"], state["symbol"], state["interval"]
        last_refresh = state["last_refresh"]
    # Avoid burning API quota: refresh historical bars at most every 30 seconds,
    # while browser polling itself does not trigger provider requests each time.
    if not force and time.time() - last_refresh < 30:
        return
    try:
        candles = fetch_candles(mode, symbol, interval)
        now = time.time()
        # If the provider includes the currently forming bar, it is displayed
        # but excluded from prediction. If the latest bar is older, it is closed.
        with lock:
            if (state["mode"], state["symbol"], state["interval"]) != (mode, symbol, interval):
                return
            state["candles"] = candles
            state["last_refresh"] = now
            state["source"] = "OTCharts · Quotex OTC candles" if mode == "otc" else "Twelve Data · real forex candles"
            state["error"] = None
    except Exception as exc:
        with lock:
            state["error"] = str(exc)
            state["last_refresh"] = time.time()
            state["source"] = "Feed error"


def selected_tick_price(symbol, price, ts, mode):
    with lock:
        if mode != state["mode"] or symbol != state["symbol"]:
            return
        interval = state["interval"]
        candles = list(state["candles"])
    start = current_interval_start(ts, interval)
    if not candles:
        return
    with lock:
        # Only mutate a bar if its timestamp matches the tick's interval;
        # otherwise leave history intact and let the next REST refresh add bars.
        if candles[-1]["timestamp"] == start:
            c = state["candles"][-1]
            c["high"] = max(c["high"], price)
            c["low"] = min(c["low"], price)
            c["close"] = price
        elif candles[-1]["timestamp"] < start:
            state["candles"].append({"timestamp": start, "open": price, "high": price, "low": price, "close": price})
            state["candles"] = state["candles"][-150:]
        state["live_price"] = price
        state["last_tick"] = ts


def otc_stream_worker():
    """Connect to OTCharts SSE stream. If plan does not permit streaming, REST still works."""
    import urllib.error
    while True:
        with lock:
            mode, symbol = state["mode"], state["symbol"]
            key = OTCHARTS_KEY
            otc_id = OTC_SYMBOLS.get(symbol)
        if mode != "otc" or not key or not otc_id:
            time.sleep(2)
            continue
        url = "https://otcharts.com/v1/stream?" + urllib.parse.urlencode({"venue": "quotex", "symbol": otc_id})
        try:
            req = urllib.request.Request(url, headers={
                "Authorization": "Bearer " + key,
                "Accept": "text/event-stream",
                "User-Agent": "FinorixPro/1.0",
            })
            with urllib.request.urlopen(req, timeout=35) as response:
                with lock:
                    state["otc_stream_connected"] = True
                event_name = ""
                for raw in response:
                    if not raw:
                        continue
                    line = raw.decode("utf-8", errors="ignore").strip()
                    if line.startswith("event:"):
                        event_name = line[6:].strip()
                    elif line.startswith("data:"):
                        try:
                            data = json.loads(line[5:].strip())
                        except json.JSONDecodeError:
                            continue
                        if event_name == "tick" or ("price" in data and "symbol" in data):
                            try:
                                selected_tick_price(symbol, float(data["price"]), float(data.get("time", time.time())), "otc")
                            except (ValueError, TypeError, KeyError):
                                pass
                    elif not line:
                        event_name = ""
        except Exception as exc:
            with lock:
                state["otc_stream_connected"] = False
                # Keep a clear but nonfatal stream note; REST history can still work.
                if state["mode"] == "otc":
                    state["error"] = f"OTCharts stream unavailable; REST candles remain enabled ({exc})"
        finally:
            with lock:
                state["otc_stream_connected"] = False
        time.sleep(5)


def real_websocket_worker():
    """Twelve Data websocket for real FX ticks, when key/plan supports it."""
    try:
        import websocket
    except ImportError:
        return
    while True:
        if not TWELVE_KEY:
            time.sleep(10)
            continue
        try:
            url = "wss://ws.twelvedata.com/v1/quotes/price?apikey=" + urllib.parse.quote(TWELVE_KEY)
            def on_open(ws):
                with lock:
                    state["real_ws_connected"] = True
                ws.send(json.dumps({"action": "subscribe", "params": {"symbols": ",".join(REAL_SYMBOLS)}}))
            def on_message(ws, message):
                try:
                    data = json.loads(message)
                    if data.get("event") != "price":
                        return
                    symbol = data.get("symbol")
                    if symbol not in REAL_SYMBOLS:
                        return
                    price = float(data["price"])
                    ts = float(data.get("timestamp", time.time()))
                    selected_tick_price(symbol, price, ts, "real")
                except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                    return
            def on_error(ws, error):
                with lock:
                    state["real_ws_connected"] = False
            def on_close(ws, *args):
                with lock:
                    state["real_ws_connected"] = False
            ws = websocket.WebSocketApp(url, on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close)
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception:
            with lock:
                state["real_ws_connected"] = False
        time.sleep(5)


def api_response():
    refresh_history(False)
    with lock:
        mode, symbol, interval = state["mode"], state["symbol"], state["interval"]
        candles = [c.copy() for c in state["candles"]]
        live_price = state["live_price"]
        last_tick = state["last_tick"]
        error = state["error"]
        source = state["source"]
        ws_connected = state["otc_stream_connected"] if mode == "otc" else state["real_ws_connected"]
        last_refresh = state["last_refresh"]

    now = time.time()
    frame = TIMEFRAMES[interval]
    this_start = current_interval_start(now, interval)
    # Merge a recent live price into the currently forming bar only.
    if live_price is not None and last_tick is not None and now - last_tick < 120 and candles:
        if candles[-1]["timestamp"] == this_start:
            candles[-1]["high"] = max(candles[-1]["high"], live_price)
            candles[-1]["low"] = min(candles[-1]["low"], live_price)
            candles[-1]["close"] = live_price

    # Exclude the current timeframe candle from prediction if present.
    completed = [c for c in candles if c["timestamp"] + frame <= now]
    prediction = predict_next_candle(completed)
    current = next((c for c in reversed(candles) if c["timestamp"] == this_start), None)
    if current is None and candles:
        current = candles[-1] if candles[-1]["timestamp"] + frame > now else None

    return {
        "status": "ok",
        "mode": "QUOTEX OTC" if mode == "otc" else "REAL FOREX",
        "market": "OTC" if mode == "otc" else "REAL FOREX",
        "symbol": symbol,
        "interval": interval,
        "timeframe_seconds": frame,
        "source": source,
        "candles": candles[-80:],
        "current_candle": current,
        "last_closed_timestamp": completed[-1]["timestamp"] if completed else None,
        "prediction": prediction,
        "price": live_price if live_price is not None else (current["close"] if current else None),
        "time": {
            "utc": utc_string(now),
            "utc6": local_string(now),
            "current_candle": local_string(current["timestamp"]) if current else "--",
            "seconds_remaining": max(0, int((this_start + frame) - now)),
        },
        "websocket": {"connected": bool(ws_connected), "last_tick": utc_string(last_tick)},
        "last_refresh": last_refresh,
        "error": error,
        "note": "Signal is for the candle after the last completed candle. Strength is not a calibrated win probability.",
    }


class Handler(BaseHTTPRequestHandler):
    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
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
                body = b"index.html not found"
                self.send_response(500)
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/candles":
            # Accept optional browser query parameters and apply them safely.
            mode = query.get("mode", [None])[0]
            symbol = query.get("symbol", [None])[0]
            interval = query.get("interval", [None])[0]
            changed = False
            with lock:
                if mode in ("otc", "real") and mode != state["mode"]:
                    state["mode"] = mode
                    state["symbol"] = ("EUR/USD (OTC)" if mode == "otc" else "EUR/USD")
                    changed = True
                if symbol:
                    allowed = OTC_SYMBOLS if state["mode"] == "otc" else REAL_SYMBOLS
                    if symbol in allowed and symbol != state["symbol"]:
                        state["symbol"] = symbol
                        changed = True
                if interval in TIMEFRAMES and interval != state["interval"]:
                    state["interval"] = interval
                    changed = True
                if changed:
                    state["candles"] = []
                    state["live_price"] = None
                    state["last_tick"] = None
                    state["last_refresh"] = 0
                    state["error"] = None
            if changed:
                refresh_history(True)
            self.send_json(api_response())
            return

        if path == "/api/health":
            with lock:
                data = {
                    "status": "ok", "mode": state["mode"], "symbol": state["symbol"],
                    "source": state["source"], "error": state["error"],
                    "otc_stream_connected": state["otc_stream_connected"],
                    "real_ws_connected": state["real_ws_connected"],
                }
            self.send_json(data)
            return

        if path == "/api/set-market":
            mode = query.get("mode", ["real"])[0]
            symbol = query.get("symbol", ["EUR/USD"])[0]
            interval = query.get("interval", ["1min"])[0]
            if mode not in ("otc", "real") or interval not in TIMEFRAMES:
                self.send_json({"status": "error", "error": "Invalid market or timeframe."}, 400)
                return
            allowed = OTC_SYMBOLS if mode == "otc" else REAL_SYMBOLS
            if symbol not in allowed:
                self.send_json({"status": "error", "error": "Symbol is not supported for this market."}, 400)
                return
            with lock:
                state.update({"mode": mode, "symbol": symbol, "interval": interval,
                              "candles": [], "live_price": None, "last_tick": None,
                              "last_refresh": 0, "error": None})
            refresh_history(True)
            self.send_json({"status": "ok", "mode": mode, "symbol": symbol, "interval": interval})
            return

        self.send_json({"status": "error", "error": "Not found"}, 404)

    def log_message(self, fmt, *args):
        # Keep Render logs useful without noisy per-second request logging.
        if self.path.startswith("/api/health"):
            return
        super().log_message(fmt, *args)


def main():
    with lock:
        mode, symbol, interval = state["mode"], state["symbol"], state["interval"]
    try:
        refresh_history(True)
    except Exception:
        pass
    threading.Thread(target=otc_stream_worker, daemon=True).start()
    threading.Thread(target=real_websocket_worker, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"FINORIX PRO running on port {PORT}; initial market={mode} symbol={symbol} timeframe={interval}")
    print("Read-only market data. Signals are probabilistic and use completed candles.")
    server.serve_forever()


if __name__ == "__main__":
    main()
