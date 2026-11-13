"""Safe expression language for orchestration / pipeline conditions and input maps.

Literals (numbers, strings, true / false / null), comparisons (``== != < <= > >= in``), boolean
operators (``and or not``, also ``&& || !``), parentheses and *field access* on the evaluation
context (``nodes.fetch.output.items[0].name``). There are no calls, no operators that build
objects, and no attribute access beyond dictionary keys / list indexes, so an expression can only
read the data it is given. Parsing is bounded (length, nesting depth), evaluation is total: a
missing path is ``None``, an ordering of incomparable values is ``False``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any  # Any: JSON values of the evaluation context

MAX_LENGTH = 1000
MAX_DEPTH = 12
_TOKEN = re.compile(
    r"""\s*(?:
    (?P<num>-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)|
    (?P<str>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')|
    (?P<op>==|!=|<=|>=|&&|\|\||<|>|!|\(|\)|\[|\]|\.)|
    (?P<id>[A-Za-z][A-Za-z0-9_]*)
    )""",
    re.X,
)
_KEYWORDS = {"and", "or", "not", "in", "true", "false", "null"}
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "'": "'"}


class ExprError(ValueError):
    """The expression is not valid in the safe language."""


@dataclass(frozen=True)
class _Tok:
    kind: str
    value: str


def _tokenize(source: str) -> list[_Tok]:
    if len(source) > MAX_LENGTH:
        raise ExprError(f"expression is longer than {MAX_LENGTH} characters")
    pos, out = 0, []
    while pos < len(source):
        if source[pos:].strip() == "":
            break
        m = _TOKEN.match(source, pos)
        if m is None or m.end() == pos:
            raise ExprError(f"unexpected character at position {pos}")
        pos = m.end()
        kind = m.lastgroup or ""
        text = m.group(kind)
        if kind == "id" and text in _KEYWORDS:
            kind = "kw"
        out.append(_Tok(kind, text))
    return out


Node = tuple[Any, ...]  # Any: small tagged tuples forming the AST


class _Parser:
    def __init__(self, tokens: list[_Tok]) -> None:
        self.t = tokens
        self.i = 0
        self.depth = 0

    def peek(self) -> _Tok | None:
        return self.t[self.i] if self.i < len(self.t) else None

    def take(self) -> _Tok:
        tok = self.peek()
        if tok is None:
            raise ExprError("unexpected end of expression")
        self.i += 1
        return tok

    def accept(self, kind: str, value: str | None = None) -> bool:
        tok = self.peek()
        if tok is not None and tok.kind == kind and (value is None or tok.value == value):
            self.i += 1
            return True
        return False

    def parse(self) -> Node:
        node = self.or_()
        left = self.peek()
        if left is not None:
            raise ExprError(f"unexpected {left.value!r}")
        return node

    def _enter(self) -> None:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise ExprError("expression is nested too deeply")

    def or_(self) -> Node:
        self._enter()
        node = self.and_()
        while self.accept("kw", "or") or self.accept("op", "||"):
            node = ("or", node, self.and_())
        self.depth -= 1
        return node

    def and_(self) -> Node:
        node = self.not_()
        while self.accept("kw", "and") or self.accept("op", "&&"):
            node = ("and", node, self.not_())
        return node

    def not_(self) -> Node:
        if self.accept("kw", "not") or self.accept("op", "!"):
            self._enter()
            node = ("not", self.not_())
            self.depth -= 1
            return node
        return self.cmp()

    def cmp(self) -> Node:
        left = self.primary()
        tok = self.peek()
        if tok is not None and (
            (tok.kind == "op" and tok.value in ("==", "!=", "<", "<=", ">", ">="))
            or (tok.kind == "kw" and tok.value == "in")
        ):
            self.i += 1
            return ("cmp", tok.value, left, self.primary())
        if tok is not None and tok.kind == "kw" and tok.value == "not":
            nxt = self.t[self.i + 1] if self.i + 1 < len(self.t) else None
            if nxt is not None and nxt.kind == "kw" and nxt.value == "in":
                self.i += 2
                return ("not", ("cmp", "in", left, self.primary()))
        return left

    def primary(self) -> Node:
        tok = self.take()
        if tok.kind == "num":
            return ("lit", float(tok.value) if re.search(r"[.eE]", tok.value) else int(tok.value))
        if tok.kind == "str":
            return ("lit", _unquote(tok.value))
        if tok.kind == "kw" and tok.value in ("true", "false", "null"):
            return ("lit", {"true": True, "false": False, "null": None}[tok.value])
        if tok.kind == "op" and tok.value == "(":
            self._enter()
            inner = self.or_()
            self.depth -= 1
            if not self.accept("op", ")"):
                raise ExprError("missing closing parenthesis")
            return inner
        if tok.kind == "id":
            path: list[str | int] = [tok.value]
            while True:
                if self.accept("op", "."):
                    seg = self.take()
                    if seg.kind != "id":
                        raise ExprError("a field name must follow '.'")
                    path.append(seg.value)
                elif self.accept("op", "["):
                    idx = self.take()
                    if idx.kind == "num" and idx.value.isdigit():
                        path.append(int(idx.value))
                    elif idx.kind == "str":
                        path.append(_unquote(idx.value))
                    else:
                        raise ExprError("an index must be a non-negative integer or a string")
                    if not self.accept("op", "]"):
                        raise ExprError("missing closing bracket")
                else:
                    break
            nxt = self.peek()
            if nxt is not None and nxt.value == "(":
                raise ExprError("calls are not allowed")
            return ("path", tuple(path))
        raise ExprError(f"unexpected {tok.value!r}")


def _unquote(text: str) -> str:
    body = text[1:-1]
    return re.sub(r"\\(.)", lambda m: _ESCAPES.get(m.group(1), m.group(1)), body)


def parse(source: str) -> Node:
    """Parse `source`; raises `ExprError` when it is not a valid safe expression."""
    if not isinstance(source, str) or not source.strip():
        raise ExprError("an expression is required")
    return _Parser(_tokenize(source)).parse()


def paths(node: Node) -> set[tuple[str | int, ...]]:
    """Every field path an expression reads (validation of node references)."""
    kind = node[0]
    if kind == "path":
        return {node[1]}
    if kind == "lit":
        return set()
    if kind == "not":
        return paths(node[1])
    if kind == "cmp":
        return paths(node[2]) | paths(node[3])
    return paths(node[1]) | paths(node[2])


def _lookup(path: tuple[str | int, ...], ctx: dict[str, Any]) -> Any:  # Any: JSON
    cur: Any = ctx
    for seg in path:
        if isinstance(cur, dict) and isinstance(seg, str):
            cur = cur.get(seg)
        elif isinstance(cur, list) and isinstance(seg, int):
            cur = cur[seg] if 0 <= seg < len(cur) else None
        else:
            return None
        if cur is None:
            return None
    return cur


def _num(v: Any) -> bool:  # Any: JSON value
    return isinstance(v, int | float) and not isinstance(v, bool)


def _equal(a: Any, b: Any) -> bool:  # Any: JSON values
    if _num(a) and _num(b):
        return bool(a == b)
    return type(a) is type(b) and bool(a == b)


def _compare(op: str, a: Any, b: Any) -> bool:  # Any: JSON values
    if op == "==":
        return _equal(a, b)
    if op == "!=":
        return not _equal(a, b)
    if op == "in":
        if isinstance(b, str) and isinstance(a, str):
            return a in b
        if isinstance(b, list):
            return any(_equal(a, item) for item in b)
        if isinstance(b, dict) and isinstance(a, str):
            return a in b
        return False
    if not ((_num(a) and _num(b)) or (isinstance(a, str) and isinstance(b, str))):
        return False
    return bool({"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op])


def truthy(value: Any) -> bool:  # Any: JSON value
    return bool(value)


def evaluate(node: Node, ctx: dict[str, Any]) -> Any:  # Any: JSON result
    kind = node[0]
    if kind == "lit":
        return node[1]
    if kind == "path":
        return _lookup(node[1], ctx)
    if kind == "not":
        return not truthy(evaluate(node[1], ctx))
    if kind == "and":
        return truthy(evaluate(node[1], ctx)) and truthy(evaluate(node[2], ctx))
    if kind == "or":
        return truthy(evaluate(node[1], ctx)) or truthy(evaluate(node[2], ctx))
    return _compare(node[1], evaluate(node[2], ctx), evaluate(node[3], ctx))


def evaluate_condition(source: str, ctx: dict[str, Any]) -> bool:  # Any: JSON context
    return truthy(evaluate(parse(source), ctx))
