"""Tests for add-source-table: a data-source table added to an existing model as a lookup (the key attribute's
ID form read on it, new attributes related to the key through it). Checks are pure; the write sequence runs
against a fake client, so nothing touches the network. Generic retail names.
"""
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
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "skills", "build-mosaic-model", "scripts"))

import build_mosaic as bm  # noqa: E402

MP = "/api/model/dataModels/M1"
KEY_ID, FACT_ID, NEW_TID, NEW_ATTR = "K" * 32, "F" * 32, "T" * 32, "N" * 32
BIGINT = {"type": "big_decimal", "precision": 19, "scale": 0}


def _model(extra_attrs=(), tables=()):
    key = {"information": {"objectId": KEY_ID, "name": "Store"},
           "forms": [{"id": bm._ID_FORM, "category": "ID", "name": "Store Number", "dataType": BIGINT,
                      "expressions": [{"expression": {"text": "STORE_NO"},
                                       "tables": [{"objectId": FACT_ID, "name": "SALES"}]}]},
                     {"id": "D" * 32, "category": "DESC", "name": "Store Name"}]}
    return {"attributes": [key, *extra_attrs], "factMetrics": [{"information": {"name": "net sales"}}],
            "metrics": [], "tables": list(tables)}


COLUMNS = [{"name": "STORE_NO", "dataType": BIGINT}, {"name": "FORMAT_GROUP", "dataType": {"type": "utf8_char"}}]


def _plan(model=None, cols=None, **kw):
    return bm.source_table_plan(model or _model(), kw.pop("key", "Store"), cols if cols is not None else COLUMNS,
                                kw.pop("table", "STORE_LOOKUP"), **kw)


def _dies(test, words, fn):
    with test.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()) as err:
        fn()
    test.assertIn(words, err.getvalue())


class SourceTablePlanTests(unittest.TestCase):
    def test_the_attribute_is_the_only_input(self):
        plan = _plan()
        self.assertEqual((plan["key_id_form"]["name"], plan["key_column"], plan["attributes"], plan["key_type_change"]),
                         ("Store Number", "STORE_NO", [("FORMAT_GROUP", "Format Group")], None))
        self.assertEqual(plan["key_type"]["from"], "the ID form")

    def test_the_key_column_is_found_by_the_attributes_or_id_forms_name(self):
        for col in ("store", "Store_Number", "STORENUMBER", "store_no"):
            cols = [{"name": col, "dataType": BIGINT}, COLUMNS[1]]
            with self.subTest(col=col):
                self.assertEqual(_plan(cols=cols)["key_column"], col)

    def test_no_or_several_matching_columns_ask_for_the_key_column(self):
        _dies(self, "none of ['CODE', 'FORMAT_GROUP'] is named like the attribute",
              lambda: _plan(cols=[{"name": "CODE", "dataType": BIGINT}, COLUMNS[1]]))
        _dies(self, "['STORE', 'STORE_NO'] all match",
              lambda: _plan(cols=[{"name": "STORE", "dataType": BIGINT}, COLUMNS[0]]))
        cols = [{"name": "CODE", "dataType": BIGINT}, COLUMNS[1]]
        self.assertEqual(_plan(cols=cols, key_column="code")["key_column"], "CODE")

    def test_a_key_form_that_isnt_the_id_form_stops_it(self):
        _dies(self, "joins an attribute on its ID form, which for 'Store' is 'Store Number'",
              lambda: _plan(key_form="Store Name"))
        self.assertEqual(_plan(key_form="store number")["key_column"], "STORE_NO")

    def test_a_key_column_of_another_type_stops_it_unless_retyped(self):
        cols = [{"name": "STORE_NO", "dataType": {"type": "utf8_char", "precision": 20}}]
        _dies(self, "is text but 'Store''s ID form is number (the ID form): they wouldn't join",
              lambda: _plan(cols=cols))
        change = _plan(cols=cols, retype_key=True)["key_type_change"]
        self.assertEqual((change["column"], change["to"]), ("STORE_NO", BIGINT))

    def test_the_id_forms_type_falls_back_to_the_column_it_reads(self):
        model = _model(tables=[{"information": {"objectId": FACT_ID, "name": "SALES"},
                                "physicalTable": {"columns": [{"columnName": "STORE_NO", "dataType": BIGINT}]}}])
        model["attributes"][0]["forms"][0].pop("dataType")
        self.assertEqual(_plan(model)["key_type"]["from"], "column STORE_NO of SALES")
        model["tables"] = []
        _dies(self, "can't read the type of 'Store''s ID form", lambda: _plan(model))

    def test_chosen_columns_and_names(self):
        cols = COLUMNS + [{"name": "REGION_NAME", "dataType": {"type": "utf8_char"}}]
        self.assertEqual(_plan(cols=cols, new_attributes=[("format_group", "Store Format")])["attributes"],
                         [("FORMAT_GROUP", "Store Format")])
        self.assertEqual(_plan(cols=cols)["attributes"], [("FORMAT_GROUP", "Format Group"), ("REGION_NAME", "Region Name")])

    def test_missing_columns_and_taken_names_stop_it(self):
        for attrs, words in (([("REGION", "Region")], "no column 'REGION'"),
                             ([("FORMAT_GROUP", "net sales")], "already the name of a factMetric"),
                             ([("FORMAT_GROUP", "store")], "the key attribute itself"),
                             ([("STORE_NO", "Store Copy")], "is the key column")):
            with self.subTest(words=words):
                _dies(self, words, lambda: _plan(new_attributes=attrs))

    def test_an_attribute_an_earlier_run_made_is_reused(self):
        made = {"information": {"objectId": NEW_ATTR, "name": "Format Group"},
                "forms": [{"expressions": [{"tables": [{"name": "STORE_LOOKUP"}]}]}]}
        self.assertEqual(_plan(_model([made]))["reused"], ["Format Group"])

    def test_type_family(self):
        self.assertEqual([bm.type_family(t) for t in (BIGINT, {"type": "utf8_char"}, "date", "time_stamp", None)],
                         ["number", "text", "date", "date", ""])


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self.ok, self._body = status, 200 <= status < 300, body
        self.text = "" if body is None else json.dumps(body)

    def json(self):
        return self._body


class _Fake:
    def __init__(self, routes):
        self.routes, self.calls, self.verbose = routes, [], False
        self.s = types.SimpleNamespace(headers={})

    def login(self, *, identity=False):
        pass

    def _call(self, method, path, **kw):
        self.calls.append((method, path, kw, self.s.headers.get("X-MSTR-MS-Changeset")))
        hit = self.routes.get((method, path))
        if isinstance(hit, list):
            return hit.pop(0) if len(hit) > 1 else hit[0]
        return hit or _Resp(404, {"message": "unrouted"})

    def get(self, path, **kw):
        return self._call("GET", path, **kw)

    def post(self, path, **kw):
        return self._call("POST", path, **kw)

    def put(self, path, **kw):
        return self._call("PUT", path, **kw)

    def patch(self, path, **kw):
        return self._call("PATCH", path, **kw)

    def delete(self, path, **kw):
        return self._call("DELETE", path, **kw)


def _table_body(*_a, **_k):
    pipe = {"rootTable": {"children": [{"columns": [{"name": c["name"], "dataType": c["dataType"],
                                                     "sourceDataType": c["dataType"]} for c in COLUMNS]}]}}
    return {"information": {"name": "STORE_LOOKUP"},
            "physicalTable": {"type": "pipeline", "pipeline": json.dumps(pipe),
                              "columns": [{"columnName": c["name"], "information": {"name": c["name"]},
                                           "dataType": c["dataType"]} for c in COLUMNS]}}


def _routes():
    key_full = copy.deepcopy(_model()["attributes"][0])
    return {
        ("POST", f"{MP}/export"): _Resp(200, {"model": [_model()]}),
        ("POST", "/api/model/changesets"): [_Resp(201, {"id": "CS1"}), _Resp(201, {"id": "CS2"})],
        ("POST", f"{MP}/tables"): _Resp(201, {"information": {"objectId": NEW_TID}}),
        ("GET", f"{MP}/attributes/{KEY_ID}"): _Resp(200, {**key_full, "relationships": [],
                                                          "attributeLookupTable": {"name": "SALES"}}),
        ("PATCH", f"{MP}/attributes/{KEY_ID}"): _Resp(200, {}),
        ("POST", f"{MP}/attributes"): _Resp(201, {"information": {"objectId": NEW_ATTR}}),
        ("PUT", f"{MP}/attributes/{KEY_ID}/relationships"): _Resp(200, {}),
        ("POST", "/api/model/changesets/CS1/commit"): _Resp(204),
        ("POST", "/api/model/changesets/CS2/commit"): _Resp(204),
        ("DELETE", "/api/model/changesets/CS1"): _Resp(204),
        ("DELETE", "/api/model/changesets/CS2"): _Resp(204),
    }


def _args(**kw):
    base = {"model_id": "M1", "instance": None, "instance_id": "DS1", "namespace": "SALES_SCHEMA",
            "table": "STORE_LOOKUP", "key_column": None, "key_attribute": "Store", "key_form": None,
            "retype_key": False, "attributes": ["format_group=Store Format"], "set_lookup": False, "dry_run": False}
    base.update(kw)
    return types.SimpleNamespace(**base)


class AddSourceTableFlowTests(unittest.TestCase):
    def setUp(self):
        self._orig = bm._make_pipeline_table_body
        bm._make_pipeline_table_body = _table_body
        self.addCleanup(setattr, bm, "_make_pipeline_table_body", self._orig)

    def _run(self, fake, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            bm.cmd_add_source_table(fake, _args(**kw))
        return json.loads(out.getvalue())

    def test_table_key_and_attribute_in_one_changeset_then_relationships(self):
        fake = _Fake(_routes())
        out = self._run(fake)
        writes = [(m, p, cs) for m, p, _, cs in fake.calls if m != "GET" and not p.endswith("/export")]
        self.assertEqual(writes, [
            ("POST", "/api/model/changesets", None),
            ("POST", f"{MP}/tables", "CS1"),
            ("PATCH", f"{MP}/attributes/{KEY_ID}", "CS1"),
            ("POST", f"{MP}/attributes", "CS1"),
            ("POST", "/api/model/changesets/CS1/commit", "CS1"),
            ("POST", "/api/model/changesets", None),
            ("PUT", f"{MP}/attributes/{KEY_ID}/relationships", "CS2"),
            ("POST", "/api/model/changesets/CS2/commit", "CS2")])
        patch = next(kw for m, p, kw, _ in fake.calls if m == "PATCH")["json"]
        exprs = patch["forms"][0]["expressions"]
        self.assertEqual(exprs[-1], {"expression": {"tokens": [{"type": "column_reference", "value": "STORE_NO"}]},
                                     "tables": [{"objectId": NEW_TID, "subType": "logical_table",
                                                 "name": "STORE_LOOKUP"}]})
        self.assertNotIn("attributeLookupTable", patch)
        attr = next(kw for m, p, kw, _ in fake.calls if m == "POST" and p == f"{MP}/attributes")["json"]
        self.assertEqual((attr["information"]["name"], attr["attributeLookupTable"]["objectId"]),
                         ("Store Format", NEW_TID))
        self.assertIn("displays", attr)
        rel = next(kw for m, p, kw, _ in fake.calls if m == "PUT")["json"]["relationships"]
        self.assertEqual(rel, [{"parent": {"objectId": NEW_ATTR, "subType": "attribute"},
                                "child": {"objectId": KEY_ID, "subType": "attribute"},
                                "relationshipType": "one_to_many",
                                "relationshipTable": {"objectId": NEW_TID, "subType": "logical_table"}}])
        self.assertEqual((out["attributes"], out["relationships_added"], out["key_lookup_table"]),
                         ({"Store Format": NEW_ATTR}, 1, "SALES"))

    def test_set_lookup_points_the_key_at_the_new_table(self):
        fake = _Fake(_routes())
        self._run(fake, set_lookup=True)
        patch = next(kw for m, p, kw, _ in fake.calls if m == "PATCH")["json"]
        self.assertEqual((patch["attributeLookupTable"]["objectId"], patch["autoDetectLookupTable"]),
                         (NEW_TID, False))
        self.assertEqual(patch["forms"][0]["lookupTable"]["objectId"], NEW_TID)

    def test_dry_run_writes_nothing(self):
        fake = _Fake(_routes())
        out = self._run(fake, dry_run=True)
        self.assertEqual([(m, p) for m, p, _, _ in fake.calls], [("POST", f"{MP}/export")])
        self.assertEqual(out["relationships"], ["Store Format (parent) -> Store (child) via STORE_LOOKUP"])
        self.assertTrue(out["new_table"])

    def test_a_failed_attribute_discards_the_changeset(self):
        routes = _routes()
        routes[("POST", f"{MP}/attributes")] = _Resp(400, {"message": "no"})
        fake = _Fake(routes)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            bm.cmd_add_source_table(fake, _args())
        writes = [(m, p) for m, p, _, _ in fake.calls if m != "GET"]
        self.assertIn(("DELETE", "/api/model/changesets/CS1"), writes)
        self.assertNotIn(("POST", "/api/model/changesets/CS1/commit"), writes)

    def test_only_the_attribute_is_needed(self):
        fake = _Fake(_routes())
        out = self._run(fake, attributes=None)
        self.assertEqual((out["key_column"], out["attributes"]), ("STORE_NO", {"Format Group": NEW_ATTR}))

    def test_a_text_key_column_stops_it_before_any_write(self):
        global COLUMNS
        saved = COLUMNS
        COLUMNS = [{"name": "STORE_NO", "dataType": {"type": "utf8_char"}}, saved[1]]
        self.addCleanup(lambda: globals().__setitem__("COLUMNS", saved))
        fake = _Fake(_routes())
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            bm.cmd_add_source_table(fake, _args())
        self.assertEqual([(m, p) for m, p, _, _ in fake.calls], [("POST", f"{MP}/export")])

    def test_a_text_key_column_is_typed_like_the_attribute_in_the_table_body(self):
        global COLUMNS
        saved = COLUMNS
        COLUMNS = [{"name": "STORE_NO", "dataType": {"type": "utf8_char"}}, saved[1]]
        self.addCleanup(lambda: globals().__setitem__("COLUMNS", saved))
        fake = _Fake(_routes())
        self._run(fake, retype_key=True)
        body = next(kw for m, p, kw, _ in fake.calls if p == f"{MP}/tables")["json"]
        self.assertEqual(body["physicalTable"]["columns"][0]["dataType"], BIGINT)
        pipe = json.loads(body["physicalTable"]["pipeline"])["rootTable"]["children"][0]["columns"][0]
        self.assertEqual((pipe["dataType"], pipe["sourceDataType"]), (BIGINT, {"type": "utf8_char"}))


if __name__ == "__main__":
    unittest.main()


class AttributeColumnMatchTests(unittest.TestCase):
    def test_a_reviewed_name_finds_its_column(self):
        cols = COLUMNS + [{"name": "REGION_NAME", "dataType": {"type": "utf8_char"}}]
        plan = bm.source_table_plan(_model(), "Store", cols, "STORE_LOOKUP",
                                    new_attributes=[("format group", "format group")])
        self.assertEqual(plan["attributes"], [("FORMAT_GROUP", "format group")])
