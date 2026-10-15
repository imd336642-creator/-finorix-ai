# FINORIX PRO — corrected market scanner

## Files

- `server.py`: HTTP API, provider selection, Twelve Data real\-FX feed, OTCharts Quotex OTC feed\.
- `engine.py`: cautious signal scoring using completed candles\.
- `index.html`: mobile\-friendly chart and next\-candle signal UI\.
- `requirements.txt`: runtime dependency\.
- `README.md`: setup notes\.

## Render environment variables

Set these in Render → your service → Environment:

- `TWELVE_DATA_API_KEY` — required for real Forex\.
- `OTCHARTS_API_KEY` — required for Quotex OTC\.
  Keep keys only in server\-side environment variables; never paste them into HTML or public source code\.

The app defaults to OTC when `OTCHARTS_API_KEY` exists; otherwise it defaults to real Forex\. Use the market selector to switch\.

## Start command

`python server.py`

Python version: 3\.10\+ recommended\.

## Important feed behavior

- OTC candles come from OTCharts’ `venue=quotex` feed and use its documented symbols &#40;for example `EURUSD_otc`&#41;\.
- Real Forex candles come from Twelve Data\.
- The app tries OTCharts’ live SSE stream and Twelve Data’s WebSocket for tick updates\. If the plan/API does not allow a live stream, the chart still uses REST candles; live ticks may not be available\.
- REST candle history refresh is throttled to reduce API quota use\.
- `1min` and `5min` are supported by this UI\. The selected provider must offer that interval for the selected symbol\.
- Signals are generated from completed candles only and target the candle after the last completed candle\. This prevents a changing live candle from repeatedly repainting the signal\.
- The number called “Signal strength” is derived from a rule score; it is **not** a calibrated probability or guaranteed win rate\.
- OTCharts is an independent read\-only market\-data service, not an official Quotex API\. OTC quotes can differ from other feeds\. This app never places trades\.

## Deploy update

Upload/replace the four code/config files in the existing GitHub repository, commit the changes, then redeploy the same Render service\. Do not create a second service unless you want a separate deployment\. Check Render logs and `/api/health` if a feed does not connect\.
