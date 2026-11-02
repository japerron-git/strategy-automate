"""Tests for skills/strategy-model-assessment/scripts/assess_model.py: parsing what the Mosaic MCP
tools return, scope / run / grade against a fake MCP runner, the change-plan ordering and commands,
and the file checks. Nothing touches the network.
"""
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import _hermetic  # noqa: E402,F401  (scrub MSTR_*/proxy env, private secret store)

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
        self.assertEqual(am.action_commands({"kind": "publish"}, mid)["apply"][2:], ["publish", "--model-id", mid])
        self.assertIsNone(am.action_commands({"kind": "certify"}, mid)["dry_run"])

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
        self.assertEqual(len(runner.pending), 1)


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
        self.assertEqual(sugg, {"status code": "code_lookup", "reporting period": "period_lookup"})
        self.assertEqual(am._period_parts("201,912"), ("2019", "2019-Q4", "12"))
        self.assertEqual(am._period_parts("20200301"), ("2020", "2020-Q1", "03"))


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
                "table": "sales.csv", "column": "STATUS", "change": "load as text, pad to 2"}}]),
            _finding("F3", "metric_defect", actions=[
                {"kind": "source_change", "args": {"table": "sales.csv", "column": "COSTS", "change": "load as number"}},
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
