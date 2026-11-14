"""show-forms: the displays body that makes a hidden form visible (a form in neither report nor browse displays
isn't listed by the connector), keeping the displays the attribute has."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "build-mosaic-model", "scripts"))
import build_mosaic as bm  # noqa: E402

ID_FORM = "45C11FA478E745FEA08D781CEA190FE5"   # Strategy's system ID form
DESC_FORM = "CCFBE2A5EADB4F50941FB879CCF1721C"


def _attr(report, browse):
    return {"information": {"objectId": "A1", "name": "Channel"},
            "forms": [{"id": ID_FORM, "name": "ID", "category": "ID"},
                      {"id": DESC_FORM, "name": "Description", "category": "DESC"}],
            "displays": {"reportDisplays": [{"id": i} for i in report], "browseDisplays": [{"id": i} for i in browse]}}


class ShowFormsTests(unittest.TestCase):
    def test_adds_the_hidden_form_and_keeps_the_rest(self):
        body, added, problems = bm.show_forms_body(_attr([DESC_FORM], [DESC_FORM]), ["ID"])
        self.assertEqual((added, problems), (["ID"], []))
        self.assertEqual(body["displays"]["reportDisplays"], [{"id": DESC_FORM}, {"id": ID_FORM}])
        self.assertEqual(body["displays"]["browseDisplays"], [{"id": DESC_FORM}, {"id": ID_FORM}])

    def test_by_category_and_half_shown(self):
        body, added, _ = bm.show_forms_body(_attr([DESC_FORM, ID_FORM], [DESC_FORM]), ["id"])
        self.assertEqual(added, ["id"])
        self.assertEqual(body["displays"]["browseDisplays"], [{"id": DESC_FORM}, {"id": ID_FORM}])

    def test_already_shown_changes_nothing(self):
        body, added, problems = bm.show_forms_body(_attr([DESC_FORM, ID_FORM], [ID_FORM, DESC_FORM]), ["ID"])
        self.assertEqual((body, added, problems), (None, [], []))

    def test_unknown_form_is_a_problem(self):
        body, _, problems = bm.show_forms_body(_attr([], []), ["Label"])
        self.assertIsNone(body)
        self.assertIn("form 'Label' not found", problems[0])


if __name__ == "__main__":
    unittest.main()
