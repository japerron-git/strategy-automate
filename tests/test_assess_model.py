"""Tests for skills/strategy-model-assessment/scripts/assess_model.py: parsing what the Mosaic MCP
tools return, scope / run / grade against a fake MCP runner, the change-plan ordering and commands,
and the file checks. Nothing touches the network.
"""
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import _hermetic  # noqa: E402,F401  (scrub MSTR_*/proxy env, private secret store)

import argparse
import contextlib
import copy
import csv
import io
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL = os.path.join(ROOT, "skills", "strategy-model-assessment")
sys.path.insert(0, os.path.join(SKILL, "scripts"))

import assess_model as am  # noqa: E402

EXAMPLES = os.path.join(SKILL, "examples")

SEMANTICS = """## Schema: retail. Table: Store Sales
### Attributes(Dimensions)
- **region** (varchar): Sales region.
- **snapshot month** (date): Month of the snapshot (YYYY-MM-01).
- **months active** (bigint)
### Metrics(Measures)
- **net sales** (double): Sales after discounts and returns.
sum(sales.csv.{NET SALES})
- **active customers** (bigint): Distinct customers with a purchase in the period.
Count(Customer)
"""


def _md(columns, rows):
    lines = ["| " + " | ".join(columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
    lines += ["| " + " | ".join("" if v is None else str(v) for v in r) + " |" for r in rows]
    return "\n".join(lines) + f"\n\n*Query returned {len(rows)} rows*"


def _text(t, error=False):
    out = {"content": [{"type": "text", "text": t}]}
    if error:
        out["isError"] = True
    return out


class FakeRunner:
    """Answers get_semantics with SEMANTICS and queries from a {fragment: reply} table."""

    def __init__(self, replies=None):
        self.replies = replies or {}
        self.calls = []

    def call(self, tool, arguments):
        self.calls.append((tool, arguments))
        if tool == "get_semantics":
            return _text(SEMANTICS)
        sql = arguments["query"]
        for fragment, reply in self.replies.items():
            if fragment in sql:
                return reply(sql) if callable(reply) else reply
        return _text("Query failed: unknown column", error=True)


# ── parsing ─────────────────────────────────────────────────────────────────────────────────

class ParsingTests(unittest.TestCase):
    def test_markdown_table_with_escaped_pipe_and_blank_cell(self):
        cols, rows = am.parse_markdown_table("| a | b |\n| --- | --- |\n| x | 1 |\n| y\\|z |  |\n\n*2 rows*")

        self.assertEqual(cols, ["a", "b"])
        self.assertEqual(rows, [{"a": "x", "b": "1"}, {"a": "y|z", "b": None}])

    def test_table_split_across_text_blocks(self):
        reply = {"content": [{"type": "text", "text": "| v |\n| --- |\n| a |\n| b |\n"},
                             {"type": "text", "text": "\n| c |\n"}, {"type": "text", "text": "\n*Query returned 3 rows*"}]}
        self.assertEqual([r["v"] for r in am.parse_query_result(reply)["rows"]], ["a", "b", "c"])

    def test_query_result_shapes(self):
        md = am.parse_query_result(_text(_md(["region", "sales"], [["East", 10]])))
        self.assertEqual((md["ok"], md["rows"]), (True, [{"region": "East", "sales": "10"}]))

        structured = am.parse_query_result({"structuredContent": {"columns": ["r", "n"], "rows": [["W", 2]]}})
        self.assertEqual(structured["rows"], [{"r": "W", "n": 2}])

        as_json = am.parse_query_result(_text(json.dumps([{"r": "N", "n": 3}])))
        self.assertEqual(as_json["columns"], ["r", "n"])

        err = am.parse_query_result(_text("MOSAIC_SQL_ERROR: column not found", error=True))
        self.assertFalse(err["ok"])
        self.assertIn("column not found", err["error"])

        empty = am.parse_query_result(_text("*Query returned 0 rows*"))
        self.assertEqual((empty["ok"], empty["rows"]), (True, []))

        junk = am.parse_query_result(_text("something unexpected"))
        self.assertFalse(junk["ok"])

    def test_semantics_attributes_metrics_and_formulas(self):
        sem = am.parse_semantics(SEMANTICS)

        self.assertEqual([a["name"] for a in sem["attributes"]], ["region", "snapshot month", "months active"])
        self.assertEqual(sem["attributes"][2], {"name": "months active", "type": "bigint", "description": ""})
        self.assertEqual(sem["metrics"][0]["formula"], "sum(sales.csv.{NET SALES})")
        self.assertEqual(sem["metrics"][1]["type"], "bigint")


# ── scope / run ─────────────────────────────────────────────────────────────────────────────

class ScopeTests(unittest.TestCase):
    def test_profiles_each_attribute_and_samples_low_cardinality_ones(self):
        runner = FakeRunner({
            'COUNT(DISTINCT "region")': _text(_md(["distinct_values", "min_value", "max_value"], [[3, "East", "West"]])),
            'SELECT DISTINCT CAST("region"': _text(_md(["value"], [["East"], ["North"], ["West"]])),
            'COUNT(DISTINCT "snapshot month")': _text(_md(["distinct_values", "min_value", "max_value"],
                                                          [[38, "2023-01-01", "2026-02-01"]])),
        })
        out = am.scope_model(runner, "retail", "Store Sales", sample_max=5, where="\"region\" <> 'Test'",
                             profile_batch=1)

        region, month, active = out["attributes"]
        self.assertEqual(region["profile"], {"distinct_values": 3.0, "min": "East", "max": "West",
                                             "values": ["East", "North", "West"]})
        self.assertNotIn("values", month["profile"])                    # 38 > sample_max
        self.assertIn("error", active["profile"])                       # the query failed; recorded, not fatal
        self.assertEqual(out["notes"]["undescribed_attributes"], ["months active"])
        sqls = [a["query"] for t, a in runner.calls if t == "query"]
        self.assertTrue(all('FROM "store sales" WHERE "region" <> \'Test\'' in s for s in sqls))

    def test_no_profile_and_empty_semantics(self):
        runner = FakeRunner()
        out = am.scope_model(runner, "retail", "Store Sales", profile=False)
        self.assertEqual([t for t, _ in runner.calls], ["get_semantics", "query"])   # semantics + the anchor
        self.assertEqual(len(out["metrics"]), 2)

        class Empty(FakeRunner):
            def call(self, tool, arguments):
                return _text("## Schema: retail")
        with self.assertRaises(SystemExit):
            am.scope_model(Empty(), "retail", "Store Sales")


def _bank():
    return {"version": 1, "project": "retail", "model": "Store Sales", "questions": [
        {"id": "Q1", "level": 1, "question": "Sales by region?", "expected_path": "governed_metric",
         "sql": "SELECT region, SUM(\"Net Sales\") AS sales FROM {{MODEL}} GROUP BY region",
         "key": ["region"], "measures": ["sales"],
         "expected": {"source": "inline", "strength": "reference",
                      "rows": [{"region": "East", "sales": 100}, {"region": "West", "sales": 50}]}},
        {"id": "Q2", "level": 2, "question": "Sales in the latest month?", "expected_path": "simple_sql",
         "sql": "SELECT SUM(\"Net Sales\") AS sales FROM {{MODEL}} WHERE latest = 'Y'", "measures": ["sales"],
         "expected": {"source": "same_model_sql", "sql": "SELECT SUM(raw_sales) AS sales FROM {{MODEL}} WHERE m = 1"}},
        {"id": "Q3", "level": 3, "question": "Top region?", "expected_path": "simple_sql",
         "sql": "SELECT broken FROM {{MODEL}}", "expected": {"source": "none"}},
        {"id": "Q4", "level": 5, "question": "Retention?", "expected_path": "not_answerable", "sql": "",
         "expected": {"source": "none"}},
        {"id": "Q5", "level": 4, "question": "Balance by region?", "expected_path": "simple_sql",
         "sql": "SELECT region, SUM(bal) AS balance FROM {{MODEL}} GROUP BY region", "key": ["region"],
         "measures": ["balance"], "expected": {"source": "reference_file", "file": "q5.csv"}},
    ]}


def _replies():
    return {
        'SUM("Net Sales") AS sales FROM "store sales" GROUP BY': _text(_md(["region", "sales"],
                                                                         [["East", 100], ["West", 50]])),
        "WHERE latest = 'Y'": _text(_md(["sales"], [[150]])),
        "SUM(raw_sales)": _text(_md(["sales"], [[149]])),
        "SUM(bal)": _text(_md(["region", "balance"], [["East", 7], ["West", 3]])),
    }


class RunTests(unittest.TestCase):
    def test_runs_questions_and_same_model_expected_sql(self):
        runner = FakeRunner(_replies())
        out = am.run_bank(runner, "retail", _bank())

        by_id = {r["id"]: r for r in out["results"]}
        self.assertEqual(by_id["Q1"]["rows"], [{"region": "East", "sales": "100"}, {"region": "West", "sales": "50"}])
        self.assertIn('FROM "store sales"', by_id["Q1"]["sql"])
        self.assertEqual(by_id["Q2"]["expected_run"]["rows"], [{"sales": "149"}])
        self.assertFalse(by_id["Q3"]["ok"])
        self.assertTrue(by_id["Q4"]["skipped"])
        self.assertEqual(out["summary"], {"questions": 5, "ok": 3, "errors": 2})
        self.assertTrue(all(a["project"] == "retail" for t, a in runner.calls))

    def test_filters_and_row_cap(self):
        runner = FakeRunner(_replies())
        out = am.run_bank(runner, "retail", _bank(), levels="1,4", max_rows=1)

        self.assertEqual([r["id"] for r in out["results"]], ["Q1", "Q5"])
        self.assertEqual((out["results"][0]["row_count"], len(out["results"][0]["rows"]),
                          out["results"][0]["truncated"]), (2, 1, True))
        self.assertEqual([r["id"] for r in am.run_bank(runner, "retail", _bank(), only="Q2")["results"]], ["Q2"])

    def test_runner_exceptions_are_recorded(self):
        class Broken(FakeRunner):
            def call(self, tool, arguments):
                raise ConnectionError("network down")
        out = am.run_bank(Broken(), "retail", _bank(), only="Q1")
        self.assertIn("network down", out["results"][0]["error"])


# ── grade ───────────────────────────────────────────────────────────────────────────────────

class GradeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="assess-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        with open(os.path.join(self.tmp, "q5.csv"), "w", encoding="utf-8") as f:
            f.write("region,balance\nEast,7\nWest,4\n")
        self.results = am.run_bank(FakeRunner(_replies()), "retail", _bank())

    def test_verdicts_and_summary(self):
        out = am.grade_bank(_bank(), self.results, bank_dir=self.tmp)

        v = {g["id"]: g["verdict"] for g in out["questions"]}
        self.assertEqual(v, {"Q1": "pass", "Q2": "fail", "Q3": "error", "Q4": "ungraded", "Q5": "fail"})
        q2 = next(g for g in out["questions"] if g["id"] == "Q2")
        self.assertEqual(q2["strength"], "same_model")
        self.assertEqual(out["summary"]["by_verdict"], {"pass": 1, "fail": 2, "error": 1, "ungraded": 1})
        self.assertEqual(out["summary"]["by_level"]["1"], {"pass": 1})
        self.assertEqual(out["summary"]["by_expected_path"]["simple_sql"], {"fail": 2, "error": 1})

    def test_tolerance_turns_a_near_miss_into_a_pass(self):
        out = am.grade_bank(_bank(), self.results, bank_dir=self.tmp, tolerance=0.01)
        self.assertEqual({g["id"]: g["verdict"] for g in out["questions"]}["Q2"], "pass")

    def test_missing_reference_file_and_not_run_question(self):
        os.remove(os.path.join(self.tmp, "q5.csv"))
        results = copy.deepcopy(self.results)
        results["results"] = [r for r in results["results"] if r["id"] != "Q1"]
        out = am.grade_bank(_bank(), results, bank_dir=self.tmp)

        v = {g["id"]: g for g in out["questions"]}
        self.assertEqual(v["Q1"]["verdict"], "not_run")
        self.assertEqual(v["Q5"]["verdict"], "error")

    def test_duplicate_keys_are_an_error_not_a_crash(self):
        results = copy.deepcopy(self.results)
        q1 = next(r for r in results["results"] if r["id"] == "Q1")
        q1["rows"] = [{"region": "East", "sales": "1"}, {"region": "East", "sales": "2"}]
        out = am.grade_bank(_bank(), results, bank_dir=self.tmp)
        self.assertEqual({g["id"]: g["verdict"] for g in out["questions"]}["Q1"], "error")

    def test_previous_run_reports_regressions(self):
        previous = {"questions": [{"id": "Q1", "verdict": "fail"}, {"id": "Q2", "verdict": "pass"},
                                  {"id": "Q3", "verdict": "error"}]}
        out = am.grade_bank(_bank(), self.results, bank_dir=self.tmp, previous=previous)

        self.assertEqual(out["regressions"], ["Q2"])
        self.assertEqual({c["id"] for c in out["changes"]}, {"Q1", "Q2"})


# ── plan ────────────────────────────────────────────────────────────────────────────────────

def _finding(fid, category, status="approved", actions=None, **kw):
    base = {"id": fid, "title": f"title {fid}", "category": category, "impact": "high", "effort": "small",
            "status": status, "tests": [f"Q-{fid}"], "actions": actions if actions is not None else
            [{"kind": "manual", "steps": "do it"}]}
    base.update(kw)
    return base


class PlanTests(unittest.TestCase):
    def test_phase_order_dependencies_and_status_filter(self):
        doc = {"model": "Store Sales", "findings": [
            _finding("F5", "missing_metric", depends_on=["F4"]),
            _finding("F4", "missing_metric"),
            _finding("F1", "metric_defect"),
            _finding("F2", "data_type"),
            _finding("F3", "description", status="declined", decision_note="not needed"),
            _finding("F6", "metric_defect", status="modified", decision_note="narrower filter"),
        ]}
        plan = am.build_plan(doc, model_id="MID")

        self.assertEqual(plan["problems"], [])
        self.assertEqual([(p["phase"], p["findings"]) for p in plan["phases"]],
                         [(2, ["F2"]), (4, ["F1", "F6"]), (5, ["F4", "F5"])])
        self.assertEqual(plan["not_planned"], [{"id": "F3", "status": "declined"}])
        phase2 = plan["phases"][0]
        self.assertIn("publish", [c["command"]["argv"][2] for c in phase2["after"]])
        self.assertNotIn("publish", [c["command"]["argv"][2] for c in plan["phases"][1]["after"]])
        self.assertEqual(plan["phases"][1]["after"][-1]["command"]["argv"][2:], ["certify", "--object-id", "MID"])
        self.assertEqual(plan["phases"][2]["unit_tests"], ["Q-F4", "Q-F5"])
        self.assertIn("results_after_phase_5.json", plan["phases"][2]["regression"][0]["argv"])

    def test_dependency_on_a_later_phase_or_unapproved_finding(self):
        doc = {"findings": [_finding("F1", "metric_defect", depends_on=["F2"]), _finding("F2", "missing_metric"),
                            _finding("F3", "description", depends_on=["F4"]),
                            _finding("F4", "missing_metric", status="proposed")]}
        plan = am.build_plan(doc, model_id="MID")

        self.assertTrue(any("later phase" in p for p in plan["problems"]))
        self.assertTrue(any("F3 depends on F4, which is proposed" in w for w in plan["warnings"]))

    def test_commands_for_each_action_kind(self):
        mid = "MID"
        cond = am.action_commands({"kind": "create_conditional_metric", "args": {
            "name": "Active Balance", "source_metric": "Balance", "attribute": "Status", "elements": ["A", "B"],
            "and": [["Channel", "Web"]], "level_rule": "last", "level_attribute": "Snapshot Month",
            "format": "currency"}}, mid)
        self.assertEqual(cond["problems"], [])
        self.assertEqual(cond["apply"][2:], [
            "create-conditional-metric", "--model-id", mid, "--name", "Active Balance", "--source-metric", "Balance",
            "--attribute", "Status", "--elements", "A", "B", "--and", "Channel", "Web", "--level-rule", "last",
            "--level-attribute", "Snapshot Month", "--format", "currency"])
        self.assertEqual(cond["dry_run"], cond["apply"] + ["--dry-run"])

        comp = am.action_commands({"kind": "create_compound_metric", "args": {
            "name": "Margin", "formula": "[A] - [B]", "join": "outer", "replace": True}}, mid)
        self.assertEqual(comp["apply"][2:], ["create-compound-metric", "--model-id", mid, "--name", "Margin",
                                             "--formula", "[A] - [B]", "--join", "outer", "--replace"])

        dele = am.action_commands({"kind": "delete_derived_metric", "args": {"name": "Old", "force": True}}, mid)
        self.assertEqual(dele["apply"][-2:], ["--yes", "--force"])
        self.assertEqual(dele["dry_run"][-1], "--dry-run")

        patch = am.action_commands({"kind": "patch_model_object", "args": {
            "kind": "attribute", "object_id": "AID", "json_file": "form_type.json"}}, mid)
        self.assertEqual(patch["dry_run"][2], "get-model-object")
        self.assertEqual(patch["apply"][-5:], ["--json-file", "form_type.json", "--before-out", "before_AID.json", "--yes"])

        facts = am.action_commands({"kind": "patch_fact_metrics", "args": {"spec": "spec.yaml"}}, mid)
        self.assertEqual(facts["dry_run"][-1], "--dry-run")
        self.assertEqual(am.action_commands({"kind": "publish"}, mid)["apply"][2:],
                         ["publish", "--model-id", mid, "--poll-seconds", "1800"])   # large models publish slowly
        self.assertIsNone(am.action_commands({"kind": "certify"}, mid)["dry_run"])

    def test_level_metric_and_weighted_average_commands(self):
        lvl = am.action_commands({"kind": "create_level_metric", "args": {
            "name": "Original Amount", "source_metric": "Original Amount (raw)", "entity_attribute": "Account",
            "format": "currency"}}, "MID")
        self.assertEqual(lvl["apply"][2:], ["create-level-metric", "--model-id", "MID", "--name", "Original Amount",
                                            "--source-metric", "Original Amount (raw)", "--entity-attribute",
                                            "Account", "--format", "currency"])
        wa = am.action_commands({"kind": "create_weighted_average", "args": {
            "name": "Weighted Avg Score", "value_attribute": "Score", "weight_metric": "Original Amount (raw)",
            "entity_attribute": "Account"}}, "MID")
        self.assertEqual(wa["problems"], [])
        self.assertEqual(wa["dry_run"][-1], "--dry-run")
        self.assertIn("--value-attribute", wa["apply"])
        both = am.action_commands({"kind": "create_weighted_average", "args": {
            "name": "x", "value_attribute": "a", "value_metric": "b", "weight_metric": "w", "entity_attribute": "e"}},
            "MID")
        self.assertTrue(any("exactly one" in p for p in both["problems"]))

    def test_bad_actions_are_problems(self):
        bad = [
            ({"kind": "create_conditional_metric", "args": {"name": "x", "attribute": "a", "elements": ["1"]}},
             "exactly one"),
            ({"kind": "create_conditional_metric", "args": {"name": "x", "attribute": "a", "elements": ["1"],
                                                            "count_attribute": "c", "and": [["only"]]}}, "args.and"),
            ({"kind": "create_compound_metric", "args": {"name": "x"}}, "args.formula"),
            ({"kind": "delete_derived_metric", "args": {}}, "args.name or args.object_id"),
            ({"kind": "manual"}, "missing steps"),
            ({"kind": "teleport"}, "unknown action kind"),
        ]
        for action, text in bad:
            with self.subTest(action=action):
                self.assertTrue(any(text in p for p in am.action_commands(action, "MID")["problems"]))

    def test_powershell_and_posix_quoting(self):
        argv = ["python", "x.py", "--name", "Ending Balance (Active)", "--formula", "[A] - [B]", "--desc", "it's"]
        self.assertEqual(am.render(argv, "powershell"),
                         "python x.py --name 'Ending Balance (Active)' --formula '[A] - [B]' --desc 'it''s'")
        self.assertEqual(am.render(argv), "python x.py --name 'Ending Balance (Active)' --formula '[A] - [B]' "
                                          "--desc 'it'\"'\"'s'")
        self.assertIsNone(am.render(None))

    def test_missing_model_id_uses_a_placeholder_and_warns(self):
        plan = am.build_plan({"findings": [_finding("F1", "metric_defect")]})
        self.assertTrue(any("placeholder" in w for w in plan["warnings"]))
        self.assertIn("<MODEL_ID>", plan["phases"][0]["after"][0]["command"]["argv"])


# ── check ───────────────────────────────────────────────────────────────────────────────────

class CheckTests(unittest.TestCase):
    def test_examples_are_valid(self):
        for flag, name in (("--bank", "question_bank.example.json"), ("--findings", "findings.example.json"),
                           ("--domain", "domain_notes.example.json")):
            with self.subTest(name=name), contextlib.redirect_stdout(io.StringIO()) as out:
                code = am.main(["check", flag, os.path.join(EXAMPLES, name)])
            self.assertEqual(code, 0, out.getvalue())

    def test_bank_problems(self):
        bank = _bank()
        bank["questions"] += [{"id": "Q1", "level": 7, "question": "", "expected_path": "guess", "sql": "",
                               "key": "region", "expected": {"source": "inline"}},
                              {"id": "Q9", "level": 1, "question": "q", "expected_path": "simple_sql",
                               "sql": "SELECT 1 FROM x", "expected": {"source": "same_model_sql", "sql": "s",
                                                                       "strength": "reference"}}]
        report = am.check_bank(bank)
        text = " ".join(report["problems"])
        for fragment in ("duplicate ids", "level must be 1-5", "missing question text", "expected_path must be",
                         "missing sql", "key must be a list", "inline expected needs rows",
                         "same-model SQL can only be strength same_model"):
            self.assertIn(fragment, text)
        self.assertTrue(any("placeholder" in w for w in report["warnings"]))
        self.assertEqual(report["mix"]["1"]["count"], 2)

    def test_findings_problems_and_cycles(self):
        doc = {"findings": [
            _finding("F1", "metric_defect", depends_on=["F2"]),
            _finding("F2", "missing_metric", depends_on=["F1"]),
            _finding("F3", "mystery"),
            _finding("F4", "mystery", phase=6),
            _finding("F5", "format", depends_on=["F99"], impact="huge", actions=[{"kind": "teleport"}]),
            _finding("F6", "format", status="declined"),
        ]}
        report = am.check_findings(doc)
        text = " ".join(report["problems"])
        for fragment in ("dependency cycle", "unknown category 'mystery'", "unknown finding F99",
                         "impact must be one of", "unknown action kind"):
            self.assertIn(fragment, text)
        self.assertNotIn("F4: unknown category", text)
        self.assertTrue(any("F6: declined without a decision_note" in w for w in report["warnings"]))

    def test_domain_claims_need_sources(self):
        doc = {"claims": [{"id": "D1", "claim": "A definition", "sources": [], "verified_against_data": "yes"},
                          {"id": "D2", "claim": "Another", "sources": [{"title": "Provider guide"}],
                           "verified_against_data": "contradicted"},
                          {"id": "D3", "claim": "", "sources": [{"url": "https://example.com/guide"}],
                           "verified_against_data": "maybe"}]}
        report = am.check_domain(doc)
        text = " ".join(report["problems"])
        self.assertIn("D1: no source", text)
        self.assertIn("D3: missing claim text", text)
        self.assertIn("D3: verified_against_data must be", text)
        self.assertNotIn("D2:", text)
        self.assertTrue(any("D2: contradicted" in w for w in report["warnings"]))


class UseCaseAndDocumentTests(unittest.TestCase):
    DOMAIN = {"claims": [
        {"id": "D1", "claim": "Org definition of churn", "verified_against_data": "yes",
         "sources": [{"title": "Internal KPI definitions", "type": "user_document", "location": "section 2.1"}]},
        {"id": "D2", "claim": "Provider code list", "verified_against_data": "not_applicable",
         "sources": [{"title": "Provider guide", "type": "user_document"}, {"url": "https://example.com/g", "type": "blog"}]}],
        "use_cases": [{"id": "U1", "description": "Monthly churn report", "audience": "management",
                       "priority": "high", "source": "Internal KPI definitions, section 1"},
                      {"id": "U2", "description": "Store comparisons", "audience": "analysts", "priority": "medium",
                       "source": "user statement"},
                      {"id": "U3", "description": "", "priority": "urgent"}]}

    def test_domain_lists_user_documents_and_checks_use_cases(self):
        report = am.check_domain(self.DOMAIN)

        self.assertEqual(report["user_documents"], ["Internal KPI definitions", "Provider guide"])
        self.assertEqual(report["use_cases"], 3)
        text = " ".join(report["problems"])
        self.assertIn("U3: missing use-case description", text)
        self.assertIn("U3: priority must be one of", text)
        warnings = " ".join(report["warnings"])
        self.assertIn("'Provider guide' cited without a section or page", warnings)
        self.assertIn("source type 'blog'", warnings)
        self.assertIn("U3: no source", warnings)
        self.assertNotIn("Internal KPI definitions' cited without", warnings)

    def test_no_use_cases_is_a_warning(self):
        report = am.check_domain({"claims": []})
        self.assertTrue(any("no use_cases" in w for w in report["warnings"]))

    def test_coverage_against_the_bank(self):
        bank = {"model": "M", "questions": [
            {"id": "Q1", "use_case": "U2", "domain_claims": ["D1"]},
            {"id": "Q2", "use_case": "U2", "domain_claims": ["D9"]},
            {"id": "Q3", "use_case": "U7"},
            {"id": "Q4"}]}
        report = am.check_coverage(bank, self.DOMAIN)

        text = " ".join(report["problems"])
        self.assertIn("Q2: cites unknown domain claim D9", text)
        self.assertIn("Q3: unknown use case U7", text)
        self.assertIn("use case U1 has no questions", text)
        self.assertIn("use case U3 has no questions", text)
        self.assertEqual(report["questions_per_use_case"], {"U1": 0, "U2": 2, "U3": 0})
        self.assertTrue(any("Q4: no use_case" in w for w in report["warnings"]))

    def test_high_priority_use_case_with_a_thin_share_is_flagged(self):
        domain = {"claims": [], "use_cases": [{"id": "U1", "description": "a", "priority": "high", "source": "s"},
                                              {"id": "U2", "description": "b", "priority": "low", "source": "s"}]}
        bank = {"questions": [{"id": "Q1", "use_case": "U1"}] + [{"id": f"Q{i}", "use_case": "U2"} for i in range(2, 6)]}
        report = am.check_coverage(bank, domain)
        self.assertTrue(any("U1 is high priority" in w for w in report["warnings"]))

    def test_grade_summarizes_by_use_case(self):
        bank = _bank()
        bank["questions"][0]["use_case"] = "U1"
        bank["questions"][1]["use_case"] = "U1"
        results = am.run_bank(FakeRunner(_replies()), "retail", bank, levels="1,2")
        out = am.grade_bank(bank, results)
        self.assertEqual(out["summary"]["by_use_case"]["U1"], {"pass": 1, "fail": 1})
        self.assertEqual(out["questions"][0]["use_case"], "U1")

    def test_check_cli_adds_coverage_when_bank_and_domain_are_given(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = am.main(["check", "--bank", os.path.join(EXAMPLES, "question_bank.example.json"),
                            "--domain", os.path.join(EXAMPLES, "domain_notes.example.json")])
        report = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(report["coverage"]["questions_per_use_case"], {"U1": 1, "U2": 1, "U3": 1})


# ── replay mode, maturity, relationships, lookups ──────────────────────────────────────────

class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="assess-replay-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _save(self, name, text):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
            f.write(text)

    def test_scope_collects_pending_calls_then_completes(self):
        out_path = os.path.join(self.dir, "scope.json")
        argv = ["--replies", self.dir, "scope", "--project", "retail", "--model", "Store Sales", "--out", out_path,
                "--sample-max", "5", "--profile-batch", "1"]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(am.main(argv), 3)                             # nothing saved yet
        with open(os.path.join(self.dir, "pending.json"), encoding="utf-8") as f:
            self.assertEqual([c["tool"] for c in json.load(f)["calls"]], ["get_semantics"])

        self._save("semantics.txt", SEMANTICS)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(am.main(argv), 3)                             # now the profiling queries
        with open(os.path.join(self.dir, "pending.json"), encoding="utf-8") as f:
            calls = json.load(f)["calls"]
        self.assertEqual(len(calls), 4)                                    # 3 profiles + the anchor
        for c in calls:
            sql = c["arguments"]["query"]
            self.assertEqual(c["file"], am.query_id(sql) + ".txt")
            if 'COUNT("net sales")' in sql:
                self._save(c["file"], _md(["c0"], [["1,200"]]))
            elif '"region"' in sql:
                self._save(c["file"], _md(["distinct_values", "min_value", "max_value"], [[2, "East", "West"]]))
            elif '"months active"' in sql:
                self._save(c["file"], "ERROR: column cannot be profiled")
            else:
                self._save(c["file"], json.dumps({"content": [{"type": "text", "text": _md(
                    ["distinct_values", "min_value", "max_value"], [[38, "2023-01-01", "2026-02-01"]])}]}))
        with contextlib.redirect_stdout(io.StringIO()) as printed, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(am.main(argv), 3)                             # the region sample query
        self.assertNotIn('"ok": true', printed.getvalue())
        with open(os.path.join(self.dir, "pending.json"), encoding="utf-8") as f:
            (sample,) = json.load(f)["calls"]
        self.assertIn('SELECT DISTINCT CAST("region" AS VARCHAR)', sample["arguments"]["query"])
        self._save(sample["file"], _md(["value"], [["East"], ["West"]]))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(am.main(argv), 0)
        with open(out_path, encoding="utf-8") as f:
            scope = json.load(f)
        prof = {a["name"]: a["profile"] for a in scope["attributes"]}
        self.assertEqual(prof["region"]["values"], ["East", "West"])
        self.assertEqual(prof["snapshot month"]["distinct_values"], 38.0)
        self.assertIn("cannot be profiled", prof["months active"]["error"])
        self.assertEqual((scope["anchor"]["metric"], scope["anchor"]["non_blank"]), ("net sales", 1200.0))

    def test_run_marks_unanswered_questions_pending(self):
        runner = am.ReplayRunner(self.dir)
        out = am.run_bank(runner, "retail", _bank(), only="Q1")
        self.assertTrue(out["results"][0]["pending"])
        self.assertEqual(sorted(runner.pending)[-1], "semantics")       # the model description is measured too
        self.assertEqual(len(runner.pending), 2)
        self.assertIsNone(out["semantics_cost"])


class MaturityTests(unittest.TestCase):
    def test_raw_build_is_m0(self):
        scope = {"attributes": [{"name": "CUSTOMER_ID", "type": "bigint", "description": ""},
                                {"name": "region", "type": "varchar", "description": "",
                                 "profile": {"distinct_values": 1}}],
                 "metrics": [{"name": "Total NET_SALES (sales)", "type": "double", "description": "",
                              "formula": "sum(sales.csv.{NET_SALES})"},
                             {"name": "Total STORE_ID (sales)", "type": "double", "description": "",
                              "formula": "sum(sales.csv.{STORE_ID})"}]}
        m = am.maturity_signals(scope)
        self.assertEqual(m["estimated_level"], "M0")
        self.assertEqual(m["derived_metrics"], 0)
        self.assertIn("Total STORE_ID (sales)", m["metrics_that_look_like_attributes"])
        self.assertEqual(m["single_value_attributes"], ["region"])
        self.assertIn("CUSTOMER_ID", m["numeric_attributes"])

    def test_governed_model_is_m3_or_m4(self):
        attrs = [{"name": "status__code", "type": "varchar", "description": "d"},
                 {"name": "status__description", "type": "varchar", "description": "d"},
                 {"name": "latest period flag", "type": "varchar", "description": "d"}]
        metrics = [{"name": f"m{i}", "type": "double", "description": "d", "formula": "{A} / {B}"} for i in range(6)]
        self.assertEqual(am.maturity_signals({"attributes": attrs, "metrics": metrics})["estimated_level"], "M4")
        attrs[2]["name"] = "snapshot month"
        self.assertEqual(am.maturity_signals({"attributes": attrs, "metrics": metrics})["estimated_level"], "M3")
        self.assertEqual(am.maturity_signals({"attributes": attrs, "metrics": metrics[:2]})["estimated_level"], "M2")
        # many governed metrics on top of structural defects stay at M2; helper metrics don't count
        flawed = metrics + [{"name": f"{n} score", "type": "bigint", "formula": f"sum(t.{{{n}}})"} for n in "abc"]
        self.assertEqual(am.maturity_signals({"attributes": attrs, "metrics": flawed})["estimated_level"], "M2")
        helpers = [{"name": f"WA x {k}", "type": "double", "formula": "Sum(Max([a]){~+,[b]+})"}
                   for k in ("numerator", "denominator")]
        self.assertEqual(am.maturity_signals({"attributes": attrs, "metrics": metrics[:2] + helpers})["derived_metrics"],
                         am.maturity_signals({"attributes": attrs, "metrics": metrics[:2]})["derived_metrics"])

    def test_numeric_dates_are_reported_even_with_calendar_parents(self):
        date = lambda n: {"name": n, "type": "bigint", "profile": {"distinct_values": 30, "min": "201901",  # noqa: E731
                                                                    "max": "202201"}}
        calendar = am.maturity_signals({"attributes": [date("reporting period"), date("close date"),
                                                       {"name": "year", "type": "bigint"},
                                                       {"name": "latest period flag", "type": "varchar"},
                                                       {"name": "close date year", "type": "bigint"}], "metrics": []})
        self.assertEqual(calendar["dates_stored_as_numbers"], ["reporting period", "close date"])
        self.assertEqual(calendar["dates_without_time_attributes"], ["reporting period", "close date"])
        converted = am.maturity_signals({"attributes": [
            {"name": "month date", "type": "date"}, {"name": "month date (year)__id", "type": "bigint"},
            {"name": "month date (month)__id", "type": "bigint"}], "metrics": []})
        self.assertEqual((converted["dates_stored_as_numbers"], converted["dates_without_time_attributes"]), ([], []))

    def test_stored_dates_need_valid_values_and_a_second_signal(self):
        def a(name, values, typ="bigint", desc=""):
            return {"name": name, "type": typ, "description": desc,
                    "profile": {"values": values, "min": min(values) if values else None,
                                "max": max(values) if values else None}}
        p = am.stored_date_pattern
        self.assertEqual(p(a("reporting period", ["201,901", "202,112", "202,201"])), "yyyyMM")
        self.assertEqual(p(a("close date", ["20190131", "20220228"])), "yyyyMMdd")
        self.assertEqual(p(a("first payment month", ["201901", "0", "202201"])), "yyyyMM")       # NULL as 0 ignored
        # values alone are not enough: an identifier, or a name with no date meaning
        self.assertEqual(p(a("account id", ["200001", "201212"])), "")
        self.assertEqual(p(a("batch", ["200001", "201212"])), "")
        self.assertEqual(p(a("period key", ["200001", "201212"], desc="Reporting month as YYYYMM.")), "yyyyMM")
        # every value must be a real date, not only the extremes
        self.assertEqual(p(a("reporting period", ["201901", "201913", "202201"])), "")
        self.assertEqual(p(a("close date", ["20120131", "20123456"])), "")                     # day 34
        # text
        self.assertEqual(p(a("activity month", ["202112", "202201"], typ="varchar")), "yyyyMM")
        self.assertEqual(p(a("as of", ["2021-12", "2022-01"], typ="varchar")), "yyyy-MM")
        self.assertEqual(p(a("status", ["202112", "202201"], typ="varchar")), "")
        # a description alone, with no values profiled
        self.assertEqual(p({"name": "activity month key", "type": "bigint",
                            "description": "Reporting month, stored as YYYYMM integer."}), "yyyyMM")
        self.assertEqual(am.date_pattern(a("close date", ["20190131", "20220228"])), "yyyyMMdd")
        m = am.maturity_signals({"attributes": [a("activity month", ["202112", "202201"], typ="varchar"),
                                                a("reporting period", ["202112", "202201"])], "metrics": []})
        self.assertEqual((m["dates_stored_as_numbers"], m["dates_stored_as_text"]), (["reporting period"], ["activity month"]))


def _snapshot_rows():
    rows = []
    for cid in range(40):
        store = f"S{cid % 8}"
        region = ["North", "South", "West"][(cid % 8) % 3]
        for month in (1, 2, 3):
            rows.append({"customer": f"C{cid:02d}", "store": store, "region": region,
                         "region code": region[0], "segment": "ABC"[(cid + month) % 3],
                         "snapshot month": f"2026-0{month}-01", "balance": cid * 10.0 + month})
    return rows


def _table_scope(rows):
    types = {"balance": "double", "snapshot month": "date"}
    attrs = []
    for name in rows[0]:
        attrs.append({"name": name, "type": types.get(name, "varchar"), "description": "",
                      "profile": {"distinct_values": float(len({r[name] for r in rows}))}})
    return {"model": "Store Sales", "attributes": attrs, "metrics": []}


class TableRunner:
    """Evaluates the COUNT(DISTINCT …) relationship queries and DISTINCT samples on in-memory rows."""

    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def call(self, tool, arguments):
        self.calls += 1
        sql = arguments["query"]
        sample = re.match(r'SELECT DISTINCT CAST\("([^"]+)" AS VARCHAR\) AS value', sql)
        if sample:
            values = sorted({str(r[sample.group(1)]) for r in self.rows})
            return _text(_md(["value"], [[v] for v in values]))
        if " UNION ALL " in sql or sql.startswith("SELECT DISTINCT '"):
            out = []
            for label, name in re.findall(r"SELECT DISTINCT '([^']+)' AS attribute, CAST\(\"([^\"]+)\" AS VARCHAR\)", sql):
                out += [[label, v] for v in sorted({str(r[name]) for r in self.rows})]
            return _text(_md(["attribute", "value"], out))
        cols, row = [], []
        for fn, name, alias in re.findall(r'(COUNT\(DISTINCT |MIN\(|MAX\()"([^"]+)"\) AS (d\d+|n\d+|x\d+|'
                                          r'distinct_values|min_value|max_value)', sql):
            vals = [r[name] for r in self.rows if r[name] is not None]
            cols.append(alias)
            row.append(len(set(vals)) if fn.startswith("COUNT") else (min(vals) if fn == "MIN(" else max(vals)))
        for expr, alias in re.findall(r"COUNT\(DISTINCT (.+?)\) AS ([ap]\d+)", sql):
            names = re.findall(r'CAST\("([^"]+)" AS VARCHAR\)', expr)
            keys = {tuple("<null>" if r[n] is None else str(r[n]) for n in names) for r in self.rows}
            cols.append(alias)
            row.append(len(keys))
        return _text(_md(cols, [row]))


class RelationshipTests(unittest.TestCase):
    def setUp(self):
        self.rows = _snapshot_rows()
        self.scope = _table_scope(self.rows)

    def test_entity_guess_and_pairs(self):
        self.assertEqual(am.guess_entity(self.scope), "customer")
        pairs = am.relationship_pairs(self.scope)
        entity = [p for p in pairs if p["kind"] == "entity"]
        self.assertEqual({p["parent"] for p in entity}, {"store", "region", "region code", "segment",
                                                         "snapshot month", "balance"})
        others = [p for p in pairs if p["kind"] == "pair"]
        self.assertIn({"child": "store", "parent": "region", "kind": "pair"}, others)
        self.assertFalse(any("balance" in (p["child"], p["parent"]) for p in others))   # measure-like doubles skipped
        self.assertEqual(len(am.relationship_pairs(self.scope, max_pairs=2)), len(entity) + 2)

    def test_queries_are_batched_and_quoted(self):
        pairs = am.relationship_pairs(self.scope)
        queries = am.relationship_queries("Store Sales", pairs, batch=4, where="\"region\" <> 'X'")
        self.assertEqual(len(queries), -(-len(pairs) // 4))
        self.assertIn("COUNT(DISTINCT COALESCE(CAST(\"customer\" AS VARCHAR), '<null>') || '|' || "
                      "COALESCE(CAST(\"store\" AS VARCHAR), '<null>')) AS p0", queries[0]["sql"])
        self.assertTrue(queries[0]["sql"].endswith("FROM \"store sales\" WHERE \"region\" <> 'X'"))

    def test_review_classifies_and_reduces(self):
        declared = [{"child": "store", "parent": "region"}, {"child": "segment", "parent": "snapshot month"},
                    {"child": "nothing", "parent": "else"}]
        out = am.review_relationships(TableRunner(self.rows), "retail", self.scope, batch=5, declared=declared)
        sug = out["suggestions"]
        self.assertEqual(out["entity"], "customer")
        self.assertEqual(set(sug["entity_level_attributes"]), {"store", "region", "region code"})
        self.assertEqual(set(sug["attributes_varying_within_entity"]), {"segment", "snapshot month", "balance"})
        self.assertIn({"child": "store", "parent": "region", "evidence": "weak"}, sug["parent_child"])
        self.assertIn(("region", "region code"), {tuple(sorted((o["a"], o["b"]))) for o in sug["same_entity_one_to_one"]})
        checks = {(c["child"], c["parent"]): c["data"] for c in out["declared"]["checks"]}
        self.assertEqual(checks[("store", "region")], "many_to_one")
        self.assertEqual(checks[("segment", "snapshot month")], "no_dependency")
        self.assertEqual(checks[("nothing", "else")], "not_tested")
        self.assertEqual([c["child"] for c in out["declared"]["contradicted"]], ["segment"])

    def test_scope_profiles_in_batches_and_falls_back_on_a_failed_batch(self):
        class Semantics(TableRunner):
            def call(self, tool, arguments):
                if tool == "get_semantics":
                    lines = ["### Attributes(Dimensions)"] + [f"- **{n}** (varchar): x" for n in self.rows[0]]
                    return _text("\n".join(lines))
                return super().call(tool, arguments)

        runner = Semantics(self.rows)
        out = am.scope_model(runner, "retail", "Store Sales", profile_batch=4, sample_max=3)
        prof = {a["name"]: a["profile"] for a in out["attributes"]}
        self.assertEqual(prof["store"]["distinct_values"], 8.0)
        self.assertEqual(prof["region"]["values"], ["North", "South", "West"])
        self.assertEqual(prof["snapshot month"]["min"], "2026-01-01")
        self.assertEqual(runner.calls, 2 + 1)              # 2 profile batches + 1 UNION ALL sample batch (4 attributes)

        class FailingBatch(Semantics):
            def call(self, tool, arguments):
                if tool == "query" and " AS d1" in arguments["query"]:
                    return _text("Query failed", error=True)
                return super().call(tool, arguments)

        runner = FailingBatch(self.rows)
        out = am.scope_model(runner, "retail", "Store Sales", profile_batch=4, sample_max=3)
        self.assertEqual({a["name"]: a["profile"]["distinct_values"] for a in out["attributes"]}["segment"], 3.0)
        self.assertEqual(runner.calls, 7 + 1)              # both batches fail (not counted), 7 single queries + 1 sample batch

    def test_classify_and_reduce_helpers(self):
        self.assertEqual(am.classify_pair(100, 10, 101, "pair")["verdict"], "many_to_one_with_exceptions")
        self.assertEqual(am.classify_pair(100, 10, 150, "pair")["verdict"], "no_dependency")
        self.assertEqual(am.classify_pair(10, 10, 10, "pair")["verdict"], "one_to_one")
        self.assertEqual(am.classify_pair(500, 5, 500, "entity")["evidence"], "strong")
        self.assertEqual(am.classify_pair(40, 5, 40, "pair")["evidence"], "moderate")
        self.assertEqual(am.classify_pair(6, 3, 8, "pair")["verdict"], "no_dependency")    # 2 of 6 is not "near"
        self.assertEqual(sorted(am._reduce([("a", "b"), ("b", "c"), ("a", "c")])), [("a", "b"), ("b", "c")])

    def test_pending_replies_are_counted(self):
        runner = am.ReplayRunner(tempfile.mkdtemp(prefix="assess-rel-"))
        self.addCleanup(shutil.rmtree, runner.folder, True)
        out = am.review_relationships(runner, "retail", self.scope, batch=100)
        self.assertEqual((out["pending_queries"], len(runner.pending)), (1, 1))


class EarlyStageSignalTests(unittest.TestCase):
    SCOPE = {"model": "Store Sales", "attributes": [
        {"name": "reporting period", "type": "bigint", "description": "Period as YYYYMM integer",
         "profile": {"distinct_values": 37, "min": "201,901", "max": "202,201"}},
        {"name": "close date", "type": "bigint", "description": "",
         "profile": {"distinct_values": 14, "min": "20200301", "max": "20211201"}},
        {"name": "status code", "type": "bigint", "description": "", "profile": {"distinct_values": 4,
                                                                                "values": ["1", "2", "3", "9"]}},
        {"name": "units", "type": "bigint", "description": "", "profile": {"distinct_values": 3}}],
        "metrics": [{"name": "interest rate", "type": "double", "description": "", "formula": "sum(t.{RATE})"},
                    {"name": "customer age", "type": "bigint", "description": "", "formula": "sum(t.{AGE})"},
                    {"name": "net sales", "type": "double", "description": "", "formula": "sum(t.{SALES})"},
                    {"name": "cumulative costs", "type": "varchar", "description": "", "formula": "max(t.{COSTS})"}]}

    def test_signals(self):
        m = am.maturity_signals(self.SCOPE)
        self.assertEqual(m["dates_stored_as_numbers"], ["reporting period", "close date"])
        self.assertEqual(m["summed_rates_scores_or_ages"], ["interest rate", "customer age"])
        self.assertEqual(m["metrics_stored_as_text"], ["cumulative costs"])

    def test_numeric_codes_and_integer_periods_get_lookups(self):
        sugg = {}
        for x in am.lookup_suggestions(self.SCOPE):
            sugg.setdefault(x["attribute"], []).append(x["kind"])
        self.assertEqual(sugg, {"status code": ["code_lookup"], "reporting period": ["time_attributes", "period_lookup"],
                                "close date": ["time_attributes"]})
        period = next(x for x in am.lookup_suggestions(self.SCOPE)
                      if x["kind"] == "time_attributes" and x["attribute"] == "reporting period")
        self.assertEqual((period["convert"], period["intervals"]),
                         ({"type": "date", "date_format": "yyyyMM"}, ["year", "quarter", "month"]))
        fallback = {x["attribute"]: x["kind"] for x in am.lookup_suggestions(self.SCOPE, time_attributes=False)}
        self.assertEqual(fallback["close date"], "date_lookup")
        self.assertEqual(am._period_parts("201,912"), ("2019", "2019-Q4", "12"))
        self.assertEqual(am._period_parts("20200301"), ("2020", "2020-Q1", "03"))


    def test_date_lookup_gives_calendar_parents_as_attributes(self):
        (sug,) = [x for x in am.lookup_suggestions(self.SCOPE, time_attributes=False) if x["kind"] == "date_lookup"]
        self.assertEqual(sug["columns"], ["close date", "close date year", "close date quarter"])
        self.assertEqual((sug["new_attributes"], sug["new_forms"]), (["close date year", "close date quarter"], []))
        self.assertIn("preferred to a derived attribute", sug["reason"])
        scope = {"model": "Store Sales", "attributes": [{**self.SCOPE["attributes"][1], "profile": {
            "distinct_values": 2, "min": "20200301", "max": "20211201", "values": ["20200301", "20211201"]}}]}
        d = tempfile.mkdtemp(prefix="assess-datelookup-")
        self.addCleanup(shutil.rmtree, d, True)
        (done,) = am.build_lookups(None, "p", scope, [sug], d)
        with open(done["csv"], encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows, [["close date", "close date year", "close date quarter"],
                                ["20200301", "2020", "2020-Q1"], ["20211201", "2021", "2021-Q4"]])
        named = {**self.SCOPE, "attributes": self.SCOPE["attributes"] + [{"name": "close date year", "type": "bigint"}]}
        self.assertNotIn("date_lookup", [x["kind"] for x in am.lookup_suggestions(named, time_attributes=False)])


def _table_reply(rows):
    head = "| " + " | ".join(rows[0]) + " |\n| " + " | ".join("---" for _ in rows[0]) + " |\n"
    return _text(head + "".join("| " + " | ".join(str(c) for c in r) + " |\n" for r in rows[1:]))


class BestAnswerAndCostTests(unittest.TestCase):
    """An expert answer per question, re-run every round; per-question connector cost."""

    def _bank(self):
        return {"model": "Store Sales", "questions": [
            {"id": "Q1", "level": 1, "sql": "SELECT region, SUM(\"sales\") AS n FROM [[MODEL]] GROUP BY 1",
             "key": ["region"], "measures": ["n"], "expected": {"source": "none"}},
            {"id": "Q2", "level": 4, "sql": "", "expected_path": "not_answerable", "expected": {"source": "none"}},
            {"id": "Q3", "level": 2, "sql": "SELECT region, SUM(\"sales\") AS n FROM [[MODEL]] WHERE x GROUP BY 1",
             "key": ["region"], "measures": ["n"],
             "expected": {"source": "inline", "rows": [{"region": "N", "n": 5}]}}]}

    def _runner(self, governed, best):
        return FakeRunner({"WHERE x": _table_reply([["region", "n"], ["N", 5]]),
                           "/* B1 */": _table_reply([["region", "n"], ["N", best]]),
                           "/* BX */": _table_reply([["region", "n"], ["N", 5]]),
                           "SUM(\"sales\")": _table_reply([["region", "n"], ["N", governed]])})

    def _set(self, bank):
        for qid, sql in (("Q1", "SELECT region, SUM(x) AS n /* B1 */ FROM [[MODEL]] GROUP BY 1"),
                         ("Q2", "SELECT 'N' AS region, 1 AS n /* BX */ FROM [[MODEL]]"),
                         ("Q3", "SELECT region, 5 AS n /* BX */ FROM [[MODEL]] GROUP BY 1")):
            self.assertTrue(am.set_best_answer(bank, qid, sql=sql, reason_kind="initial", rationale="r", round_no=1)["ok"])

    def test_model_description_size_is_tracked(self):
        bank = self._bank()
        res = am.run_bank(self._runner(4, 4), "retail", bank)
        self.assertGreater(res["semantics_cost"]["chars"], 0)
        graded = am.grade_bank(bank, res)
        self.assertEqual(graded["summary"]["cost"]["model_description"], res["semantics_cost"])

    def test_cost_is_measured_per_query(self):
        c = am.query_cost("SELECT 1", "x" * 92)
        self.assertEqual(c, {"queries": 1, "sql_chars": 8, "reply_chars": 92, "est_tokens": 25})
        self.assertEqual(am.add_costs(c, c, None)["queries"], 2)

    def test_best_answer_grades_ungraded_questions_and_is_checked_by_references(self):
        bank = self._bank()
        self._set(bank)
        res = am.run_bank(self._runner(4, 4), "retail", bank)
        self.assertIn("best_run", next(r for r in res["results"] if r["id"] == "Q2"))   # runs even with no governed SQL
        graded = am.grade_bank(bank, res)
        by = {g["id"]: g for g in graded["questions"]}
        self.assertEqual((by["Q1"]["verdict"], by["Q1"]["strength"]), ("pass", "best_answer"))
        self.assertEqual((by["Q3"]["verdict"], by["Q3"]["best_vs_reference"]), ("pass", "pass"))
        self.assertEqual(by["Q1"]["cost"]["queries"], 1)
        self.assertEqual(by["Q1"]["best_cost"]["queries"], 1)
        self.assertGreater(graded["summary"]["cost"]["answers"]["est_tokens"], 0)
        self.assertIn("1", graded["summary"]["cost"]["by_level"])
        self.assertIn("reasoning is not included", graded["summary"]["cost"]["note"])

    def test_drift_override_and_suspicious_agreement(self):
        bank = self._bank()
        self._set(bank)
        r1 = am.grade_bank(bank, am.run_bank(self._runner(3, 4), "retail", bank))     # governed 3, best 4: fail
        self.assertEqual(next(g for g in r1["questions"] if g["id"] == "Q1")["verdict"], "fail")
        r2 = am.grade_bank(bank, am.run_bank(self._runner(7, 7), "retail", bank), previous=r1)
        self.assertEqual(r2["best_answer_moved"], ["Q1"])          # same SQL, new result: data changed under it
        self.assertEqual(r2["suspicious_agreement"], ["Q1"])       # both moved and now agree
        self.assertTrue(am.set_best_answer(bank, "Q1", sql="SELECT region, 9 AS n /* B1 */ FROM [[MODEL]]",
                                           reason_kind="decision", reason="user: count REO", round_no=3)["ok"])
        r3 = am.grade_bank(bank, am.run_bank(self._runner(7, 9), "retail", bank), previous=r2)
        self.assertEqual((r3["best_answer_overridden"], r3["best_answer_moved"]), (["Q1"], []))
        self.assertEqual(len(bank["questions"][0]["best_answer"]["history"]), 1)

    def test_override_rules(self):
        bank = self._bank()
        self._set(bank)
        bad = lambda **kw: am.set_best_answer(bank, "Q1", **kw)["problems"]  # noqa: E731
        self.assertIn("has a best answer", " ".join(bad(sql="SELECT 2", reason_kind="initial", reason="")))
        self.assertIn("needs --reason", " ".join(bad(sql="SELECT 2", reason_kind="sql_error", reason="")))
        self.assertIn("independent of the question's own SQL",
                      " ".join(bad(sql=bank["questions"][0]["sql"], reason_kind="sql_error", reason="x")))
        self.assertIn("not both", " ".join(bad(sql="SELECT 2", not_answerable="x", reason_kind="sql_error", reason="x")))
        ok = am.set_best_answer(bank, "Q2", not_answerable="no loss amounts in this model", reason_kind="data_change",
                                reason="column dropped", round_no=4)
        self.assertEqual(ok["version"], 2)
        graded = am.grade_bank(bank, {"results": [{"id": "Q2", "skipped": True, "error": "no sql"}]})
        self.assertTrue(next(g for g in graded["questions"] if g["id"] == "Q2")["best_answer"].startswith("not answerable"))


class BaselineTests(unittest.TestCase):
    """Reviewed answers of ungraded questions, frozen: a later change is reported for review, never graded."""

    BANK = {"questions": [
        {"id": "Q1", "level": 2, "key": ["region"], "measures": ["n"], "expected": {"source": "none"}},
        {"id": "Q2", "level": 2, "key": ["region"], "measures": ["n"],
         "expected": {"source": "inline", "rows": [{"region": "N", "n": 5}]}},
        {"id": "Q3", "level": 3, "expected": {"source": "none"}}]}

    def _results(self, n):
        return {"results": [{"id": "Q1", "ok": True, "rows": [{"region": "N", "n": n}, {"region": "S", "n": 2}]},
                            {"id": "Q2", "ok": True, "rows": [{"region": "N", "n": 5}]},
                            {"id": "Q3", "ok": False, "error": "boom"}]}

    def test_freeze_then_grade_same_and_changed(self):
        bank = json.loads(json.dumps(self.BANK))
        out = am.freeze_baselines(bank, self._results(4), ["Q1", "Q2", "Q3", "Q9"], round_no=5, note="reviewed")
        self.assertEqual(out["frozen"], ["Q1"])
        self.assertEqual([x["id"] for x in out["skipped"]], ["Q2", "Q3"])     # graded already; no result
        self.assertEqual(out["unknown"], ["Q9"])
        self.assertEqual(bank["questions"][0]["baseline"]["round"], 5)

        same = am.grade_bank(bank, self._results(4))
        q1 = next(g for g in same["questions"] if g["id"] == "Q1")
        self.assertEqual((q1["verdict"], q1["baseline"]), ("ungraded", "same"))   # still ungraded, not a pass
        self.assertEqual(same["summary"]["by_verdict"].get("pass"), 1)            # only Q2 passes
        self.assertEqual(same["baseline_changes"], [])

        changed = am.grade_bank(bank, self._results(7))
        self.assertEqual(changed["baseline_changes"], ["Q1"])
        self.assertEqual(changed["summary"]["baseline"], {"changed": 1})

    def test_cli_freezes_into_the_bank(self):
        d = tempfile.mkdtemp(prefix="assess-baseline-")
        self.addCleanup(shutil.rmtree, d, True)
        bp, rp = os.path.join(d, "bank.json"), os.path.join(d, "results.json")
        with open(bp, "w", encoding="utf-8") as f:
            json.dump(self.BANK, f)
        with open(rp, "w", encoding="utf-8") as f:
            json.dump(self._results(4), f)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(am.main(["baseline", "--bank", bp, "--results", rp, "--ids", "Q1", "--round", "5"]), 0)
        with open(bp, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["questions"][0]["baseline"]["rows"][1], {"region": "S", "n": 2})


class SourceChangeAndLookupStepTests(unittest.TestCase):
    def test_lookup_steps_make_filtered_columns_attributes(self):
        text = am.lookup_steps({"csv": "period.csv", "key_attribute": "snapshot month",
                                "new_attributes": ["latest period flag", "year"], "new_forms": ["month name"]})
        self.assertIn("Create 'latest period flag', 'year' as attributes of their own (not forms)", text)
        self.assertIn("Add 'month name' as forms of 'snapshot month'", text)
        self.assertIn("EXISTING attribute 'snapshot month'", text)
        self.assertIn("as attributes of their own if any metric will filter on them",
                      am.lookup_steps({"csv": "c.csv", "key_attribute": "k"}))

    def test_plan_rejects_a_filter_on_a_column_added_as_a_form(self):
        doc = {"findings": [
            _finding("F1", "lookup", actions=[{"kind": "add_lookup_table", "args": {
                "csv": "p.csv", "key_attribute": "snapshot month", "new_forms": ["Latest Period Flag"]}}]),
            _finding("F2", "missing_metric", depends_on=["F1"], actions=[{"kind": "create_conditional_metric", "args": {
                "name": "Ending Balance", "source_metric": "Balance", "attribute": "region", "elements": ["East"],
                "and": [["latest period flag", "Y"]]}}])]}
        plan = am.build_plan(doc, model_id="MID")
        self.assertTrue(any("adds as a form" in p for p in plan["problems"]))

    def test_source_changes_are_consolidated_into_one_phase_1_step(self):
        doc = {"findings": [
            _finding("F1", "source_data", actions=[{"kind": "source_change", "args": {
                "table": "sales.csv", "column": "ACTIVE FLAG", "change": "add: Y while open"}}]),
            _finding("F2", "data_type", actions=[{"kind": "source_change", "args": {
                "table": "sales.csv", "column": "STATUS", "change": "pad to 2 digits (1 -> 01)"}}]),
            _finding("F3", "metric_defect", actions=[
                {"kind": "source_change", "args": {"table": "sales.csv", "column": "COSTS", "change": "-1 to NULL"}},
                {"kind": "create_compound_metric", "args": {"name": "Net", "formula": "[A] - [B]"}}]),
            _finding("F4", "data_type", actions=[{"kind": "source_change", "args": {"column": "X"}}], status="proposed")]}
        plan = am.build_plan(doc, model_id="MID")
        self.assertEqual(plan["problems"], [])
        p1, p2, p4 = plan["phases"]
        self.assertEqual([p["phase"] for p in plan["phases"]], [1, 2, 4])
        rebuild = p1["steps"][0]
        self.assertEqual((rebuild["kind"], rebuild["finding"]), ("source_change", "F1, F2, F3"))
        self.assertEqual(sum(1 for st in p1["steps"] if st["kind"] == "source_change"), 1)
        for col in ("ACTIVE FLAG", "STATUS", "COSTS"):
            self.assertIn(col, rebuild["manual"])
        self.assertEqual(p1["findings"][:3], ["F1", "F2", "F3"])
        self.assertEqual(set(p1["unit_tests"]), {"Q-F1", "Q-F2", "Q-F3"})
        self.assertEqual([st["kind"] for st in p2["steps"]], ["note"])
        self.assertNotIn("publish", [a["command"]["argv"][2] for a in p2["after"]])     # nothing left to republish
        self.assertEqual([st["kind"] for st in p4["steps"]], ["note", "create_compound_metric"])

    def test_column_type_changes_merge_into_one_phase_2_command(self):
        doc = {"findings": [
            _finding("F1", "data_type", actions=[
                {"kind": "change_column_type", "args": {"column": "STATUS CODE", "type": "text"}},
                {"kind": "change_column_type", "args": {"column": "PERIOD", "type": "date", "date_format": "yyyyMM"}}]),
            _finding("F2", "metric_defect", actions=[
                {"kind": "change_column_type", "args": {"column": "COSTS", "type": "double"}},
                {"kind": "change_column_type", "args": {"column": "COSTS2", "type": "double"}},
                {"kind": "create_compound_metric", "args": {"name": "Net", "formula": "[A] - [B]"}}]),
            _finding("F3", "lookup", actions=[{"kind": "change_column_type", "args": {
                "column": "CODE", "type": "text", "table": "codes lookup"}}])]}
        plan = am.build_plan(doc, model_id="MID")
        self.assertEqual(plan["problems"], [])
        p2, p4 = plan["phases"]
        main, lookup = p2["steps"][:2]
        self.assertEqual(main["apply"]["argv"][2:], ["set-column-types", "--model-id", "MID", "--set", "STATUS CODE",
                                                     "text", "--set", "PERIOD", "date", "yyyyMM", "--set", "COSTS",
                                                     "double", "--set", "COSTS2", "double"])
        self.assertEqual(main["dry_run"]["argv"][-1], "--dry-run")
        self.assertEqual(main["finding"], "F1, F2")
        self.assertEqual(lookup["apply"]["argv"][-2:], ["--table", "codes lookup"])
        self.assertEqual(len(p2["steps"]), 2)                       # F1 and F3 need nothing else
        self.assertIn("publish", [a["command"]["argv"][2] for a in p2["after"]])
        self.assertEqual(set(p2["unit_tests"]), {"Q-F1", "Q-F2", "Q-F3"})
        notes = [st for st in p4["steps"] if st["kind"] == "note"]
        self.assertEqual(len(notes), 1)                              # one note per finding, not per action
        self.assertIn("phase 2", notes[0]["note"])

    def test_column_type_validation_and_type_change_hint(self):
        plan = am.build_plan({"findings": [
            _finding("F1", "data_type", actions=[{"kind": "change_column_type", "args": {"column": "D", "type": "date"}},
                                                 {"kind": "change_column_type", "args": {"column": "X", "type": "varchar"}}]),
            _finding("F2", "data_type", actions=[{"kind": "change_column_type", "args": {"column": "d", "type": "text"}}]),
            _finding("F3", "source_data", actions=[{"kind": "source_change", "args": {
                "column": "CODE", "change": "Load as VARCHAR so RA survives"}}])]}, model_id="MID")
        text = " | ".join(plan["problems"])
        self.assertIn("needs args.date_format", text)
        self.assertIn("'varchar' is not one of", text)
        self.assertIn("changed twice", text)
        self.assertIn("looks like a column type change", text)
        ok = am.build_plan({"findings": [_finding("F1", "source_data", actions=[{"kind": "source_change", "args": {
            "column": "FLAG", "change": "new column: Y while the account is open"}}])]}, model_id="MID")
        self.assertEqual(ok["problems"], [])

    def test_source_change_on_a_retyped_column_is_a_problem(self):
        plan = am.build_plan({"findings": [_finding("F1", "data_type", actions=[
            {"kind": "change_column_type", "args": {"column": "STATUS CODE", "type": "text"}},
            {"kind": "source_change", "args": {"column": "status code, REGION", "change": "pad to 2 digits"}}])]},
            model_id="MID")
        self.assertTrue(any("'status code' has a source change and a type change" in p for p in plan["problems"]))
        self.assertFalse(any("REGION" in p for p in plan["problems"]))

    def test_relationships_ship_their_hints_file(self):
        doc = {"findings": [
            _finding("F1", "relationship", actions=[{"kind": "wire_relationships", "args": {"relationships": [
                {"parent_attribute": "region", "child_attribute": "store", "relationship_table": "sales",
                 "type": "one_to_many"}]}}]),
            _finding("F2", "attribute", actions=[{"kind": "wire_relationships", "args": {"relationships": [
                {"parent_attribute": "year", "child_attribute": "month", "relationship_table": "calendar"}]}}])]}
        plan = am.build_plan(doc, model_id="MID")
        self.assertEqual(plan["problems"], [])
        (p3,) = plan["phases"]
        (step,) = p3["steps"]
        self.assertEqual(step["apply"]["argv"][2:], ["wire-relationships", "--model-id", "MID", "--hints",
                                                     "specs/relationships.json"])
        self.assertEqual(step["dry_run"]["argv"][-1], "--dry-run")
        self.assertEqual([r["child_attribute"] for r in plan["files"]["specs/relationships.json"]["relationships"]],
                         ["store", "month"])
        bad = am.build_plan({"findings": [_finding("F1", "relationship", actions=[{"kind": "wire_relationships",
            "args": {"relationships": [{"child_attribute": "b", "type": "many"}]}}])]}, model_id="MID")
        text = " | ".join(bad["problems"])
        self.assertIn("missing ['parent_attribute']", text)
        self.assertIn("'many' is not one of", text)
        ok = am.build_plan({"findings": [_finding("F1", "relationship", actions=[{"kind": "wire_relationships",
            "args": {"relationships": [{"parent_attribute": "a", "child_attribute": "b"}]}}])]}, model_id="MID")
        self.assertEqual(ok["problems"], [])                       # the table is optional

    def test_notes_name_their_kind_and_risks_show_once(self):
        doc = {"findings": [_finding("F5", "data_type", risks=["watch the dates"], actions=[
            {"kind": "change_column_type", "args": {"column": "D", "type": "date", "date_format": "yyyyMM"}},
            {"kind": "wire_relationships", "args": {"relationships": [{"parent_attribute": "y", "child_attribute": "d"}]}},
            {"kind": "manual", "steps": "one"}, {"kind": "manual", "steps": "two"}])]}
        plan = am.build_plan(doc, model_id="MID")
        p2 = plan["phases"][0]
        notes = [st["note"] for st in p2["steps"] if st["kind"] == "note"]
        self.assertEqual(notes, ["Relationships declared in phase 3 (wire-relationships); nothing to do here."])
        manual = [st for st in p2["steps"] if st["kind"] == "manual"]
        self.assertEqual([st["risks"] for st in manual], [["watch the dates"], []])

    def test_inline_fact_metric_spec_becomes_a_bundle_file(self):
        doc = {"findings": [_finding("F7", "metric_defect", actions=[{"kind": "patch_fact_metrics", "args": {
            "metrics": [{"name": "rate", "function": "avg"}]}}])]}
        plan = am.build_plan(doc, model_id="MID")
        (step,) = plan["phases"][0]["steps"]
        self.assertEqual(step["apply"]["argv"][-2:], ["--spec", "specs/F7_fact_metrics.json"])
        self.assertEqual(plan["files"]["specs/F7_fact_metrics.json"], {"metrics": [{"name": "rate", "function": "avg"}]})
        self.assertNotIn("spec", doc["findings"][0]["actions"][0]["args"])       # the input is left alone

    def test_display_column_as_attribute_is_a_warning(self):
        lk = {"kind": "add_lookup_table", "args": {"csv": "c.csv", "key_attribute": "status",
                                                    "new_attributes": ["status description", "group"]}}
        plan = am.build_plan({"findings": [_finding("F1", "lookup", actions=[lk])]}, model_id="MID")
        self.assertEqual([w for w in plan["warnings"] if "display column" in w],
                         ["F1: 'status description' looks like a display column; list it under new_forms unless a "
                          "metric will filter on it"])
        self.assertIn("sources are in separate files that are not loaded", plan["phases"][0]["steps"][0]["manual"])

    def test_plan_cli_writes_spec_files(self):
        d = tempfile.mkdtemp(prefix="assess-plan-")
        self.addCleanup(shutil.rmtree, d, True)
        fp = os.path.join(d, "findings.json")
        with open(fp, "w", encoding="utf-8") as f:
            json.dump({"findings": [_finding("F7", "metric_defect", actions=[{"kind": "patch_fact_metrics", "args": {
                "metrics": [{"name": "rate", "function": "avg"}]}}])]}, f)
        with contextlib.redirect_stdout(io.StringIO()):
            am.main(["plan", "--findings", fp, "--model-id", "MID", "--out", os.path.join(d, "change_plan.json")])
        with open(os.path.join(d, "specs", "F7_fact_metrics.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["metrics"][0]["function"], "avg")

    def test_formula_attribute_needs_the_users_request_and_warns_dependents(self):
        fa = {"kind": "create_formula_attribute", "args": {"name": "close year", "formula": "Year([CLOSE DATE])",
                                                         "data_type": "integer"}}
        cm = {"kind": "create_conditional_metric", "args": {"name": "Closed 2021", "count_attribute": "account",
                                                            "attribute": "close year", "elements": ["2021"]}}
        plan = am.build_plan({"findings": [_finding("F1", "attribute", actions=[fa]),
                                           _finding("F2", "missing_metric", actions=[cm])]}, model_id="MID")
        self.assertTrue(any("Prefer a lookup table" in p and "requested_by_user" in p for p in plan["problems"]))
        self.assertTrue(any("built on 'close year', a derived attribute" in w for w in plan["warnings"]))
        fa["args"].update(requested_by_user=True)
        plan = am.build_plan({"findings": [_finding("F1", "attribute", actions=[fa])]}, model_id="MID")
        self.assertEqual(plan["problems"], [])
        (step,) = plan["phases"][0]["steps"]
        self.assertEqual(step["apply"]["argv"][2:], ["create-formula-attribute", "--model-id", "MID", "--name",
                                                     "close year", "--formula", "Year([CLOSE DATE])",
                                                     "--data-type", "integer"])
        fa["args"].update(formula="CaseV([CODE], '01', 'Y', 'N')")
        plan = am.build_plan({"findings": [_finding("F1", "attribute", actions=[fa])]}, model_id="MID")
        self.assertTrue(any("string literal" in w for w in plan["warnings"]))

    def test_manual_steps_given_as_a_list_read_as_one_paragraph(self):
        plan = am.build_plan({"findings": [_finding("F1", "description", actions=[
            {"kind": "manual", "steps": ["Rename the metric", "Describe it."]}])]}, model_id="MID")
        self.assertEqual(plan["phases"][0]["steps"][0]["manual"], "1. Rename the metric. 2. Describe it.")

    def test_lookup_key_gets_the_type_of_a_retyped_attribute(self):
        doc = {"findings": [
            _finding("F1", "data_type", actions=[{"kind": "change_column_type", "args": {
                "column": "close date", "type": "date", "date_format": "yyyyMM"}}]),
            _finding("F2", "lookup", actions=[{"kind": "add_lookup_table", "args": {
                "csv": "lookups/close_date_lookup.csv", "key_attribute": "close date",
                "new_attributes": ["close date year"]}}])]}
        plan = am.build_plan(doc, model_id="MID")
        kinds = [(st["kind"], st["finding"]) for st in plan["phases"][0]["steps"]]
        self.assertEqual(kinds, [("change_column_type", "F1"), ("add_lookup_table", "F2"), ("change_column_type", "F2")])
        retype = plan["phases"][0]["steps"][2]
        self.assertEqual(retype["apply"]["argv"][2:], ["set-column-types", "--model-id", "MID", "--table",
                                                       "close_date_lookup", "--set", "close date", "date", "yyyyMM"])

    def test_unconfirmed_formula_functions_are_a_warning(self):
        fa = {"kind": "create_formula_attribute", "args": {"name": "open flag", "requested_by_user": True,
                                                         "formula": "If(IsNull([CLOSE CODE]), 1, 0)"}}
        plan = am.build_plan({"findings": [_finding("F1", "attribute", actions=[fa])]}, model_id="MID")
        self.assertTrue(any("uses If, IsNull, not yet confirmed live" in w and "checks columns only" in w
                            for w in plan["warnings"]))
        fa["args"]["formula"] = "Year([CLOSE DATE])"
        plan = am.build_plan({"findings": [_finding("F1", "attribute", actions=[fa])]}, model_id="MID")
        self.assertFalse(any("not yet confirmed live" in w for w in plan["warnings"]))
        fa["args"]["formula"] = "Sign([BALANCE])"
        plan = am.build_plan({"findings": [_finding("F1", "attribute", actions=[fa])]}, model_id="MID")
        self.assertTrue(any("refused as an unknown function" in w for w in plan["warnings"]))

    def test_lookups_of_a_phase_become_one_checklist_step(self):
        lk = lambda csv, key, **kw: {"kind": "add_lookup_table", "args": {"csv": csv, "key_attribute": key, **kw}}  # noqa: E731
        doc = {"findings": [
            _finding("F1", "data_type", actions=[{"kind": "change_column_type", "args": {"column": "status", "type": "text"}}]),
            _finding("F2", "lookup", risks=["map to the existing attribute"], actions=[
                lk("lookups/status_lookup.csv", "status", new_attributes=["status group"], new_forms=["status description"]),
                lk("lookups/region_lookup.csv", "region", new_forms=["region name"])]),
            _finding("F3", "lookup", actions=[lk("lookups/close_date_lookup.csv", "close date",
                                                 new_attributes=["close date year"])])]}
        plan = am.build_plan(doc, model_id="MID")
        steps = plan["phases"][0]["steps"]
        self.assertEqual([st["kind"] for st in steps], ["change_column_type", "add_lookup_table", "change_column_type"])
        merged = steps[1]
        self.assertEqual((merged["finding"], merged["title"]), ("F2, F3", "Add 3 lookup tables"))
        self.assertIn("(1) lookups/status_lookup.csv -> key 'status'; attributes: status group; forms: status description.",
                      merged["manual"])
        self.assertIn("(2) lookups/region_lookup.csv -> key 'region'; forms: region name; key column typed like 'region'.",
                      merged["manual"])
        self.assertIn("set by the commands right after this step", merged["manual"])
        self.assertEqual(merged["risks"], ["map to the existing attribute"])
        self.assertEqual(steps[2]["apply"]["argv"][-5:], ["--table", "status_lookup", "--set", "status", "text"])

    def test_test_steps_do_not_stop_the_script(self):
        doc = {"findings": [_finding("F4", "data_type", actions=[{"kind": "manual", "steps": [
            "Remove the latitude form from the region attribute", "Test after republish: region values '041' appear"]},
            {"kind": "manual", "steps": "1. After republish, test: codes include 'RA'. 2. Check the flag is never blank."}])]}
        plan = am.build_plan(doc, model_id="MID")
        steps = plan["phases"][0]["steps"]
        self.assertEqual([st["kind"] for st in steps], ["verify", "manual", "verify"])
        self.assertEqual(steps[1]["manual"], "1. Remove the latitude form from the region attribute.")
        self.assertTrue(steps[0]["note"].startswith("Checked by /verify-changes after this phase: Test after republish"))
        self.assertIsNone(steps[2]["manual"])

    def test_names_and_descriptions_merge_into_one_command(self):
        doc = {"findings": [
            _finding("F14", "data_type", actions=[{"kind": "rename_describe", "args": {"items": [
                {"object_type": "fact_metric", "current_name": "costs", "description": "Empty in this data."}]}}]),
            _finding("F16", "description", actions=[{"kind": "rename_describe", "args": {"items": [
                {"object_type": "attribute", "current_name": "region", "new_name": "Sales Region"}]}}])]}
        plan = am.build_plan(doc, model_id="MID")
        self.assertEqual(plan["problems"], [])
        p2, p6 = plan["phases"]
        self.assertEqual([st["kind"] for st in p2["steps"]], ["note"])
        (step,) = p6["steps"]
        self.assertEqual(step["apply"]["argv"][2:], ["rename-describe", "--model-id", "MID", "--spec",
                                                     "specs/rename_describe.json"])
        self.assertEqual([i["current_name"] for i in plan["files"]["specs/rename_describe.json"]["items"]],
                         ["costs", "region"])
        bad = am.build_plan({"findings": [_finding("F1", "description", actions=[{"kind": "rename_describe", "args": {
            "items": [{"object_type": "metric", "current_name": "x", "description": "y" * 251}]}}])]}, model_id="MID")
        text = " | ".join(bad["problems"])
        self.assertIn("object_type must be attribute or fact_metric", text)
        self.assertIn("over 250 characters", text)

    def test_connector_form_names_in_rename_describe(self):
        def plan_for(items):
            return am.build_plan({"findings": [_finding("F1", "description", actions=[
                {"kind": "rename_describe", "args": {"items": items}}])]}, model_id="MID")
        ok = plan_for([{"object_type": "attribute", "current_name": "site__latitude", "description": "Site, lat, long."}])
        self.assertEqual(ok["problems"], [])
        self.assertTrue(any("connector form name" in w and "'site'" in w for w in ok["warnings"]))
        bad = plan_for([{"object_type": "attribute", "current_name": "site (code)", "new_name": "Site code"},
                        {"object_type": "attribute", "current_name": "status__code", "description": "a"},
                        {"object_type": "attribute", "current_name": "Status", "description": "b"}])
        text = " | ".join(bad["problems"])
        self.assertIn("forms can't be renamed", text)
        self.assertIn("attribute 'status' gets 2 different descriptions", text)

    def test_verified_finding_keeps_its_later_phase_actions(self):
        describe = {"kind": "rename_describe", "args": {"items": [
            {"object_type": "attribute", "current_name": "latest flag", "description": "1 = latest period."}]}}
        lookup = {"kind": "manual", "steps": "add the lookup"}
        f1 = _finding("F1", "missing_metric", status="verified", actions=[lookup, describe], applied_phases=[1, 2, 5])
        plan = am.build_plan({"findings": [f1]}, model_id="MID")
        self.assertEqual(plan["problems"], [])
        self.assertEqual([p["phase"] for p in plan["phases"]], [6])                  # only the part still to do
        self.assertEqual(plan["files"]["specs/rename_describe.json"]["items"][0]["current_name"], "latest flag")
        self.assertEqual(plan["not_planned"], [])
        self.assertTrue(any("still planned" in w for w in plan["warnings"]))
        done = am.build_plan({"findings": [dict(f1, applied_phases=[5, 6])]}, model_id="MID")
        self.assertEqual(done["phases"], [])
        self.assertEqual(done["not_planned"], [{"id": "F1", "status": "verified"}])
        unknown = am.build_plan({"findings": [dict(f1, applied_phases=None)]}, model_id="MID")
        self.assertTrue(any("no applied_phases" in w for w in unknown["warnings"]))
        self.assertTrue(any("applied_phases must be" in p for p in
                            am.check_findings({"findings": [dict(f1, applied_phases=[9])]})["problems"]))

    def test_source_change_needs_a_change(self):
        plan = am.build_plan({"findings": [_finding("F1", "source_data", actions=[
            {"kind": "source_change", "args": {"column": "X"}}])]}, model_id="MID")
        self.assertTrue(any("missing args.change" in p for p in plan["problems"]))


class LookupTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="assess-lookups-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.scope = {"model": "Store Sales", "attributes": [
            {"name": "status code", "type": "varchar", "description": "Account status: 01 = open, 09 = closed",
             "profile": {"distinct_values": 4, "values": ["01", "02", "03", "09"]}},
            {"name": "channel", "type": "varchar", "description": "", "profile": {"distinct_values": 3}},
            {"name": "channel description", "type": "varchar", "description": "", "profile": {"distinct_values": 3}},
            {"name": "active flag", "type": "varchar", "description": "",
             "profile": {"distinct_values": 2, "values": ["N", "Y"]}},
            {"name": "event__code", "type": "varchar", "description": "", "profile": {"distinct_values": 5}},
            {"name": "customer", "type": "varchar", "description": "", "profile": {"distinct_values": 4000}},
            {"name": "snapshot month", "type": "date", "description": "", "profile": {"distinct_values": 3}}],
            "metrics": []}

    def test_suggestions(self):
        sugg = {s["attribute"]: s for s in am.lookup_suggestions(self.scope) if s["kind"] != "time_attributes"}
        self.assertEqual(set(sugg), {"status code", "snapshot month"})
        self.assertIn("explained only in the description", sugg["status code"]["reason"])
        self.assertEqual(sugg["snapshot month"]["kind"], "period_lookup")
        self.assertEqual((sugg["status code"]["new_forms"], sugg["status code"]["new_attributes"]),
                         (["status code description"], []))                  # descriptions are display forms
        self.assertIn("latest period flag", sugg["snapshot month"]["new_attributes"])   # filtered on

        self.scope["attributes"].append({"name": "latest period flag", "type": "varchar", "description": "",
                                         "profile": {"distinct_values": 2, "values": ["N", "Y"]}})
        self.assertNotIn("snapshot month", {s["attribute"] for s in am.lookup_suggestions(self.scope)
                                            if s["kind"] != "time_attributes"})
        self.scope["attributes"].append({"name": "snapshot month (year)__id", "type": "bigint"})
        self.assertNotIn("time_attributes", [s["kind"] for s in am.lookup_suggestions(self.scope)])
        rel = {"entity": "customer", "suggestions": {"entity_level_attributes": list("abcdef")}}
        kinds = [s["kind"] for s in am.lookup_suggestions(self.scope, relationships=rel)]
        self.assertIn("entity_table", kinds)

    def test_csv_templates(self):
        rows = [{"status code": c, "snapshot month": m} for c in ("01", "02", "09") for m in
                ("2025-12-01", "2026-01-01", "2026-02-01")]
        sugg = am.lookup_suggestions(self.scope) + [{"id": "L-entity", "kind": "entity_table", "attribute": "customer",
                                                     "columns": ["customer"]}]
        runner = TableRunner(rows)
        built = {b["attribute"]: b for b in am.build_lookups(runner, "retail", self.scope, sugg, self.dir)}

        # status code reuses the scope's complete sample (4 values); only the period is queried
        self.assertEqual(runner.calls, 1)
        with open(built["status code"]["csv"], encoding="utf-8", newline="") as f:
            self.assertEqual(list(csv.reader(f)), [["status code", "status code description"],
                                                   ["01", ""], ["02", ""], ["03", ""], ["09", ""]])
        with open(built["snapshot month"]["csv"], encoding="utf-8", newline="") as f:
            period = list(csv.reader(f))
        self.assertEqual(period, [["snapshot month", "latest period flag"], ["2025-12-01", "N"], ["2026-01-01", "N"],
                                  ["2026-02-01", "Y"]])          # the calendar comes from time attributes
        self.assertIsNone(built["customer"]["csv"])
        # fallback: the calendar from the lookup CSV
        sugg = am.lookup_suggestions(self.scope, time_attributes=False)
        built = {b["attribute"]: b for b in am.build_lookups(TableRunner(rows), "retail", self.scope, sugg, self.dir)}
        with open(built["snapshot month"]["csv"], encoding="utf-8", newline="") as f:
            period = list(csv.reader(f))
        self.assertEqual(period[0], ["snapshot month", "year", "quarter", "month", "latest period flag"])
        self.assertEqual(period[-1], ["2026-02-01", "2026", "2026-Q1", "02", "Y"])

    def test_null_rendered_as_zero_is_dropped_from_numeric_code_lookups(self):
        scope = {"model": "Store Sales", "attributes": [
            {"name": "exit code", "type": "bigint", "description": "",
             "profile": {"distinct_values": 3, "min": "1", "max": "9", "values": ["0", "1", "2", "9"]}}], "metrics": []}
        sugg = am.lookup_suggestions(scope)
        built = am.build_lookups(TableRunner([{"exit code": 1}]), "retail", scope, sugg, self.dir)
        with open(built[0]["csv"], encoding="utf-8", newline="") as f:
            self.assertEqual([r[0] for r in csv.reader(f)][1:], ["1", "2", "9"])
        self.assertIn("dropped ['0']", built[0]["note"])

    def test_plan_turns_add_lookup_table_into_manual_steps(self):
        ok = am.action_commands({"kind": "add_lookup_table", "args": {"csv": "status_code_lookup.csv",
                                                                     "key_attribute": "status code",
                                                                     "new_attributes": ["status description"]}}, "MID")
        self.assertEqual(ok["problems"], [])
        self.assertIn("EXISTING attribute 'status code'", ok["manual"])
        self.assertIn("status description", ok["manual"])
        bad = am.action_commands({"kind": "add_lookup_table", "args": {}}, "MID")
        self.assertEqual(len(bad["problems"]), 2)


# ── CLI ─────────────────────────────────────────────────────────────────────────────────────

class CliTests(unittest.TestCase):
    def test_scope_run_grade_plan_end_to_end_with_private_outputs(self):
        tmp = tempfile.mkdtemp(prefix="assess-cli-")
        self.addCleanup(shutil.rmtree, tmp, True)
        bank_path = os.path.join(tmp, "bank.json")
        bank = _bank()
        bank["questions"] = bank["questions"][:2]
        with open(bank_path, "w", encoding="utf-8") as f:
            json.dump(bank, f)
        factory = lambda args: FakeRunner(_replies())  # noqa: E731
        paths = {k: os.path.join(tmp, f"{k}.json") for k in ("scope", "results", "graded", "plan")}
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(am.main(["scope", "--project", "retail", "--model", "Store Sales", "--no-profile",
                                      "--out", paths["scope"]], runner_factory=factory), 0)
            self.assertEqual(am.main(["run", "--project", "retail", "--bank", bank_path, "--out", paths["results"]],
                                     runner_factory=factory), 0)
            self.assertEqual(am.main(["grade", "--bank", bank_path, "--results", paths["results"],
                                      "--out", paths["graded"]]), 0)
            self.assertEqual(am.main(["plan", "--findings", os.path.join(EXAMPLES, "findings.example.json"),
                                      "--model-id", "MID", "--quoting", "powershell", "--out", paths["plan"]]), 0)
        with open(paths["graded"], encoding="utf-8") as f:
            graded = json.load(f)
        self.assertEqual(graded["summary"]["by_verdict"], {"pass": 1, "fail": 1})
        if os.name == "posix":
            for p in paths.values():
                self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)

    def test_run_refuses_an_invalid_bank(self):
        tmp = tempfile.mkdtemp(prefix="assess-cli-")
        self.addCleanup(shutil.rmtree, tmp, True)
        bank_path = os.path.join(tmp, "bank.json")
        with open(bank_path, "w", encoding="utf-8") as f:
            json.dump({"model": "M", "questions": [{"id": "Q1", "level": 9}]}, f)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = am.main(["run", "--project", "p", "--bank", bank_path, "--out", os.path.join(tmp, "r.json")],
                           runner_factory=lambda a: self.fail("runner must not be created"))
        self.assertEqual(code, 2)
        self.assertIn("level must be 1-5", out.getvalue())

    def test_sign_in_failure_is_one_line_not_a_traceback(self):
        class McpError(RuntimeError):
            pass

        def factory(args):
            raise McpError("sign-in was not completed")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = am.main(["scope", "--project", "p", "--model", "m", "--out", os.devnull], runner_factory=factory)
        self.assertEqual(code, 2)
        self.assertIn("FATAL: McpError: sign-in was not completed", err.getvalue())

    def test_check_needs_a_file_and_plan_rejects_unknown_status(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(am.main(["check"]), 2)
            self.assertEqual(am.main(["plan", "--findings", os.path.join(EXAMPLES, "findings.example.json"),
                                      "--out", os.devnull, "--status", "approved,maybe"]), 2)


if __name__ == "__main__":
    unittest.main()


class RoleTests(unittest.TestCase):
    """Numeric columns read as the wrong kind of object, from the data rather than the names alone."""

    SCOPE = {"model": "Store Sales", "attributes": [
        {"name": "store id", "type": "bigint", "profile": {"distinct_values": 5000}},
        {"name": "unit price", "type": "double", "profile": {"distinct_values": 4200, "min": "0.5", "max": "99"}},
        {"name": "region code", "type": "integer", "profile": {"distinct_values": 9}}],
        "metrics": [{"name": m, "type": t, "formula": f"sum(sales.csv.{{{m}}})"} for m, t in (
            ("number of units", "bigint"), ("status code", "integer"), ("returned flag", "integer"),
            ("opening size", "double"), ("net sales", "double"), ("level", "integer"))]}

    def _profiles(self):
        return {"number of units": {"min": "1", "max": "6", "non_blank": "900", "non_integer": "0"},
                "status code": {"min": "0", "max": "12", "non_blank": "900", "non_integer": "0"},
                "returned flag": {"min": "0", "max": "1", "non_blank": "900", "non_integer": "0"},
                "opening size": {"min": "100.5", "max": "9,500.25", "non_blank": "900", "non_integer": "850"},
                "net sales": {"min": "-50.0", "max": "4,000.75", "non_blank": "900", "non_integer": "880"},
                "level": {"min": "1", "max": "7", "non_blank": "900", "non_integer": "0"}}

    def test_classification(self):
        variation = {"entities": 100, "row_count": 900, "varying": {
            "number of units": 0, "status code": 60, "returned flag": 40, "opening size": 0, "net sales": 95, "level": 50}}
        out = {c["name"]: c for c in am.classify_roles(self.SCOPE, self._profiles(), variation, entity="store id")}
        self.assertEqual(out["status code"]["role"], "attribute")
        self.assertEqual(out["returned flag"]["role"], "both")
        self.assertEqual(out["level"]["role"], "review")
        self.assertEqual(out["opening size"]["role"], "entity_characteristic")
        self.assertEqual([a["kind"] for a in out["opening size"]["suggested_actions"]], ["create_level_metric"])
        units = out["number of units"]                                         # a small count, constant per entity
        self.assertEqual(units["role"], "both")
        self.assertEqual([a["kind"] for a in units["suggested_actions"]], ["add_column_attribute", "create_level_metric"])
        self.assertNotIn("net sales", out)                                      # a real measure
        self.assertEqual(out["unit price"]["role"], "measure")                  # continuous, only an attribute
        self.assertNotIn("store id", out)                                       # the entity key
        self.assertNotIn("region code", out)                                    # a code attribute is fine

    def test_one_row_per_entity_skips_the_constancy_rule(self):
        variation = {"entities": 900, "row_count": 900, "varying": {"opening size": 0}}
        out = am.classify_roles(self.SCOPE, {"opening size": self._profiles()["opening size"]}, variation)
        self.assertEqual([c["name"] for c in out if c["object"] == "metric"], [])

    def test_review_roles_runs_batched_queries(self):
        prof = [["n0", "x0", "c0", "f0", "n1", "x1", "c1", "f1"], ["0", "1", "900", "0", "1", "6", "900", "0"]]
        var = [["entities", "row_count", "v0", "v1"], ["100", "900", "40", "0"]]
        scope = {"model": "Store Sales", "attributes": self.SCOPE["attributes"],
                 "metrics": self.SCOPE["metrics"][2:3] + self.SCOPE["metrics"][:1]}    # returned flag, number of units
        runner = FakeRunner({"FLOOR(": _table_reply(prof), "GROUP BY 1) t": _table_reply(var)})
        out = am.review_roles(runner, "retail", scope, entity="store id")
        self.assertEqual(out["rows_per_entity"], 9.0)
        self.assertEqual({c["name"]: c["role"] for c in out["candidates"] if c["object"] == "metric"},
                         {"returned flag": "both", "number of units": "both"})
        self.assertEqual(len([c for c in runner.calls if c[0] == "query"]), 2)

    def test_plan_turns_an_attribute_role_into_a_command(self):
        act = {"kind": "add_column_attribute", "args": {"column": "status code", "name": "Status"}}
        plan = am.build_plan({"findings": [_finding("F1", "attribute", actions=[act])]}, model_id="MID")
        self.assertEqual(plan["problems"], [])
        (ph,) = plan["phases"]
        self.assertEqual(ph["phase"], 3)
        self.assertEqual(ph["steps"][0]["apply"]["argv"][2:], ["add-column-attribute", "--model-id", "MID", "--name",
                                                                "Status", "--column", "status code"])
        bad = am.build_plan({"findings": [_finding("F1", "attribute", actions=[
            {"kind": "add_column_attribute", "args": {"column": "status code", "name": "<status code as an attribute>"}}])]},
            model_id="MID")
        self.assertTrue(any("placeholder" in p for p in bad["problems"]))


class CommandRoutingTests(unittest.TestCase):
    """Every command that takes --project queries the model, so it must go through the live path (the roles
    command once fell through to the offline path and crashed)."""

    def test_every_project_command_is_live(self):
        ap = am.build_parser()
        subs = next(a for a in ap._actions if isinstance(a, argparse._SubParsersAction))
        needs_model = {name for name, sp in subs.choices.items()
                       if any("--project" in a.option_strings for a in sp._actions)}
        # plan only names the project; check-lookup queries the model only when given one
        self.assertEqual(needs_model - {"plan", "check-lookup"}, set(am.LIVE_COMMANDS))

    def test_roles_runs_from_the_command_line_in_replay_mode(self):
        d = tempfile.mkdtemp(prefix="roles-cli-")
        self.addCleanup(shutil.rmtree, d, True)
        scope = {"model": "M", "attributes": [{"name": "store id", "type": "varchar", "profile": {"distinct_values": "1,234"}},
                                              {"name": "price", "type": "double", "profile": {"distinct_values": "error"}}],
                 "metrics": [{"name": "units", "type": "bigint", "formula": "sum(t.csv.{UNITS})"}]}
        for name, obj in (("scope.json", scope), ("rel.json", {"entity": "store id"})):
            with open(os.path.join(d, name), "w", encoding="utf-8") as f:
                json.dump(obj, f)
        argv = ["--replies", os.path.join(d, "replies"), "roles", "--project", "P", "--scope",
                os.path.join(d, "scope.json"), "--relationships", os.path.join(d, "rel.json"),
                "--out", os.path.join(d, "roles.json")]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(am.main(argv), 3)                       # replies pending, no crash
        with open(os.path.join(d, "replies", "pending.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["count"], 2)               # profile + variation queries


class AnchorTests(unittest.TestCase):
    """Fact rows are counted with a fact metric: aggregates over attributes only read their lookups."""

    def test_the_fullest_raw_fact_metric_is_the_anchor(self):
        metrics = [{"name": "net sales", "type": "double", "formula": "sum(t.csv.{NET SALES})"},
                   {"name": "units", "type": "bigint", "formula": "sum(t.csv.{UNITS})"},
                   {"name": "margin pct", "type": "double", "formula": "net sales / units"}]   # not raw
        self.assertEqual(am.raw_fact_metrics(metrics), ["net sales", "units"])
        runner = FakeRunner({'COUNT("net sales")': _table_reply([["c0", "c1"], ["900", "1,000"]])})
        anchor = am.choose_anchor(runner, "retail", "Store Sales", metrics)
        self.assertEqual((anchor["metric"], anchor["non_blank"]), ("units", 1000.0))
        self.assertIsNone(am.choose_anchor(FakeRunner({}), "retail", "Store Sales", metrics[2:]))

    def test_role_queries_count_with_metrics_not_constants(self):
        prof = am.roles_profile_sql("M", ["units"])
        self.assertIn('COUNT(CASE WHEN "units" <> FLOOR("units") THEN "units" END)', prof)
        self.assertNotIn("THEN 1", prof)
        var = am.roles_variation_sql("M", "store id", ["units"], anchor="net sales")
        self.assertIn('COUNT("net sales") AS rows_', var)
        self.assertNotIn("COUNT(*) AS rows_", var)


class _SqlRunner:
    """Answers each query from a function of its SQL (the probe issues many shapes of query)."""

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def call(self, tool, args):
        self.calls.append((tool, args))
        return _table_reply(self.answer(args["query"]))


class ProbeTests(unittest.TestCase):
    """Defects that show in some query shapes only, reproduced from a live run: the probe compares shapes,
    reads metrics plain (as analysts write them), filters on the latest-period flag, measures what a filter
    keeps in entities, and looks for gaps."""

    SCOPE = {"model": "Store Sales", "anchor": {"metric": "net sales"},
             "attributes": [{"name": "store id", "type": "bigint", "profile": {"distinct_values": 100}},
                            {"name": "period", "type": "varchar", "profile": {"distinct_values": 3}},
                            {"name": "latest flag", "type": "varchar", "profile": {"distinct_values": 2}},
                            {"name": "region", "type": "varchar", "profile": {"distinct_values": 3}},
                            {"name": "exit type", "type": "varchar", "profile": {"distinct_values": 4}},
                            {"name": "promo flag", "type": "varchar", "profile": {"distinct_values": 3}},
                            {"name": "months open", "type": "bigint", "profile": {"distinct_values": 40}},
                            {"name": "term months", "type": "bigint", "profile": {"distinct_values": 5}}],
             "metrics": [{"name": "net sales", "type": "double", "formula": "sum(t.csv.{NET SALES})"},
                         {"name": "largest proceeds", "type": "double", "formula": "max(t.csv.{PROCEEDS})"},
                         {"name": "months delinquent", "type": "double", "formula": "avg(map.csv.{MONTHS})"},
                         {"name": "weighted score", "type": "double", "formula": "score x / score w"},
                         {"name": "active count", "type": "bigint", "formula": "Count(store){~, period}",
                          "description": "Stores open."}]}

    @staticmethod
    def answer(sql):
        flt = "\"latest flag\" = 'Y'" in sql
        if sql.startswith('SELECT CAST(COUNT("net sales") AS VARCHAR) AS n FROM'):
            return [["n"], ["400" if flt else "1000"]]
        if sql.startswith("SELECT CAST(COUNT(*) AS VARCHAR) AS n FROM (SELECT"):
            return [["n"], ["100"]]                                      # every store is in the latest period
        if 'COUNT("months open")' in sql:
            return [["n"], ["40"]]
        if 'COUNT("term months")' in sql:
            return [["n"], ["1000"]]
        if sql.startswith('SELECT CAST("latest flag" AS VARCHAR) AS v, CAST(COUNT'):
            return [["v", "n"], ["N", "600"], ["Y", "400"]]
        if sql.startswith('SELECT CAST("region" AS VARCHAR) AS v, CAST(COUNT'):
            return [["v", "n"], ["a", "500"], ["b", "300"], ["c", "200"]]
        if 'CAST("region" AS VARCHAR) AS p, CAST("period" AS VARCHAR) AS t' in sql:
            return [["p", "t", "n"], ["a", "202401", "200"], ["a", "202403", "300"], ["b", "202401", "100"],
                    ["b", "202402", "300"], ["c", "202403", "100"]]          # a lacks 202402
        for attr, values in (("exit type", ["sold", "paid", "closed", "transfer"]), ("promo flag", ["Y", "N", "Unknown"]),
                             ("months open", ["1", "2"]), ("term months", ["360"])):
            q = f'CAST("{attr}" AS VARCHAR) AS v'
            if sql.startswith(f"SELECT {q} FROM"):
                return [["v"]] + [[v] for v in values]
            if f'CAST("region" AS VARCHAR) AS p, {q}' in sql:
                return [["p", "v", "n"], ["a", values[0], "1000"]]
            if sql.startswith(f"SELECT {q}, CAST(COUNT"):
                if attr == "exit type" and flt:
                    return [["v", "n"], ["sold", "250"]]                 # the latest-flag filter drops blank keys
                if attr == "promo flag":
                    return [["v", "n"], ["Y", "400" if flt else "600"], ["N", "0" if flt else "400"]]
                return [["v", "n"], [values[0], "400" if flt else "1000"]]
        cols = ["m0", "m1", "m2", "m3", "m4"]
        if sql.startswith('SELECT CAST("period" AS VARCHAR) AS g'):
            return [["g"] + cols, ["202401", "300", "653", "18", "738", ""], ["202402", "300", "653", "19", "741", ""],
                    ["202403", "400", "653", "20", "740", "42"]]
        if sql.startswith('SELECT CAST("latest flag" AS VARCHAR) AS g'):
            return [["g"] + cols, ["N", "600", "653", "19", "739", ""], ["Y", "400", "653", "20", "740", "42"]]
        if sql.startswith('SELECT CAST("region" AS VARCHAR) AS g'):
            return [["g"] + cols, ["a", "200", "653", "21", "735", "20"], ["b", "100", "653", "20", "742", "12"],
                    ["c", "100", "653", "19", "744", "10"]]
        if sql.startswith("SELECT AVG("):
            return [["m2"], ["49.5"]]
        if sql.startswith("SELECT CAST(SUM("):                              # wrapped: bypasses Max
            return [cols, ["1,000", "25,047,440.54", "49.5", "740", "42"]]
        if sql.startswith("SELECT SUM("):
            return [cols, ["400", "653", "20", "740", "42"]] if flt else [cols, ["1,000", "653", "49.5", "740", "42"]]
        raise AssertionError(sql)

    def _run(self):
        return am.probe_objects(_SqlRunner(self.answer), "retail", self.SCOPE, period="period",
                                attributes=["exit type", "promo flag", "months open", "term months"])

    def test_filter_partner_entities_and_gaps(self):
        out = self._run()
        self.assertEqual((out["filter"]["attribute"], out["filter"]["value"]), ("latest flag", "Y"))
        self.assertEqual((out["filter"]["rows"], out["filter"]["entities"], out["entities"]), (400.0, 100.0, 100.0))
        self.assertEqual(out["partner"], "region")
        self.assertEqual(out["gaps"], [{"partner": "a", "missing": ["202402"]}])

    def test_attribute_checks(self):
        attrs = {a["name"]: a for a in self._run()["attributes"]}
        self.assertTrue(any("filtered to latest flag = Y: keeps 250 of 400" in x for x in attrs["exit type"]["issues"]))
        self.assertEqual(attrs["promo flag"]["lookup_only_values"], ["Unknown"])
        self.assertTrue(any("read from its lookup" in x for x in attrs["months open"]["issues"]))
        self.assertEqual(attrs["term months"]["issues"], [])

    def test_metric_checks(self):
        mets = {m["name"]: m for m in self._run()["metrics"]}
        self.assertEqual(mets["net sales"]["issues"], [])
        largest = " | ".join(mets["largest proceeds"]["issues"])
        self.assertIn("wrapping changes its value: SUM gives 653.0, CAST(SUM(...)) gives 25047440.54", largest)
        self.assertIn("same value (653.00) in every group of 'region' when filtered to latest flag = Y", largest)
        self.assertNotIn("ignores a filter", largest)                    # the filter keeps every store
        self.assertTrue(any("reads table 'map.csv'" in x for x in mets["months delinquent"]["issues"]))
        self.assertEqual(mets["weighted score"]["issues"], [])            # loan-level value, all loans kept: fine
        self.assertTrue(any("one period only" in x for x in mets["active count"]["issues"]))

    def test_wrapping_an_average_is_a_note_and_an_empty_column_is_not_a_bypass(self):
        scope = dict(self.SCOPE, metrics=[{"name": "net sales", "type": "double", "formula": "sum(t.csv.{NET SALES})"},
                                          {"name": "avg rate", "type": "double", "formula": "avg(t.csv.{RATE})"},
                                          {"name": "empty cost", "type": "double", "formula": "sum(t.csv.{COST})"}])

        def answer(sql):
            if sql.startswith("SELECT CAST(SUM("):
                return [["m0", "m1", "m2"], ["1000", "4,800", "0"]]
            if sql.startswith("SELECT SUM("):
                return [["m0", "m1", "m2"], ["1000", "4.8", ""]]
            if sql.startswith("SELECT AVG("):
                return [["m1"], ["4.82"]]
            if sql.startswith('SELECT CAST("') and "AS g" in sql:
                return [["g", "m0", "m1", "m2"]]
            return self.answer(sql)
        out = am.probe_objects(_SqlRunner(answer), "retail", scope, period="period", attributes=[])
        mets = {m["name"]: m for m in out["metrics"]}
        self.assertEqual(mets["avg rate"]["issues"], [])
        self.assertTrue(any("Expected for an average" in x for x in mets["avg rate"]["notes"]))
        self.assertEqual(mets["empty cost"]["issues"], [])

    def test_truncated_average(self):
        scope = dict(self.SCOPE, metrics=[{"name": "net sales", "type": "double", "formula": "sum(t.csv.{NET SALES})"},
                                          {"name": "avg score", "type": "bigint", "formula": "avg(t.csv.{SCORE})"}])

        def answer(sql):
            if sql.startswith("SELECT CAST(SUM("):
                return [["m0", "m1"], ["1000", "746"]]
            if sql.startswith("SELECT SUM("):
                return [["m0", "m1"], ["1000", "746"]]
            if sql.startswith("SELECT AVG("):
                return [["m1"], ["746.868"]]
            if sql.startswith('SELECT CAST("') and "AS g" in sql:
                return [["g", "m0", "m1"]]
            return self.answer(sql)
        out = am.probe_objects(_SqlRunner(answer), "retail", scope, period="period", attributes=[])
        issues = {m["name"]: m["issues"] for m in out["metrics"]}
        self.assertTrue(any("truncated (746 for 746.868)" in x and "(bigint)" in x for x in issues["avg score"]))
        self.assertEqual(issues["net sales"], [])

    def test_needs_an_anchor(self):
        with self.assertRaises(SystemExit):
            am.probe_objects(_SqlRunner(self.answer), "retail", dict(self.SCOPE, anchor=None))

    def test_changed_objects(self):
        old = {"attributes": [{"name": "region", "type": "varchar"}],
               "metrics": [{"name": "net sales", "type": "double", "formula": "sum(x)"}]}
        new = {"attributes": [{"name": "region", "type": "varchar"}, {"name": "region group", "type": "varchar"}],
               "metrics": [{"name": "net sales", "type": "double", "formula": "max(x)"},
                           {"name": "units", "type": "bigint", "formula": "sum(u)"}]}
        findings = {"findings": [_finding("F1", "description", actions=[
            {"kind": "rename_describe", "args": {"items": []}}]), _finding("F2", "attribute", actions=[
            {"kind": "add_column_attribute", "args": {"name": "region", "column": "x"}}])]}
        out = am.changed_objects(old, new, findings, [3])
        self.assertEqual(out["attributes"], ["region group", "region"])          # new, then named by a phase-3 action
        self.assertEqual(out["metrics"], ["net sales", "units"])                 # formula changed, new


class QuestionSqlTests(unittest.TestCase):
    def test_rewrite_keeps_history_and_tracks_answerability(self):
        bank = {"questions": [{"id": "Q1", "sql": "", "expected_path": "not_answerable",
                               "best_answer": {"sql": "SELECT 2"}}]}
        bad = am.set_question_sql(bank, "Q1", sql="SELECT 2", reason_kind="new_object", reason="x")
        self.assertIn("can't be its best answer", " ".join(bad["problems"]))
        self.assertFalse(am.set_question_sql(bank, "Q1", sql="SELECT 1", reason_kind="new_object", reason="")["ok"])
        ok = am.set_question_sql(bank, "Q1", sql="SELECT 1", reason_kind="new_object", reason="metric M added",
                                 round_no=2)
        self.assertEqual((ok["ok"], ok["expected_path"]), (True, "governed_metric"))
        self.assertEqual(bank["questions"][0]["sql_history"][0]["expected_path"], "not_answerable")
        previous = {"questions": [{"id": "Q1", "verdict": "ungraded", "no_sql": True, "expected_path": "not_answerable"}]}
        graded = am.grade_bank(bank, {"results": [{"id": "Q1", "ok": True, "rows": [["1"]], "columns": ["x"]}]},
                               previous=previous)
        self.assertEqual(graded["answerability"]["newly_runnable"], ["Q1"])
        self.assertEqual(graded["answerability"]["simpler_path"],
                         [{"id": "Q1", "before": "not_answerable", "after": "governed_metric"}])

    def test_definition_is_a_reason_kind(self):
        bank = {"questions": [{"id": "Q1", "sql": "SELECT 1", "best_answer": {"sql": "SELECT 3"}}]}
        out = am.set_best_answer(bank, "Q1", sql="SELECT 4", reason_kind="definition", reason="balance-weighted")
        self.assertTrue(out["ok"])


class TimeAttributePlanTests(unittest.TestCase):
    def test_type_change_then_time_attributes_in_phase_3(self):
        f = _finding("F1", "data_type", actions=[
            {"kind": "change_column_type", "args": {"column": "reporting period", "type": "date",
                                                    "date_format": "yyyyMM"}},
            {"kind": "add_time_attributes", "args": {"attribute": "reporting period",
                                                     "intervals": ["year", "quarter", "month"]}}])
        plan = am.build_plan({"findings": [f]}, model_id="MID")
        self.assertEqual(plan["problems"], [])
        steps = {p["phase"]: [s["kind"] for s in p["steps"] if s["kind"] != "note"] for p in plan["phases"]}
        self.assertEqual(steps[2], ["change_column_type"])
        self.assertIn("add_time_attributes", steps[3])
        self.assertEqual(plan["files"]["specs/time_attributes.json"],
                         {"items": [{"attribute": "reporting period", "intervals": ["year", "quarter", "month"]}]})
        bad = am.build_plan({"findings": [_finding("F1", "data_type", actions=[
            {"kind": "add_time_attributes", "args": {"attribute": "x", "intervals": ["fortnight"]}}])]}, model_id="MID")
        self.assertTrue(any("unknown" in p for p in bad["problems"]))


class ExistingTableTests(unittest.TestCase):
    """Lookups on a model that already has mapping tables: extend them, keyed on the most detailed code."""

    SCOPE = {"model": "Store Sales", "anchor": {"metric": "net sales"}, "attributes": [
        {"name": "store id", "type": "bigint", "profile": {"distinct_values": 100}},
        {"name": "status code", "type": "varchar", "profile": {"distinct_values": 12, "values": ["00", "01", "02", "XX"]}},
        {"name": "status bucket", "type": "varchar", "profile": {"distinct_values": 4, "values": ["0", "1", "2", "9"]}},
        {"name": "late flag", "type": "varchar", "profile": {"distinct_values": 2, "values": ["Y", "N"]}},
        {"name": "risk level", "type": "varchar", "profile": {"distinct_values": 2, "values": ["High", "Low"]}},
        {"name": "exit type", "type": "varchar", "profile": {"distinct_values": 3,
                                                             "values": ["Prepaid", "REO sale", "Note sale"]}},
        {"name": "channel code", "type": "varchar", "profile": {"distinct_values": 3, "values": ["R", "B", "C"]}}],
        "metrics": []}
    REL = {"suggestions": {"parent_child": [{"child": "status code", "parent": "status bucket"},
                                            {"child": "status code", "parent": "late flag"}]}}
    PROBES = {"attributes": [{"name": "status bucket", "lookup_only_values": ["9"]},
                             {"name": "status code", "lookup_only_values": ["XX"]}]}

    def test_readable_values_need_no_lookup(self):
        self.assertTrue(am.looks_like_codes(["00", "01", "XX"]))
        self.assertFalse(am.looks_like_codes(["PREPAID", "REO_SALE"]))      # upper-case words read as they are
        self.assertFalse(am.looks_like_codes(["19DNA1", "19HQA2"]))          # identifiers analysts already use
        self.assertTrue(am.looks_like_codes(["RA", "XX", "01", "P"]))
        self.assertFalse(am.looks_like_codes(["High", "Low"]))
        self.assertFalse(am.looks_like_codes(["Prepaid", "REO sale"]))
        self.assertTrue(am.looks_like_codes(["Alpha", "Beta"], "Alpha = first lien, Beta = second lien"))
        kinds = {s["attribute"]: s["kind"] for s in am.lookup_suggestions(self.SCOPE, time_attributes=False)}
        self.assertNotIn("risk level", kinds)
        self.assertNotIn("exit type", kinds)
        self.assertEqual(kinds["channel code"], "code_lookup")

    def test_inferred_table_is_extended_once_keyed_on_the_most_detailed_code(self):
        tables = am.existing_lookup_tables(self.SCOPE, relationships=self.REL, probes=self.PROBES)
        (t,) = tables.values()
        self.assertEqual((t["key"], sorted(t["determined"]), t["source"]), ("status code", ["late flag", "status bucket"],
                                                                            "inferred"))
        sugg = am.lookup_suggestions(self.SCOPE, relationships=self.REL, probes=self.PROBES, time_attributes=False)
        ext = [s for s in sugg if s["kind"] == "extend_lookup"]
        self.assertEqual(len(ext), 1)
        self.assertEqual(ext[0]["keep_columns"], ["status code", "late flag", "status bucket"])
        self.assertEqual(ext[0]["new_forms"], ["status code description", "status bucket description"])
        self.assertIn("export the model", ext[0]["reason"])
        self.assertEqual(ext[0]["headers"], "connector names")
        self.assertNotIn("status bucket", [s["attribute"] for s in sugg if s["kind"] == "code_lookup"])

    def test_model_export_names_the_table(self):
        export = {"model": [{"tables": [{"information": {"name": "facts.csv"}},
                                        {"information": {"name": "status_map.csv"},
                                         "physicalTable": {"columns": [{"columnName": "STATUS"}, {"columnName": "BUCKET"}]}}],
                             "factMetrics": [{"expressions": [{"tables": [{"name": "facts.csv"}]}]}],
                             "attributes": [{"information": {"name": "Store ID"}, "attributeLookupTable": {"name": "facts.csv"}},
                                            {"information": {"name": "Status Code"}, "attributeLookupTable": {"name": "status_map.csv"}},
                                            {"information": {"name": "Status Bucket"}, "attributeLookupTable": {"name": "status_map.csv"}}]}]}
        tables = am.existing_lookup_tables(self.SCOPE, relationships=self.REL, export=export)
        self.assertEqual(list(tables), ["status_map.csv"])
        self.assertEqual((tables["status_map.csv"]["key"], tables["status_map.csv"]["columns"]),
                         ("status code", ["STATUS", "BUCKET"]))

    def test_template_from_the_table_and_blank_keys(self):
        d = tempfile.mkdtemp(prefix="extend-")
        self.addCleanup(shutil.rmtree, d, True)
        sugg = [s for s in am.lookup_suggestions(self.SCOPE, relationships=self.REL, probes=self.PROBES,
                                                 time_attributes=False) if s["kind"] == "extend_lookup"]

        def answer(sql):
            if "IS NULL OR CAST" in sql:
                return [["n"], ["136,699"]]
            if "GROUP BY 1, 2, 3" in sql:
                self.assertIn("CASE WHEN \"status code\" IS NULL THEN '' ELSE CAST", sql)   # blanks stay blank
                return [["c0", "c1", "c2"], ["01", "N", "1"], ["00", "N", "0"], ["XX", "", "9"]]
            raise AssertionError(sql)
        (built,) = am.build_lookups(_SqlRunner(answer), "retail", self.SCOPE, sugg, d)
        with open(built["csv"], encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[0], ["status code", "late flag", "status bucket", "status code description",
                                   "status bucket description"])
        self.assertEqual(rows[1], ["00", "N", "0", "", ""])
        self.assertEqual(rows[3], ["XX", "", "9", "", ""])
        self.assertEqual(built["blank_key_fact_rows"], 136699.0)
        self.assertIn("kept them", built["blank_key_note"])                  # no probe evidence of loss: a note

    EXPORT = {"model": [{"tables": [
        {"information": {"name": "facts.csv"}},
        {"information": {"name": "status_map.csv"}, "physicalTable": {"columns": [
            {"columnName": "STATUS"}, {"columnName": "BUCKET SORT"}, {"columnName": "BUCKET"},
            {"columnName": "MONTHS LATE"}, {"columnName": "UNUSED"}]}}],
        "factMetrics": [{"expressions": [{"tables": [{"name": "facts.csv"}]}]}],
        "attributes": [
            {"information": {"name": "Store ID"}, "attributeLookupTable": {"name": "facts.csv"}},
            {"information": {"name": "Status Code"}, "attributeLookupTable": {"name": "status_map.csv"},
             "keyForm": {"id": "K"}, "forms": [{"id": "K", "name": "ID", "category": "ID",
                                                "expressions": [{"expression": {"text": "[STATUS]"}}]}]},
            {"information": {"name": "Status Bucket"}, "attributeLookupTable": {"name": "status_map.csv"},
             "keyForm": {"id": "K"}, "forms": [
                 {"id": "K", "name": "ID", "category": "ID", "expressions": [{"expression": {"text": "[BUCKET SORT]"}}]},
                 {"id": "L", "name": "Status Bucket", "category": "Bucket Sort Order None",
                  "expressions": [{"expression": {"text": "[BUCKET]"}}]},
                 {"id": "D", "name": "DESC", "category": "DESC", "expressions": [{"expression": {"text": "[BUCKET]"}}]}]},
            {"information": {"name": "Late Flag"}, "attributeLookupTable": {"name": "status_map.csv"},
             "keyForm": {"id": "K"}, "forms": [{"id": "K", "name": "ID", "category": "ID",
                                                "expressions": [{"expression": {"text": "[MONTHS LATE]"}}]}]}]}]}

    def test_with_the_export_headers_are_the_tables_and_every_column_is_kept(self):
        scope = dict(self.SCOPE, attributes=self.SCOPE["attributes"] + [{"name": "status bucket__desc", "type": "varchar"}])
        sugg = am.lookup_suggestions(scope, relationships=self.REL, export=self.EXPORT, time_attributes=False)
        (ext,) = [s for s in sugg if s["kind"] == "extend_lookup"]
        self.assertEqual(ext["keep_columns"], ["STATUS", "BUCKET SORT", "BUCKET", "MONTHS LATE", "UNUSED"])
        self.assertEqual(ext["new_forms"], ["status code description"])        # the bucket already has a label
        self.assertEqual(ext["read_as"]["BUCKET"], "status bucket__desc")
        self.assertEqual(ext["headers"], "table columns")
        hidden = [s for s in sugg if s["kind"] == "hidden_form"]
        self.assertEqual([(h["attribute"], h["form"], h["query_as"]) for h in hidden],
                         [("status bucket", "Status Bucket", "status bucket (status bucket)")])

        def answer(sql):
            if "IS NULL OR CAST" in sql:
                return [["n"], ["0"]]
            return [["c0", "c1", "c2", "c3"], ["01", "1", "30-59 days", "1"], ["RA", "5", "180+ days", ""]]
        d = tempfile.mkdtemp(prefix="extend-x-")
        self.addCleanup(shutil.rmtree, d, True)
        built = next(b for b in am.build_lookups(_SqlRunner(answer), "retail", scope, [ext], d))
        with open(built["csv"], encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[0], ["STATUS", "BUCKET SORT", "BUCKET", "MONTHS LATE", "UNUSED",
                                   "status code description"])
        self.assertEqual(rows[2], ["RA", "5", "180+ days", "", "", ""])   # a blank stays blank, not 0
        self.assertEqual(built["unreadable_columns"], ["UNUSED"])

    def test_rows_lost_through_a_described_attribute_still_reported(self):
        probes = {"attributes": [{"name": "late flag", "issues": [
            "filtered to latest flag = Y: keeps 341,684 of 478,383 fact rows (136,699 lost: ...)"]}]}
        sugg = am.lookup_suggestions(self.SCOPE, probes=probes, time_attributes=False)
        (b,) = [s for s in sugg if s["kind"] == "blank_key"]
        self.assertEqual(b["attribute"], "late flag")

        def answer(sql):
            return [["n"], ["9,771,371"]]
        d = tempfile.mkdtemp(prefix="blank-")
        self.addCleanup(shutil.rmtree, d, True)
        (built,) = am.build_lookups(_SqlRunner(answer), "retail", self.SCOPE, [b], d)
        self.assertIn("probe saw rows lost", built["blank_key_warning"])

    def test_plan_refuses_a_replacement_that_drops_a_column(self):
        def plan(cols):
            return am.build_plan({"findings": [_finding("F1", "lookup", actions=[
                {"kind": "add_lookup_table", "args": {"csv": "lookups/status_code_lookup.csv", "key_attribute": "status code",
                                                      "extends_table": "status_map.csv",
                                                      "keep_columns": ["status code", "late flag"], "columns": cols,
                                                      "new_forms": ["status code description"]}}])]}, model_id="MID")
        self.assertTrue(any("drops ['late flag']" in p for p in plan(["status code", "status code description"])["problems"]))
        ok = plan(["status code", "late flag", "status code description"])
        self.assertEqual(ok["problems"], [])
        self.assertIn("REPLACES the data of existing table 'status_map.csv'", json.dumps(ok))


class RelationshipErrorTests(unittest.TestCase):
    def test_a_failed_query_does_not_crash_the_summary(self):
        class R:
            def call(self, tool, args):
                return {"content": [{"type": "text", "text": "Query failed: per-job memory limit"}], "isError": True}
        scope = {"model": "S", "attributes": [{"name": "store id", "type": "bigint", "profile": {"distinct_values": 100}},
                                              {"name": "region", "type": "varchar", "profile": {"distinct_values": 3}},
                                              {"name": "city", "type": "varchar", "profile": {"distinct_values": 10}}],
                 "metrics": []}
        out = am.review_relationships(R(), "p", scope, entity="store id")
        self.assertEqual(out["failed_queries"], ["Query failed: per-job memory limit"])


class FormListingTests(unittest.TestCase):
    """The connector can list an attribute only through its forms ('<attribute> (id)'): the export's forms
    are still matched, so hidden forms and the columns they read are found."""

    def test_attribute_listed_only_as_its_id_form(self):
        T = ExistingTableTests
        scope = dict(T.SCOPE, attributes=[a for a in T.SCOPE["attributes"] if a["name"] != "status bucket"]
                     + [{"name": "status bucket (id)", "type": "varchar"}])
        forms = am.export_forms(scope, T.EXPORT)
        self.assertIn("status bucket", forms)
        key = next(f for f in forms["status bucket"]["forms"] if f["key"])
        self.assertEqual((key["visible"], key["query_as"]), (True, "status bucket (id)"))
        hidden = am.hidden_forms(scope, T.EXPORT)
        self.assertEqual(sorted(h["form"] for h in hidden), ["DESC", "Status Bucket"])
        tables = am.existing_lookup_tables(scope, relationships=T.REL, export=T.EXPORT)
        self.assertEqual(tables["status_map.csv"]["read_as"]["BUCKET SORT"], "status bucket (id)")
        self.assertEqual(tables["status_map.csv"]["read_as"]["BUCKET"], "status bucket (status bucket)")

    def test_blank_keys_counted_on_the_tables_key_and_time_attributes_name_every_table(self):
        T = ExistingTableTests
        probes = {"attributes": [{"name": "late flag", "issues": ["filtered to x = Y: keeps 10 of 20 fact rows (10 lost)"]}]}
        sugg = am.lookup_suggestions(T.SCOPE, relationships=T.REL, probes=probes, export=T.EXPORT, time_attributes=False)
        (b,) = [x for x in sugg if x["kind"] == "blank_key"]
        self.assertEqual(b["count_on"], "status code")                        # the column that joins the fact
        export = {"model": [{"tables": [], "attributes": [
            {"information": {"name": "Reporting Period"}, "attributeLookupTable": {"name": "facts.csv"},
             "keyForm": {"id": "K"}, "forms": [{"id": "K", "name": "ID", "expressions": [
                 {"expression": {"text": "[PERIOD]"}, "tables": [{"name": "facts.csv"}]},
                 {"expression": {"text": "[Calendar Month]"}, "tables": [{"name": "Month_Map.csv"}]}]}]}]}]}
        scope = {"model": "M", "attributes": [{"name": "reporting period", "type": "bigint", "description": "YYYYMM",
                                               "profile": {"distinct_values": 30, "values": ["201901", "202201"]}}]}
        (t,) = [x for x in am.lookup_suggestions(scope, export=export) if x["kind"] == "time_attributes"]
        self.assertEqual(t["tables"], ["facts.csv", "Month_Map.csv"])
        self.assertIn("each of these tables", t["shared_table_note"])

    # an attribute the connector lists only as "<attribute>__code" still keys its table, and a hidden key form
    # is reported
    MAP_EXPORT = {"model": [{"tables": [
        {"information": {"name": "facts.csv"}},
        {"information": {"name": "exit_map.csv"}, "physicalTable": {"columns": [
            {"columnName": "ZB CODE"}, {"columnName": "EXIT"}, {"columnName": "CREDIT EVENT"}]}}],
        "factMetrics": [{"expressions": [{"tables": [{"name": "facts.csv"}]}]}],
        "attributes": [
            {"information": {"name": "Exit Code"}, "attributeLookupTable": {"name": "exit_map.csv"},
             "keyForm": {"id": "K"}, "forms": [{"id": "K", "name": "code", "category": "ID", "expressions": [
                 {"expression": {"text": "[ZB CODE]"}, "tables": [{"name": "exit_map.csv"}, {"name": "facts.csv"}]}]}]},
            {"information": {"name": "Exit Type"}, "attributeLookupTable": {"name": "exit_map.csv"},
             "keyForm": {"id": "K"}, "forms": [{"id": "K", "name": "ID", "category": "ID", "expressions": [
                 {"expression": {"text": "[EXIT]"}, "tables": [{"name": "exit_map.csv"}]}]}]},
            {"information": {"name": "Credit Event"}, "attributeLookupTable": {"name": "exit_map.csv"},
             "keyForm": {"id": "K"}, "forms": [{"id": "K", "name": "ID", "category": "ID", "expressions": [
                 {"expression": {"text": "[CREDIT EVENT]"}, "tables": [{"name": "exit_map.csv"}]}]}]},
            {"information": {"name": "Channel"}, "attributeLookupTable": {"name": "facts.csv"},
             "keyForm": {"id": "K"}, "forms": [
                 {"id": "K", "name": "ID", "category": "ID", "expressions": [{"expression": {"text": "[CH]"}}]},
                 {"id": "D", "name": "description", "category": "DESC",
                  "expressions": [{"expression": {"text": "[CH DESC]"}}]}]}]}]}
    MAP_SCOPE = {"model": "M", "anchor": {"metric": "balance"}, "attributes": [
        {"name": "exit code__code", "type": "varchar", "profile": {"distinct_values": 9}},
        {"name": "exit type", "type": "varchar", "profile": {"distinct_values": 12}},
        {"name": "credit event", "type": "varchar", "profile": {"distinct_values": 2}},
        {"name": "channel__description", "type": "varchar", "profile": {"distinct_values": 3}}]}

    def test_table_keyed_on_an_attribute_listed_only_through_a_form(self):
        tables = am.existing_lookup_tables(self.MAP_SCOPE, export=self.MAP_EXPORT)
        g = tables["exit_map.csv"]
        self.assertEqual(sorted(g["attributes"]), ["credit event", "exit code", "exit type"])
        self.assertEqual(g["key"], "exit code")          # it joins the fact, though exit type has more values
        self.assertEqual(g["connector_names"]["exit code"], "exit code__code")
        probes = {"attributes": [{"name": n, "issues": ["filtered to x = Y: keeps 10 of 20 fact rows (10 lost)"]}
                                 for n in ("exit type", "credit event")]}
        sugg = am.lookup_suggestions(self.MAP_SCOPE, probes=probes, export=self.MAP_EXPORT, time_attributes=False)
        self.assertEqual({b["attribute"]: b["count_on"] for b in sugg if b["kind"] == "blank_key"},
                         {"exit type": "exit code__code", "credit event": "exit code__code"})

    def test_hidden_key_form_is_reported(self):
        hidden = am.hidden_forms(self.MAP_SCOPE, self.MAP_EXPORT)
        self.assertIn(("channel", "ID", True, "channel (id)"),
                      [(h["attribute"], h["form"], h["key"], h["query_as"]) for h in hidden])
        sugg = am.lookup_suggestions(self.MAP_SCOPE, export=self.MAP_EXPORT, time_attributes=False)
        (h,) = [s for s in sugg if s["kind"] == "hidden_form" and s["attribute"] == "channel"]
        self.assertIn("key form", h["reason"])
        self.assertIn("filter or group on the codes", h["reason"])

    def test_tables_found_through_the_tables_forms_read(self):
        def attr(name, lookup, forms):
            out = {"information": {"name": name}, "keyForm": {"id": forms[0][0]},
                   "forms": [{"id": i, "name": fn, "category": cat,
                              "expressions": [{"expression": {"text": f"[{c}]"}, "tables": [{"name": t}]} for c, t in ex]}
                             for i, fn, cat, ex in forms]}
            if lookup:
                out["attributeLookupTable"] = {"name": lookup}
            return out
        export = {"model": [{"tables": [
            {"information": {"name": "facts.csv"}},
            {"information": {"name": "seller_lookup.csv"}, "physicalTable": {"columns": [
                {"columnName": "SELLER"}, {"columnName": "SELLER GROUP"}]}},
            {"information": {"name": "Month_Map.csv"}, "physicalTable": {"columns": [
                {"columnName": "Calendar Month"}, {"columnName": "Activity Month"}]}},
            {"information": {"name": "ptype_lookup.csv"}, "physicalTable": {"columns": [
                {"columnName": "PT"}, {"columnName": "PT DESC"}]}}],
            "factMetrics": [{"expressions": [{"tables": [{"name": "facts.csv"}]}]}],
            "attributes": [
                attr("Seller Name", "facts.csv", [("K", "ID", "ID", [("SELLER", "seller_lookup.csv"),
                                                                     ("SELLER", "facts.csv")])]),
                attr("Seller Group", "seller_lookup.csv", [("K", "ID", "ID", [("SELLER GROUP", "seller_lookup.csv")])]),
                attr("Reporting Period", "facts.csv", [("K", "ID", "ID", [("PERIOD", "facts.csv"),
                                                                          ("Calendar Month", "Month_Map.csv")])]),
                attr("Activity Month", "Month_Map.csv", [("K", "ID", "ID", [("Activity Month", "Month_Map.csv")])]),
                attr("Property Type", None, [("K", "ID", "ID", [("PT", "ptype_lookup.csv"), ("PT", "facts.csv")]),
                                             ("D", "DESC", "DESC", [("PT DESC", "ptype_lookup.csv")])])]}]}
        scope = {"model": "M", "attributes": [
            {"name": "seller name", "type": "varchar", "profile": {"distinct_values": 40}},
            {"name": "seller group", "type": "varchar", "profile": {"distinct_values": 300}},
            {"name": "reporting period", "type": "bigint", "profile": {"distinct_values": 30}},
            {"name": "activity month", "type": "varchar", "profile": {"distinct_values": 400}},
            {"name": "property type", "type": "varchar", "profile": {"distinct_values": 5}},
            {"name": "property type__desc", "type": "varchar", "profile": {"distinct_values": 5}}]}
        tables = am.existing_lookup_tables(scope, export=export)
        self.assertEqual({t: g["key"] for t, g in tables.items()},
                         {"seller_lookup.csv": "seller name", "Month_Map.csv": "reporting period",
                          "ptype_lookup.csv": "property type"})   # the join, though the other attribute has more values
        self.assertEqual(tables["Month_Map.csv"]["read_as"]["Calendar Month"], "reporting period")
        self.assertNotIn("PERIOD", tables["Month_Map.csv"]["read_as"])      # that column is the fact's
        self.assertEqual(tables["ptype_lookup.csv"]["read_as"]["PT DESC"], "property type__desc")
        self.assertEqual(tables["seller_lookup.csv"]["declared"], ["seller group"])

    def test_hidden_form_suggestion_flags_a_key_form(self):
        sugg = am.lookup_suggestions(self.MAP_SCOPE, export=self.MAP_EXPORT, time_attributes=False)
        self.assertEqual({(s["attribute"], s["form"]): s["key_form"] for s in sugg if s["kind"] == "hidden_form"}
                         [("channel", "ID")], True)

    def test_no_blank_rows_on_an_exported_key_points_elsewhere(self):
        sug = {"kind": "blank_key", "attribute": "exit type", "count_on": "exit code__code",
               "count_on_source": "model export", "rows_lost": ["keeps 10 of 20"]}
        d = tempfile.mkdtemp(prefix="blank-")
        self.addCleanup(shutil.rmtree, d, True)

        def answer(sql):
            self.assertIn('"exit code__code" IS NULL', sql)
            return [["n"], ["0"]]
        (out,) = am.build_lookups(_SqlRunner(answer), "P", self.MAP_SCOPE, [sug], d)
        self.assertIn("a key value with no row in the table", out["blank_key_note"])
        self.assertNotIn("with a model export", out["blank_key_note"])


class LookupFormatTests(unittest.TestCase):
    """Templates are written in the table's own load format (the export's import settings), sources stay out of
    the load file, and check-lookup confirms a filled file is load-ready."""

    EXPORT = {"model": [{
        "tables": [{"information": {"name": "facts.csv"}},
                   {"information": {"name": "status_map.csv"}, "physicalTable": {"columns": [
                       {"columnName": "STATUS"}, {"columnName": "BUCKET"}]}}],
        "factMetrics": [{"expressions": [{"tables": [{"name": "facts.csv"}]}]}],
        "attributes": [
            {"information": {"name": "Status Code"}, "attributeLookupTable": {"name": "status_map.csv"},
             "keyForm": {"id": "K"}, "forms": [{"id": "K", "name": "ID", "category": "ID", "expressions": [
                 {"expression": {"text": "[STATUS]"}, "tables": [{"name": "status_map.csv"}, {"name": "facts.csv"}]}]}]},
            {"information": {"name": "Status Bucket"}, "attributeLookupTable": {"name": "status_map.csv"},
             "keyForm": {"id": "K"}, "forms": [{"id": "K", "name": "ID", "category": "ID", "expressions": [
                 {"expression": {"text": "[BUCKET]"}, "tables": [{"name": "status_map.csv"}]}]}]}],
        "pipeline": {"rootTable": {"children": [
            {"name": "status_map", "importSource": {"dataOptions": {"separator": ";", "quote": "", "header": True,
                                                                     "encoding": "UTF-8"}}},
            {"name": "facts", "importSource": {"dataOptions": {"separator": ",", "quote": "", "header": "true",
                                                                "encoding": "UTF-8"}}}]}}}]}
    SCOPE = {"model": "Store Sales", "anchor": {"metric": "net sales"}, "attributes": [
        {"name": "status code", "type": "varchar", "profile": {"distinct_values": 3, "values": ["00", "01", "XX"]}},
        {"name": "status bucket", "type": "varchar", "profile": {"distinct_values": 2, "values": ["0", "1"]}}],
        "metrics": []}

    def _build(self, rows):
        d = tempfile.mkdtemp(prefix="fmt-")
        self.addCleanup(shutil.rmtree, d, True)
        sugg = [s for s in am.lookup_suggestions(self.SCOPE, export=self.EXPORT, time_attributes=False)
                if s["kind"] == "extend_lookup"]

        def answer(sql):
            if "IS NULL OR CAST" in sql:
                return [["n"], ["0"]]
            return [["c0", "c1"]] + rows
        (built,) = am.build_lookups(_SqlRunner(answer), "P", self.SCOPE, sugg, d, export=self.EXPORT)
        return built, d

    def test_load_formats_from_the_export(self):
        f = am.load_formats(self.EXPORT)
        self.assertEqual((f["status_map"]["separator"], f["status_map"]["quote"], f["status_map"]["header"]),
                         (";", "", True))
        self.assertTrue(f["facts"]["header"])                                  # "true" read as true
        (x,) = [s for s in am.lookup_suggestions(self.SCOPE, export=self.EXPORT, time_attributes=False)
                if s["kind"] == "extend_lookup"]
        self.assertEqual(x["load_format"]["separator"], ";")
        self.assertNotIn("source", x["columns"])
        self.assertEqual(x["key_column"], "STATUS")

    def test_template_in_the_tables_format_with_a_sources_file(self):
        built, _ = self._build([["00", "0"], ["01", "1"], ["XX", ""]])
        with open(built["csv"], encoding="utf-8", newline="") as f:
            text = f.read()
        self.assertEqual(text.splitlines()[0], "STATUS;BUCKET;status code description")
        self.assertIn("XX;;", text)                                         # a blank stays blank
        self.assertNotIn('"', text)                                          # nothing quoted
        with open(built["sources_csv"], encoding="utf-8", newline="") as f:
            self.assertEqual(next(csv.reader(f)), ["STATUS", "status code description source"])
        self.assertEqual(built["template"]["fixed_columns"], ["STATUS", "BUCKET"])
        self.assertNotIn("format_problems", built)

    def test_a_value_holding_the_separator_is_reported_not_quoted(self):
        built, _ = self._build([["00", "0;1"], ["01", "1"]])
        self.assertTrue(any("separator" in p for p in built["format_problems"]))

    def _filled(self, built, d, lines):
        path = os.path.join(d, "filled.csv")
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write("\n".join(lines) + "\n")
        return path

    def test_check_passes_a_load_ready_file(self):
        built, d = self._build([["00", "0"], ["01", "1"], ["XX", ""]])
        path = self._filled(built, d, ["STATUS;BUCKET;status code description",
                                       "00;0;Current", "01;1;30 to 59 days late", "XX;;Unknown"])
        res = am.check_lookup_csv(built, path)
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(res["descriptions_filled"], 3)
        self.assertIn("sources file names no source", " ".join(res["warnings"]))

    def test_check_catches_shifted_columns_sources_and_changed_rows(self):
        built, d = self._build([["00", "0"], ["01", "1"], ["XX", ""]])
        path = self._filled(built, d, ["STATUS;BUCKET;status code description;source",
                                       "00;0;Current; per glossary;glossary", "01;2;Late \"30\";glossary",
                                       "XX;0;Unknown;glossary"])
        res = am.check_lookup_csv(built, path)
        text = " | ".join(res["problems"])
        self.assertFalse(res["ok"])
        self.assertIn("sources go in the sources file", text)
        self.assertIn("don't split into 3 fields", text)
        self.assertIn("double quote", text)
        self.assertIn("change the table's existing columns", text)
        self.assertIn("blank value(s) filled", text)

    def test_check_against_the_reloaded_table(self):
        built, d = self._build([["00", "0"], ["01", "1"]])
        path = self._filled(built, d, ["STATUS;BUCKET;status code description", "00;0;Current", "01;1;Late"])
        ok = am.check_lookup_csv(built, path, live_rows=[["00", "0"], ["01", "1"]])
        self.assertTrue(ok["ok"], ok["problems"])
        bad = am.check_lookup_csv(built, path, live_rows=[["00", "0"], ["01", ""]])
        self.assertIn("doesn't match the file", " ".join(bad["problems"]))

    def test_check_lookup_command_records_the_result(self):
        built, d = self._build([["00", "0"]])
        lk = os.path.join(d, "lookups.json")
        with open(lk, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "model": "Store Sales", "lookups": [built]}, f)
        path = self._filled(built, d, ["STATUS;BUCKET;status code description", "00;0;Current, on time"])
        with contextlib.redirect_stdout(io.StringIO()):
            code = am.main(["check-lookup", "--lookups", lk, "--id", built["id"], "--csv", path, "--record"])
        self.assertEqual(code, 0)            # a comma is fine in a table that splits on ';'
        with open(lk, encoding="utf-8") as f:
            self.assertTrue(json.load(f)["lookups"][0]["check"]["ok"])

    def test_new_tables_use_the_models_usual_format(self):
        fmt = am._model_load_format(self.EXPORT)
        self.assertEqual(fmt["quote"], "")
        self.assertTrue(am._model_load_format(None)["assumed"])


class LookupPlanCheckTests(unittest.TestCase):
    def test_source_column_and_failed_check_are_plan_problems(self):
        d = tempfile.mkdtemp(prefix="plan-src-")
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "t.csv")
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write("CODE;LABEL;source\n01;x;y\n")
        fmt = {"separator": ";", "quote": "", "header": True, "encoding": "UTF-8"}
        out = am.action_commands({"kind": "add_lookup_table", "args": {
            "csv": path, "key_attribute": "code", "load_format": fmt,
            "check": {"ok": False, "problems": ["line 3 has 4 fields"]}}}, "MID")
        text = " | ".join(out["problems"])
        self.assertIn("source column", text)
        self.assertIn("check-lookup failed", text)
        self.assertIn("separator ';', no quote character", out["manual"])


class ReferenceChangeTests(unittest.TestCase):
    """Names saved SQL uses that the model no longer has, and their rewrite."""

    OLD = {"model": "M", "attributes": [{"name": "status code"}, {"name": "region"}, {"name": "channel__id"},
                                        {"name": "channel__desc"}, {"name": "open date"}, {"name": "promo"}],
           "metrics": [{"name": "net sales"}, {"name": "units"}]}
    NEW = {"model": "M", "attributes": [{"name": "status code__id"}, {"name": "status code__desc"},
                                        {"name": "region"}, {"name": "channel"},
                                        {"name": "open date (month)__id"}, {"name": "promo__a"}, {"name": "promo__b"}],
           "metrics": [{"name": "net sales"}, {"name": "unit count"}]}

    def test_replacements(self):
        ch = {c["name"]: c for c in am.reference_changes(self.OLD, self.NEW)}
        self.assertEqual(ch["status code"]["replacement"], "status code__id")      # a form was added
        self.assertEqual(ch["channel__id"]["replacement"], "channel")               # forms dropped
        self.assertIsNone(ch["open date"]["replacement"])                           # time intervals don't count
        self.assertEqual(ch["promo"]["candidates"], ["promo__a", "promo__b"])       # ambiguous
        self.assertIsNone(ch["promo"]["replacement"])
        self.assertIsNone(ch["units"]["replacement"])
        self.assertNotIn("region", ch)

    def test_renamed_object_found_by_id(self):
        old_x = {"model": [{"factMetrics": [{"information": {"objectId": "M1", "name": "Units"}}]}]}
        new_x = {"model": [{"factMetrics": [{"information": {"objectId": "M1", "name": "Unit Count"}}]}]}
        ch = {c["name"]: c for c in am.reference_changes(self.OLD, self.NEW, export=new_x, old_export=old_x)}
        self.assertEqual(ch["units"]["replacement"], "unit count")

    def test_replace_identifier_leaves_literals_and_counts_bare_mentions(self):
        sql, done, left = am.replace_identifier(
            """SELECT "Status Code", COUNT("net sales") FROM t WHERE x = '"status code"' AND status_x = 1""",
            "status code", "status code__id")
        self.assertIn('"status code__id"', sql)
        self.assertIn("'\"status code\"'", sql)          # inside a literal: untouched
        self.assertEqual((done, left), (1, 0))
        _, _, left = am.replace_identifier("SELECT promo, units FROM t", "units", "unit count")
        self.assertEqual(left, 1)

    def test_rewrite_saved_sql_with_history_and_findings(self):
        bank = {"model": "M", "questions": [
            {"id": "Q1", "sql": 'SELECT "status code", COUNT("net sales") AS n FROM [[MODEL]] GROUP BY 1',
             "best_answer": {"sql": 'SELECT "status code", SUM(CASE WHEN "net sales" IS NULL THEN 0 ELSE 1 END) '
                                    'FROM [[MODEL]] GROUP BY 1', "reason_kind": "initial"},
             "expected": {"source": "same_model_sql", "sql": 'SELECT "status code" FROM [[MODEL]]'}},
            {"id": "Q2", "sql": 'SELECT "promo", SUM("units") FROM [[MODEL]] GROUP BY 1'}]}
        changes = am.reference_changes(self.OLD, self.NEW)
        res = am.apply_reference_changes(bank, changes, round_no=3, apply=True)
        q1, q2 = bank["questions"]
        self.assertIn('"status code__id"', q1["sql"])
        self.assertEqual(q1["sql_history"][-1]["reason_kind"], "model_change")
        self.assertEqual(q1["best_answer"]["reason_kind"], "model_change")
        self.assertIn('"status code__id"', q1["best_answer"]["sql"])
        self.assertIn('"status code__id"', q1["expected"]["sql"])
        self.assertEqual(q2["sql"], 'SELECT "promo", SUM("units") FROM [[MODEL]] GROUP BY 1')   # unresolved: kept
        self.assertEqual({u["name"] for u in res["unresolved"]}, {"promo", "units"})
        fs = am.reference_findings(changes, res, 3)
        self.assertEqual({f["id"] for f in fs}, {"N-promo", "N-units"})
        self.assertTrue(all(f["needs_answer"] and f["status"] == "proposed" and f["introduced_in_round"] == 3
                            for f in fs))

    def test_references_command(self):
        d = tempfile.mkdtemp(prefix="refs-")
        self.addCleanup(shutil.rmtree, d, True)
        paths = {}
        for name, obj in (("old", self.OLD), ("new", self.NEW), ("bank", {"model": "M", "questions": [
                {"id": "Q1", "sql": 'SELECT "status code" FROM [[MODEL]]'}]})):
            paths[name] = os.path.join(d, name + ".json")
            with open(paths[name], "w", encoding="utf-8") as f:
                json.dump(obj, f)
        out = os.path.join(d, "references.json")
        with contextlib.redirect_stdout(io.StringIO()):
            am.main(["references", "--old-scope", paths["old"], "--scope", paths["new"], "--bank", paths["bank"],
                     "--apply", "--round", "2", "--out", out])
        with open(paths["bank"], encoding="utf-8") as f:
            self.assertIn("status code__id", json.load(f)["questions"][0]["sql"])


class _ErrRunner(_SqlRunner):
    def call(self, tool, args):
        self.calls.append((tool, args))
        rows = self.answer(args["query"])
        if isinstance(rows, str):
            return _text(rows, error=True)
        return _table_reply(rows)


class JoinPathTests(unittest.TestCase):
    SCOPE = {"model": "Store Sales", "anchor": {"metric": "net sales", "counts": {"net sales": 100}},
             "attributes": [{"name": "region", "type": "varchar", "profile": {"distinct_values": 3}},
                            {"name": "store", "type": "varchar", "profile": {"distinct_values": 40}},
                            {"name": "flag", "type": "varchar", "profile": {"distinct_values": 2}},
                            {"name": "one", "type": "varchar", "profile": {"distinct_values": 1}},
                            {"name": "status__id", "type": "varchar"}, {"name": "status__desc", "type": "varchar"},
                            {"name": "open date", "type": "date"},
                            {"name": "open date (month)__id", "type": "bigint"}],
             "metrics": [{"name": "net sales", "type": "double", "formula": "sum(t.csv.{NET SALES})"},
                         {"name": "latest sales", "type": "double", "formula": "Sum({Net Sales}) {~+}"},
                         {"name": "regional sales", "type": "double", "formula": "Sum({net sales}) where ({region} = 'a')"},
                         {"name": "store age", "type": "double", "formula": "Avg(t.csv.{Store Age})"}]}

    def _answer(self, sql):
        if sql.startswith('SELECT COUNT("net sales") AS n FROM "store sales"') and "WHERE" not in sql:
            return [["n"], ["100"]]
        if 'GROUP BY "region"' in sql:
            return [["v", "n"], ["a", "100"], ["b", "100"]]
        if 'GROUP BY "store"' in sql:
            return [["v", "n"], ["s1", "1"], ["s2", "2"]]
        if 'GROUP BY "flag"' in sql:
            return "Query failed: no join path between the tables"
        if 'GROUP BY "one"' in sql:
            return [["v", "n"], ["", "97"], ["Y", "3"]]            # a blank and one value: two groups
        if 'GROUP BY "status__id"' in sql:
            return [["v", "n"], ["01", "4"], ["02", "96"]]
        if 'GROUP BY "open date"' in sql:
            return [["v", "n"], ["2026-01-05", "1"], ["2026-01-08", "1"]]
        if "WHERE \"store\" = 's1'" in sql:
            return [["n"], ["1"]]
        if "WHERE \"one\" = 'Y'" in sql:
            return [["n"], ["100"]]                                  # the filter is ignored
        if "WHERE \"status__id\" = '01'" in sql:
            return [["n"], ["4"]]
        if "WHERE \"open date\" = DATE '2026-01-05'" in sql:
            return [["n"], ["1"]]
        raise AssertionError(sql)

    def test_verdicts_filters_and_whole_name_matches(self):
        runner = _ErrRunner(self._answer)
        out = am.check_join_paths(runner, "P", self.SCOPE, round_no=2)
        # one fact table; "latest sales" is built on a metric, so it is not a fact
        self.assertEqual([f["anchor"] for f in out["facts"]], ["net sales"])
        v = {n: r["net sales"]["verdict"] for n, r in out["attributes"].items()}
        self.assertEqual(v, {"region": "cross_join", "store": "joined", "flag": "join_error", "one": "joined",
                             "status__id": "joined", "open date": "joined"})        # key form; no intervals
        self.assertEqual(out["attributes"]["one"]["net sales"]["filter"]["verdict"], "ignored")
        self.assertEqual(out["attributes"]["store"]["net sales"]["filter"]["verdict"], "ok")
        self.assertEqual(set(out["unjoined"]), {"region", "flag"})
        self.assertEqual(set(out["filter_problems"]), {"one"})
        self.assertEqual(out["metrics_filtering_flagged"], [{"metric": "regional sales", "attribute": "region"}])
        ids = {f["id"]: f for f in out["findings"]}
        self.assertEqual(set(ids), {"J-region", "J-flag", "J-filter-one"})
        self.assertIn("filter is ignored", ids["J-filter-one"]["evidence"]["observed"])

    def test_whole_names_only(self):
        scope = {**self.SCOPE, "metrics": [
            {"name": "net sales", "type": "double", "formula": "sum(t.csv.{NET SALES})"},
            {"name": "loans", "type": "double", "formula": "Count(Loan)"},
            {"name": "age", "type": "double", "formula": "Avg({Loan Age})"}],
            "attributes": [{"name": "loan", "type": "varchar"}]}
        runner = _ErrRunner(lambda sql: [["n"], ["100"]] if "GROUP BY" not in sql else
                            [["v", "n"], ["L1", "100"], ["L2", "100"]])
        out = am.check_join_paths(runner, "P", scope)
        self.assertEqual(out["metrics_filtering_flagged"], [{"metric": "loans", "attribute": "loan"}])

    EXPORT = {"model": [{
        "factMetrics": [{"information": {"objectId": "FM", "name": "Net Sales"},
                         "fact": {"expressions": [{"tables": [{"name": "facts.csv"}]}]}}],
        "hierarchy": {"relationships": [{"parent": {"objectId": "A2"}, "child": {"objectId": "A1"},
                                         "relationshipType": "one_to_many"}]},
        "attributes": [
            {"information": {"objectId": "A1", "name": "Store"}, "attributeLookupTable": {"name": "stores.csv"},
             "forms": [{"expressions": [{"tables": [{"name": "stores.csv"}, {"name": "facts.csv"}]}]}]},
            {"information": {"objectId": "A2", "name": "Region"}, "attributeLookupTable": {"name": "stores.csv"},
             "forms": [{"expressions": [{"tables": [{"name": "stores.csv"}]}]}]},
            {"information": {"objectId": "A3", "name": "Promo"}, "attributeLookupTable": {"name": "promos.csv"},
             "forms": [{"expressions": [{"tables": [{"name": "promos.csv"}]}]}]}]}]}

    def test_export_graph_fact_under_fact_and_hierarchy_relationships(self):
        g = am.export_join_graph(self.EXPORT)
        self.assertEqual(g["fact_tables"], ["facts.csv"])
        self.assertEqual([u["attribute"] for u in g["unreachable"]], ["promo"])
        self.assertEqual(g["relationships"], [{"parent": "region", "child": "store", "type": "one_to_many"}])
        prev = {"relationships": [{"parent": "region", "child": "store"}, {"parent": "country", "child": "region"}]}
        out = am.check_join_paths(None, "P", self.SCOPE, export=self.EXPORT, previous=prev, round_no=3)
        self.assertEqual(out["relationship_changes"]["lost"], [{"parent": "country", "child": "region"}])
        self.assertIn("L-country-region", {f["id"] for f in out["findings"]})
        self.assertIn("J-promo", {f["id"] for f in out["findings"]})
        empty = am.check_join_paths(None, "P", self.SCOPE, export=self.EXPORT, previous={"relationships": []})
        self.assertIn("not compared", empty["relationship_changes"]["note"])

    def test_attributes_mapped_to_the_fact_are_skipped_the_rest_each_tested(self):
        scope = {"model": "Store Sales", "anchor": {"metric": "net sales"},
                 "metrics": [{"name": "net sales", "type": "double", "formula": "sum(facts.csv.{NET SALES})"}],
                 "attributes": [{"name": "store", "type": "varchar"}, {"name": "region", "type": "varchar"}]}
        runner = _ErrRunner(lambda sql: [["n"], ["100"]] if "GROUP BY" not in sql and "WHERE" not in sql else
                            [["n"], ["30"]] if "WHERE" in sql else [["v", "n"], ["r1", "30"], ["r2", "70"]])
        out = am.check_join_paths(runner, "P", scope, export=self.EXPORT)
        self.assertEqual(out["mapped_to_fact"], {"net sales": ["store"]})
        self.assertEqual(out["attributes"]["store"]["net sales"]["filter"]["verdict"], "ok")   # still tested
        self.assertEqual(out["attributes"]["region"]["net sales"]["verdict"], "joined")
        self.assertEqual(out["attributes"]["region"]["net sales"]["filter"]["verdict"], "ok")

    def test_ignored_filter_names_the_missing_relationship(self):
        scope = {"model": "Store Sales", "anchor": {"metric": "net sales"},
                 "metrics": [{"name": "net sales", "type": "double", "formula": "sum(facts.csv.{NET SALES})"}],
                 "attributes": [{"name": "promo", "type": "varchar"}, {"name": "region", "type": "varchar"}]}

        def answer(sql):
            if "GROUP BY" in sql:
                return [["v", "n"], ["x", "30"], ["y", "70"]]
            return [["n"], ["100"]]          # the total, and every filter: ignored
        out = am.check_join_paths(_ErrRunner(answer), "P", scope, export=self.EXPORT)
        f = {x["id"]: x for x in out["findings"]}
        self.assertIn("declares no relationship", f["J-filter-promo"]["evidence"]["observed"])
        self.assertIn("region -> store", f["J-filter-region"]["evidence"]["observed"])

    REL_EXPORT = {"model": [{
        "factMetrics": [{"fact": {"expressions": [{"tables": [{"name": "facts.csv"}]}]}}],
        "hierarchy": {"relationships": []},
        "attributes": [
            {"information": {"objectId": "S", "name": "Status Code"}, "attributeLookupTable": {"name": "status_map.csv"},
             "forms": [{"expressions": [{"tables": [{"name": "status_map.csv"}, {"name": "facts.csv"}]}]}]},
            {"information": {"objectId": "B", "name": "Status Bucket"}, "attributeLookupTable": {"name": "status_map.csv"},
             "forms": [{"expressions": [{"tables": [{"name": "status_map.csv"}]}]}]}]}]}

    def test_ignored_filter_proposes_the_relationship_and_a_check(self):
        scope = {"model": "Store Sales", "anchor": {"metric": "net sales"},
                 "metrics": [{"name": "net sales", "type": "double", "formula": "sum(facts.csv.{NET SALES})"}],
                 "attributes": [{"name": "status bucket", "type": "integer"}, {"name": "status code", "type": "varchar"}]}

        def answer(sql):
            if 'GROUP BY "status bucket"' in sql:
                return [["v", "n"], ["5", "822"], ["4", "900"]]
            if 'GROUP BY' in sql:
                return [["v", "n"], ["01", "3"], ["02", "5"]]
            if '"status code" =' in sql:
                return [["n"], ["3"]]
            return [["n"], ["15,878,654"]]
        out = am.check_join_paths(_ErrRunner(answer), "P", scope, export=self.REL_EXPORT, round_no=1)
        (f,) = out["findings"]
        self.assertEqual(f["id"], "J-filter-status_bucket")
        self.assertIn("status bucket = 5 counts", f["evidence"]["observed"])          # the literal sent, unquoted
        self.assertEqual(f["actions"], [{"kind": "wire_relationships", "args": {"relationships": [
            {"parent_attribute": "Status Bucket", "child_attribute": "Status Code", "type": "one_to_many",
             "relationship_table": "status_map.csv"}]}}])
        self.assertEqual(f["checks"], [{"command": "join-paths", "attribute": "status bucket", "expect": "filter ok"}])
        self.assertEqual(am.check_findings({"findings": [f]})["warnings"], [])        # a check stands in for tests
        doc = {"findings": [{"id": f["id"], "status": "proposed", "actions": [], "source": "join-paths"}]}
        am.merge_findings(doc, [f])
        self.assertEqual(doc["findings"][0]["actions"], f["actions"])               # unanswered: takes the proposal
        self.assertEqual(am.resolved_findings(doc, []), {"resolved": [f["id"]], "still_present": []})
        plan = am.build_plan({"findings": [{**f, "status": "approved"}]}, model_id="MID")
        self.assertEqual(plan["problems"], [])
        self.assertEqual(plan["phases"][0]["phase"], 3)

    def test_numeric_filter_values_lose_thousands_separators(self):
        self.assertEqual(am._typed_literal("201,812", "bigint"), "201812")
        self.assertEqual(am._typed_literal("5", "integer"), "5")
        self.assertEqual(am._typed_literal("1,234.5", "double"), "1234.5")
        self.assertEqual(am._typed_literal("201,812", "varchar"), "'201,812'")
        self.assertEqual(am._sql_literal("201,812", "bigint"), "201812")      # the probe's filters too

    def test_merge_findings_keeps_answers(self):
        doc = {"findings": [{"id": "J-region", "status": "declined", "evidence": {"observed": "old"}}]}
        new = [am.integrity_finding("J-region", "t", "relationship", "new", "r", round_no=4),
               am.integrity_finding("J-flag", "t", "relationship", "x", "r", round_no=4)]
        res = am.merge_findings(doc, new)
        self.assertEqual(res["added"], ["J-flag"])
        self.assertEqual(doc["findings"][0]["status"], "declined")
        self.assertEqual(doc["findings"][0]["evidence"]["observed"], "new")


class PhaseGateTests(unittest.TestCase):
    def _doc(self, f2_status="proposed"):
        act = {"kind": "patch_model_object", "args": {"kind": "metric", "object_id": "X", "json": {}}}
        return {"model": "M", "findings": [
            {"id": "F1", "category": "metric_defect", "impact": "high", "effort": "small", "status": "approved",
             "actions": [act], "tests": ["Q1"]},
            {"id": "F2", "category": "metric_defect", "impact": "low", "effort": "small", "status": f2_status,
             "actions": [act], "tests": ["Q2"]},
            {"id": "J-x", "category": "relationship", "impact": "high", "effort": "small", "status": "proposed",
             "actions": [], "needs_answer": True}]}

    def test_unanswered_findings_block_their_phase(self):
        plan = am.build_plan(self._doc(), model_id="MID")
        ph4 = next(p for p in plan["phases"] if p["phase"] == 4)
        self.assertEqual(ph4["blocked_by"], ["F2"])
        self.assertEqual(plan["unanswered"], {"3": ["J-x"], "4": ["F2"]})
        answered = am.build_plan(self._doc("declined"), model_id="MID")
        self.assertNotIn("blocked_by", next(p for p in answered["phases"] if p["phase"] == 4))
        self.assertNotEqual(plan["plan_id"], answered["plan_id"])     # a newer plan has another id


class UnbracedFormulaTests(unittest.TestCase):
    """A column with no space appears without braces in the formula (seen live)."""

    def test_raw_metric_and_table(self):
        metrics = [{"name": "order quantity", "type": "bigint", "formula": "sum(retail_orders.xlsx.Quantity)"},
                   {"name": "latest upb", "type": "double", "formula": "Sum({Current Actual UPB Amount})"},
                   {"name": "total", "type": "double", "formula": "sum(retail_orders.xlsx.{Order Total})"},
                   {"name": "ratio", "type": "double", "formula": "total / order quantity"}]
        self.assertEqual(am.raw_fact_metrics(metrics), ["order quantity", "total"])
        self.assertEqual(am._metric_table(metrics[0]), "retail_orders.xlsx")
        self.assertEqual(am._metric_table(metrics[2]), "retail_orders.xlsx")


class SqlLintTests(unittest.TestCase):
    CASES = {
        'SELECT CAST("open date" / 100 AS INTEGER) AS y, SUM("x") FROM t GROUP BY 1 ORDER BY 1': 1,
        'SELECT "region", SUM("x") AS s FROM t GROUP BY 1 ORDER BY 2': 0,
        'SELECT "region", SUM("x") FROM t GROUP BY "region" HAVING SUM("x") > 1': 0,
        'SELECT v, SUM(n) FROM (SELECT "a" AS v, COUNT("m") AS n FROM t GROUP BY "a") GROUP BY v': 0,
        "SELECT CASE WHEN v > 5 THEN 'hi' ELSE 'lo' END AS b, SUM(n) FROM "
        '(SELECT "a" AS v, COUNT("m") AS n FROM t GROUP BY "a") GROUP BY 1': 1,          # outer query too
        'SELECT "a" / 100 AS yr, SUM("x") FROM t GROUP BY yr': 1,
        'SELECT t."a", "b", SUM("x") FROM t GROUP BY 1, 2': 0,
        'SELECT SUM("x") FROM t WHERE "a" = \'GROUP BY x\'': 0,
        'SELECT "open date (year)__id" AS y, SUM("x") FROM t GROUP BY 1': 0,
    }

    def test_grouped_expressions(self):
        for sql, n in self.CASES.items():
            self.assertEqual(len(am.grouped_expressions(sql)), n, sql)

    def test_refused_when_saved_and_warned_in_check(self):
        bad = 'SELECT CAST("open date" / 100 AS INTEGER) AS y, SUM("x") FROM {{MODEL}} GROUP BY 1'
        bank = {"model": "M", "questions": [{"id": "Q1", "level": 1, "question": "q", "sql": 'SELECT 1 FROM {{MODEL}}'}]}
        res = am.set_question_sql(bank, "Q1", sql=bad, reason_kind="sql_error", reason="x")
        self.assertFalse(res["ok"])
        self.assertIn("doesn't regroup", " ".join(res["problems"]))
        bank["questions"][0]["sql"] = bad
        self.assertTrue(any("doesn't regroup" in w for w in am.check_bank(bank)["warnings"]))

    def test_weighted_average_parts_are_not_summed_rates(self):
        metrics = [{"name": "wa rate numerator", "formula": "Sum(Max({Rate}) * Max({Balance}))"},
                   {"name": "wa rate denominator", "formula": "Sum((Max({Balance}) * Max({Rate})) / Max({Rate}))"},
                   {"name": "interest rate", "formula": "Sum(t.csv.{Rate})"}]
        self.assertEqual([m["name"] for m in metrics if not am._weighted_part(m)], ["interest rate"])

    def test_timeout_has_its_own_verdict(self):
        bank = {"model": "M", "questions": [{"id": "Q1", "level": 1, "question": "q", "sql": "SELECT 1",
                                             "expected": {"source": "none"}}]}
        results = {"results": [{"id": "Q1", "ok": False, "error": 'tool "query" timed out after 230s'}]}
        g = am.grade_bank(bank, results)
        self.assertEqual(g["questions"][0]["verdict"], "timeout")


class OpenItemTests(unittest.TestCase):
    """Show hidden forms, guard hand reloads, sources of each output, questions out of approved findings."""

    def test_show_forms_action_and_hidden_form_proposal(self):
        c = am.action_commands({"kind": "show_forms", "args": {"attribute": "Channel", "forms": ["ID"]}}, "MID")
        self.assertEqual(c["apply"][-6:], ["--model-id", "MID", "--attribute", "Channel", "--forms", "ID"])
        self.assertEqual(c["dry_run"][-1], "--dry-run")
        sugg = am.lookup_suggestions(FormListingTests.MAP_SCOPE, export=FormListingTests.MAP_EXPORT,
                                     time_attributes=False)
        (h,) = [s for s in sugg if s["kind"] == "hidden_form" and s["attribute"] == "channel"]
        self.assertEqual(h["action"], {"kind": "show_forms", "args": {"attribute": "channel", "forms": ["ID"]}})

    def _export(self, rels):
        return {"model": [{"hierarchy": {"relationships": [
            {"parent": {"objectId": p}, "child": {"objectId": c}, "relationshipType": "one_to_many"} for p, c in rels]},
            "factMetrics": [{"fact": {"expressions": [{"tables": [{"name": "facts.csv"}]}]}}],
            "attributes": [
                {"information": {"objectId": "S", "name": "Status"}, "attributeLookupTable": {"name": "map.csv"},
                 "forms": [{"expressions": [{"tables": [{"name": "map.csv"}, {"name": "facts.csv"}]}]}]},
                {"information": {"objectId": "B", "name": "Bucket"}, "attributeLookupTable": {"name": "map.csv"},
                 "forms": [{"expressions": [{"tables": [{"name": "map.csv"}]}]}]}]}]}

    def test_relationship_diff_and_its_exit_code(self):
        before, after = self._export([("B", "S")]), self._export([])
        res = am.relationship_diff(before, after)
        self.assertEqual([(r["parent"], r["child"]) for r in res["lost"]], [("bucket", "status")])
        self.assertEqual(res["restore_actions"][0]["args"]["relationships"][0]["relationship_table"], "map.csv")
        d = tempfile.mkdtemp(prefix="reldiff-")
        self.addCleanup(shutil.rmtree, d, True)
        paths = []
        for i, x in enumerate((before, after, before)):
            paths.append(os.path.join(d, f"{i}.json"))
            with open(paths[-1], "w", encoding="utf-8") as f:
                json.dump(x, f)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(am.main(["relationship-diff", "--before", paths[0], "--after", paths[1]]), 1)
            self.assertEqual(am.main(["relationship-diff", "--before", paths[0], "--after", paths[2]]), 0)

    def test_hand_reload_is_guarded_in_the_plan(self):
        d = tempfile.mkdtemp(prefix="guard-")
        self.addCleanup(shutil.rmtree, d, True)
        csvp = os.path.join(d, "map.csv")
        with open(csvp, "w", encoding="utf-8") as f:
            f.write("STATUS,BUCKET,desc\n01,1,x\n")
        doc = {"model": "M", "findings": [{"id": "F1", "category": "lookup", "status": "approved", "tests": ["Q1"],
                                           "actions": [{"kind": "add_lookup_table", "args": {
                                               "csv": csvp, "key_attribute": "status", "extends_table": "map.csv",
                                               "keep_columns": ["STATUS", "BUCKET"]}}]}]}
        plan = am.build_plan(doc, model_id="MID")
        (step,) = [s for p in plan["phases"] for s in p["steps"] if s["kind"] == "add_lookup_table"]
        self.assertIn("export-model", step["guard_before"][0]["argv"])
        self.assertIn("relationship-diff", step["guard_after"][1]["argv"])
        self.assertTrue(all("ignore/exports/" in " ".join(g["argv"]) for g in step["guard_before"] + step["guard_after"]))
        self.assertTrue(any("manual step" in w for w in plan["warnings"]))

    def test_questions_in_manual_steps_are_flagged(self):
        f = {"id": "F1", "category": "metric_defect", "status": "proposed", "tests": ["Q1"], "actions": [
            {"kind": "manual", "steps": ["Design SMM/CPR with the data owner", "Edit the model description"]}]}
        w = am.check_findings({"findings": [f]})["warnings"]
        self.assertEqual(len([x for x in w if "question for someone" in x]), 1)

    def test_source_info(self):
        d = tempfile.mkdtemp(prefix="src-")
        self.addCleanup(shutil.rmtree, d, True)
        p = os.path.join(d, "export.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write("{}")
        info = am.source_info(scope=None, export=p)
        self.assertEqual(info["export"]["file"], "export.json")
        self.assertEqual(len(info["export"]["sha"]), 12)
        self.assertNotIn("scope", info)
