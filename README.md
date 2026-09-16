# Finorix AI — live market feed prototype

Mobile-friendly Finorix-style UI with a closed-candle, no-lookahead prediction engine and a live/near-live Twelve Data market feed.

## Live feed setup

1. Create a Twelve Data account and obtain an API key.
2. Set the environment variable:

```bash
export TWELVE_DATA_API_KEY="YOUR_API_KEY"
```

3. Run:

```bash
python server.py
```

Then open `http://localhost:8080`.

### Render

Add `TWELVE_DATA_API_KEY` under **Environment Variables** and deploy. Keep the key server-side; never put it in `index.html`.

## Supported live symbols

EUR/USD, GBP/USD, USD/JPY, AUD/USD, USD/CAD, USD/CHF, NZD/USD, EUR/GBP.

`USD/BDT (OTC)` and other QX OTC instruments are deliberately not mapped to the standard forex feed. OTC prices are broker-specific and need a QX-compatible data source to reproduce them accurately.

## Feed behavior

The browser refreshes the feed every 15 seconds. Prediction is generated only from completed candles; the currently forming candle is excluded from the signal calculation. Twelve Data documents WebSocket as the lower-latency option, while REST candle availability can lag after a candle close, so this version is a safe near-live REST implementation.

## Important

This is a market-data and probabilistic-analysis prototype, not a guaranteed next-candle predictor. Validate it with walk-forward/out-of-sample testing before using real money.
