"""Minimal Grafana client: runs raw SQL through POST /api/ds/query.

Uses only the standard library so it works on a locked-down laptop.
"""
import base64
import json
import socket
import ssl
import urllib.error
import urllib.request

PG_TYPE = "grafana-postgresql-datasource"
MSSQL_TYPE = "mssql"


class GrafanaError(Exception):
    pass


class QueryResult:
    def __init__(self, rows=None, error=None):
        self.rows = rows or []
        self.error = error


def _frames_to_rows(frames):
    rows = []
    for fr in frames or []:
        fields = (fr.get("schema") or {}).get("fields") or []
        names = [(f.get("name") or "col%d" % i).lower() for i, f in enumerate(fields)]
        cols = (fr.get("data") or {}).get("values") or []
        if not cols:
            continue
        for i in range(len(cols[0])):
            rows.append({names[c]: cols[c][i] for c in range(min(len(names), len(cols)))})
    return rows


class GrafanaClient:
    def __init__(self, base_url, token="", basic_auth="", verify_tls=True, timeout=60):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if token:
            self.headers["Authorization"] = "Bearer " + token
        elif basic_auth:  # "user:password"
            self.headers["Authorization"] = "Basic " + base64.b64encode(basic_auth.encode()).decode()
        if verify_tls:
            self.ctx = ssl.create_default_context()
        else:
            self.ctx = ssl.create_default_context()
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    def query(self, queries):
        """queries: list of dicts {ref_id, ds_type, ds_uid, sql}.
        Returns {ref_id: QueryResult}. Raises GrafanaError on transport failure."""
        body = {
            "queries": [
                {
                    "refId": q["ref_id"],
                    "datasource": {"type": q["ds_type"], "uid": q["ds_uid"]},
                    "rawSql": q["sql"],
                    "format": "table",
                    "rawQuery": True,
                    "editorMode": "code",
                    "intervalMs": 60000,
                    "maxDataPoints": 5000,
                }
                for q in queries
            ],
            "from": "now-6h",
            "to": "now",
        }
        req = urllib.request.Request(
            self.base_url + "/api/ds/query",
            data=json.dumps(body).encode("utf-8"),
            headers=self.headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                payload = json.loads(raw)
            except ValueError:
                raise GrafanaError("HTTP %s from Grafana: %s" % (e.code, raw[:200]))
            if e.code in (401, 403):
                raise GrafanaError(
                    "HTTP %s - Grafana requires authentication. Set api_token in config.json "
                    "or the GRAFANA_TOKEN environment variable." % e.code
                )
            if "results" not in payload:
                raise GrafanaError("HTTP %s from Grafana: %s" % (e.code, payload.get("message", raw[:200])))
        except (socket.timeout, TimeoutError):
            raise GrafanaError("Timed out after %ds waiting for Grafana - the query is too slow "
                               "(Grafana itself is reachable)" % self.timeout)
        except urllib.error.URLError as e:
            if isinstance(getattr(e, "reason", None), (socket.timeout, TimeoutError)):
                raise GrafanaError("Timed out after %ds waiting for Grafana" % self.timeout)
            raise GrafanaError("Cannot reach Grafana: %s" % getattr(e, "reason", e))
        except (OSError, ValueError) as e:
            raise GrafanaError("Cannot reach Grafana: %s" % e)

        out = {}
        for q in queries:
            r = (payload.get("results") or {}).get(q["ref_id"])
            if r is None:
                out[q["ref_id"]] = QueryResult(error="no result returned for this query")
            elif r.get("error"):
                out[q["ref_id"]] = QueryResult(error=str(r["error"])[:300])
            else:
                out[q["ref_id"]] = QueryResult(rows=_frames_to_rows(r.get("frames")))
        return out
