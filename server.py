from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from datetime import datetime, timezone
import json
import os
import time

from engine import predict_next_candle

ROOT = Path(__file__).parent
TWELVE_DATA_URL = "https://api.twelvedata.com/time_series"
CACHE_TTL_SECONDS = 12
ALLOWED_INTERVALS = {"1min": "1min", "5min": "5min"}
ALLOWED_SYMBOLS = {"EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "USD/CAD", "USD/CHF", "NZD/USD", "EUR/GBP"}
_cache = {}


def normalize_time_series(payload):
    values = payload.get("values") or []
    candles = []
    for row in values:
        try:
            candles.append({
                "timestamp": str(row["datetime"]),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row.get("volume", 0) or 0),
            })
        except (KeyError, TypeError, ValueError):
            continue
    candles.sort(key=lambda x: x["timestamp"])
    return candles


def fetch_candles(symbol, interval):
    api_key = os.environ.get("TWELVE_DATA_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("TWELVE_DATA_API_KEY is not configured")
    cache_key = (symbol, interval)
    now = time.monotonic()
    cached = _cache.get(cache_key)
    if cached and now - cached["at"] < CACHE_TTL_SECONDS:
        return cached["data"]

    query = f"symbol={symbol}&interval={interval}&outputsize=120&timezone=UTC&apikey={api_key}"
    req = Request(f"{TWELVE_DATA_URL}?{query}", headers={"User-Agent": "FinorixAI/1.0"})
    try:
        with urlopen(req, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"Market data provider HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError("Market data provider connection failed") from exc

    if payload.get("status") == "error" or "values" not in payload:
        raise RuntimeError(payload.get("message", "Market data provider returned no candle data"))
    candles = normalize_time_series(payload)
    if not candles:
        raise RuntimeError("Market data provider returned an empty candle set")
    _cache[cache_key] = {"at": now, "data": candles}
    return candles


def split_closed_candles(candles, interval):
    minutes = 1 if interval == "1min" else 5
    current = datetime.now(timezone.utc)
    bucket_minute = (current.minute // minutes) * minutes
    current_bucket = current.replace(minute=bucket_minute, second=0, microsecond=0)
    closed = []
    for candle in candles:
        try:
            dt = datetime.strptime(candle["timestamp"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            closed.append(candle)
            continue
        if dt < current_bucket:
            closed.append(candle)
    return closed


def json_response(handler, status, payload):
    body = json.dumps(payload, separators=(",", ":")).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class Handler(SimpleHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/api/predict":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            result = predict_next_candle(payload.get("candles", []))
            json_response(self, 200, result)
        except Exception as exc:
            json_response(self, 400, {"error": str(exc)})

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            json_response(self, 200, {
                "status": "ok",
                "engine": "finorix-live",
                "market_data": "configured" if os.environ.get("TWELVE_DATA_API_KEY") else "missing_api_key",
            })
            return

        if parsed.path == "/api/candles":
            try:
                params = parse_qs(parsed.query)
                symbol = params.get("symbol", ["EUR/USD"])[0].upper()
                interval = params.get("interval", ["1min"])[0]
                if symbol not in ALLOWED_SYMBOLS:
                    raise ValueError("Unsupported live symbol. OTC symbols are not available from this feed.")
                if interval not in ALLOWED_INTERVALS:
                    raise ValueError("Unsupported interval")
                candles = fetch_candles(symbol, interval)
                closed = split_closed_candles(candles, interval)
                if len(closed) < 3:
                    raise RuntimeError("Waiting for enough closed candles")
                prediction = predict_next_candle(closed[-100:])
                json_response(self, 200, {
                    "source": "Twelve Data",
                    "symbol": symbol,
                    "interval": interval,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "candles": candles[-100:],
                    "closed_candles": closed[-100:],
                    "last_closed_timestamp": closed[-1]["timestamp"],
                    "prediction": prediction,
                })
            except Exception as exc:
                json_response(self, 503, {"error": str(exc)})
            return

        return super().do_GET()


if __name__ == "__main__":
    os.chdir(ROOT)
    port = int(os.environ.get("PORT", "8080"))
    print(f"Finorix AI running on http://0.0.0.0:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
