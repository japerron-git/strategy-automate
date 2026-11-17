"""Tests for build_mosaic.py --argv-file: the argument list from a JSON file, so text with a `"` reaches python whole.

Windows PowerShell 5.1 passes a `"` inside an argument to a native command unescaped, and python split a
description there (seen live 2026-11-17). Generated scripts pass such a step's arguments through a file instead.
"""
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import _hermetic  # noqa: E402,F401  (scrub MSTR_*/proxy env, private secret store)

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "skills", "build-mosaic-model", "scripts"))

import build_mosaic as bm  # noqa: E402

AWKWARD = ['Group by "order date (month)__id"; not the base date.', 'x"a b"', 'C:\\dir with space\\', "rate in %",
           "it's", "$env:X `n", "", "line\nbreak", "naïve — ok"]


class ArgvFileTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="argv-file-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _file(self, content, raw=False):
        path = os.path.join(self.dir, "args.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content if raw else json.dumps(content))
        return path

    def test_reads_every_character_back(self):
        argv = ["create-time-shift-metric", "--model-id", "M", "--name", "N", "--function", "PreviousMonth",
                "--time-attribute", "order date", "--source-metric", "sales", "--description", AWKWARD[0]]
        path = self._file(argv)
        self.assertEqual(bm.expand_argv_file(["--argv-file", path]), argv)
        self.assertEqual(bm.expand_argv_file([f"--argv-file={path}"]), argv)
        args = bm.build_parser().parse_args(bm.expand_argv_file(["--argv-file", path]))
        self.assertEqual(args.description, AWKWARD[0])
        self.assertEqual(bm.expand_argv_file(["--argv-file", self._file(AWKWARD)]), AWKWARD)

    def test_a_bom_is_accepted(self):
        path = os.path.join(self.dir, "bom.json")
        with open(path, "w", encoding="utf-8-sig") as f:
            json.dump(["certify", "--object-id", "X"], f)
        self.assertEqual(bm.expand_argv_file(["--argv-file", path]), ["certify", "--object-id", "X"])

    def test_other_argument_lists_pass_through(self):
        self.assertEqual(bm.expand_argv_file(["certify", "--object-id", "X"]), ["certify", "--object-id", "X"])
        self.assertEqual(bm.expand_argv_file([]), [])

    def test_refusals(self):
        for argv in (["--argv-file", self._file({"a": 1})], ["--argv-file", self._file([1, 2])],
                     ["--argv-file", self._file("not json", raw=True)], ["--argv-file", os.path.join(self.dir, "no")],
                     ["certify", "--argv-file", "x"], ["--argv-file", self._file(["--argv-file", "y"])]):
            with self.assertRaises(SystemExit, msg=argv), contextlib.redirect_stderr(io.StringIO()):
                bm.expand_argv_file(argv)


if __name__ == "__main__":
    unittest.main()
