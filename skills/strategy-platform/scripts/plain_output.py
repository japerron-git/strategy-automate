"""Plain console output for commands run from a generated PowerShell script.

Windows PowerShell 5.1 shows a native command's stderr as a red NativeCommandError, and decodes its output with
the console code page whatever the script sets: a check mark written by Python came out as "\\u2713" and an
ellipsis as "à" in a phase log (2026-11-15). When MOSAIC_PLAIN_OUTPUT=1 (the generated scripts set it), stderr
is merged into stdout and every character outside ASCII is written as an ASCII stand-in, so the console and
the log read the same on any code page. Nothing changes when the variable is not set.
"""
from __future__ import annotations

import io
import os
import sys

ENV = "MOSAIC_PLAIN_OUTPUT"
_ASCII = str.maketrans({"✓": "OK", "✔": "OK", "✗": "X", "✘": "X", "→": "->", "←": "<-", "↳": "->",
                        "…": "...", "—": "-", "–": "-", "≥": ">=", "≤": "<=", "≠": "!=", "×": "x",
                        "‘": "'", "’": "'", "“": '"', "”": '"', "•": "*", "·": "-", "⚠": "!", " ": " "})


def ascii_text(text: str) -> str:
    """Text with the usual symbols spelled in ASCII and anything else as \\uXXXX."""
    return text.translate(_ASCII).encode("ascii", "backslashreplace").decode("ascii")


class _PlainStream(io.TextIOBase):
    def __init__(self, target):
        self._target = target

    def writable(self) -> bool:
        return True

    def write(self, s: str) -> int:
        self._target.write(ascii_text(s))
        if "\n" in s:
            self._target.flush()
        return len(s)

    def flush(self) -> None:
        self._target.flush()


def enable(environ=os.environ) -> bool:
    """Merge stderr into stdout and write ASCII only, when the generated script asked for it."""
    if environ.get(ENV) != "1" or isinstance(sys.stdout, _PlainStream):
        return False
    sys.stdout = _PlainStream(sys.stdout)
    sys.stderr = sys.stdout
    return True
