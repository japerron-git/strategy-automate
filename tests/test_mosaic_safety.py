"""Tests for skills/build-mosaic-model/scripts/mosaic_safety.py — stdlib unittest only."""
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import _hermetic  # noqa: E402,F401  (scrub MSTR_*/proxy env, private secret store)

import json
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "skills", "build-mosaic-model", "scripts"))

import mosaic_safety as ms  # noqa: E402


class _FakeResponse:
    """Minimal stand-in for requests.Response — text + status_code only."""
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text


# ── Error parsing ────────────────────────────────────────────────────────────

class TestParseMstrError(unittest.TestCase):
    def test_parses_strategy_error_body(self):
        body = {"code": "8004ccc7", "iServerCode": -2147072486,
                "message": "Table cannot be used as the join table"}
        resp = _FakeResponse(400, json.dumps(body))
        info = ms.parse_mstr_error(resp)
        self.assertEqual(info["code"], "8004ccc7")
        self.assertEqual(info["iServerCode"], -2147072486)
        self.assertIn("join table", info["message"])
        self.assertEqual(info["status"], 400)

    def test_parses_iServerCode_as_int_from_string(self):
        body = {"code": "8004cb0a", "iServerCode": "-2147072486",
                "message": "session cap"}
        info = ms.parse_mstr_error(_FakeResponse(503, json.dumps(body)))
        self.assertEqual(info["iServerCode"], -2147072486)

    def test_handles_plain_text_body(self):
        info = ms.parse_mstr_error(_FakeResponse(500, "Internal Server Error"))
        self.assertEqual(info["code"], None)
        self.assertEqual(info["status"], 500)
        self.assertIn("Internal", info["message"])

    def test_handles_empty_response(self):
        info = ms.parse_mstr_error(_FakeResponse(200, ""))
        self.assertEqual(info["code"], None)
        self.assertEqual(info["iServerCode"], None)

    def test_handles_dict_input(self):
        info = ms.parse_mstr_error({"code": "8004cb0a", "iServerCode": -1})
        self.assertEqual(info["code"], "8004cb0a")
        self.assertEqual(info["iServerCode"], -1)

    def test_handles_none(self):
        info = ms.parse_mstr_error(None)
        self.assertEqual(info["code"], None)


class TestFormatMstrError(unittest.TestCase):
    def test_one_line_with_code_and_status(self):
        body = {"code": "8004ccc7", "iServerCode": -2147072486,
                "message": "Table cannot be used"}
        s = ms.format_mstr_error(_FakeResponse(400, json.dumps(body)),
                                 prefix="wire-relationships")
        self.assertIn("HTTP 400", s)
        self.assertIn("[8004ccc7]", s)
        self.assertIn("wire-relationships", s)


class TestIsSessionCapError(unittest.TestCase):
    def test_detects_by_code(self):
        body = {"code": "8004cb0a", "message": "session cap"}
        self.assertTrue(ms.is_session_cap_error(_FakeResponse(400, json.dumps(body))))

    def test_detects_by_iServerCode(self):
        body = {"iServerCode": -2147072486, "message": "cap"}
        self.assertTrue(ms.is_session_cap_error(_FakeResponse(400, json.dumps(body))))

    def test_negative_for_unrelated_error(self):
        body = {"code": "8004ccc7", "iServerCode": -1234, "message": "x"}
        self.assertFalse(ms.is_session_cap_error(_FakeResponse(400, json.dumps(body))))

    def test_accepts_already_parsed_dict(self):
        self.assertTrue(ms.is_session_cap_error({"code": "8004cb0a"}))


# ── Expression helpers ───────────────────────────────────────────────────────

class TestMakeExpression(unittest.TestCase):
    def test_basic_column_reference(self):
        out = ms.make_expression("CUST_ID", "T_MOSAIC", table_name="CUSTOMER")
        tokens = out["expression"]["tokens"]
        self.assertEqual(tokens[0]["type"], "column_reference")
        self.assertEqual(tokens[0]["value"], "CUST_ID")
        self.assertEqual(out["tables"][0]["objectId"], "T_MOSAIC")
        self.assertEqual(out["tables"][0]["name"], "CUSTOMER")

    def test_dtype_string_wrapped(self):
        out = ms.make_expression("X", "T1", dtype="utf8_char")
        self.assertEqual(out["columns"][0]["dataType"], {"type": "utf8_char"})

    def test_dtype_dict_kept(self):
        dt = {"type": "integer", "precision": 4, "scale": 0}
        out = ms.make_expression("X", "T1", dtype=dt)
        self.assertEqual(out["columns"][0]["dataType"], dt)

    def test_no_table_omits_tables(self):
        out = ms.make_expression("Y")
        self.assertNotIn("tables", out)

    def test_blank_column_raises(self):
        with self.assertRaises(ValueError):
            ms.make_expression("")


class TestNormalizeExpressions(unittest.TestCase):
    def test_text_only_becomes_tokens(self):
        attr = {
            "forms": [{
                "expressions": [{
                    "expression": {"text": "[CUST_ID]"},
                    "tables": [{"objectId": "T1"}],
                }],
            }]
        }
        out = ms.normalize_expressions(attr)
        expr = out["forms"][0]["expressions"][0]["expression"]
        self.assertIn("tokens", expr)
        self.assertEqual(expr["tokens"][0]["type"], "column_reference")
        self.assertEqual(expr["tokens"][0]["value"], "CUST_ID")
        # Original input not mutated
        self.assertNotIn("tokens", attr["forms"][0]["expressions"][0]["expression"])

    def test_existing_tokens_left_alone(self):
        attr = {
            "forms": [{
                "expressions": [{
                    "expression": {
                        "tokens": [{"type": "column_reference", "value": "ABC"}],
                        "text": "[ABC]",
                    }
                }]
            }]
        }
        out = ms.normalize_expressions(attr)
        self.assertEqual(
            out["forms"][0]["expressions"][0]["expression"]["tokens"][0]["value"], "ABC"
        )

    def test_handles_non_dict_gracefully(self):
        self.assertEqual(ms.normalize_expressions(None), None)
        self.assertEqual(ms.normalize_expressions("foo"), "foo")


# ── attributeLookupTable bulk-response utilities ─────────────────────────────

class TestAttributeLookupTableMap(unittest.TestCase):
    def test_extracts_lookup_id(self):
        attrs = [
            {"information": {"objectId": "A1", "name": "Customer"},
             "attributeLookupTable": {"objectId": "T1", "name": "CUSTOMER"}},
            {"information": {"objectId": "A2", "name": "Order"},
             "attributeLookupTable": {"objectId": "T2", "name": "ORDERS"}},
        ]
        m = ms.attribute_lookup_table_map(attrs)
        self.assertEqual(m, {"A1": "T1", "A2": "T2"})

    def test_skips_attributes_with_no_lookup(self):
        attrs = [
            {"information": {"objectId": "A1"}, "attributeLookupTable": {"objectId": "T1"}},
            {"information": {"objectId": "A2"}},  # no lookup table
        ]
        m = ms.attribute_lookup_table_map(attrs)
        self.assertEqual(m, {"A1": "T1"})

    def test_name_map_extracts_table_name(self):
        attrs = [{
            "information": {"objectId": "A1"},
            "attributeLookupTable": {"objectId": "T1", "name": "CUSTOMER"}}]
        self.assertEqual(ms.attribute_table_name_map(attrs), {"A1": "CUSTOMER"})


# ── Role-playing dimensions ──────────────────────────────────────────────────

class TestDetectRolePlayingSecondaries(unittest.TestCase):
    def test_single_relationship_is_primary(self):
        rels = [{"parent_attribute": "Date", "child_attribute": "Sold Date",
                 "relationship_table": "WEB_SALES"}]
        p, s = ms.detect_role_playing_secondaries(rels)
        self.assertEqual(len(p), 1)
        self.assertEqual(len(s), 0)

    def test_secondary_role_detected(self):
        rels = [
            {"parent_attribute": "Date", "child_attribute": "Sold Date",
             "relationship_table": "WEB_SALES"},
            {"parent_attribute": "Date", "child_attribute": "Ship Date",
             "relationship_table": "WEB_SALES"},  # same (parent, table) → secondary
        ]
        p, s = ms.detect_role_playing_secondaries(rels)
        self.assertEqual(len(p), 1)
        self.assertEqual(len(s), 1)
        self.assertEqual(s[0]["child_attribute"], "Ship Date")

    def test_different_tables_both_primary(self):
        rels = [
            {"parent_attribute": "Date", "child_attribute": "Sold Date",
             "relationship_table": "WEB_SALES"},
            {"parent_attribute": "Date", "child_attribute": "Ship Date",
             "relationship_table": "STORE_SALES"},
        ]
        p, s = ms.detect_role_playing_secondaries(rels)
        self.assertEqual(len(p), 2)
        self.assertEqual(len(s), 0)

    def test_case_insensitive_grouping(self):
        rels = [
            {"parent_attribute": "Date", "child_attribute": "A",
             "relationship_table": "WEB_SALES"},
            {"parent_attribute": "date", "child_attribute": "B",
             "relationship_table": "web_sales"},
        ]
        p, s = ms.detect_role_playing_secondaries(rels)
        self.assertEqual(len(p), 1)
        self.assertEqual(len(s), 1)



class ErrorShapeTests(unittest.TestCase):
    """The tenant's reason must reach the operator whatever shape the error takes."""

    class R:
        def __init__(self, status, text, headers=None):
            self.status_code, self.text, self.headers = status, text, headers or {}

    def test_nested_unknown_and_header_errors(self):
        R = self.R
        self.assertEqual(ms.format_mstr_error(R(400, '{"errors":[{"code":"8004cc0b","message":"bad function"}]}')),
                         "HTTP 400 [8004cc0b] bad function")
        self.assertEqual(ms.format_mstr_error(R(400, '{"objectErrors":[{"field":"forms"}]}')),
                         'HTTP 400 {"objectErrors":[{"field":"forms"}]}')
        self.assertEqual(ms.format_mstr_error(R(400, "", {"X-MSTR-ErrorCode": "ERR001",
                                                          "X-MSTR-ErrorMessage": "Invalid formula"})),
                         "HTTP 400 [ERR001] Invalid formula")
        self.assertEqual(ms.format_mstr_error(R(400, '{"code":"8004ccc7","message":"Table cannot be used"}')),
                         "HTTP 400 [8004ccc7] Table cannot be used")

if __name__ == "__main__":
    unittest.main()
