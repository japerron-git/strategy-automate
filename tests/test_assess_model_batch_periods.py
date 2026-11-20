"""Tests for assess_model.py: grading against form-column headers and missing key columns, the bank's
output-column and AVG(decimal literal) checks, snapshot periods vs event dates and periods after the run date,
the anchor's tie-break and the summed-rate words. Generic retail data; nothing touches the network.
"""
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import _hermetic  # noqa: E402,F401  (scrub MSTR_*/proxy env, private secret store)

import datetime
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "skills", "strategy-model-assessment", "scripts"))

import assess_model as am  # noqa: E402


def _reply(columns, rows):
    lines = ["| " + " | ".join(columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
    lines += ["| " + " | ".join("" if v is None else str(v) for v in r) + " |" for r in rows]
    return {"content": [{"type": "text", "text": "\n".join(lines) + f"\n\n*Query returned {len(rows)} rows*"}]}


class _Runner:
    """Answers each query from a function of its SQL; records the SQL."""

    def __init__(self, answer):
        self.answer, self.sql = answer, []

    def call(self, tool, args):
        self.sql.append(args["query"])
        return self.answer(args["query"])


# ── grade: form columns, missing keys, duplicate keys ─────────────────────────────────────────────

class GradeColumnTests(unittest.TestCase):
    Q = {"id": "Q1", "key": ["order date (month)__id"], "measures": ["net sales"],
         "sql": 'SELECT "order date (month)__id", SUM("net sales") AS "net sales" FROM {{MODEL}} GROUP BY 1'}

    def test_a_form_key_matches_the_connectors_header(self):
        # the connector heads a selected 'x__form' column 'x (form)'
        rows = [{"order date (month) (id)": "202401", "net sales": "10.5"}]
        ref = [{"order date (month)__id": "202401", "net sales": "10.5"}]
        verdict, cmp, err = am._compare(self.Q, rows, ref, 1e-6)
        self.assertEqual((verdict, err), ("pass", None))
        self.assertEqual(cmp["matched_rows"], 1)

    def test_column_canon(self):
        self.assertEqual(am.column_canon('"Region__Code"'), "region (code)")
        self.assertEqual(am.column_canon("region (code)"), "region (code)")
        self.assertEqual(am.column_canon("net  sales"), "net sales")

    def test_a_missing_key_column_is_named_not_read_as_blank(self):
        rows = [{"d": "202401", "net sales": "10.5"}]
        ref = [{"order date (month)__id": "202401", "net sales": "10.5"}]
        verdict, _, err = am._compare(self.Q, rows, ref, 1e-6, ("question", "best answer"))
        self.assertEqual(verdict, "error")
        self.assertIn("key column 'order date (month)__id' is not in the question result", err)
        self.assertIn("its columns: d, net sales", err)
        self.assertNotIn("regroup", err)

    def test_duplicate_keys_blame_an_expression_only_when_the_sql_groups_by_one(self):
        q = {"id": "Q2", "key": ["region"], "measures": ["n"]}
        rows = [{"region": "East", "n": "1"}, {"region": "East", "n": "2"}]
        ref = [{"region": "East", "n": "3"}]
        _, _, plain = am._compare(q, rows, ref, 1e-6, sqls=("SELECT region, channel, SUM(n) n FROM m GROUP BY 1, 2", ""))
        self.assertIn("don't identify one row", plain)
        self.assertNotIn("regroup", plain)
        _, _, expr = am._compare(q, rows, ref, 1e-6, sqls=("SELECT UPPER(region) region, SUM(n) n FROM m "
                                                           "GROUP BY UPPER(region)", ""))
        self.assertIn("doesn't regroup", expr)
        self.assertTrue(expr.startswith("comparison: question: duplicate keys"))

    def test_grade_bank_reads_a_form_key_against_the_best_answer(self):
        bank = {"model": "M", "questions": [{**self.Q, "level": 1, "expected_path": "governed_metric",
                                             "best_answer": {"sql": 'SELECT "order date (month)__id", '
                                                                    'SUM("net sales") AS "net sales" FROM t GROUP BY 1'}}]}
        results = {"results": [{"id": "Q1", "ok": True,
                                "rows": [{"order date (month) (id)": "202401", "net sales": "7"}],
                                "best_run": {"ok": True,
                                             "rows": [{"order date (month) (id)": "202401", "net sales": "7"}]}}]}
        graded = am.grade_bank(bank, results)
        self.assertEqual(graded["questions"][0]["verdict"], "pass")


# ── check --bank: output columns and AVG over a decimal literal ──────────────────────────────────

class BankLintTests(unittest.TestCase):
    def test_output_columns(self):
        self.assertEqual(am.output_columns('SELECT "region", SUM("net sales") AS total, COUNT(*) FROM {{MODEL}} '
                                           'GROUP BY 1'), ["region", "total", None])
        self.assertEqual(am.output_columns("WITH t AS (SELECT a FROM m) SELECT DISTINCT t.a, b d FROM t"), ["a", "d"])
        self.assertIsNone(am.output_columns("SELECT * FROM m"))
        self.assertEqual(am.output_columns("SELECT 'a, b' AS x, y FROM m"), ["x", "y"])

    def test_a_key_the_best_answer_doesnt_output_is_warned(self):
        q = {"id": "Q3", "level": 2, "question": "Sales by month?", "expected_path": "simple_sql",
             "key": ["order month"], "measures": ["total"],
             "sql": 'SELECT "order month", SUM("net sales") AS total FROM {{MODEL}} GROUP BY 1',
             "best_answer": {"sql": 'SELECT m AS month_of_order, SUM("net sales") total FROM {{MODEL}} GROUP BY 1'}}
        warns = am.check_bank({"model": "M", "questions": [q]})["warnings"]
        hit = [w for w in warns if "outputs no column" in w]
        self.assertEqual(len(hit), 1)
        self.assertIn("best answer", hit[0])
        self.assertIn("'order month'", hit[0])
        self.assertIn("month_of_order", hit[0])

    def test_a_form_key_selected_by_its_name_is_not_warned(self):
        q = {"key": ["region__code"], "measures": ["n"], "sql": 'SELECT "region__code", SUM(x) AS n FROM m GROUP BY 1'}
        self.assertEqual(am.bank_column_lint(q), [])

    def test_avg_over_a_decimal_literal_is_warned(self):
        self.assertEqual(len(am.avg_decimal_lint("SELECT AVG(1.0 * n) FROM t")), 1)
        self.assertEqual(len(am.avg_decimal_lint("SELECT avg(n * 1.0) AS a FROM t")), 1)
        self.assertEqual(am.avg_decimal_lint("SELECT 1.0 * SUM(a) / SUM(b) FROM t"), [])
        self.assertEqual(am.avg_decimal_lint("SELECT AVG(CAST(n AS DOUBLE)) FROM t"), [])
        self.assertEqual(am.avg_decimal_lint("SELECT 'AVG(1.0 * n)' AS label FROM t"), [])
        self.assertTrue(any("AVG(1.0 * n)" in w for w in am._sql_lint("SELECT AVG(1.0 * n) FROM t")))


# ── periods: snapshot vs event date, periods after the run date ────────────────────────────────────

def _scope(period_values, generated="2025-03-15", kind=None):
    scope = {"model": "Orders", "generated": generated, "anchor": {"metric": "net sales"},
             "attributes": [{"name": "customer id", "type": "bigint", "description": "Customer identifier.",
                             "profile": {"distinct_values": 500}},
                            {"name": "order month", "type": "date",
                             "profile": {"distinct_values": len(period_values), "min": min(period_values),
                                         "max": max(period_values), "values": list(period_values)}},
                            {"name": "region", "type": "varchar", "profile": {"distinct_values": 4}}],
             "metrics": [{"name": "net sales", "type": "double", "formula": "sum(t.csv.{NET SALES})"}]}
    if kind:
        scope["period"] = {"attribute": "order month", "kind": kind}
    return scope


class PeriodTests(unittest.TestCase):
    def test_period_date(self):
        self.assertEqual(am.period_date("2024-12-01"), datetime.date(2024, 12, 1))
        self.assertEqual(am.period_date("2024-12-01 00:00:00"), datetime.date(2024, 12, 1))
        self.assertEqual(am.period_date("202412"), datetime.date(2024, 12, 1))
        self.assertEqual(am.period_date("20241215"), datetime.date(2024, 12, 15))
        self.assertEqual(am.period_date("202,412"), datetime.date(2024, 12, 1))   # thousands separator shown
        self.assertIsNone(am.period_date("East"))
        self.assertIsNone(am.period_date("202413"))

    def test_latest_observed_skips_periods_after_the_run_date(self):
        scope = {"generated": "2025-03-15"}
        latest, future = am.latest_observed(["202501", "202502", "202503", "202506", "202512"], scope)
        self.assertEqual((latest, future), ("202503", ["202506", "202512"]))
        self.assertEqual(am.latest_observed(["A", "B"], scope), ("B", []))   # not dates: as given

    def _profile(self, one_share_rows):
        def answer(sql):
            self.assertIn('COUNT("net sales") AS r_', sql)
            self.assertIn('GROUP BY 1, 2', sql)
            return _reply(["entities", "one_period", "avg_periods"], [one_share_rows])
        runner = _Runner(answer)
        scope = _scope(["2025-01-01", "2025-02-01", "2025-03-01"])
        return am.period_profile(runner, "P", "Orders", scope), runner

    def test_a_snapshot_period(self):
        p, runner = self._profile(["500", "0", "3"])
        self.assertEqual((p["kind"], p["attribute"], p["entity"]), ("snapshot", "order month", "customer id"))
        self.assertEqual(len(runner.sql), 1)

    def test_an_event_date(self):
        p, _ = self._profile(["500", "498", "1.004"])
        self.assertEqual(p["kind"], "event")
        self.assertIn("latest-period flag would mean nothing", p["note"])

    def test_neither(self):
        p, _ = self._profile(["500", "300", "1.4"])
        self.assertEqual(p["kind"], "mixed")

    def test_periods_after_the_run_date_are_reported(self):
        scope = _scope(["2025-01-01", "2025-02-01", "2025-09-01"])
        p = am.period_profile(_Runner(lambda sql: _reply(["entities", "one_period", "avg_periods"],
                                                         [["500", "0", "2"]])), "P", "Orders", scope)
        self.assertEqual(p["after_run_date"]["largest"], "2025-09-01")
        self.assertEqual(p["latest_observed"], "2025-02-01")

    def test_no_entity_no_query(self):
        scope = _scope(["2025-01-01", "2025-02-01"])
        scope["anchor"] = None
        runner = _Runner(lambda sql: self.fail("no query without an anchor"))
        self.assertEqual(am.period_profile(runner, "P", "Orders", scope)["kind"], "unknown")

    def test_no_latest_period_lookup_for_an_event_date(self):
        values = ["2025-01-01", "2025-02-01", "2025-03-01"]
        kinds = {k: [s["kind"] for s in am.lookup_suggestions(_scope(values, kind=k))] for k in ("event", "snapshot")}
        self.assertNotIn("period_lookup", kinds["event"])
        self.assertIn("period_lookup", kinds["snapshot"])

    def test_the_latest_flag_never_marks_a_future_period(self):
        values = ["2025-01-01", "2025-02-01", "2025-03-01", "2025-11-01"]
        scope = _scope(values, kind="snapshot")
        sugg = [s for s in am.lookup_suggestions(scope) if s["kind"] == "period_lookup"]
        runner = _Runner(lambda sql: _reply(["value"], [[v] for v in values]) if "DISTINCT" in sql
                         else _reply(["n"], [["0"]]))
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            built = am.build_lookups(runner, "P", scope, sugg, d)
            entry = built[0]
            with open(entry["csv"], encoding="utf-8") as f:
                flags = {line.split(",")[0]: line.strip().split(",")[-1] for line in f.read().splitlines()[1:]}
        self.assertEqual(flags["2025-03-01"], "Y")
        self.assertEqual(flags["2025-11-01"], "N")
        self.assertIn("after the run date", entry["latest_note"])


# ── anchor tie-break, summed-rate words ─────────────────────────────────────────────────────────

class AnchorTieTests(unittest.TestCase):
    METRICS = [{"name": "planned discount", "type": "double", "formula": "sum(t.csv.{PLANNED DISCOUNT})",
                "description": "Empty in this load: 0 on every row; do not use."},
               {"name": "returns amount", "type": "double", "formula": "sum(t.csv.{RETURNS})"},
               {"name": "net sales", "type": "double", "formula": "sum(t.csv.{NET SALES})"}]

    def test_a_tie_prefers_a_metric_with_values(self):
        def answer(sql):
            if "MAX(ABS(" in sql:
                self.assertTrue(sql.index('"returns amount"') < sql.index('"planned discount"'))   # described empty: last
                return _reply(["z0", "z1", "z2"], [["0", "125.5", "0"]])
            return _reply(["c0", "c1", "c2"], [["1,000", "1,000", "1,000"]])
        anchor = am.choose_anchor(_Runner(answer), "P", "Orders", self.METRICS)
        self.assertEqual(anchor["metric"], "net sales")
        self.assertIn("non-zero", anchor["tie_break"])

    def test_no_tie_no_extra_query(self):
        runner = _Runner(lambda sql: _reply(["c0", "c1", "c2"], [["1,000", "900", "800"]]))
        anchor = am.choose_anchor(runner, "P", "Orders", self.METRICS)
        self.assertEqual((anchor["metric"], len(runner.sql)), ("planned discount", 1))
        self.assertNotIn("tie_break", anchor)


class SummedRateWordTests(unittest.TestCase):
    def test_term_as_a_horizon_is_not_a_rate(self):
        self.assertIsNone(am._SUMMED_NONADDITIVE.search(am._words("long term spend amount")))
        self.assertIsNotNone(am._SUMMED_NONADDITIVE.search(am._words("contract term months")))
        self.assertIsNotNone(am._SUMMED_NONADDITIVE.search(am._words("discount rate amount")))


if __name__ == "__main__":
    unittest.main()


# ── plan: Studio publish after lookups, unfiltered counts, names through the phases ───────────────────────

def _f(fid, category, actions, **kw):
    return {"id": fid, "title": f"title {fid}", "category": category, "impact": "high", "effort": "small",
            "status": "approved", "tests": [], "actions": actions, **kw}


class PlanNameTests(unittest.TestCase):
    def test_a_phase_that_adds_lookup_tables_asks_for_a_studio_publish(self):
        lk = {"kind": "add_lookup_table", "args": {"csv": "lookups/region.csv", "key_attribute": "region",
                                                   "new_attributes": ["region group"]}}
        plan = am.build_plan({"findings": [_f("F1", "lookup", [lk])]}, model_id="MID")
        after = [a["what"] for a in plan["phases"][0]["after"]]
        self.assertTrue(any("Publish the model from Studio" in w for w in after))

    def test_an_unfiltered_distinct_count_is_planned(self):
        cm = {"kind": "create_conditional_metric", "args": {"name": "customer count", "count_attribute": "customer"}}
        plan = am.build_plan({"findings": [_f("F1", "missing_metric", [cm])]}, model_id="MID")
        self.assertEqual(plan["problems"], [])
        argv = plan["phases"][0]["steps"][0]["apply"]["argv"]
        self.assertIn("--count-attribute", argv)
        self.assertNotIn("--attribute", argv)
        cm["args"]["level_rule"] = "last"
        self.assertTrue(any("need a filter" in p for p in
                            am.build_plan({"findings": [_f("F1", "missing_metric", [cm])]}, model_id="MID")["problems"]))
        cm["args"].pop("level_rule")
        cm["args"]["attribute"] = "region"
        self.assertTrue(any("go together" in p for p in
                            am.build_plan({"findings": [_f("F1", "missing_metric", [cm])]}, model_id="MID")["problems"]))

    def test_a_rename_that_frees_a_name_runs_before_the_phase_that_needs_it(self):
        ta = {"kind": "add_time_attributes", "args": {"attribute": "start date", "intervals": ["year", "month"],
                                                      "names": {"year": "Year"}}}
        rn = {"kind": "rename_describe", "args": {"items": [
            {"object_type": "attribute", "current_name": "year", "new_name": "Legacy Year"},
            {"object_type": "attribute", "current_name": "region", "description": "Sales region."}]}}
        plan = am.build_plan({"findings": [_f("F1", "attribute", [ta]), _f("F2", "description", [rn])]},
                             model_id="MID", bundle_dir="b")
        self.assertEqual(plan["problems"], [])
        by_phase = {ph["phase"]: ph["steps"] for ph in plan["phases"]}
        first = by_phase[3][0]
        self.assertEqual((first["kind"], first["finding"]), ("rename_describe", "F2"))
        self.assertIn("frees 'year' for the year time attribute of 'start date' (F1)", first["risks"][0])
        self.assertEqual(by_phase[3][1]["kind"], "add_time_attributes")
        files = plan["files"]
        self.assertEqual(files["b/specs/rename_free_names_phase_3.json"]["items"],
                         [{"object_type": "attribute", "current_name": "year", "new_name": "Legacy Year"}])
        self.assertEqual(files["b/specs/rename_describe.json"]["items"],
                         [{"object_type": "attribute", "current_name": "region", "description": "Sales region."}])
        self.assertEqual(files["b/specs/time_attributes.json"]["items"][0]["names"], {"year": "Year"})

    def test_a_rename_onto_a_created_name_and_a_name_created_twice_are_problems(self):
        ta = {"kind": "add_time_attributes", "args": {"attribute": "start date", "intervals": ["year"],
                                                      "names": {"year": "Year"}}}
        rn = {"kind": "rename_describe", "args": {"items": [
            {"object_type": "attribute", "current_name": "fiscal year", "new_name": "year"}]}}
        cm = {"kind": "create_compound_metric", "args": {"name": "Year", "formula": "[a] / [b]"}}
        plan = am.build_plan({"findings": [_f("F1", "attribute", [ta]), _f("F2", "description", [rn]),
                                           _f("F3", "missing_metric", [cm])]}, model_id="MID")
        self.assertTrue(any("'Year' is created twice" in p for p in plan["problems"]))
        self.assertTrue(any("renames 'fiscal year' to 'year'" in p for p in plan["problems"]))

    def test_time_attribute_names_must_match_intervals(self):
        ta = {"kind": "add_time_attributes", "args": {"attribute": "d", "intervals": ["year"],
                                                      "names": {"month": "Month"}}}
        plan = am.build_plan({"findings": [_f("F1", "attribute", [ta])]}, model_id="MID")
        self.assertTrue(any("intervals not in args.intervals" in p for p in plan["problems"]))
