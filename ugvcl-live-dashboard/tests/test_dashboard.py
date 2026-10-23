import json, os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dashboard import queries as Q
from dashboard.grafana import GrafanaClient, _frames_to_rows
from dashboard.demo_grafana import start_demo
from dashboard.service import DashboardService

CFG = json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")))


class T(unittest.TestCase):
    def test_frames(self):
        fr = [{"schema": {"fields": [{"name": "Metric"}, {"name": "Meters"}]}, "data": {"values": [["a", "b"], [1, 2]]}}]
        self.assertEqual(_frames_to_rows(fr), [{"metric": "a", "meters": 1}, {"metric": "b", "meters": 2}])
        self.assertEqual(_frames_to_rows([]), [])

    def test_result_types_match_grafana(self):
        # values copied from the 'All data summary today (MPRT)' dashboard
        self.assertIn("resulttypefk = 1605", Q.today_sql(CFG["clrs"]["CLR1"]))
        self.assertIn("resulttypefk = 4314", Q.today_sql(CFG["clrs"]["CLR4"]))
        self.assertIn("resulttypefk = 486", Q.today_sql(CFG["clrs"]["CLR3"]))
        self.assertIn("resulttypefk = 2637", Q.today_sql(CFG["clrs"]["CLR2"]))

    def test_attr_name_escaped(self):
        self.assertIn("'O''Brien'", Q.attr_by_name_sql("O'Brien"))

    def test_end_to_end_with_demo(self):
        svc = DashboardService(GrafanaClient(start_demo(CFG)), CFG)
        svc.refresh_all_now()
        import time
        snap = svc.snapshot()
        while snap["pending"]:
            time.sleep(0.2)
            snap = svc.snapshot()
        self.assertEqual(snap["clr_order"], ["CLR1", "CLR2", "CLR3", "CLR4"])
        for c in snap["clr_order"]:
            d = snap["clrs"][c]
            self.assertLessEqual(d["coverage"]["midnight"]["pct"], 100)
            self.assertEqual(len(d["lp_hourly"]), 24)
            self.assertEqual(len(d["trend"]), 7)
        self.assertRaises(ValueError, svc.attr_distribution, "bad name")


class DateTests(unittest.TestCase):
    def test_past_date_and_totals(self):
        import time
        from datetime import date, timedelta
        svc = DashboardService(GrafanaClient(start_demo(CFG)), CFG)
        svc.refresh_all_now()
        y = (date.today() - timedelta(days=1)).isoformat()
        v = svc.view(y, 14)
        while v["pending"]:
            time.sleep(0.2)
            v = svc.view(y, 14)
        self.assertFalse(v["view"]["is_today"])
        self.assertEqual(len(v["totals"]["trend"]["points"]), 14)
        for m in ("midnight", "load_profile", "instant", "billing"):
            parts = [c["coverage"][m]["received"] for c in v["clrs"].values()]
            self.assertEqual(v["totals"]["coverage"][m]["received"], sum(parts))
            self.assertEqual(v["totals"]["coverage"][m]["n"], 4)
        self.assertEqual(v["totals"]["devices"]["total"], sum(c["devices"]["total"] for c in v["clrs"].values()))
        for bad in ("2999-01-01", "2020-01-01", "x'; --"):
            self.assertRaises(ValueError, svc.view, bad, 7)

    def test_total_pct_is_ratio_not_mean(self):
        from dashboard.service import DashboardService as D
        clrs = {"A": {"coverage": {m: {"received": 90, "expected": 100, "pct": 90.0, "over_expected": False} for m in ("midnight", "billing", "load_profile", "instant")}},
                "B": {"coverage": {m: {"received": 10, "expected": 100, "pct": 10.0, "over_expected": False} for m in ("midnight", "billing", "load_profile", "instant")}}}
        self.assertEqual(D._totals(clrs)["coverage"]["midnight"]["pct"], 50.0)
        clrs["B"] = {}
        self.assertEqual(D._totals(clrs)["coverage"]["midnight"]["n"], 1)   # partial is flagged


class CommTests(unittest.TestCase):
    def test_parse(self):
        from dashboard.commcheck import parse_serials
        v, bad, d = parse_serials("US1, us1;US2\n'; DROP TABLE x;-- US3 --", 100)
        self.assertEqual(d, 1)                       # us1 duplicate
        self.assertIn("US3", v)
        self.assertNotIn("--", v)                    # no letter/digit -> rejected
        self.assertTrue(all(__import__("re").match(r"^[A-Za-z0-9_\-]+$", x) for x in v))  # nothing risky survives
        self.assertRaises(ValueError, parse_serials, "A B C", 2)

    def test_sql_has_no_unvalidated_input(self):
        from dashboard.commcheck import parse_serials
        v, bad, _ = parse_serials("US1 a'b US2", 10)
        sql = Q.last_comm_sql(CFG["clrs"]["CLR2"], v)
        self.assertNotIn("a'b", sql)
        self.assertIn("AttrKeyFk IN (873, 751, 645)", sql)  # keys from the Grafana dashboard
        self.assertIn("IN (928, 779, 674)", Q.last_comm_sql(CFG["clrs"]["CLR4"], v))

    def test_classify_rules(self):
        from dashboard.commcheck import classify
        per = {"CLR1": {"A": ("2026-10-14 08:00:00", True), "B": (None, False)}, "CLR2": {}}
        rows = {r["serial"]: r for r in classify(["a", "B", "C"], per, set())}
        self.assertEqual(rows["a"]["status"], "Communication")
        self.assertEqual(rows["B"]["status"], "Non-communication")
        self.assertEqual(rows["C"]["reason"], "Serial not found in any CLR")

    def test_failed_clr_is_unknown_not_noncomm(self):
        from dashboard.commcheck import classify
        per = {"CLR1": {"A": ("2026-10-14 08:00:00", True)}, "CLR2": {}}
        rows = {r["serial"]: r for r in classify(["A", "C"], per, {"CLR2"})}
        self.assertEqual(rows["A"]["status"], "Communication")   # still proven by CLR1
        self.assertEqual(rows["C"]["status"], "Unknown")          # cannot claim non-comm


if __name__ == "__main__":
    unittest.main()
