"""Windows defaults text I/O to cp1252; canopy must always say utf-8."""
from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "canopy"

# read_text( / write_text( / open( whose argument list closes without encoding=
_CALL = re.compile(
    r"\.(read_text|write_text)\(([^()]*(?:\([^()]*\)[^()]*)*)\)"
    r"|(?<![\w.])open\(([^()]*(?:\([^()]*\)[^()]*)*)\)"
)


def _offenders():
    out = []
    for p in sorted(SRC.rglob("*.py")):
        text = p.read_text(encoding="utf-8")
        for m in _CALL.finditer(text):
            args = m.group(2) if m.group(2) is not None else m.group(3)
            if args is None or "encoding=" in args:
                continue
            if any(mode in args for mode in ('"rb"', "'rb'", '"wb"', "'wb'")):
                continue
            line = text.count("\n", 0, m.start()) + 1
            out.append(f"{p.relative_to(SRC)}:{line}: {m.group(0)[:60]}")
    return out


def test_all_text_io_declares_utf8():
    assert _offenders() == []


def test_all_text_subprocess_calls_declare_utf8():
    out = []
    for p in sorted(SRC.rglob("*.py")):
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if "text=True" in line and "encoding=" not in line:
                out.append(f"{p.relative_to(SRC)}:{i}")
    assert out == []
