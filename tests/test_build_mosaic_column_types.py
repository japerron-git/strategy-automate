"""Tests for set-column-types: change column types in the model without reloading the source.
Synthetic table; the PATCH shape matches a run that worked live (physicalTable.columns and the pipeline
both carry the new type)."""
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import _hermetic  # noqa: E402,F401  (scrub MSTR_*/proxy env, private secret store)

import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "skills", "build-mosaic-model", "scripts"))

import build_mosaic as bm  # noqa: E402
from test_build_mosaic_conditional_metric import _FakeMSTR, _Resp  # noqa: E402

MP = "/api/model/dataModels/M1"
INT = {"type": "integer", "precision": 4, "scale": 0}
TXT = {"type": "utf8_char", "precision": 0, "scale": 0}


def _table():
    cols = [("C1", "STATUS CODE", INT), ("C2", "PERIOD", INT), ("C3", "AMOUNT", {"type": "double", "precision": 0,
                                                                                   "scale": 0})]
    pipeline = {"rootTable": {"children": [{"columns": [{"id": i, "name": n, "dataType": dict(t),
                                                         "sourceDataType": dict(t)} for i, n, t in cols]}]}}
    return {"information": {"objectId": "T1", "name": "sales", "subType": "logical_table"},
            "physicalTable": {"information": {"objectId": "P1", "subType": "physical_table", "name": "sales",
                                              "dateModified": "x"},
                              "columns": [{"information": {"objectId": i, "name": n, "extra": 1}, "columnName": n,
                                           "dataType": dict(t)} for i, n, t in cols],
                              "pipeline": json.dumps(pipeline)}}


def _model():
    t = _table()
    return {"tables": [t],
            "attributes": [
                {"information": {"objectId": "A1", "name": "Status"},
                 "forms": [{"expressions": [{"expression": {"text": "[STATUS CODE]"}}]}]},
                {"information": {"objectId": "A2", "name": "Region"},
                 "forms": [{"expressions": [{"expression": {"text": "[REGION]"}}]}]}],
            "factMetrics": [{"information": {"objectId": "F1", "name": "Amount"},
                             "fact": {"expressions": [{"expression": {"text": "[AMOUNT]"}}]}}]}


class _Fake(_FakeMSTR):
    def patch(self, path, **kw):
        return self._call("PATCH", path, **kw)


def _routes(attr_type=INT):
    attr = {"forms": [{"name": "ID", "dataType": dict(attr_type), "displayFormat": "number", "translations": [],
                       "expressions": [{"expression": {"text": "[STATUS CODE]"}, "tables": [{"objectId": "T1"}]}]}]}
    return {
        ("POST", f"{MP}/export"): _Resp(200, {"model": [_model()]}),
        ("GET", f"{MP}/tables/T1"): _Resp(200, _table()),
        ("GET", f"{MP}/attributes/A1"): _Resp(200, attr),
        ("PATCH", f"{MP}/tables/T1"): _Resp(200, {}),
        ("PATCH", f"{MP}/attributes/A1"): _Resp(200, {}),
        ("POST", "/api/model/changesets"): _Resp(201, {"id": "CS1"}),
        ("POST", "/api/model/changesets/CS1/commit"): _Resp(204),
        ("DELETE", "/api/model/changesets/CS1"): _Resp(204),
    }


def _args(**kw):
    base = dict(model_id="M1", set=[["STATUS CODE", "text"]], spec=None, table=None, no_joined=False, dry_run=False)
    base.update(kw)
    return types.SimpleNamespace(**base)


def _run(fake, args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        bm.cmd_set_column_types(fake, args)
    return json.loads(out.getvalue())


def _dies(fn, *a):
    with contextlib.redirect_stderr(io.StringIO()), unittest.TestCase().assertRaises(SystemExit):
        fn(*a)


class TargetTests(unittest.TestCase):
    def test_set_and_validation(self):
        t = bm.column_type_targets([["STATUS CODE", "Text"], ["PERIOD", "date", "yyyyMM"]], None)
        self.assertEqual(t, {"status code": ("STATUS CODE", "text", ""), "period": ("PERIOD", "date", "yyyyMM")})
        _dies(bm.column_type_targets, [["X", "varchar"]], None)            # unknown type
        _dies(bm.column_type_targets, [["X", "date"]], None)               # date needs a format
        _dies(bm.column_type_targets, [["X", "text", "yyyyMM"]], None)     # format only for dates
        _dies(bm.column_type_targets, [["X"]], None)
        _dies(bm.column_type_targets, None, None)

    def test_csv_spec_skips_disabled_rows(self):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: [os.remove(os.path.join(d, f)) for f in os.listdir(d)] and os.rmdir(d))
        p = os.path.join(d, "types.csv")
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write("enabled,column_name,target_type,date_format,notes\ny,STATUS CODE,text,,codes\n"
                    "n,AMOUNT,integer,,off\n")
        self.assertEqual(list(bm.column_type_targets(None, p)), ["status code"])


class TablePatchTests(unittest.TestCase):
    def test_both_column_list_and_pipeline_change(self):
        t = _table()
        targets = bm.column_type_targets([["status code", "text"], ["PERIOD", "date", "yyyyMM"]], None)
        pt, changed, same = bm.column_type_table_patch(t, targets)
        self.assertEqual([c[0] for c in changed], ["STATUS CODE", "PERIOD"])
        by = {c["columnName"]: c for c in pt["columns"]}
        self.assertEqual(by["STATUS CODE"]["dataType"], TXT)
        self.assertEqual(by["PERIOD"]["format"], "yyyyMM")
        self.assertEqual(by["AMOUNT"]["dataType"]["type"], "double")              # untouched
        self.assertEqual(by["STATUS CODE"]["information"], {"objectId": "C1", "name": "STATUS CODE"})
        self.assertEqual(pt["information"], {"objectId": "P1", "subType": "physical_table", "name": "sales"})
        pipe = {c["name"]: c for c in json.loads(pt["pipeline"])["rootTable"]["children"][0]["columns"]}
        self.assertEqual(pipe["STATUS CODE"]["dataType"], TXT)
        self.assertEqual(pipe["STATUS CODE"]["sourceDataType"], INT)              # the source stays as it is
        self.assertEqual(pipe["PERIOD"]["format"], "yyyyMM")
        self.assertEqual(t["physicalTable"]["columns"][0]["dataType"], INT)       # input not mutated

    def test_already_set_and_missing_column(self):
        t = _table()
        t["physicalTable"]["columns"][0]["dataType"] = dict(TXT)
        _, changed, same = bm.column_type_table_patch(t, bm.column_type_targets([["STATUS CODE", "text"]], None))
        self.assertEqual((changed, same), ([], ["STATUS CODE"]))
        _dies(bm.column_type_table_patch, _table(), bm.column_type_targets([["NOPE", "text"]], None))

    def test_default_table_is_the_widest(self):
        small = {"information": {"name": "lookup"}, "physicalTable": {"columns": [{}]}}
        self.assertEqual(bm._pick_table([small, _table()], None)["information"]["name"], "sales")
        self.assertEqual(bm._pick_table([small, _table()], "LOOKUP"), small)
        _dies(bm._pick_table, [small], "other")


class NameResolutionTests(unittest.TestCase):
    """The connector shows attribute names, not columns: --set may name an attribute or a fact metric."""

    def setUp(self):
        self.model = _model()
        self.model["attributes"][0]["forms"][0]["category"] = "ID"
        self.cols = {c["columnName"].casefold(): (c["information"]["objectId"], c["columnName"])
                     for c in _table()["physicalTable"]["columns"]}

    def test_attribute_and_fact_metric_names_resolve_to_columns(self):
        targets = bm.column_type_targets([["Status", "text"], ["amount", "double"], ["PERIOD", "date", "yyyyMM"]], None)
        out, notes = bm.resolve_column_targets(self.model, self.cols, targets)
        self.assertEqual(out, {"status code": ("STATUS CODE", "text", ""), "amount": ("AMOUNT", "double", ""),
                               "period": ("PERIOD", "date", "yyyyMM")})
        self.assertEqual(notes, ["'Status' reads column STATUS CODE"])
        out, _ = bm.resolve_column_targets(self.model, self.cols, bm.column_type_targets([["status__id", "text"]], None))
        self.assertEqual(list(out), ["status code"])          # the connector's "<attribute>__<form>" name

    def test_unknown_names_and_conflicts_stop(self):
        _dies(bm.resolve_column_targets, self.model, self.cols, bm.column_type_targets([["Nothing", "text"]], None))
        _dies(bm.resolve_column_targets, self.model, self.cols,
              bm.column_type_targets([["Status", "text"], ["STATUS CODE", "integer"]], None))

    def test_table_names_match_loosely(self):
        t = _table()
        t["information"]["name"] = "Sales Data.csv"
        self.assertIs(bm._pick_table([t], "sales_data"), t)
        self.assertIs(bm._pick_table([t], "SALES DATA.CSV"), t)
        self.assertEqual(bm._table_key("My_Table.parquet"), "mytable")


def _lookup_table():
    pipeline = {"rootTable": {"children": [{"columns": [{"id": "L1", "name": "STATUS", "dataType": dict(INT),
                                                         "sourceDataType": dict(INT)}]}]}}
    return {"information": {"objectId": "T2", "name": "status_lookup.csv", "subType": "logical_table"},
            "physicalTable": {"information": {"objectId": "P2", "name": "status_lookup.csv"},
                              "columns": [{"information": {"objectId": "L1", "name": "STATUS"}, "columnName": "STATUS",
                                           "dataType": dict(INT)}],
                              "pipeline": json.dumps(pipeline)}}


def _joined_model():
    m = _model()
    m["tables"].append(_lookup_table())
    m["attributes"][0]["forms"][0]["expressions"] = [
        {"expression": {"text": "[STATUS CODE]"}, "tables": [{"objectId": "T1"}]},
        {"expression": {"text": "[STATUS]"}, "tables": [{"objectId": "T2"}]}]
    return m


class JoinedTableTests(unittest.TestCase):
    """A lookup or mapping table keyed on the retyped column takes the same type, or the join breaks."""

    def test_joined_targets(self):
        targets = bm.column_type_targets([["STATUS CODE", "text"]], None)
        self.assertEqual(bm.joined_column_targets(_joined_model(), "T1", targets),
                         {"T2": {"status": ("STATUS", "text", "")}})
        self.assertEqual(bm.joined_column_targets(_model(), "T1", targets), {})

    def test_flow_retypes_the_joined_key(self):
        routes = _routes()
        routes[("POST", f"{MP}/export")] = _Resp(200, {"model": [_joined_model()]})
        routes[("GET", f"{MP}/tables/T2")] = _Resp(200, _lookup_table())
        routes[("PATCH", f"{MP}/tables/T2")] = _Resp(200, {})
        fake = _Fake(routes)
        out = _run(fake, _args())
        self.assertEqual(out["joined"], [{"table": "status_lookup.csv", "column": "STATUS", "from": "integer",
                                          "to": "utf8_char"}])
        body = next(kw["json"] for m, p, kw, _ in fake.calls if (m, p) == ("PATCH", f"{MP}/tables/T2"))
        self.assertEqual(body["physicalTable"]["columns"][0]["dataType"], TXT)
        fake = _Fake(routes)
        out = _run(fake, _args(no_joined=True))
        self.assertEqual(out["joined"], [])
        self.assertNotIn(("PATCH", f"{MP}/tables/T2"), [(m, p) for m, p, _, _ in fake.calls])


class ObjectRefTests(unittest.TestCase):
    def test_connector_form_names_resolve_to_the_attribute(self):
        fake = _Fake({("GET", f"{MP}/attributes"): _Resp(200, {"attributes": [
            {"information": {"objectId": "A9", "name": "exit status"}}]})})
        with contextlib.redirect_stderr(io.StringIO()):
            ref = bm.resolve_model_object_ref(fake, "M1", "attributes", "attribute", "exit status__code")
        self.assertEqual(ref, {"objectId": "A9", "subType": "attribute", "name": "exit status"})
        _dies(bm.resolve_model_object_ref, fake, "M1", "attributes", "attribute", "nothing__code")


class FlowTests(unittest.TestCase):
    def test_apply_writes_table_then_aligns_forms(self):
        fake = _Fake(_routes())
        out = _run(fake, _args())
        self.assertEqual(fake.identity, True)
        writes = [(m, p) for m, p, _, _ in fake.calls if m in ("PATCH", "POST") and not p.endswith("/export")]
        self.assertEqual(writes, [("POST", "/api/model/changesets"), ("PATCH", f"{MP}/tables/T1"),
                                  ("POST", "/api/model/changesets/CS1/commit"),
                                  ("POST", "/api/model/changesets"), ("PATCH", f"{MP}/attributes/A1"),
                                  ("POST", "/api/model/changesets/CS1/commit")])
        self.assertEqual({cs for m, _, _, cs in fake.calls if m == "PATCH"}, {"CS1"})   # inside a changeset
        form = next(kw["json"] for m, p, kw, _ in fake.calls if (m, p) == ("PATCH", f"{MP}/attributes/A1"))["forms"][0]
        self.assertEqual((form["dataType"], form["displayFormat"]), (TXT, "text"))
        self.assertNotIn("translations", form)
        self.assertEqual(form["expressions"][0]["expression"]["tokens"][0]["target"]["objectId"], "C1")
        self.assertEqual([c["column"] for c in out["changed"]], ["STATUS CODE"])
        self.assertEqual(out["aligned"], ["attribute 'Status'"])
        self.assertIn("republish", out["next"])

    def test_a_date_form_gets_the_date_role(self):
        routes = _routes()
        routes[("GET", f"{MP}/attributes/A1")]._body["forms"][0]["semanticRole"] = "month"   # an integer yyyyMM
        fake = _Fake(routes)
        _run(fake, _args(set=[["STATUS CODE", "date", "yyyyMM"]]))
        form = next(kw["json"] for m, p, kw, _ in fake.calls if (m, p) == ("PATCH", f"{MP}/attributes/A1"))["forms"][0]
        self.assertEqual((form["dataType"]["type"], form["displayFormat"], form["semanticRole"]), ("date", "date", "date"))
        table = next(kw["json"] for m, p, kw, _ in fake.calls if (m, p) == ("PATCH", f"{MP}/tables/T1"))
        col = next(c for c in table["physicalTable"]["columns"] if c["columnName"] == "STATUS CODE")
        self.assertEqual((col["dataType"]["type"], col["format"]), ("date", "yyyyMM"))   # as Studio stores it

    def test_forms_already_aligned_by_the_service(self):
        routes = _routes(attr_type=TXT)
        attr = routes[("GET", f"{MP}/attributes/A1")]._body
        attr["forms"][0]["displayFormat"] = "text"
        fake = _Fake(routes)
        out = _run(fake, _args())
        self.assertEqual(out["aligned"], [])
        self.assertEqual(sum(1 for m, p, _, _ in fake.calls if m == "PATCH"), 1)   # the table only

    def test_dry_run_writes_nothing(self):
        fake = _Fake(_routes())
        out = _run(fake, _args(dry_run=True))
        self.assertTrue(out["dry_run"])
        self.assertEqual([c for c in fake.calls if c[0] in ("PATCH", "DELETE") or c[1].startswith("/api/model/change")],
                         [])

    def test_failed_table_patch_discards_and_stops(self):
        routes = _routes()
        routes[("PATCH", f"{MP}/tables/T1")] = _Resp(400, {"message": "bad"})
        fake = _Fake(routes)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()), \
                contextlib.redirect_stdout(io.StringIO()):
            bm.cmd_set_column_types(fake, _args())
        self.assertIn(("DELETE", "/api/model/changesets/CS1"), [(m, p) for m, p, _, _ in fake.calls])
        self.assertNotIn(("PATCH", f"{MP}/attributes/A1"), [(m, p) for m, p, _, _ in fake.calls])

    def test_formula_form_is_reported_not_rewritten(self):
        routes = _routes()
        attr = copy.deepcopy(routes[("GET", f"{MP}/attributes/A1")]._body)
        attr["forms"].append({"name": "Label", "expressions": [{"expression": {"text": "Concat([STATUS CODE], 'x')"}}]})
        routes[("GET", f"{MP}/attributes/A1")] = _Resp(200, attr)
        out = _run(_Fake(routes), _args())
        self.assertEqual(out["aligned"], [])
        self.assertTrue(any("Status" in n for n in out["check_by_hand"]))


if __name__ == "__main__":
    unittest.main()
