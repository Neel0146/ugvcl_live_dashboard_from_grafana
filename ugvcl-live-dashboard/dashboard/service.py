"""Background refresher + snapshot builder.

Every (section, CLR) pair is its own Grafana request, so one slow or failing
query never blocks the others. Browsers never trigger database work: a
background loop refreshes each section on its own interval and the API serves
the cached result. If a refresh fails, the last good value is kept and the
failure is shown with its age.
"""
import threading
import time
from datetime import date, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

from . import queries as Q
from .grafana import GrafanaError, MSSQL_TYPE, PG_TYPE

METRICS = ("midnight", "billing", "load_profile", "instant")


def _metric(m):
    return lambda clr: Q.metric_sql(clr, m)


# name -> (datasource kind, sql builder). Order = start-up order: light queries first.
SECTIONS = {
    "devices": ("mssql", Q.devices_sql),
    "expected": ("mssql", Q.expected_sql),
    "midnight": ("pg", _metric("midnight")),
    "billing": ("pg", _metric("billing")),
    "load_profile": ("pg", _metric("load_profile")),
    "instant": ("pg", _metric("instant")),
    "metering_mode": ("mssql", Q.metering_mode_sql),
    "lp_hourly": ("pg", Q.lp_hourly_sql),
}
DEFAULT_TTL = {"devices": 300, "expected": 600, "midnight": 120, "billing": 300, "load_profile": 180,
               "instant": 180, "metering_mode": 900, "lp_hourly": 900}
DATE_SECTIONS = ("midnight", "billing", "load_profile", "instant", "lp_hourly")   # follow the selected date
TREND_CHOICES = (7, 14, 30)
MAX_HISTORY_DAYS = 366


def _day_sql(section, cfg, day):
    if section == "lp_hourly":
        return Q.lp_hourly_sql(cfg, day)
    return Q.metric_sql(cfg, section, day)


def _od_ttl(day_iso, today):
    age = (today - date.fromisoformat(day_iso)).days
    return 300 if age <= 0 else (1800 if age == 1 else 21600)
ERROR_RETRY_SECONDS = 60


def _num(v):
    try:
        return 0 if v is None else (int(v) if float(v).is_integer() else float(v))
    except (TypeError, ValueError):
        return 0


class _Section:
    def __init__(self, name, ttl):
        self.name = name
        self.ttl = ttl
        self.data = {}      # clr -> rows (last good)
        self.updated = {}   # clr -> epoch of last good
        self.errors = {}    # clr -> message (current)
        self.elapsed = {}   # clr -> seconds the last attempt took
        self.last_attempt = 0.0
        self.pending = set()  # clrs currently being fetched


class DashboardService:
    def __init__(self, client, config):
        self.client = client
        self.clrs = config["clrs"]
        ttl = dict(DEFAULT_TTL)
        ttl.update(config.get("refresh_seconds", {}))
        self.sections = {n: _Section(n, ttl[n]) for n in SECTIONS}
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=int(config.get("max_parallel_queries", 4)))
        self._stop = threading.Event()
        self._attr_cache = {}
        self.started = time.time()
        self.title = config.get("app_title", "UGVCL Smart Meter Live Monitor")
        self.subtitle = config.get("app_subtitle", "")
        self.od = {}   # on-demand cache: (kind, day, extra, clr) -> entry   (past dates, trend)
        self.od_pool = ThreadPoolExecutor(max_workers=2)

    # ---------- refresh loop ----------
    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="refresher").start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            now = time.time()
            for sec in self.sections.values():
                with self.lock:
                    if sec.pending:
                        continue
                    wait = ERROR_RETRY_SECONDS if sec.errors and sec.ttl > ERROR_RETRY_SECONDS else sec.ttl
                    if now - sec.last_attempt < wait:
                        continue
                    sec.pending = set(self.clrs)
                    sec.last_attempt = now
                for clr in self.clrs:
                    self.pool.submit(self._fetch, sec, clr)
            self._stop.wait(2)

    def refresh_all_now(self):
        """Synchronous refresh of everything (tests, diagnose)."""
        for sec in self.sections.values():
            for clr in self.clrs:
                with self.lock:
                    sec.pending.add(clr)
                self._fetch(sec, clr)

    def _fetch(self, sec, clr):
        kind, builder = SECTIONS[sec.name]
        cfg = self.clrs[clr]
        q = {"ref_id": clr, "ds_type": PG_TYPE if kind == "pg" else MSSQL_TYPE,
             "ds_uid": cfg["postgres_uid" if kind == "pg" else "mssql_uid"], "sql": builder(cfg)}
        t0 = time.time()
        rows, err = None, None
        try:
            r = self.client.query([q])[clr]
            if r.error:
                err = r.error
            else:
                rows = r.rows
        except GrafanaError as e:
            err = str(e)
        except Exception as e:  # never let a worker die silently
            err = "internal error: %s" % e
        took = time.time() - t0
        with self.lock:
            sec.elapsed[clr] = round(took, 1)
            if err is None:
                sec.data[clr] = rows
                sec.updated[clr] = time.time()
                sec.errors.pop(clr, None)
            else:
                sec.errors[clr] = err
            sec.pending.discard(clr)

    # ---------- on-demand data (past dates, trend) ----------
    def _ensure(self, specs, refresh=False):
        """specs: [(kind, day_iso, extra)]. Starts fetches for missing/expired entries.
        Returns the number of entries still being fetched."""
        now, today = time.time(), date.today()
        todo = []
        with self.lock:
            if len(self.od) > 3000:
                for k in sorted(self.od, key=lambda k: self.od[k]["updated"] or 0)[:500]:
                    if not self.od[k]["pending"]:
                        del self.od[k]
            for kind, day_iso, extra in specs:
                for clr in self.clrs:
                    key = (kind, day_iso, extra, clr)
                    e = self.od.get(key)
                    if e is None:
                        e = self.od[key] = {"rows": None, "updated": None, "error": None, "elapsed": None,
                                            "pending": False, "expires": 0}
                    if refresh and not e["pending"]:
                        e["expires"] = 0
                    if not e["pending"] and now >= e["expires"]:
                        e["pending"] = True
                        todo.append(key)
            pending = sum(1 for kind, day_iso, extra in specs for clr in self.clrs
                          if self.od[(kind, day_iso, extra, clr)]["pending"])
        for key in todo:
            self.od_pool.submit(self._od_fetch, key, today)
        return pending

    def _od_fetch(self, key, today):
        kind, day_iso, extra, clr = key
        cfg = self.clrs[clr]
        try:
            sql = Q.midnight_trend_sql(cfg, day_iso, extra) if kind == "trend" else _day_sql(kind, cfg, day_iso)
            q = {"ref_id": clr, "ds_type": PG_TYPE, "ds_uid": cfg["postgres_uid"], "sql": sql}
            t0 = time.time()
            r = self.client.query([q])[clr]
            err, rows = r.error, r.rows
        except GrafanaError as e:
            err, rows, t0 = str(e), None, time.time()
        except Exception as e:
            err, rows, t0 = "internal error: %s" % e, None, time.time()
        took = round(time.time() - t0, 1)
        with self.lock:
            e = self.od[key]
            e["elapsed"], e["pending"] = took, False
            if err is None:
                e["rows"], e["updated"], e["error"] = rows, time.time(), None
                e["expires"] = time.time() + _od_ttl(day_iso, today)
            else:
                e["error"] = err
                e["expires"] = time.time() + 60

    # ---------- view (what the page shows) ----------
    @staticmethod
    def _parse_day(day_str):
        if not day_str:
            return date.today()
        try:
            d = datetime.strptime(day_str, "%Y-%m-%d").date()
        except ValueError:
            raise ValueError("Date must look like 2026-10-20")
        today = date.today()
        if d > today:
            raise ValueError("That date is in the future")
        if (today - d).days > MAX_HISTORY_DAYS:
            raise ValueError("History is limited to the last %d days" % MAX_HISTORY_DAYS)
        return d

    def view(self, day_str=None, trend_days=7, refresh=False):
        d = self._parse_day(day_str)
        is_today = d == date.today()
        days = trend_days if trend_days in TREND_CHOICES else 7
        day_iso = d.isoformat()
        specs = [("trend", day_iso, days)]
        if not is_today:
            specs += [(sec, day_iso, None) for sec in DATE_SECTIONS]
        if refresh and is_today:
            with self.lock:
                for n in DATE_SECTIONS:
                    self.sections[n].last_attempt = 0
        pending = self._ensure(specs, refresh)

        now = time.time()
        with self.lock:
            live = {n: (dict(s.data), dict(s.updated), dict(s.errors), dict(s.elapsed), s.ttl)
                    for n, s in self.sections.items()}
            od = {k: dict(v) for k, v in self.od.items()
                  if (k[1] == day_iso) and (k[0] == "trend" and k[2] == days or k[0] != "trend")}

        def rows_for(section, clr):
            if section in DATE_SECTIONS and not is_today:
                e = od.get((section, day_iso, None, clr))
                return None if e is None else e["rows"]
            return live[section][0].get(clr)

        out = {}
        for clr in self.clrs:
            c = {}
            rows = rows_for("devices", clr)
            if rows:
                r = rows[0]
                total, rf, gprs = _num(r.get("total_devices")), _num(r.get("rf_devices")), _num(r.get("gprs_devices"))
                c["devices"] = {"total": total, "rf": rf, "gprs": gprs, "unclassified": max(0, total - rf - gprs)}
            rows = rows_for("expected", clr)
            exp = {m: _num(rows[0].get(m)) for m in METRICS} if rows else None
            received = {}
            for m in METRICS:
                mr = rows_for(m, clr)
                if mr is not None:
                    received[m] = _num(mr[0].get("meters")) if mr else 0
            if exp or received:
                cov = {}
                for m in METRICS:
                    rec, ex = received.get(m), (exp[m] if exp else None)
                    pct = round(min(100.0, 100.0 * rec / ex), 2) if (rec is not None and ex) else None
                    cov[m] = {"received": rec, "expected": ex, "pct": pct,
                              "over_expected": bool(rec is not None and ex is not None and rec > ex)}
                c["coverage"] = cov
                if exp and "midnight" in received:
                    c["non_comm_midnight"] = max(0, exp["midnight"] - received["midnight"])
            rows = rows_for("lp_hourly", clr)
            if rows is not None:
                hours = [0] * 24
                for r in rows:
                    h = _num(r.get("hr"))
                    if 0 <= h < 24:
                        hours[h] = _num(r.get("meters"))
                c["lp_hourly"] = hours
            e = od.get(("trend", day_iso, days, clr))
            if e and e["rows"] is not None:
                c["trend"] = [{"date": r.get("day_str"), "meters": _num(r.get("meters"))} for r in e["rows"]]
            rows = rows_for("metering_mode", clr)
            if rows is not None:
                c["metering_mode"] = [{"value": r.get("value"), "count": _num(r.get("cnt"))} for r in rows]
            out[clr] = c

        status = {}
        for n in SECTIONS:
            data, updated, errors, elapsed, ttl = live[n]
            if n in DATE_SECTIONS and not is_today:
                es = {c: od.get((n, day_iso, None, c)) for c in self.clrs}
                ts = [e["updated"] for e in es.values() if e and e["updated"]]
                status[n] = {"ttl": None, "newest_update": max(ts) if ts else None, "scope": "date",
                             "errors": {c: e["error"] for c, e in es.items() if e and e["error"]},
                             "elapsed": {c: e["elapsed"] for c, e in es.items() if e and e["elapsed"] is not None},
                             "loaded": bool(ts)}
            else:
                ts = list(updated.values())
                status[n] = {"ttl": ttl, "newest_update": max(ts) if ts else None,
                             "scope": "date" if n in DATE_SECTIONS else "current",
                             "errors": errors, "elapsed": elapsed, "loaded": len(data) > 0}
        es = {c: od.get(("trend", day_iso, days, c)) for c in self.clrs}
        ts = [e["updated"] for e in es.values() if e and e["updated"]]
        status["trend"] = {"ttl": None, "newest_update": max(ts) if ts else None, "scope": "date",
                           "errors": {c: e["error"] for c, e in es.items() if e and e["error"]},
                           "elapsed": {c: e["elapsed"] for c, e in es.items() if e and e["elapsed"] is not None},
                           "loaded": bool(ts)}
        return {
            "app": {"title": self.title, "subtitle": self.subtitle},
            "view": {"date": day_iso, "is_today": is_today, "trend_days": days,
                     "month": d.strftime("%b %Y")},
            "generated_at": now, "clr_order": list(self.clrs), "clrs": out,
            "totals": self._totals(out), "sections": status, "pending": pending,
        }

    @staticmethod
    def _totals(clrs):
        """Sum over the CLRs that have data. 'n' says how many CLRs contributed, so the page can
        flag a partial total instead of presenting it as complete."""
        T = {}
        have = [c for c in clrs.values() if c.get("devices")]
        if have:
            T["devices"] = {k: sum(c["devices"][k] for c in have) for k in ("total", "rf", "gprs", "unclassified")}
            T["devices"]["n"] = len(have)
        cov = {}
        for m in METRICS:
            pts = [c["coverage"][m] for c in clrs.values()
                   if c.get("coverage") and c["coverage"][m]["received"] is not None and c["coverage"][m]["expected"] is not None]
            if pts:
                r, e = sum(p["received"] for p in pts), sum(p["expected"] for p in pts)
                cov[m] = {"received": r, "expected": e, "n": len(pts),
                          "pct": round(min(100.0, 100.0 * r / e), 2) if e else None,
                          "over_expected": any(p["over_expected"] for p in pts)}
        if cov:
            T["coverage"] = cov
        nc = [c["non_comm_midnight"] for c in clrs.values() if c.get("non_comm_midnight") is not None]
        if nc:
            T["non_comm_midnight"] = {"value": sum(nc), "n": len(nc)}
        hr = [c["lp_hourly"] for c in clrs.values() if c.get("lp_hourly") is not None]
        if hr:
            T["lp_hourly"] = {"values": [sum(x[h] for x in hr) for h in range(24)], "n": len(hr)}
        tr = [c["trend"] for c in clrs.values() if c.get("trend") is not None]
        if tr:
            byd = {}
            for series in tr:
                for r in series:
                    byd[r["date"]] = byd.get(r["date"], 0) + r["meters"]
            T["trend"] = {"points": [{"date": k, "meters": byd[k]} for k in sorted(byd)], "n": len(tr)}
        mm = [c["metering_mode"] for c in clrs.values() if c.get("metering_mode") is not None]
        if mm:
            tot = {}
            for series in mm:
                for r in series:
                    tot[r["value"]] = tot.get(r["value"], 0) + r["count"]
            T["metering_mode"] = {"values": [{"value": k, "count": v} for k, v in sorted(tot.items(), key=lambda kv: -kv[1])],
                                  "n": len(mm)}
        return T

    def snapshot(self):  # backward compatible: today's view
        return self.view(None, 7)

    # ---------- attribute explorer ----------
    def _cached(self, key, ttl, fn):
        with self.lock:
            hit = self._attr_cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        val = fn()
        with self.lock:
            self._attr_cache[key] = (time.time(), val)
        return val

    def _query_each(self, sql_for):
        """One request per CLR; returns {clr: QueryResult-or-error-string}."""
        out = {}
        for clr, cfg in self.clrs.items():
            q = {"ref_id": clr, "ds_type": MSSQL_TYPE, "ds_uid": cfg["mssql_uid"], "sql": sql_for(cfg)}
            try:
                r = self.client.query([q])[clr]
                out[clr] = r.error if r.error else r.rows
            except GrafanaError as e:
                out[clr] = str(e)
        return out

    def attr_names(self):
        def fetch():
            res = self._query_each(lambda cfg: Q.attr_names_sql())
            names, errors = set(), {}
            for clr, r in res.items():
                if isinstance(r, str):
                    errors[clr] = r
                else:
                    names.update(str(x.get("name")) for x in r if x.get("name"))
            if not names and errors:
                raise GrafanaError("; ".join("%s: %s" % kv for kv in errors.items()))
            return sorted(names, key=str.lower)
        return self._cached("names", 3600, fetch)

    def attr_distribution(self, name):
        if name not in set(self.attr_names()):
            raise ValueError("unknown attribute name")

        def fetch():
            res = self._query_each(lambda cfg: Q.attr_by_name_sql(name))
            out = {"name": name, "clrs": {}, "errors": {}, "truncated": []}
            for clr, r in res.items():
                if isinstance(r, str):
                    out["errors"][clr] = r
                else:
                    out["clrs"][clr] = [{"value": str(x.get("value")), "count": _num(x.get("cnt"))} for x in r]
                    if len(r) >= 200:
                        out["truncated"].append(clr)
            out["generated_at"] = time.time()
            return out
        return self._cached("attr:" + name, 300, fetch)
