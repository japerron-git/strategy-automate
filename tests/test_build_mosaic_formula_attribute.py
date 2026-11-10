"""Tests for create-formula-attribute (an attribute computed from the table's columns). Synthetic model;
the token and body shapes match runs that worked live (AddMonths, Year typed integer, MonthsBetween)."""
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import _hermetic  # noqa: E402,F401  (scrub MSTR_*/proxy env, private secret store)

import contextlib
import io
import json
import os
import sys
import types
import unittest
import unittest.mock

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "skills", "build-mosaic-model", "scripts"))

import build_mosaic as bm  # noqa: E402
from test_build_mosaic_column_types import _Fake, _model, MP  # noqa: E402
from test_build_mosaic_conditional_metric import _Resp  # noqa: E402

COLS = {"status code": ("C1", "STATUS CODE"), "period": ("C2", "PERIOD")}


def _routes():
    return {("POST", f"{MP}/export"): _Resp(200, {"model": [_model()]}),
            ("POST", f"{MP}/attributes"): _Resp(201, {"information": {"objectId": "NEW"}}),
            ("GET", f"{MP}/attributes/A2"): _Resp(200, {"forms": [{"name": "ID", "translations": [],
                                                                  "expressions": [{"expression": {"text": "x"}}]}]}),
            ("PATCH", f"{MP}/attributes/A2"): _Resp(200, {}),
            ("POST", "/api/model/changesets"): _Resp(201, {"id": "CS1"}),
            ("POST", "/api/model/changesets/CS1/commit"): _Resp(204),
            ("DELETE", "/api/model/changesets/CS1"): _Resp(204)}


def _args(**kw):
    base = dict(model_id="M1", name="Period Year", formula="Year([PERIOD])", description="d", display_format="number",
                data_type="integer", table=None, update=False, unverified_ok=False, dry_run=False)
    base.update(kw)
    return types.SimpleNamespace(**base)


def _run(fake, args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        bm.cmd_create_formula_attribute(fake, args)
    return json.loads(out.getvalue())


def _dies(fn, *a):
    with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()), \
            unittest.TestCase().assertRaises(SystemExit):
        fn(*a)


class TokenTests(unittest.TestCase):
    def test_verified_shapes(self):
        toks, unverified = bm.formula_attribute_tokens("AddMonths([PERIOD], -1)", COLS)
        self.assertEqual([(t["type"], t["value"]) for t in toks],
                         [("function", "AddMonths"), ("character", "("), ("column_reference", "[PERIOD]"),
                          ("character", ","), ("character", "-"), ("integer", "1"), ("character", ")"),
                          ("end_of_text", "")])
        self.assertEqual(toks[2]["target"], {"objectId": "C2", "subType": "column", "name": "PERIOD"})
        self.assertEqual(unverified, [])

    def test_unverified_and_unknown(self):
        self.assertEqual(bm.formula_attribute_tokens("CaseV([STATUS CODE], '01', 1.5, 0)", COLS)[1],
                         ["decimal", "string literal"])
        _dies(bm.formula_attribute_tokens, "Year([NOPE])", COLS)
        _dies(bm.formula_attribute_tokens, "Year([PERIOD]) ;", COLS)


class FlowTests(unittest.TestCase):
    def test_create_in_one_changeset(self):
        fake = _Fake(_routes())
        out = _run(fake, _args())
        self.assertEqual(out["attribute"], "Period Year")
        body = next(kw["json"] for m, p, kw, _ in fake.calls if (m, p) == ("POST", f"{MP}/attributes"))
        form = body["forms"][0]
        self.assertEqual((form["category"], form["dataType"]["type"], form["displayFormat"]), ("ID", "integer", "number"))
        self.assertEqual(body["displays"]["reportDisplays"], [{"id": bm._ID_FORM, "name": "ID"}])
        self.assertEqual(body["attributeLookupTable"]["name"], "sales")
        self.assertEqual({cs for m, p, _, cs in fake.calls if (m, p) == ("POST", f"{MP}/attributes")}, {"CS1"})

    def test_gates_dry_run_and_update(self):
        fake = _Fake(_routes())
        _dies(bm.cmd_create_formula_attribute, fake, _args(formula="CaseV([STATUS CODE], '01', 'Y', 'N')"))
        self.assertNotIn("POST", [m for m, p, _, _ in fake.calls if p.endswith("/attributes")])
        _dies(bm.cmd_create_formula_attribute, _Fake(_routes()), _args(name="Region"))     # exists: needs --update
        fake = _Fake(_routes())
        out = _run(fake, _args(dry_run=True))
        self.assertEqual((out["dry_run"], out["method"]), (True, "POST"))
        self.assertEqual([c for c in fake.calls if c[1].startswith("/api/model/changesets")], [])
        fake = _Fake(_routes())
        _run(fake, _args(name="Region", update=True, data_type=None))
        patched = next(kw["json"] for m, p, kw, _ in fake.calls if m == "PATCH")["forms"][0]
        self.assertNotIn("translations", patched)
        self.assertEqual(patched["expressions"][0]["expression"]["tokens"][0]["value"], "Year")


class ColumnAttributeTests(unittest.TestCase):
    def test_reads_the_column_behind_a_fact_metric_as_is(self):
        fake = _Fake(_routes())
        out = _run(fake, _args(name="Amount band", formula="", column="Amount", data_type=None))
        self.assertEqual(out["attribute"], "Amount band")
        body = next(kw["json"] for m, p, kw, _ in fake.calls if (m, p) == ("POST", f"{MP}/attributes"))
        tokens = body["forms"][0]["expressions"][0]["expression"]["tokens"]
        self.assertEqual([(t["type"], t["value"]) for t in tokens], [("column_reference", "[AMOUNT]"), ("end_of_text", "")])
        self.assertEqual(body["forms"][0]["lookupTable"]["name"], "sales")
        _dies(bm.cmd_create_formula_attribute, _Fake(_routes()), _args(name="X", formula="", column="Nope"))
        fake = _Fake(_routes())
        _dies(bm.cmd_create_formula_attribute, fake, _args(name="amount", formula="", column="Amount"))   # a metric's name
        self.assertNotIn("POST", [m for m, p, _, _ in fake.calls if p.endswith("/attributes")])

    def test_parser(self):
        ap_args = ["add-column-attribute", "--model-id", "M1", "--name", "Units", "--column", "Amount", "--dry-run"]
        fake = _Fake(_routes())
        fake.open_changesets, fake.logout = [], lambda: None
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                unittest.mock.patch.object(bm, "MSTR", lambda *a, **k: fake), \
                unittest.mock.patch.object(sys, "argv", ["build_mosaic.py"] + ap_args):
            try:
                bm.main()
            except SystemExit as e:
                self.assertIn(e.code, (0, None))
        self.assertTrue(json.loads(out.getvalue())["dry_run"])


if __name__ == "__main__":
    unittest.main()
