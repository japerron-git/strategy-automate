"""Tests for rename-describe: names and descriptions of attributes and fact metrics, by name, in one
changeset. The PATCH shape ({"information": {"name", "description"}}) matches runs that worked live."""
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

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "skills", "build-mosaic-model", "scripts"))

import build_mosaic as bm  # noqa: E402
from test_build_mosaic_column_types import _Fake, MP  # noqa: E402
from test_build_mosaic_conditional_metric import _Resp  # noqa: E402

MODEL = {"attributes": [{"information": {"objectId": "A1", "name": "Region", "description": "old"}}],
         "factMetrics": [{"information": {"objectId": "F1", "name": "Amount", "description": ""}},
                         {"information": {"objectId": "F2", "name": "Amount (raw)", "description": ""}}]}


def _dies(fn, *a):
    with contextlib.redirect_stderr(io.StringIO()), unittest.TestCase().assertRaises(SystemExit):
        fn(*a)


class ItemTests(unittest.TestCase):
    def test_validation(self):
        ok = bm.rename_describe_items({"items": [{"object_type": "Attribute", "current_name": "Region",
                                                  "description": "d"}]})
        self.assertEqual(ok, [{"object_type": "attribute", "current_name": "Region", "new_name": None,
                               "description": "d"}])
        _dies(bm.rename_describe_items, [{"object_type": "metric", "current_name": "x", "description": "d"}])
        _dies(bm.rename_describe_items, [{"object_type": "attribute", "current_name": "x", "description": "d" * 251}])
        _dies(bm.rename_describe_items, [{"object_type": "attribute", "current_name": "x"}])
        _dies(bm.rename_describe_items, {"items": []})


class WriteTests(unittest.TestCase):
    def test_writes_skip_done_and_refuse_collisions(self):
        items = bm.rename_describe_items([
            {"object_type": "attribute", "current_name": "region", "new_name": "Sales Region"},
            {"object_type": "fact_metric", "current_name": "Amount", "description": "Total amount."},
            {"object_type": "fact_metric", "current_name": "Old name", "new_name": "Amount (raw)"},   # already done
            {"object_type": "attribute", "current_name": "Region", "description": "old"}])          # after rename
        writes, notes = bm.rename_describe_writes(MODEL, "M1", items)
        self.assertEqual(writes[0], ("attribute 'Region': rename -> 'Sales Region'", f"{MP}/attributes/A1",
                                     {"information": {"name": "Sales Region", "description": "old"}}))
        self.assertEqual(writes[1][2], {"information": {"name": "Amount", "description": "Total amount."}})
        self.assertEqual(len(writes), 2)
        self.assertIn("Old name: already renamed to 'Amount (raw)'", notes)
        _dies(bm.rename_describe_writes, MODEL, "M1", bm.rename_describe_items(
            [{"object_type": "fact_metric", "current_name": "Amount", "new_name": "Amount (raw)"}]))
        _dies(bm.rename_describe_writes, MODEL, "M1", bm.rename_describe_items(
            [{"object_type": "attribute", "current_name": "Nope", "description": "x"}]))

    def test_connector_form_names_describe_their_attribute(self):
        model = {"attributes": [{"information": {"objectId": "A1", "name": "Site", "description": ""}},
                                {"information": {"objectId": "A2", "name": "Status", "description": ""}}],
                 "factMetrics": [{"information": {"objectId": "F1", "name": "Amount", "description": ""}}]}
        writes, notes = bm.rename_describe_writes(model, "M1", bm.rename_describe_items([
            {"object_type": "attribute", "current_name": "site__latitude", "description": "Site: code, lat, long."},
            {"object_type": "attribute", "current_name": "Site (longitude)", "description": "Site: code, lat, long."},
            {"object_type": "attribute", "current_name": "status__code", "description": "Status code."}]))
        self.assertEqual([(w[1], w[2]["information"]["description"]) for w in writes],
                         [(f"{MP}/attributes/A1", "Site: code, lat, long."), (f"{MP}/attributes/A2", "Status code.")])
        self.assertTrue(any("form" in n and "'Site'" in n for n in notes))

    def test_all_problems_are_reported_before_any_write(self):
        model = {"attributes": [{"information": {"objectId": "A1", "name": "Site", "description": ""}}],
                 "factMetrics": [{"information": {"objectId": "F1", "name": "Amount", "description": ""}}]}
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            bm.rename_describe_writes(model, "M1", bm.rename_describe_items([
                {"object_type": "attribute", "current_name": "site__latitude", "new_name": "Lat"},     # form rename
                {"object_type": "attribute", "current_name": "Nope", "description": "x"},             # unknown
                {"object_type": "attribute", "current_name": "Amount", "description": "x"},           # wrong type
                {"object_type": "attribute", "current_name": "Site", "description": "a"},
                {"object_type": "attribute", "current_name": "site (code)", "description": "b"}]))    # conflict
        text = err.getvalue()
        self.assertIn("4 of 5 item(s)", text)
        for part in ("forms can't be renamed", "'Nope' not found", "set object_type to fact_metric",
                     "two different descriptions"):
            self.assertIn(part, text)

    def test_describe_by_new_name_after_rename_in_same_spec(self):
        writes, _ = bm.rename_describe_writes(MODEL, "M1", bm.rename_describe_items([
            {"object_type": "attribute", "current_name": "Region", "new_name": "Sales Region"},
            {"object_type": "attribute", "current_name": "Sales Region", "description": "Where it sold."}]))
        self.assertEqual(writes[0][2], {"information": {"name": "Sales Region", "description": "Where it sold."}})

    def test_flow_one_changeset_and_dry_run(self):
        d = tempfile.mkdtemp()
        spec = os.path.join(d, "spec.json")
        self.addCleanup(lambda: (os.remove(spec), os.rmdir(d)))
        with open(spec, "w", encoding="utf-8") as f:
            json.dump({"items": [{"object_type": "attribute", "current_name": "Region", "description": "new"}]}, f)
        routes = {("POST", f"{MP}/export"): _Resp(200, {"model": [MODEL]}),
                  ("PATCH", f"{MP}/attributes/A1"): _Resp(200, {}),
                  ("POST", "/api/model/changesets"): _Resp(201, {"id": "CS1"}),
                  ("POST", "/api/model/changesets/CS1/commit"): _Resp(204),
                  ("DELETE", "/api/model/changesets/CS1"): _Resp(204)}
        for dry in (True, False):
            fake = _Fake(routes)
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                bm.cmd_rename_describe(fake, types.SimpleNamespace(model_id="M1", spec=spec, dry_run=dry))
            writes = [(m, p) for m, p, _, _ in fake.calls if m in ("PATCH", "DELETE") or "changesets" in p]
            self.assertEqual(writes, [] if dry else [("POST", "/api/model/changesets"), ("PATCH", f"{MP}/attributes/A1"),
                                                      ("POST", "/api/model/changesets/CS1/commit")])
            self.assertEqual(json.loads(out.getvalue())["changed"], ["attribute 'Region': description"])


if __name__ == "__main__":
    unittest.main()
