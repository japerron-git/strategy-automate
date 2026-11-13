import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import _hermetic  # noqa: E402,F401  (scrub MSTR_*/proxy env, private secret store)

import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "skills", "build-mosaic-model", "scripts"))

import strategy_validate_models as svm  # noqa: E402


class StrategyValidateModelsTests(unittest.TestCase):
    def test_compare_rows_reports_ok_with_tolerance(self):
        model = [{"region": "A", "revenue": "100.0000001"}]
        reference = [{"region": "A", "revenue": "100.0"}]

        result = svm.compare_rows(model, reference, ["region"], ["revenue"], 1e-6, "q")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["matched_rows"], 1)

    def test_compare_rows_reports_missing_and_delta_rows(self):
        model = [{"region": "A", "revenue": "90"}, {"region": "B", "revenue": "5"}]
        reference = [{"region": "A", "revenue": "100"}, {"region": "C", "revenue": "7"}]

        result = svm.compare_rows(model, reference, ["region"], ["revenue"], 1e-6, "q")

        self.assertEqual(result["status"], "mismatch")
        self.assertEqual(result["model_only_rows"], [["B"]])
        self.assertEqual(result["reference_only_rows"], [["C"]])
        self.assertEqual(result["delta_rows"][0]["key"], ["A"])


class _R:
    def __init__(self, status=200, body=None):
        self.status_code, self.ok, self._body, self.text = status, 200 <= status < 300, body, ""

    def json(self):
        return self._body


class _FakeTrino:
    """POST answers page 0; GETs answer from `pages` by URL; every call is recorded."""

    def __init__(self, first, pages):
        self.first, self.pages, self.calls = first, pages, []

    def post(self, url, **kw):
        self.calls.append(("POST", url))
        return _R(200, self.first)

    def get(self, url, **kw):
        self.calls.append(("GET", url))
        page = self.pages[url]
        if isinstance(page, BaseException):
            raise page
        return page

    def delete(self, url, **kw):
        self.calls.append(("DELETE", url))
        return _R(204)


URL = "https://trino.example/v1/statement"
NEXT = "https://trino.example/v1/statement/q1/1"


class ValidatorSafetyTests(unittest.TestCase):
    def test_empty_versus_empty_is_never_a_pass(self):
        self.assertEqual(svm.compare_rows([], [], ["k"], ["m"], 1e-6, "q")["status"], "empty")
        self.assertNotEqual(svm.compare_rows([], [], ["k"], ["m"], 1e-6, "q", allow_empty=True)["status"], "empty")

    def test_like_patterns_are_not_placeholders(self):
        sql = "SELECT region, SUM(rev) FROM {{MODEL}} WHERE region LIKE '%south%' GROUP BY 1"
        self.assertEqual(svm.inject_model(sql, "Sales Model"),
                         "SELECT region, SUM(rev) FROM \"sales model\" WHERE region LIKE '%south%' GROUP BY 1")
        self.assertEqual(svm.inject_model("SELECT 1 FROM %s WHERE n LIKE '%s%'", "M"),
                         "SELECT 1 FROM \"m\" WHERE n LIKE '%s%'")

    def test_rows_are_assembled_across_pages(self):
        http = _FakeTrino({"columns": [{"name": "a"}], "data": [[1]], "nextUri": NEXT},
                          {NEXT: _R(200, {"data": [[2]]})})
        self.assertEqual(svm._trino_follow(http, URL, "SELECT 1", 5), [{"a": 1}, {"a": 2}])
        self.assertNotIn("DELETE", [c[0] for c in http.calls])

    def test_a_failed_page_cancels_the_query(self):
        for failure in (_R(500, {}), KeyboardInterrupt()):
            http = _FakeTrino({"columns": [{"name": "a"}], "nextUri": NEXT}, {NEXT: failure})
            with self.assertRaises((SystemExit, KeyboardInterrupt)):
                svm._trino_follow(http, URL, "SELECT 1", 5)
            self.assertEqual(http.calls[-1], ("DELETE", NEXT))

    def test_credentials_never_follow_a_foreign_next_uri(self):
        evil = "https://elsewhere.example/v1/statement/q1/1"
        http = _FakeTrino({"columns": [{"name": "a"}], "nextUri": evil}, {})
        with self.assertRaises(SystemExit):
            svm._trino_follow(http, URL, "SELECT 1", 5)
        self.assertEqual([c for c in http.calls if c[1] == evil], [])   # no GET and no DELETE there


if __name__ == "__main__":
    unittest.main()


class GroupedNumberKeyTests(unittest.TestCase):
    def test_thousands_separators_dropped_from_number_keys(self):
        self.assertEqual(svm.key_for({"y": "2,015"}, ["y"]), ("2015",))
        self.assertEqual(svm.key_for({"y": "Smith, John"}, ["y"]), ("Smith, John",))
        self.assertEqual(svm.key_for({"y": "1,234,567.5"}, ["y"]), ("1234567.5",))
