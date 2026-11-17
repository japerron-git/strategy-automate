"""Tests for add-column-fact-metric (a fact metric over a column the model already has, the reverse of
add-column-attribute). Synthetic two-table model; the body follows the fact-metric shape verified on an existing
model (a column_reference token carrying the column's objectId, end_of_text) and cmd_build's function and dimty."""
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import _hermetic  # noqa: E402,F401  (scrub MSTR_*/proxy env, private secret store)

import contextlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
import unittest.mock

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "skills", "build-mosaic-model", "scripts"))

import build_mosaic as bm  # noqa: E402
from test_build_mosaic_column_types import _Fake, _model, MP  # noqa: E402
from test_build_mosaic_conditional_metric import _Resp  # noqa: E402

INT = {"type": "integer", "precision": 4, "scale": 0}
DBL = {"type": "double", "precision": 0, "scale": 0}


def _promo_table():
    cols = [("P1", "PROMO ID", INT), ("P2", "DURATION DAYS", INT), ("P3", "PROMO PRICE", DBL)]
    return {"information": {"objectId": "T2", "name": "promotion", "subType": "logical_table"},
            "physicalTable": {"information": {"objectId": "PT2", "subType": "physical_table", "name": "promotion"},
                              "columns": [{"information": {"objectId": i, "name": n}, "columnName": n,
                                           "dataType": dict(t)} for i, n, t in cols]}}


def _two_tables():
    m = _model()            # table "sales" (STATUS CODE, PERIOD, AMOUNT); attributes Status, Region; fact Amount
    m["tables"].append(_promo_table())
    m["attributes"].append({"information": {"objectId": "A3", "name": "promotion duration"},
                            "forms": [{"category": "ID", "expressions": [
                                {"expression": {"text": "[DURATION DAYS]"}, "tables": [{"objectId": "T2"}]}]}]})
    m["metrics"] = [{"information": {"objectId": "D1", "name": "avg duration days"}}]
    return m


def _routes(model=None, readback_fn="max"):
    return {("POST", f"{MP}/export"): _Resp(200, {"model": [model or _two_tables()]}),
            ("POST", f"{MP}/factMetrics"): _Resp(201, {"information": {"objectId": "NEW"}}),
            ("GET", f"{MP}/factMetrics/NEW"): _Resp(200, {"function": readback_fn, "fact": {"expressions": [
                {"expression": {"text": "[DURATION DAYS]"}, "tables": [{"objectId": "T2"}]}]}}),
            ("POST", "/api/model/changesets"): _Resp(201, {"id": "CS1"}),
            ("POST", "/api/model/changesets/CS1/commit"): _Resp(204),
            ("DELETE", "/api/model/changesets/CS1"): _Resp(204)}


def _args(**kw):
    base = dict(model_id="M1", name="promotion duration days", column="promotion duration", function="max",
                format=None, description="Days the promotion runs.", table=None, dry_run=False)
    base.update(kw)
    return types.SimpleNamespace(**base)


def _run(fake, args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        bm.cmd_add_column_fact_metric(fake, args)
    return json.loads(out.getvalue())


def _dies(fn, *a):
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()), \
            unittest.TestCase().assertRaises(SystemExit):
        fn(*a)
    return err.getvalue()


def _posted(fake):
    return [kw["json"] for m, p, kw, _ in fake.calls if (m, p) == ("POST", f"{MP}/factMetrics")]


class TargetTests(unittest.TestCase):
    def test_attribute_column_and_form_names_resolve(self):
        m = _two_tables()
        t, col, notes = bm.column_fact_metric_target(m, "promotion duration", None)
        self.assertEqual((t["information"]["name"], col), ("promotion", "DURATION DAYS"))
        self.assertTrue(any("not on the largest table" in n for n in notes))          # a lookup's grain
        self.assertEqual(bm.column_fact_metric_target(m, "promotion duration__id", None)[1], "DURATION DAYS")
        self.assertEqual(bm.column_fact_metric_target(m, "PROMO PRICE", None)[1], "PROMO PRICE")
        t, col, notes = bm.column_fact_metric_target(m, "amount", None)              # a fact metric's column
        self.assertEqual((t["information"]["name"], col), ("sales", "AMOUNT"))
        self.assertFalse(any("largest" in n for n in notes))
        _dies(bm.column_fact_metric_target, m, "nope", None)
        _dies(bm.column_fact_metric_target, m, "promo price", "sales")                 # not on the table given

    def test_a_column_on_two_tables_prefers_the_largest(self):
        m = _two_tables()
        m["tables"][1]["physicalTable"]["columns"].append(
            {"information": {"objectId": "P9", "name": "PERIOD"}, "columnName": "PERIOD", "dataType": dict(INT)})
        for i in ("C8", "C9"):                                  # keep sales the larger table
            m["tables"][0]["physicalTable"]["columns"].append(
                {"information": {"objectId": i, "name": i}, "columnName": i, "dataType": dict(INT)})
        t, _, notes = bm.column_fact_metric_target(m, "PERIOD", None)
        self.assertEqual(t["information"]["name"], "sales")
        self.assertTrue(any("also on ['promotion']" in n for n in notes))
        self.assertEqual(bm.column_fact_metric_target(m, "PERIOD", "promotion")[0]["information"]["name"], "promotion")


class FlowTests(unittest.TestCase):
    def test_create_in_one_changeset(self):
        fake = _Fake(_routes())
        out = _run(fake, _args())
        self.assertEqual((out["metric_id"], out["table"], out["column"]), ("NEW", "promotion", "DURATION DAYS"))
        self.assertEqual(out["readback"], {"function": "max", "column": "DURATION DAYS"})
        (body,) = _posted(fake)
        self.assertEqual((body["function"], body["information"]["description"]), ("max", "Days the promotion runs."))
        tokens = body["fact"]["expressions"][0]["expression"]["tokens"]
        self.assertEqual([(t["type"], t["value"]) for t in tokens],
                         [("column_reference", "[DURATION DAYS]"), ("end_of_text", "")])
        self.assertEqual(tokens[0]["target"], {"objectId": "P2", "subType": "column", "name": "DURATION DAYS"})
        self.assertEqual(body["fact"]["expressions"][0]["tables"][0]["objectId"], "T2")
        self.assertEqual(body["fact"]["dataType"]["type"], "integer")
        self.assertEqual(body["dimty"]["dimtyUnits"][0]["dimtyUnitType"], "report_base_level")
        self.assertTrue(body["format"]["values"])                                    # never the generic default
        self.assertEqual({cs for m, p, _, cs in fake.calls if (m, p) == ("POST", f"{MP}/factMetrics")}, {"CS1"})
        self.assertIn(("POST", "/api/model/changesets/CS1/commit"), [(m, p) for m, p, _, _ in fake.calls])

    def test_format_preset_and_currency_default(self):
        fake = _Fake(_routes())
        _run(fake, _args(name="promo price amount", column="PROMO PRICE", function="avg", format="fixed2"))
        self.assertEqual(_posted(fake)[0]["format"]["values"], bm._resolve_metric_format("fixed2"))
        fake = _Fake(_routes())
        _run(fake, _args(name="promo price amount", column="PROMO PRICE", function="avg"))
        self.assertEqual(_posted(fake)[0]["format"]["values"], bm._resolve_metric_format("currency"))

    def test_dry_run_writes_nothing(self):
        fake = _Fake(_routes())
        out = _run(fake, _args(dry_run=True))
        self.assertTrue(out["dry_run"])
        self.assertEqual((out["table"], out["column"]), ("promotion", "DURATION DAYS"))
        self.assertEqual(_posted(fake), [])
        self.assertEqual([c for c in fake.calls if c[1].startswith("/api/model/changesets")], [])

    def test_name_checks(self):
        for taken in ("Region", "avg duration days"):                     # an attribute's, a derived metric's
            fake = _Fake(_routes())
            self.assertIn("already", _dies(bm.cmd_add_column_fact_metric, fake, _args(name=taken)))
            self.assertEqual(_posted(fake), [])
        fake = _Fake(_routes())                                            # a fact metric's, on another column
        self.assertIn("exists", _dies(bm.cmd_add_column_fact_metric, fake, _args(name="Amount")))
        self.assertEqual(_posted(fake), [])
        _dies(bm.cmd_add_column_fact_metric, _Fake(_routes()), _args(function="median"))

    def test_rerun_is_harmless(self):
        m = _two_tables()
        m["factMetrics"].append({"information": {"objectId": "F2", "name": "promotion duration days"},
                                 "function": "max", "fact": {"expressions": [
                                     {"expression": {"text": "[DURATION DAYS]"}, "tables": [{"objectId": "T2"}]}]}})
        fake = _Fake(_routes(m))
        out = _run(fake, _args())
        self.assertTrue(out["skipped"])
        self.assertEqual(_posted(fake), [])
        _dies(bm.cmd_add_column_fact_metric, _Fake(_routes(m)), _args(function="sum"))   # same name, other function

    def test_failed_post_discards(self):
        routes = _routes()
        routes[("POST", f"{MP}/factMetrics")] = _Resp(400, {"code": "8004ccde", "message": "bad"})
        fake = _Fake(routes)
        _dies(bm.cmd_add_column_fact_metric, fake, _args())
        self.assertIn(("DELETE", "/api/model/changesets/CS1"), [(m, p) for m, p, _, _ in fake.calls])
        self.assertNotIn(("POST", "/api/model/changesets/CS1/commit"), [(m, p) for m, p, _, _ in fake.calls])


class CliTests(unittest.TestCase):
    def test_parser_and_from_export(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "export.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"model": [_two_tables()]}, f)
            argv = ["build_mosaic.py", "add-column-fact-metric", "--model-id", "M1", "--name", "duration max",
                    "--column", "promotion duration", "--function", "max", "--from-export", path]
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                    unittest.mock.patch.object(sys, "argv", argv):
                try:
                    bm.main()
                except SystemExit as e:
                    self.assertIn(e.code, (0, None))
            res = json.loads(out.getvalue())
            self.assertTrue(res["dry_run"])
            self.assertEqual(res["column"], "DURATION DAYS")


if __name__ == "__main__":
    unittest.main()
