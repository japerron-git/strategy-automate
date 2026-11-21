"""Tests for the 20 Nov batch: renames through the phases with replace, output aliases in references, the filter
on compound metrics, all-blank rows in grading, a no-change note, distinct-count filter checks, time attributes
named like forms, and + / - over blank operands. Generic retail names; nothing touches the network.
"""
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import _hermetic  # noqa: E402,F401  (scrub MSTR_*/proxy env, private secret store)

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "skills", "strategy-model-assessment", "scripts"))
sys.path.insert(0, os.path.join(ROOT, "skills", "build-mosaic-model", "scripts"))

import assess_model as am  # noqa: E402
import build_mosaic as bm  # noqa: E402


def _f(fid, category, actions, **kw):
    return {"id": fid, "title": f"title {fid}", "category": category, "impact": "high", "effort": "small",
            "status": "approved", "tests": [], "actions": actions, **kw}


class ReplaceAndRenameTests(unittest.TestCase):
    def test_a_replace_needs_no_free_name_and_its_rename_stays_after_it(self):
        rep = {"kind": "create_compound_metric", "args": {"name": "unit gap", "formula": "[a] - [b]", "replace": True}}
        ren = {"kind": "rename_describe", "args": {"items": [
            {"object_type": "metric", "current_name": "unit gap", "new_name": "unit gap (measured)"}]}}
        plan = am.build_plan({"findings": [_f("F1", "missing_metric", [rep]), _f("F2", "description", [ren])]},
                             model_id="MID", bundle_dir="b")
        self.assertEqual(plan["problems"], [])
        self.assertNotIn("b/specs/rename_free_names_phase_5.json", plan["files"])
        kinds = {ph["phase"]: [s["kind"] for s in ph["steps"]] for ph in plan["phases"]}
        self.assertEqual(kinds[6], ["rename_describe"])


class OutputAliasTests(unittest.TestCase):
    def test_an_output_alias_is_neither_flagged_nor_rewritten(self):
        sql = 'SELECT "region", SUM("margin (executed)") / SUM("spend") AS margin FROM m GROUP BY 1 ORDER BY margin'
        self.assertEqual(am.replace_identifier(sql, "margin", "margin (all statuses)"), (sql, 0, 0))

    def test_a_quoted_alias_keeps_its_name_while_references_change(self):
        sql = 'SELECT "year" AS "year", SUM("sales") AS s FROM m GROUP BY "year"'
        out, done, left = am.replace_identifier(sql, "year", "legacy year")
        self.assertEqual(out, 'SELECT "legacy year" AS "year", SUM("sales") AS s FROM m GROUP BY "legacy year"')
        self.assertEqual((done, left), (2, 0))

    def test_a_bare_reference_is_still_flagged(self):
        self.assertEqual(am.replace_identifier("SELECT SUM(margin) FROM m", "margin", "x")[2], 1)

    def test_references_lists_carried_questions(self):
        bank = {"questions": [{"id": "Q1", "sql": 'SELECT SUM("margin") AS m FROM {{MODEL}}',
                               "best_answer": {"sql": 'SELECT SUM("cost") AS m FROM t'}}]}
        changes = [{"name": "margin", "kind": "metric", "replacement": "margin (all)", "candidates": ["margin (all)"],
                    "how": "renamed"}]
        usage = am.apply_reference_changes(bank, changes, round_no=2, apply=True)
        self.assertEqual([c["question"] for c in usage["carried"]], ["Q1"])


class CompoundFilterTests(unittest.TestCase):
    COMP = am.compound_metric_names([{"name": "margin rate", "formula": "{Margin} / ZeroToNull({Sales})"},
                                     {"name": "sales (closed)", "formula": "Sum({Sales})"},
                                     {"name": "gap", "formula": "1 - {margin rate}"}])

    def test_which_metrics_are_compound(self):
        self.assertEqual(set(self.COMP), {"margin rate", "gap"})

    def test_a_where_or_having_on_a_compound_is_warned(self):
        sub = 'SELECT COUNT(*) FROM (SELECT "store", SUM("margin rate") AS r FROM {{MODEL}} GROUP BY 1) t WHERE r < 0.5'
        self.assertEqual(len(am.compound_filter_lint(sub, self.COMP)), 1)
        having = 'SELECT "store", SUM("gap") AS g FROM m GROUP BY 1 HAVING SUM("gap") > 0'
        self.assertIn("HAVING on gap", am.compound_filter_lint(having, self.COMP)[0])

    def test_safe_shapes_pass(self):
        case = ('SELECT SUM(CASE WHEN r < 0.5 THEN 1 ELSE 0 END) FROM (SELECT "store", SUM("margin rate") AS r '
                'FROM m GROUP BY 1) t')
        barrier = ('SELECT COUNT(*) FROM (SELECT s, r FROM (SELECT "store" s, SUM("margin rate") AS r FROM m '
                   'GROUP BY 1) a ORDER BY s LIMIT 100000) t WHERE r < 0.5')
        other = 'SELECT "store", SUM("sales (closed)") AS s FROM m WHERE "status" = \'Closed\' GROUP BY 1'
        for sql in (case, barrier, other):
            self.assertEqual(am.compound_filter_lint(sql, self.COMP), [])

    def test_the_bank_check_uses_the_scope(self):
        shifted = {am.COMPOUNDS_KEY: self.COMP}
        lint = am._sql_lint('SELECT COUNT(*) FROM (SELECT "store", SUM("margin rate") AS r FROM m GROUP BY 1) t '
                            'WHERE r < 0.5', shifted)
        self.assertTrue(any("filters a compound metric" in w for w in lint))
        self.assertEqual(am.time_shift_sql_lint("SELECT 1", shifted), [])


class BlankRowTests(unittest.TestCase):
    def test_all_blank_rows_count_as_absent(self):
        q = {"id": "Q1", "key": ["market"], "measures": ["gap"]}
        rows = [{"market": "A", "gap": "5"}, {"market": "B", "gap": ""}]
        ref = [{"market": "A", "gap": "5"}]
        verdict, cmp, err = am._compare(q, rows, ref, 1e-6)
        self.assertEqual((verdict, err), ("pass", None))
        self.assertEqual(cmp["blank_only_rows"], {"question": 1})

    def test_a_blank_where_the_reference_has_a_value_still_fails(self):
        q = {"id": "Q1", "key": ["market"], "measures": ["gap"]}
        verdict, _, _ = am._compare(q, [{"market": "A", "gap": ""}], [{"market": "A", "gap": "5"}], 1e-6)
        self.assertEqual(verdict, "fail")


class NoChangeNoteTests(unittest.TestCase):
    def test_the_note_says_what_was_decided(self):
        f = _f("F1", "missing_metric", [], status="modified", recommendation="Needs your answer: which one",
               decision_note="keep it", rework_note="No model change, per your note: keep it")
        plan = am.build_plan({"findings": [f]}, model_id="MID")
        notes = [s.get("note") for ph in plan["phases"] for s in ph["steps"] if s["kind"] == "note"]
        self.assertEqual(notes, ["No model change, per your note: keep it"])
        self.assertEqual(am.no_change_note({"decision_note": "keep it"}), "No model change: keep it")


class CountedAttributeTests(unittest.TestCase):
    def test_counted_attribute(self):
        attrs = {"customer id": {}, "region (code)": {}, "region (name)": {}}
        self.assertEqual(am._counted_attribute("Count({Customer ID})", attrs), "customer id")
        self.assertEqual(am._counted_attribute("Count<Distinct=True, MetricID=[x]>([Customer ID]@[ID])", attrs),
                         "customer id")
        self.assertEqual(am._counted_attribute("Count({Region})", attrs), "")      # two forms: can't tell
        self.assertEqual(am._counted_attribute("Sum({Sales})", attrs), "")


class SmartAttributeNameTests(unittest.TestCase):
    def test_a_time_attribute_named_like_a_form_is_found_by_its_exact_name(self):
        model = {"attributes": [{"information": {"objectId": "D" * 32, "name": "ship date"},
                                 "smartAttributes": [{"information": {"objectId": "S" * 32,
                                                                      "name": "ship date (year)"}}]}]}
        writes, notes = bm.rename_describe_writes(model, "M", [
            {"object_type": "attribute", "current_name": "ship date (year)", "new_name": "",
             "description": "Year the order shipped."}])
        self.assertEqual(len(writes), 1)
        self.assertIn("S" * 32, writes[0][1])

    def test_the_plan_doesnt_call_it_a_form(self):
        rn = {"kind": "rename_describe", "args": {"items": [
            {"object_type": "attribute", "current_name": "ship date (year)", "description": "Year shipped."},
            {"object_type": "attribute", "current_name": "ship date (month)", "description": "Month shipped."}]}}
        plan = am.build_plan({"findings": [_f("F1", "description", [rn])]}, model_id="MID")
        self.assertEqual(plan["problems"], [])
        self.assertFalse(any("connector form name" in w for w in plan.get("warnings") or []))


class PlusMinusTests(unittest.TestCase):
    def test_plus_and_minus_over_metrics_are_warned(self):
        for f in ("1 - ([a] / ZeroToNull([b]))", "[a] - [c]", "[a] + [b]"):
            with self.subTest(f=f):
                self.assertTrue(any("divide last" in w for w in bm.compound_constant_warnings(f)))
        self.assertFalse(any("divide last" in w for w in bm.compound_constant_warnings("[a] / ZeroToNull([b])")))


if __name__ == "__main__":
    unittest.main()
