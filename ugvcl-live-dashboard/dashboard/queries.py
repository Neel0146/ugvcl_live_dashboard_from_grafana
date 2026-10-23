"""SQL for each dashboard section.

Every query here is derived from the existing UGVCL Grafana dashboards:
  * 'All data summary today (MPRT)'  -> today / expected counts
  * 'Meters based on Communication type' -> RF (ConnectUsing = NetworkUdp) vs GPRS
  * 'Metering_Mode', 'Device Attribute Details' -> attribute counts
Postgres side (measurement results) and SQL Server side (device master) are
kept separate because that is how the CLR datasources are split in Grafana.
Only integers from config.json are interpolated; attribute names are checked
against the names read from the database before use.
"""


def _rt(clr, key):
    return int(clr["result_types"][key])


# ---------------- SQL Server (device master) ----------------

def devices_sql(clr):
    return """
SELECT
 (SELECT COUNT(DISTINCT SerialNo) FROM Device
   WHERE SerialNo LIKE 'U%' AND IsTemplate = 0) AS total_devices,
 (SELECT COUNT(DISTINCT d.SerialNo) FROM Device d
   JOIN DeviceAttr da ON da.DeviceFk = d.Pkid
   JOIN AttrKey ak ON ak.Pkid = da.AttrKeyFk
   WHERE d.SerialNo LIKE 'U%' AND d.IsTemplate = 0
     AND ak.AttrName = 'ConnectUsing' AND da.AttrVal = 'NetworkUdp') AS rf_devices,
 (SELECT COUNT(DISTINCT d.SerialNo) FROM Device d
   WHERE d.SerialNo LIKE 'U%' AND d.IsTemplate = 0 AND d.TemplateFk IS NOT NULL
     AND NOT EXISTS (SELECT 1 FROM DeviceAttr da
                     JOIN AttrKey ak ON ak.Pkid = da.AttrKeyFk
                     WHERE da.DeviceFk = d.Pkid AND ak.AttrName = 'ConnectUsing'
                       AND da.AttrVal LIKE 'NetworkUdp')) AS gprs_devices
""".strip()


def expected_sql(clr):
    m, b, l, i = (_rt(clr, k) for k in ("midnight", "billing", "load_profile", "instant"))
    return """
SELECT
 SUM(CASE WHEN mprt.ResultTypeFk = {m} THEN 1 ELSE 0 END) AS midnight,
 SUM(CASE WHEN mprt.ResultTypeFk = {b} THEN 1 ELSE 0 END) AS billing,
 SUM(CASE WHEN mprt.ResultTypeFk = {l} THEN 1 ELSE 0 END) AS load_profile,
 SUM(CASE WHEN mprt.ResultTypeFk = {i} THEN 1 ELSE 0 END) AS instant
FROM Device d
JOIN MeasPoint mp ON mp.DeviceFk = d.Pkid
JOIN MeasPointResType mprt ON mprt.MeasPointFk = mp.Pkid
WHERE d.SerialNo LIKE 'U%' AND d.IsTemplate = 0
  AND mprt.ResultTypeFk IN ({m}, {b}, {l}, {i})
""".format(m=m, b=b, l=l, i=i).strip()


def metering_mode_sql(clr):
    return attr_by_key_sql(int(clr["metering_mode_attr_key"]))


def _attr_value_expr():
    return "COALESCE(NULLIF(LTRIM(RTRIM(CAST(da.AttrVal AS NVARCHAR(200)))), ''), '(blank)')"


def attr_by_key_sql(key):
    # Same filter as the Grafana 'Metering_Mode' dashboard (device has a serial number).
    return """
SELECT TOP 200 v AS value, COUNT(DISTINCT SerialNo) AS cnt FROM (
  SELECT d.SerialNo, {expr} AS v
  FROM DeviceAttr da JOIN Device d ON d.Pkid = da.DeviceFk
  WHERE da.AttrKeyFk = {key} AND d.SerialNo IS NOT NULL
) x GROUP BY v ORDER BY cnt DESC
""".format(expr=_attr_value_expr(), key=int(key)).strip()


def attr_names_sql():
    return "SELECT DISTINCT AttrName AS name FROM AttrKey WHERE AttrName IS NOT NULL ORDER BY AttrName"


def attr_by_name_sql(name):
    safe = name.replace("'", "''")
    return """
SELECT TOP 200 v AS value, COUNT(DISTINCT SerialNo) AS cnt FROM (
  SELECT d.SerialNo, {expr} AS v
  FROM DeviceAttr da
  JOIN Device d ON d.Pkid = da.DeviceFk
  JOIN AttrKey ak ON ak.Pkid = da.AttrKeyFk
  WHERE ak.AttrName = '{name}' AND d.SerialNo LIKE 'U%' AND d.IsTemplate = 0
) x GROUP BY v ORDER BY cnt DESC
""".format(expr=_attr_value_expr(), name=safe).strip()


# ---------------- PostgreSQL (measurement results) ----------------

import re
from datetime import date as _date, datetime as _dt, timedelta as _td

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def check_day(day):
    """None = today (database CURRENT_DATE). Otherwise a strictly validated YYYY-MM-DD."""
    if day is None:
        return None
    if not isinstance(day, str) or not _DAY_RE.match(day):
        raise ValueError("bad date")
    _dt.strptime(day, "%Y-%m-%d")
    return day


def _lit(d):
    return "DATE '%s'" % d


_TABLES = {
    "midnight": "result_24h_double",
    "billing": "result_1month_double",
    "load_profile": "result_30m_double",
    "instant": "result_0s_double",
}


def _day_cond(metric, day):
    """Filter for 'data of this date'. day=None keeps the exact Grafana condition for today."""
    day = check_day(day)
    if metric == "billing":
        base = "CURRENT_DATE" if day is None else _lit(day)
        return "r.result_timestamp = date_trunc('month', %s)" % ("now()" if day is None else base)
    if day is None:
        return "r.result_timestamp >= CURRENT_DATE"
    return "r.result_timestamp >= {d} AND r.result_timestamp < {d} + 1".format(d=_lit(day))


def metric_sql(clr, metric, day=None):
    """Meters (distinct measuring points) with data for ONE profile on a date (default today;
    billing = the month of that date). Same join and filters as the Grafana
    'All data summary today (MPRT)' gauges."""
    return ("SELECT '{name}' AS metric, COUNT(DISTINCT r.mprt_fk) AS meters "
            "FROM dbo.{tbl} r JOIN mssql_measpointrestype m ON m.pkid = r.mprt_fk "
            "WHERE m.resulttypefk = {rt} AND {cond}").format(
        name=metric, tbl=_TABLES[metric], cond=_day_cond(metric, day), rt=_rt(clr, metric))


def today_sql(clr):  # kept for reference / tests
    return "\nUNION ALL\n".join(metric_sql(clr, m) for m in _TABLES)


def lp_hourly_sql(clr, day=None):
    return """
SELECT EXTRACT(HOUR FROM r.result_timestamp)::int AS hr, COUNT(DISTINCT r.mprt_fk) AS meters
FROM dbo.result_30m_double r JOIN mssql_measpointrestype m ON m.pkid = r.mprt_fk
WHERE m.resulttypefk = {rt} AND {cond}
GROUP BY 1 ORDER BY 1
""".format(rt=_rt(clr, "load_profile"), cond=_day_cond("load_profile", day)).strip()


def midnight_trend_sql(clr, end_day, days):
    """Meters with midnight data on each date of the `days` dates ending at end_day (inclusive)."""
    end = _dt.strptime(check_day(end_day), "%Y-%m-%d").date()
    start = end - _td(days=int(days) - 1)
    return """
SELECT to_char(r.result_timestamp, 'YYYY-MM-DD') AS day_str, COUNT(DISTINCT r.mprt_fk) AS meters
FROM dbo.result_24h_double r JOIN mssql_measpointrestype m ON m.pkid = r.mprt_fk
WHERE m.resulttypefk = {rt} AND r.result_timestamp >= {a} AND r.result_timestamp < {b} + 1
GROUP BY 1 ORDER BY 1
""".format(rt=_rt(clr, "midnight"), a=_lit(start.isoformat()), b=_lit(end.isoformat())).strip()


# ---------------- Communication check (SQL Server) ----------------
# Same logic as the Grafana 'Last Communication' dashboard (Search related folder):
# the last-communication attribute keys of each CLR are read, converted to a date,
# and the latest one is the meter's last communication. A meter that returns a row
# there is 'found' (communication); a meter that does not is 'not found' (non-comm).

def last_comm_sql(clr, serials):
    """serials must already be validated (see commcheck.SERIAL_RE); they are
    still quoted defensively."""
    keys = ", ".join(str(int(k)) for k in clr["last_comm_attr_keys"])
    inlist = ", ".join("'" + s.replace("'", "''") + "'" for s in serials)
    return """
WITH s AS (
    SELECT DISTINCT SerialNo FROM Device WHERE SerialNo IN ({inlist})
),
src AS (
    SELECT d.SerialNo AS serialno, da.AttrVal AS attrval
    FROM Device d
    INNER JOIN DeviceAttr da ON da.DeviceFk = d.PkID AND da.AttrKeyFk IN ({keys})
    WHERE d.SerialNo IN ({inlist})
),
conv AS (
    SELECT serialno,
        CASE WHEN attrval LIKE '%T%'
             THEN CAST(TRY_CONVERT(datetimeoffset, attrval) AS datetime2)
             ELSE TRY_CONVERT(datetime2, attrval) END AS attrval_dt
    FROM src
),
agg AS (
    SELECT serialno, MAX(attrval_dt) AS lc FROM conv GROUP BY serialno
)
SELECT s.SerialNo AS serialno,
       CONVERT(varchar(19), a.lc, 120) AS last_comm,
       CASE WHEN a.serialno IS NULL THEN 0 ELSE 1 END AS found_attr
FROM s LEFT JOIN agg a ON a.serialno = s.SerialNo
""".format(inlist=inlist, keys=keys).strip()
