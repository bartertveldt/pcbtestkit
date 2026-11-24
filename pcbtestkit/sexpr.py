"""Minimal, fast S-expression reader/writer for KiCad files.

KiCad files (.kicad_pcb, .kicad_sch, .kicad_mod) are S-expressions. Reading
does not depend on KiCad being installed, so the toolkit works from the
command line, in CI, on a Raspberry Pi and inside KiCad itself.

Parsed nodes are plain Python lists: ``["pad", "1", "smd", "rect", [...], ...]``.
Atoms stay strings; use :func:`num` to convert.
"""
from __future__ import annotations

import re
from typing import Any, Iterator, List, Optional

_TOKEN = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+')
_UNESC = re.compile(r'\\(.)')
_ESC_MAP = {"n": "\n", "t": "\t", "r": "\r"}


class SexprError(ValueError):
    pass


def _unescape(s: str) -> str:
    if "\\" not in s:
        return s
    return _UNESC.sub(lambda m: _ESC_MAP.get(m.group(1), m.group(1)), s)


def parse(text: str) -> list:
    """Parse one top-level S-expression and return it as nested lists."""
    stack: List[list] = [[]]
    for tok in _TOKEN.findall(text):
        if tok == "(":
            new: list = []
            stack[-1].append(new)
            stack.append(new)
        elif tok == ")":
            if len(stack) == 1:
                raise SexprError("unbalanced ')'")
            stack.pop()
        elif tok[0] == '"':
            stack[-1].append(_unescape(tok[1:-1]))
        else:
            stack[-1].append(tok)
    if len(stack) != 1:
        raise SexprError("unexpected end of file (missing ')')")
    if not stack[0]:
        raise SexprError("empty document")
    return stack[0][0]


def load(path: str) -> list:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return parse(fh.read())


# --------------------------------------------------------------------------- #
# Query helpers
# --------------------------------------------------------------------------- #
def is_node(x: Any, name: Optional[str] = None) -> bool:
    return isinstance(x, list) and bool(x) and (name is None or x[0] == name)


def child(node: list, name: str) -> Optional[list]:
    for c in node[1:]:
        if isinstance(c, list) and c and c[0] == name:
            return c
    return None


def children(node: list, name: str) -> List[list]:
    return [c for c in node[1:] if isinstance(c, list) and c and c[0] == name]


def iter_children(node: list, names) -> Iterator[list]:
    for c in node[1:]:
        if isinstance(c, list) and c and c[0] in names:
            yield c


def atoms(node: list) -> List[str]:
    """Non-list arguments of a node (excluding the node name)."""
    return [c for c in node[1:] if not isinstance(c, list)]


def value(node: Optional[list], name: str, default: Any = None, idx: int = 1) -> Any:
    if node is None:
        return default
    c = child(node, name)
    if c is None or len(c) <= idx:
        return default
    return c[idx]


def num(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def xy(node: Optional[list], default=(0.0, 0.0)):
    """(start x y) -> (x, y)."""
    if node is None or len(node) < 3:
        return default
    return num(node[1]), num(node[2])


def yes(node: Optional[list], name: str) -> bool:
    """True if ``(name yes)`` / ``(name)`` / bare atom ``name`` is present."""
    if node is None:
        return False
    for c in node[1:]:
        if isinstance(c, list):
            if c and c[0] == name:
                return len(c) == 1 or c[1] in ("yes", "true")
        elif c == name:
            return True
    return False


# --------------------------------------------------------------------------- #
# Writer
# --------------------------------------------------------------------------- #
class Sym(str):
    """An unquoted atom when writing (e.g. ``smd``, ``yes``, ``F.Cu`` in layer tables)."""


def fmt_num(v: float) -> str:
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, int):
        return str(v)
    s = ("%.6f" % v).rstrip("0").rstrip(".")
    if s in ("-0", ""):
        s = "0"
    return s


def _quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _atom(x: Any) -> str:
    if isinstance(x, Sym):
        return str(x)
    if isinstance(x, (int, float)):
        return fmt_num(x)
    return _quote(str(x))


def dumps(node: list, indent: int = 0) -> str:
    """Serialise nested lists KiCad-style (tabs, one sub-list per line)."""
    out: List[str] = []
    _dump(node, indent, out)
    return "".join(out)


def _dump(node: list, indent: int, out: List[str]) -> None:
    pad = "\t" * indent
    head = node[0]
    rest = node[1:]
    simple = all(not isinstance(c, list) for c in rest)
    flat_lists = all(not isinstance(c, list) or all(not isinstance(cc, list) for cc in c) for c in rest)
    if simple:
        out.append(pad + "(" + " ".join([str(head)] + [_atom(c) for c in rest]) + ")")
        return
    # inline short lists made only of xy/xyz-style leaves (e.g. pts)
    if head in ("xy", "xyz", "at", "start", "end", "mid", "center", "size") and flat_lists:
        out.append(pad + "(" + " ".join([str(head)] + [_inline(c) for c in rest]) + ")")
        return
    line = pad + "(" + str(head)
    atoms_ = [c for c in rest if not isinstance(c, list)]
    if atoms_:
        line += " " + " ".join(_atom(a) for a in atoms_)
    out.append(line + "\n")
    for c in rest:
        if isinstance(c, list):
            _dump(c, indent + 1, out)
            out.append("\n")
    out.append(pad + ")")


def _inline(c: Any) -> str:
    if isinstance(c, list):
        return "(" + " ".join([str(c[0])] + [_atom(x) for x in c[1:]]) + ")"
    return _atom(c)
