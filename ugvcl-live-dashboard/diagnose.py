#!/usr/bin/env python3
"""Times every dashboard query, one CLR at a time, and prints what happened.

  python diagnose.py            # all sections, all CLRs
  python diagnose.py CLR1       # one CLR only
  python diagnose.py CLR1 midnight instant    # chosen sections
Copy the whole output to whoever is helping you - it contains no data, only
timings, row counts and error messages.
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from dashboard.grafana import GrafanaClient, GrafanaError, MSSQL_TYPE, PG_TYPE  # noqa: E402
from dashboard.service import SECTIONS  # noqa: E402

cfg = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
token = os.environ.get("GRAFANA_TOKEN") or cfg.get("api_token", "")
client = GrafanaClient(cfg["grafana_url"], token, os.environ.get("GRAFANA_BASIC_AUTH", ""),
                       cfg.get("verify_tls", True), cfg.get("request_timeout_seconds", 240))
args = sys.argv[1:]
clrs = [a for a in args if a in cfg["clrs"]] or list(cfg["clrs"])
secs = [a for a in args if a in SECTIONS] or list(SECTIONS)
print("Grafana:", cfg["grafana_url"], "| timeout %ss" % cfg.get("request_timeout_seconds", 240))
print("%-14s %-6s %-9s %-8s %s" % ("section", "CLR", "seconds", "rows", "result"))
for name in secs:
    kind, builder = SECTIONS[name]
    for clr in clrs:
        c = cfg["clrs"][clr]
        q = {"ref_id": "A", "ds_type": PG_TYPE if kind == "pg" else MSSQL_TYPE,
             "ds_uid": c["postgres_uid" if kind == "pg" else "mssql_uid"], "sql": builder(c)}
        t0 = time.time()
        try:
            r = client.query([q])["A"]
            res = ("ERROR: " + r.error) if r.error else ("OK " + json.dumps(r.rows[:1])[:90])
            n = 0 if r.error else len(r.rows)
        except GrafanaError as e:
            res, n = "FAILED: " + str(e), "-"
        print("%-14s %-6s %-9.1f %-8s %s" % (name, clr, time.time() - t0, n, res), flush=True)
