"""Tests for the 23 Nov batch: the model's own compound metrics read with the constant and blank rules, and the
NTILE ordering the connector refuses. Generic retail names; nothing touches the network.
(The probe's date filter, unchecked shapes and repeated rows are in test_assess_model.ProbeTests; the summed
averages in EarlyStageSignalTests and RoleTests.)
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


def _table(rows):
    header, body = rows[0], rows[1:]
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


class _Runner:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def call(self, tool, args):
        self.calls.append(args["query"])
        return _table(self.answer(args["query"]))


class DisplayFormulaTests(unittest.TestCase):
    def test_aggregate_calls_and_metric_names_become_operands(self):
        self.assertEqual(am.compound_from_display("(Sum({Sales}) / Sum({Units})) * 100"),
                         "([operand 1] / [operand 2]) * 100")
        self.assertEqual(am.compound_from_display("{Returned Orders} / Count(Order (ID))"),
                         "[Returned Orders] / [operand 1]")
        self.assertEqual(am.compound_from_display("(Sum({Sales}) - Sum({Last Year})) / Sum({Last Year})"),
                         "([operand 1] - [operand 2]) / [operand 2]")
        self.assertEqual(am.compound_from_display("100"), "")
        self.assertEqual(am.compound_from_display("Sum({Sales}"), "")          # unbalanced: not read

    def test_the_rules_read_the_converted_formula(self):
        warn = am.compound_constant_warnings
        self.assertTrue(any("constant times a blank" in w
                            for w in warn(am.compound_from_display("(Sum({Sales}) / Sum({Units})) * 100"))))
        self.assertTrue(any("+ and - read a blank" in w
                            for w in warn(am.compound_from_display("Sum({Sales}) - Sum({Target})"))))
        self.assertEqual(warn(am.compound_from_display("(Sum({Sales}) - Sum({Last Year})) / Sum({Last Year})")), [])
        self.assertEqual(warn(am.compound_from_display("Sum({Sales}) / Sum({Units})")), [])


class ProbeOwnCompoundTests(unittest.TestCase):
    """item 77: a ratio times 100 in the model reads 0 where it has no data; probe now says so from the formula."""
    SCOPE = {"model": "Store Sales", "anchor": {"metric": "net sales"},
             "attributes": [{"name": "store id", "type": "bigint", "profile": {"distinct_values": 50}}],
             "metrics": [{"name": "net sales", "type": "double", "formula": "sum(sales.{NET_SALES})"},
                         {"name": "units", "type": "double", "formula": "sum(sales.{UNITS})"},
                         {"name": "price pct", "type": "double", "formula": "(Sum({Net Sales}) / Sum({Units})) * 100"},
                         {"name": "price", "type": "double", "formula": "Sum({Net Sales}) / Sum({Units})"}]}

    @staticmethod
    def answer(sql):
        if "COUNT(" in sql:
            return [["n"], ["100"]]
        cols = [f"m{i}" for i in range(sql.count(" AS m"))]
        return [cols, ["1"] * len(cols)]

    def test_probe_flags_the_models_own_compound_metrics(self):
        out = am.probe_objects(_Runner(self.answer), "retail", self.SCOPE, attributes=[])
        mets = {m["name"]: m for m in out["metrics"]}
        self.assertTrue(any("constant times a blank" in i for i in mets["price pct"]["issues"]))
        self.assertFalse(any("its formula" in i for i in mets["price"]["issues"]))
        self.assertFalse(any("its formula" in i for i in mets["net sales"]["issues"]))
        self.assertTrue(any(w.startswith("metric 'price pct': its formula") for w in out["warnings"]))


class NtileLintTests(unittest.TestCase):
    def test_more_than_one_order_by_column_is_warned(self):
        bad = ('SELECT q, SUM(s) FROM (SELECT NTILE(4) OVER (ORDER BY h, store_id) AS q, s FROM '
               '(SELECT "store id" AS store_id, SUM("net sales") AS s, SUM("score") AS h FROM m GROUP BY 1) d) t GROUP BY q')
        self.assertTrue(any("NTILE" in w for w in am._sql_lint(bad)))
        for good in ("SELECT NTILE(4) OVER (ORDER BY h) AS q FROM t",
                     "SELECT NTILE(4) OVER (PARTITION BY region, channel ORDER BY h DESC) AS q FROM t",
                     "SELECT ROW_NUMBER() OVER (ORDER BY h, store_id) AS rn FROM t",
                     "SELECT NTILE(4) OVER (ORDER BY COALESCE(h, 0)) AS q FROM t"):
            with self.subTest(good):
                self.assertEqual(am.ntile_lint(good), [])


class WindowShiftLintTests(unittest.TestCase):
    """item 76: verified live 2026-11-23 (the divide on two models, the filtered groups on one)."""
    GROWTH = ('SELECT "month start" AS m, SUM("net sales") AS s, SUM("net sales") / LAG(SUM("net sales")) OVER '
              '(ORDER BY "month start") - 1 AS g FROM m WHERE "sale day" BETWEEN DATE \'2025-01-01\' AND '
              'DATE \'2025-12-31\' GROUP BY 1')

    def test_dividing_by_lag_and_filtering_another_attribute_are_warned(self):
        out = am.window_shift_lint(self.GROWTH)
        self.assertEqual(len(out), 2, out)
        self.assertIn("NULLIF", out[0])
        self.assertIn("'sale day'", out[1])
        self.assertTrue(any("LAG / LEAD" in w for w in am._sql_lint(self.GROWTH)))

    def test_the_recommended_shapes_pass(self):
        good = ('SELECT month, s, s / NULLIF(p, 0) - 1 AS g FROM (SELECT "month start" AS month, SUM("net sales") AS s, '
                'LAG(SUM("net sales")) OVER (ORDER BY "month start") AS p FROM m GROUP BY 1) t '
                'WHERE month >= DATE \'2025-01-01\'')
        self.assertEqual(am.window_shift_lint(good), [])
        same = ('SELECT "month start" AS m, LAG(SUM("net sales")) OVER (ORDER BY "month start") AS p FROM m '
                'WHERE "month start" >= DATE \'2025-01-01\' GROUP BY 1')
        self.assertEqual(am.window_shift_lint(same), [])              # filters the attribute it selects
        trimmed = ('SELECT month, g FROM (SELECT "month start" AS month, SUM("net sales") / LAG(SUM("net sales")) OVER '
                   '(ORDER BY "month start") - 1 AS g FROM m WHERE "sale day" >= DATE \'2024-12-01\' GROUP BY 1) t '
                   'WHERE month >= DATE \'2025-01-01\'')
        self.assertEqual(am.window_shift_lint(trimmed), [])           # shifted inside, rows trimmed outside


if __name__ == "__main__":
    unittest.main()
