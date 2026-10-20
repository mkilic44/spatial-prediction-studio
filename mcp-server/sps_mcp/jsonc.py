"""Small strict JSONC parser and span editor. Never rewrites unrelated text."""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field


def same_json_value(left, right):
    """Compare JSON values without Python's bool/integer equality coercion."""
    options = {"sort_keys": True, "separators": (",", ":"), "allow_nan": False}
    return json.dumps(left, **options) == json.dumps(right, **options)


@dataclass
class Token:
    kind: str
    start: int
    end: int
    value: object = None


@dataclass
class Member:
    key: Token
    node: "Node"
    comma: Token | None = None


@dataclass
class Node:
    value: object
    start: int
    end: int
    members: dict[str, Member] = field(default_factory=dict)


class Document:
    def __init__(self, text):
        if not isinstance(text, str) or len(text) > 8 * 1024 * 1024:
            raise ValueError("JSONC size")
        self.text, self.tokens, self.comments = text, [], []
        pos = 0
        while pos < len(text):
            ch = text[pos]
            if ch.isspace():
                pos += 1
                continue
            if text.startswith("//", pos):
                end = text.find("\n", pos)
                end = len(text) if end < 0 else end
                self.comments.append(Token("comment", pos, end))
            elif text.startswith("/*", pos):
                end = text.find("*/", pos + 2)
                if end < 0:
                    raise ValueError("Unclosed comment")
                end += 2
                self.comments.append(Token("comment", pos, end))
            elif ch == '"':
                value, end = json.decoder.scanstring(text, pos + 1, True)
                self.tokens.append(Token("string", pos, end, value))
            elif ch in "{}[]:,":
                end = pos + 1
                self.tokens.append(Token(ch, pos, end))
            else:
                match = re.match(r"(?:true|false|null|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)", text[pos:])
                if not match:
                    raise ValueError("Invalid JSONC token")
                end = pos + len(match.group())
                value = json.loads(match.group())
                if isinstance(value, float) and not math.isfinite(value):
                    raise ValueError("Nonfinite value")
                self.tokens.append(Token("scalar", pos, end, value))
            pos = end
        self.index = 0
        self.root = self._value(0)
        if self.index != len(self.tokens) or not isinstance(self.root.value, dict):
            raise ValueError("JSONC root or trailing content")

    def _take(self, kind=None):
        if self.index >= len(self.tokens):
            raise ValueError("Unexpected end")
        token = self.tokens[self.index]
        if kind is not None and token.kind != kind:
            raise ValueError("Unexpected token")
        self.index += 1
        return token

    def _next(self, kind):
        return self.index < len(self.tokens) and self.tokens[self.index].kind == kind

    def _value(self, depth):
        if depth > 64:
            raise ValueError("JSONC depth")
        start = self._take()
        if start.kind in {"string", "scalar"}:
            return Node(start.value, start.start, start.end)
        if start.kind == "{":
            values, members = {}, {}
            while not self._next("}"):
                key = self._take("string")
                if key.value in values:
                    raise ValueError("Duplicate JSONC key")
                self._take(":")
                node = self._value(depth + 1)
                comma = self._take(",") if self._next(",") else None
                values[key.value] = node.value
                members[key.value] = Member(key, node, comma)
                if comma is None and not self._next("}"):
                    raise ValueError("Missing comma")
            end = self._take("}")
            return Node(values, start.start, end.end, members)
        if start.kind == "[":
            values = []
            while not self._next("]"):
                values.append(self._value(depth + 1).value)
                if self._next(","):
                    self._take(",")
                elif not self._next("]"):
                    raise ValueError("Missing array comma")
            end = self._take("]")
            return Node(values, start.start, end.end)
        raise ValueError("Invalid value")

    def _trivia(self, start, end):
        # Preserve every original comment and whitespace in the edited span.
        parts, pos = [], start
        for token in self.tokens:
            if start <= token.start and token.end <= end:
                parts.append(self.text[pos:token.start])
                pos = token.end
        parts.append(self.text[pos:end])
        return "".join(parts)

    def _edits(self, edits):
        result = self.text
        for start, end, replacement in sorted(edits, reverse=True):
            result = result[:start] + replacement + result[end:]
        # Also validate syntax before a caller can write the edited document.
        Document(result)
        return result

    def set_member(self, parent, key, value):
        if not isinstance(parent.value, dict):
            raise ValueError("JSONC parent is not an object")
        existing = parent.members.get(key)
        if existing and same_json_value(existing.node.value, value):
            return self.text
        rendered = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
        if existing:
            node = existing.node
            comments = [c for c in self.comments if node.start <= c.start < node.end]
            trivia = self._trivia(node.start, node.end) + "\n" if comments else ""
            return self._edits([(node.start, node.end, trivia + rendered)])
        addition = json.dumps(key, ensure_ascii=False) + ": " + rendered
        newline = "\r\n" if "\r\n" in self.text else "\n"
        rendered = addition.replace("\n", newline)
        edits = [(parent.end - 1, parent.end - 1, newline + "  " + rendered + newline)]
        if parent.members:
            last = next(reversed(parent.members.values()))
            if last.comma is None:
                if last.node.end == parent.end - 1:
                    edits[0] = (edits[0][0], edits[0][1], "," + edits[0][2])
                else:
                    edits.append((last.node.end, last.node.end, ","))
        return self._edits(edits)

    def remove_member(self, parent, key):
        member = parent.members[key]
        edits = [(member.key.start, member.node.end, self._trivia(member.key.start, member.node.end))]
        if member.comma:
            edits.append((member.comma.start, member.comma.end, ""))
        else:
            keys = list(parent.members)
            index = keys.index(key)
            if index:
                preceding = parent.members[keys[index - 1]].comma
                if preceding:
                    edits.append((preceding.start, preceding.end, ""))
        return self._edits(edits)


def loads(text):
    return Document(text).root.value
