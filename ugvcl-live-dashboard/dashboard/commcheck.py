"""Communication check: paste meter serials -> communication / non-communication.

Rule (as defined for this project): a meter that is FOUND by the Grafana
'Last Communication' query is communicating; a meter that is NOT found is
non-communicating. Nothing is guessed: if a CLR query fails, meters that were
not found elsewhere are reported as 'Unknown', never as non-communication.
"""
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

from . import queries as Q
from .grafana import GrafanaError, MSSQL_TYPE

SERIAL_RE = re.compile(r"^(?=.*[A-Za-z0-9])[A-Za-z0-9_\-]{1,40}$")
SPLIT_RE = re.compile(r"[\s,;|\t]+")
NO_CLR = "No CLR matched"
BUCKETS = ["Last 24 hours", "1 to 3 days", "3 to 7 days", "7 to 30 days", "Over 30 days", "No valid date"]


def parse_serials(text, max_serials):
    """-> (unique valid serials in input order, invalid tokens, duplicate count)"""
    seen, valid, invalid, dups = set(), [], [], 0
    for tok in SPLIT_RE.split(text or ""):
        tok = tok.strip().strip("'\"")
        if not tok:
            continue
        if not SERIAL_RE.match(tok):
            invalid.append(tok)
            continue
        key = tok.upper()
        if key in seen:
            dups += 1
            continue
        seen.add(key)
        valid.append(tok)
    if len(valid) > max_serials:
        raise ValueError("Too many meters: %d (limit %d). Split the list." % (len(valid), max_serials))
    return valid, invalid, dups


def _bucket(last_comm, now):
    if not last_comm:
        return BUCKETS[5]
    try:
        dt = datetime.strptime(last_comm, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return BUCKETS[5]
    age = now - dt
    if age <= timedelta(hours=24):
        return BUCKETS[0]
    if age <= timedelta(days=3):
        return BUCKETS[1]
    if age <= timedelta(days=7):
        return BUCKETS[2]
    if age <= timedelta(days=30):
        return BUCKETS[3]
    return BUCKETS[4]


def classify(serials, per_clr, failed_clrs, now=None):
    """per_clr: {clr: {SERIAL_UPPER: (last_comm or None, found_attr bool)}} for the CLR
    queries that succeeded. failed_clrs: CLRs with at least one failed chunk."""
    now = now or datetime.now()
    rows = []
    for s in serials:
        k = s.upper()
        hits = [(clr, per_clr[clr][k]) for clr in per_clr if k in per_clr[clr]]
        found = [(clr, v) for clr, v in hits if v[1]]
        if found:
            clr, (lc, _) = sorted(found, key=lambda x: x[1][0] or "", reverse=True)[0]
            rows.append({"serial": s, "status": "Communication", "clr": clr, "last_comm": lc,
                         "reason": "Found in Last Communication", "bucket": _bucket(lc, now)})
        elif failed_clrs:
            where = hits[0][0] if hits else None
            rows.append({"serial": s, "status": "Unknown", "clr": where, "last_comm": None,
                         "reason": "Could not check: query failed for " + ", ".join(sorted(failed_clrs)),
                         "bucket": None})
        elif hits:
            rows.append({"serial": s, "status": "Non-communication", "clr": hits[0][0], "last_comm": None,
                         "reason": "Meter exists in %s but has no last-communication value" % hits[0][0],
                         "bucket": None})
        else:
            rows.append({"serial": s, "status": "Non-communication", "clr": None, "last_comm": None,
                         "reason": "Serial not found in any CLR", "bucket": None})
    return rows


def summarize(rows, clr_order, invalid, dups, errors):
    comm = [r for r in rows if r["status"] == "Communication"]
    non = [r for r in rows if r["status"] == "Non-communication"]
    unk = [r for r in rows if r["status"] == "Unknown"]
    checked = len(comm) + len(non)
    by_clr = {c: {"communication": 0, "non_communication": 0, "unknown": 0} for c in clr_order}
    by_clr[NO_CLR] = {"communication": 0, "non_communication": 0, "unknown": 0}
    for r in comm:
        by_clr[r["clr"]]["communication"] += 1
    for r in non:
        by_clr[r["clr"] or NO_CLR]["non_communication"] += 1
    for r in unk:
        by_clr[r["clr"] or NO_CLR]["unknown"] += 1
    buckets = {b: 0 for b in BUCKETS}
    for r in comm:
        buckets[r["bucket"]] += 1
    return {
        "total": len(rows), "communication": len(comm), "non_communication": len(non), "unknown": len(unk),
        "comm_pct": round(100.0 * len(comm) / checked, 2) if checked else None,
        "by_clr": by_clr, "age_buckets": buckets,
        "invalid": invalid[:50], "invalid_count": len(invalid), "duplicates": dups, "errors": errors,
        "complete": not errors,
    }


class CommChecker:
    def __init__(self, client, config):
        self.client = client
        self.clrs = config["clrs"]
        cc = config.get("comm_check", {})
        self.chunk = int(cc.get("chunk_size", 400))
        self.max_serials = int(cc.get("max_serials", 20000))
        self.pool_size = int(config.get("max_parallel_queries", 4))
        self.jobs = {}
        self.lock = threading.Lock()

    def start(self, text):
        serials, invalid, dups = parse_serials(text, self.max_serials)
        if not serials:
            raise ValueError("No valid meter serial numbers found. Use letters, digits, - and _ only.")
        jid = uuid.uuid4().hex[:12]
        chunks = [serials[i:i + self.chunk] for i in range(0, len(serials), self.chunk)]
        job = {"id": jid, "status": "running", "done": 0, "total": len(chunks) * len(self.clrs),
               "created": time.time(), "result": None, "error": None}
        with self.lock:
            for k in [k for k, j in self.jobs.items() if time.time() - j["created"] > 3600]:
                del self.jobs[k]
            if sum(1 for j in self.jobs.values() if j["status"] == "running") >= 3:
                raise ValueError("Three checks are already running. Wait for one to finish.")
            self.jobs[jid] = job
        threading.Thread(target=self._run, args=(job, serials, chunks, invalid, dups), daemon=True).start()
        return jid

    def get(self, jid):
        with self.lock:
            j = self.jobs.get(jid)
            return None if j is None else dict(j)

    def _run(self, job, serials, chunks, invalid, dups):
        per_clr = {c: {} for c in self.clrs}
        failed = {}  # clr -> first error
        lock = threading.Lock()

        def work(clr, chunk):
            cfg = self.clrs[clr]
            q = {"ref_id": "A", "ds_type": MSSQL_TYPE, "ds_uid": cfg["mssql_uid"], "sql": Q.last_comm_sql(cfg, chunk)}
            err, rows = None, None
            try:
                r = self.client.query([q])["A"]
                err, rows = r.error, r.rows
            except GrafanaError as e:
                err = str(e)
            except Exception as e:
                err = "internal error: %s" % e
            with lock:
                if err:
                    failed.setdefault(clr, err)
                else:
                    for x in rows:
                        per_clr[clr][str(x.get("serialno")).upper()] = (x.get("last_comm"), bool(x.get("found_attr")))
                job["done"] += 1

        try:
            with ThreadPoolExecutor(max_workers=self.pool_size) as ex:
                for chunk in chunks:
                    for clr in self.clrs:
                        ex.submit(work, clr, chunk)
            ok = {c: v for c, v in per_clr.items() if c not in failed}
            # data from a CLR that partly failed is still valid for 'found' hits
            rows = classify(serials, {c: per_clr[c] for c in self.clrs}, set(failed))
            result = summarize(rows, list(self.clrs), invalid, dups, failed)
            result["rows"] = rows
            result["checked_at"] = time.time()
            with self.lock:
                job["result"], job["status"] = result, "done"
        except Exception as e:  # pragma: no cover
            with self.lock:
                job["status"], job["error"] = "error", "internal error: %s" % e
