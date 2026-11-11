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
        self.assertEqual([t for t, _ in runner.calls], ["get_semantics"])
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
        self.assertEqual(len(calls), 3)
        for c in calls:
            sql = c["arguments"]["query"]
            self.assertEqual(c["file"], am.query_id(sql) + ".txt")
            if '"region"' in sql:
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

    def test_numeric_dates_with_calendar_parents_are_not_a_defect(self):
        date = lambda n: {"name": n, "type": "bigint", "profile": {"distinct_values": 30, "min": "201901",  # noqa: E731
                                                                    "max": "202201"}}
        bare = am.maturity_signals({"attributes": [date("reporting period"), date("close date")], "metrics": []})
        self.assertEqual(bare["dates_stored_as_numbers"], ["reporting period", "close date"])
        calendar = am.maturity_signals({"attributes": [date("reporting period"), date("close date"),
                                                       {"name": "year", "type": "bigint"},
                                                       {"name": "latest period flag", "type": "varchar"},
                                                       {"name": "close date year", "type": "bigint"}], "metrics": []})
        self.assertEqual(calendar["dates_stored_as_numbers"], [])


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
        sugg = {s["attribute"]: s["kind"] for s in am.lookup_suggestions(self.SCOPE)}
        self.assertEqual(sugg, {"status code": "code_lookup", "reporting period": "period_lookup",
                                "close date": "date_lookup"})
        self.assertEqual(am._period_parts("201,912"), ("2019", "2019-Q4", "12"))
        self.assertEqual(am._period_parts("20200301"), ("2020", "2020-Q1", "03"))


    def test_date_lookup_gives_calendar_parents_as_attributes(self):
        (sug,) = [x for x in am.lookup_suggestions(self.SCOPE) if x["kind"] == "date_lookup"]
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
        self.assertNotIn("date_lookup", [x["kind"] for x in am.lookup_suggestions(named)])


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
        self.assertIn("leave out the CSV's source column", plan["phases"][0]["steps"][0]["manual"])

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
        sugg = {s["attribute"]: s for s in am.lookup_suggestions(self.scope)}
        self.assertEqual(set(sugg), {"status code", "snapshot month"})
        self.assertIn("explained only in the description", sugg["status code"]["reason"])
        self.assertEqual(sugg["snapshot month"]["kind"], "period_lookup")
        self.assertEqual((sugg["status code"]["new_forms"], sugg["status code"]["new_attributes"]),
                         (["status code description"], []))                  # descriptions are display forms
        self.assertIn("latest period flag", sugg["snapshot month"]["new_attributes"])   # filtered on

        self.scope["attributes"].append({"name": "latest period flag", "type": "varchar", "description": "",
                                         "profile": {"distinct_values": 2, "values": ["N", "Y"]}})
        self.assertNotIn("snapshot month", {s["attribute"] for s in am.lookup_suggestions(self.scope)})
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
            self.assertEqual(list(csv.reader(f)), [["status code", "status code description", "source"],
                                                   ["01", "", ""], ["02", "", ""], ["03", "", ""], ["09", "", ""]])
        with open(built["snapshot month"]["csv"], encoding="utf-8", newline="") as f:
            period = list(csv.reader(f))
        self.assertEqual(period[0], ["snapshot month", "year", "quarter", "month", "latest period flag"])
        self.assertEqual(period[1], ["2025-12-01", "2025", "2025-Q4", "12", "N"])
        self.assertEqual(period[-1], ["2026-02-01", "2026", "2026-Q1", "02", "Y"])
        self.assertIsNone(built["customer"]["csv"])

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
        self.assertEqual(needs_model - {"plan"}, set(am.LIVE_COMMANDS))   # plan only names the project

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
