"""A tiny fake Grafana (/api/ds/query) that returns SIMULATED numbers.

Lets you try the dashboard anywhere, and exercises the exact same request /
response path as the real thing. Numbers are invented - never use them for
decisions. Set DEMO_FAIL_CLR=CLR3 to see how a failing datasource is shown.
"""
import json
import os
import random
import re
import threading
import time
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SIZES = {"CLR1": 312000, "CLR2": 268000, "CLR3": 241000, "CLR4": 197000}


def _frame(ref, cols):
    """cols: list of (name, type, values)."""
    return {"schema": {"refId": ref, "fields": [{"name": n, "type": t} for n, t, _ in cols]},
            "data": {"values": [v for _, _, v in cols]}}


def _progress():
    now = datetime.now()
    return (now.hour * 3600 + now.minute * 60 + now.second) / 86400.0


def _jit(clr, salt, scale=0.004):
    r = random.Random("%s-%s-%d" % (clr, salt, 0 if "|" in salt else int(time.time() // 30)))
    return 1 + r.uniform(-scale, scale)


def _answer(sql, clr, attr_names):
    n = SIZES[clr]
    dates = re.findall(r"DATE '(\d{4}-\d{2}-\d{2})'", sql)
    qday = date.fromisoformat(dates[0]) if dates else None
    past = qday is not None and qday < date.today()
    f = 1.0 if past else _progress()
    seed = str(qday) if past else ""
    if "AS found_attr" in sql:
        import zlib
        serials = re.findall(r"'([^']+)'", sql.split("SELECT DISTINCT SerialNo FROM Device WHERE SerialNo IN (")[1].split(")")[0])
        mine = list(SIZES).index(clr)
        out_s, out_l, out_f = [], [], []
        for sn in serials:
            h = zlib.crc32(sn.upper().encode())
            if h % 4 != mine or sn.upper().startswith("X"):
                continue          # not a meter of this CLR / unknown serial
            kind = (h >> 3) % 10
            if kind < 7:
                lc = (datetime.now() - timedelta(minutes=(h >> 5) % 1200)).strftime("%Y-%m-%d %H:%M:%S")
            elif kind < 9:
                lc = (datetime.now() - timedelta(days=2 + (h >> 5) % 40)).strftime("%Y-%m-%d %H:%M:%S")
            else:
                lc = None
            out_s.append(sn); out_l.append(lc); out_f.append(0 if lc is None else 1)
        return [("serialno", "string", out_s), ("last_comm", "string", out_l), ("found_attr", "number", out_f)]
    if "AS total_devices" in sql:
        rf = int(n * 0.07)
        gprs = int(n * 0.925)
        return [("total_devices", "number", [n]), ("rf_devices", "number", [rf]), ("gprs_devices", "number", [gprs])]
    if "SUM(CASE WHEN" in sql:
        return [("midnight", "number", [int(n * 0.995)]), ("billing", "number", [int(n * 0.995)]),
                ("load_profile", "number", [int(n * 0.995)]), ("instant", "number", [int(n * 0.995)])]
    mm = re.search(r"'(\w+)' AS metric", sql)
    if mm:
        k = mm.group(1)
        val = {"midnight": int(n * 0.95 * min(1, f * 9) * _jit(clr, "m|" + seed if seed else "m")),
               "load_profile": int(n * 0.93 * min(1, f * 4) * _jit(clr, "l|" + seed if seed else "l")),
               "instant": int(n * 0.88 * min(1, 0.2 + f * 2) * _jit(clr, "i|" + seed if seed else "i")),
               "billing": int(n * 0.94 * _jit(clr, "b", 0.0005))}[k]
        return [("metric", "string", [k]), ("meters", "number", [val])]
    if "EXTRACT(HOUR" in sql:
        last = 23 if past else datetime.now().hour
        hrs = list(range(0, last + 1))
        vals = []
        for h in hrs:
            full = int(n * 0.93 * _jit(clr, "h%d|%s" % (h, seed) if seed else "h%d" % h, 0.01))
            vals.append(full if (past or h < datetime.now().hour) else int(full * min(1, datetime.now().minute / 40 + 0.3)))
        return [("hr", "number", hrs), ("meters", "number", vals)]
    if "day_str" in sql:
        a, b = date.fromisoformat(dates[0]), date.fromisoformat(dates[1])
        days, vals, d = [], [], a
        while d <= b:
            days.append(d.isoformat())
            base = n * (0.95 + random.Random(clr + d.isoformat()).uniform(-0.03, 0.025))
            vals.append(int(base * (min(1, _progress() * 9) if d == date.today() else 1)))
            d += timedelta(days=1)
        return [("day_str", "string", days), ("meters", "number", vals)]
    if "SELECT DISTINCT AttrName" in sql:
        return [("name", "string", attr_names)]
    if "AttrKeyFk =" in sql:
        parts = [("Prepaid", .08), ("Postpaid", .6), ("Net", .02), ("Import", .30)]
        return [("value", "string", [p for p, _ in parts]), ("cnt", "number", [int(n * w) for _, w in parts])]
    m = re.search(r"ak\.AttrName = '([^']*)'", sql)
    if m:
        rnd = random.Random(m.group(1) + clr)
        vals = ["Value %s" % c for c in "ABCDE"]
        w = sorted([rnd.random() + .05 for _ in vals], reverse=True)
        tot = sum(w)
        return [("value", "string", vals), ("cnt", "number", [int(n * x / tot) for x in w])]
    raise ValueError("demo server does not understand this SQL")


def start_demo(cfg):
    uid_to_clr = {}
    for clr, c in cfg["clrs"].items():
        uid_to_clr[c["mssql_uid"]] = clr
        uid_to_clr[c["postgres_uid"]] = clr
    attr_names = ["ConnectUsing", "FirmwareVersion", "Manufacturer", "MeterPhase", "MeteringMode", "TariffCode"]
    fail = os.environ.get("DEMO_FAIL_CLR")

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            if self.path != "/api/ds/query":
                self.send_error(404)
                return
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            results = {}
            time.sleep(0.15)
            for q in body["queries"]:
                ref, uid, sql = q["refId"], q["datasource"]["uid"], q["rawSql"]
                clr = uid_to_clr.get(uid)
                if clr is None:
                    results[ref] = {"status": 400, "error": "datasource %s not found" % uid}
                elif os.environ.get("DEMO_FAIL_COMM_CLR") == clr and "AS found_attr" in sql:
                    results[ref] = {"status": 500, "error": "Conversion failed when converting date"}
                elif fail and clr == fail and "grafana-postgresql" in q["datasource"]["type"]:
                    results[ref] = {"status": 500, "error": "pq: canceling statement due to statement timeout"}
                else:
                    try:
                        results[ref] = {"status": 200, "frames": [_frame(ref, _answer(sql, clr, attr_names))]}
                    except ValueError as e:
                        results[ref] = {"status": 400, "error": str(e)}
            out = json.dumps({"results": results}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return "http://127.0.0.1:%d" % httpd.server_address[1]
