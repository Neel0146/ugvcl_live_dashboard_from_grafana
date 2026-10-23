#!/usr/bin/env python3
"""UGVCL live dashboard launcher.

  python run.py                 # use config.json, open http://127.0.0.1:8050
  python run.py --demo          # fake Grafana with simulated data (no network needed)
  python run.py --host 0.0.0.0  # share on the LAN (default is this PC only)
Standard library only - nothing to pip install.
"""
import argparse
import json
import os
import sys
import threading
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from dashboard.grafana import GrafanaClient  # noqa: E402
from dashboard.commcheck import CommChecker  # noqa: E402
from dashboard.server import serve  # noqa: E402
from dashboard.service import DashboardService  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8050)
    ap.add_argument("--demo", action="store_true", help="run against a built-in fake Grafana")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()

    with open(a.config, encoding="utf-8") as f:
        cfg = json.load(f)

    base_url, verify = cfg["grafana_url"], cfg.get("verify_tls", True)
    token = os.environ.get("GRAFANA_TOKEN") or cfg.get("api_token", "")
    basic = os.environ.get("GRAFANA_BASIC_AUTH", "")  # user:password
    if a.demo:
        from dashboard.demo_grafana import start_demo
        base_url, verify, token = start_demo(cfg), True, ""
        print("DEMO MODE: data is simulated, not from UGVCL.")

    client = GrafanaClient(base_url, token, basic, verify, cfg.get("request_timeout_seconds", 60))
    service = DashboardService(client, cfg)
    service.start()
    checker = CommChecker(client, cfg)
    httpd = serve(service, checker, a.host, a.port)
    url = "http://%s:%d" % ("127.0.0.1" if a.host in ("0.0.0.0", "") else a.host, a.port)
    print("Dashboard running at", url, "  (Ctrl+C to stop)")
    if not a.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.stop()


if __name__ == "__main__":
    main()
