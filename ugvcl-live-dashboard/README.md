# UGVCL Smart Meter Live Monitor

Real-time, graph-based dashboard built from your Grafana dashboards. Python standard library only
(no pip, no Node, no internet needed - the chart library is bundled in `static/vendor`).

## Run
    python run.py            # opens http://127.0.0.1:8050
    python run.py --demo     # simulated data, to see it work without Grafana
Options: `--port 8051`, `--host 0.0.0.0` (share on the office LAN; default is this PC only).
Needs Python 3.8+.

## Choose a date (new)
The bar under the title has **Previous day / Next day**, a date box, **Yesterday**, **Today**, a trend length (7, 14 or 30 days) and **Refresh**.
Pick any date up to a year back; tiles, charts and the table then show that date. The address bar keeps the date
(`?date=2026-10-20`), so a link can be shared on your LAN. Today refreshes by itself; past dates are loaded once and cached
(yesterday for 30 min, older for 6 h; **Refresh** forces a reload). Communication type, metering mode and the
attribute explorer always show the current device list.

## Totals (new)
Every chart has a **Total (all CLRs)** series, and the table has a Total column. Percent totals are total received / total expected.
If a CLR has no data the label says e.g. "Total (3 of 4 CLRs)". Click "Total" in a chart legend to hide it.

## Title
Change `app_title` and `app_subtitle` in `config.json`.

## What it shows
- **Data received today** - midnight, load profile, instant (today) and billing (this month), as % of expected meters, per CLR.
- **Communication** - RF vs GPRS meters, midnight not received today, midnight coverage for 7 days, load-profile meters per hour.
- **Device inventory** - total meters, metering mode, and an attribute explorer (any AttrName from AttrKey).
- A table with every number, and a health table showing which query failed, how long each took, and how old each result is.

## Communication check (new)
Paste meter serials (or load a .txt/.csv) and press **Check meters**. Each meter is looked up exactly like the Grafana
"Last Communication" dashboard (Search related folder): the last-communication attributes of the meter's CLR are read
and the latest date is taken. **Found = Communication. Not found = Non-communication.**
- Output: totals, communication rate, stacked bar per CLR, last-communication age chart, a results table, CSV download.
- The table says why a meter is non-communication: *serial not in any CLR* (often a typo) or *meter exists but has no last-communication value*.
- If a CLR query fails, meters not found elsewhere are shown as **Unknown**, never as non-communication.
- Attribute keys per CLR are in `config.json` (`last_comm_attr_keys`). Up to 20,000 serials per check.

## How it gets data
The server calls Grafana's `POST /api/ds/query` with the same datasource UIDs and SQL logic your dashboards use
(SQL Server = device master, PostgreSQL = measurement results, one pair per CLR). Browsers only talk to this
server; it refreshes in the background (60 s for "today", up to 10 min for inventory), so more viewers do not mean more DB load.
All read-only SELECTs. If a query fails, the last good value stays on screen and the failure is shown.

## Config (`config.json`)
- `grafana_url`, `verify_tls` (false because of the internal certificate, as in your `app.py`).
- Authentication: if Grafana asks for login, set env `GRAFANA_TOKEN` (service-account token) or `GRAFANA_BASIC_AUTH=user:password`.
  Prefer the environment variable over writing it in the file.
- Per CLR: datasource UIDs, result-type IDs (midnight/billing/load_profile/instant) and the metering-mode attribute key.
  When a CLR is added or an ID changes, edit only this file.
- `refresh_seconds` per section.

## Layout
    run.py                  launcher
    config.json             everything environment-specific
    dashboard/queries.py    all SQL
    dashboard/grafana.py    /api/ds/query client
    dashboard/service.py    background refresh + numbers shown
    dashboard/server.py     small web server
    dashboard/demo_grafana.py  fake Grafana for --demo and tests
    static/                 the web page
    tests/                  python tests/test_dashboard.py

## Definitions
See "How each number is calculated" at the bottom of the page.

## If numbers are missing or a query times out
Run `python diagnose.py` (or `python diagnose.py CLR1 midnight`). It runs each query once per CLR, prints the time it took
and the exact error. Every profile/CLR is a separate request, so one slow query only affects its own tile.
`request_timeout_seconds` (default 240) and `max_parallel_queries` (default 4) are in config.json.
