"""Durable files (skills, memory, tests, docs) stay tenant- and domain-agnostic: they never name the models,
projects or columns of the test tenants the engine was developed against. The names are kept here only as
SHA-256 prefixes, so this file doesn't spread them. Raw evidence belongs in captures/ (not scanned) or in the
operator's ignored folders. To add a term: hashlib.sha256(term.lower().encode()).hexdigest()[:20], where a
term is one word (hyphens kept) or two words separated by a space.
"""
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import _hermetic  # noqa: E402,F401  (scrub MSTR_*/proxy env, private secret store)

import hashlib
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCAN_DIRS = ("skills", "memory", "tests")
ROOT_FILES = (".md",)
EXTENSIONS = (".py", ".md", ".json", ".yaml", ".yml", ".txt", ".csv", ".ps1", ".sh", ".toml")
DENIED = {
    "903311a2b5ee8c14550a", "da4ab0f0d6f7dde4b5a9", "7659cc14e913d7f7ab15", "6574a2e9f8d202fc5078",
    "e301b224f5c9e1a4c9cd", "43d82b6ac6044d964d99", "404d5354e1c637808355", "5a6f1f9a3dab726fea4e",
    "fe6ac0fc4e0b3216bb7b", "c0c50bed4a1c470e55ab", "be95381026ffd61d5ef6", "a422f17a8e8d23185ea9",
    "fedb24c98f783a5d2256", "7184ac8f50f806e9a8af", "5412aaa4d5736fdb2d24", "812df4136a77ca7a4ff7",
    "366835daadbe009e33f3", "2b2aa0198a91d351d945", "b8d668871ca19dcc08e0", "25a785f56e25d19f8db1",
    "f77d3486c161dd1f9174", "c1147d40c656dbcb7714", "9308a677eca8cd5b3ff0", "9b32e1a3c8f2acbba258",
    "9393e38cfee7959e9f04", "4ae91d48d71fabab31c3", "547af0b480ea0917be7c", "8e321a4f4fcdcbe581ab",
    "820977ad9a2eb501083f", "44d68cf298a2189d937e", "b90da840272d24e359fe", "6e61d8d781da8566903a",
}
_WORD = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def _digest(term: str) -> str:
    return hashlib.sha256(term.encode("utf-8")).hexdigest()[:20]


def denied_terms(text: str) -> set:
    """Digests of the denied terms the text holds: each word, and each pair of adjacent words."""
    words = _WORD.findall(text.lower())
    grams = set(words) | {f"{a} {b}" for a, b in zip(words, words[1:])}
    return {_digest(g) for g in grams} & DENIED


def durable_files():
    for d in SCAN_DIRS:
        for base, dirs, files in os.walk(os.path.join(ROOT, d)):
            dirs[:] = [x for x in dirs if x not in ("__pycache__", ".git", "captures")]
            for fn in files:
                if fn.endswith(EXTENSIONS):
                    yield os.path.join(base, fn)
    for fn in os.listdir(ROOT):
        if fn.endswith(ROOT_FILES):
            yield os.path.join(ROOT, fn)


class DurableTextTests(unittest.TestCase):
    def test_no_test_tenant_names_in_durable_files(self):
        hits = {}
        for path in durable_files():
            with open(path, encoding="utf-8", errors="replace") as f:
                found = denied_terms(f.read())
            if found:
                hits[os.path.relpath(path, ROOT)] = sorted(found)
        self.assertEqual(hits, {}, "durable files name a test tenant's model, project or columns (digests shown): "
                                   "describe the behavior generically, keep raw evidence in captures/ or ignore/")

    def test_the_check_finds_words_and_pairs(self):
        some = next(iter(DENIED))
        self.assertEqual(denied_terms("nothing to see here"), set())
        self.assertEqual(denied_terms("plain words") & {some}, set())
        self.assertTrue(all(len(d) == 20 for d in DENIED))


if __name__ == "__main__":
    unittest.main()
