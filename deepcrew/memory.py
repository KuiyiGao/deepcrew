from __future__ import annotations

import codecs
import functools
import os
import re
import time
import unicodedata
from pathlib import Path
from typing import NamedTuple

from . import policy, providers
from .board import Board, read_text

INSTRUCTIONS = "instructions.md"
INSTRUCTIONS_MAX_CHARS = 20000
PROJECT_MEMORY_CHARS = 40000
IMPORT_DEPTH = 5
WORK_S = 3.0
READ_SLACK = 65536


_SP = "\t\n\v\f\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_SPACES = "\t\n\v\f\r \u00a0\u1680" + "".join(chr(c) for c in range(0x2000, 0x200B)) + "\u2028\u2029\u202f\u205f\u3000\ufeff"


def _rx(pattern: str, flags: int = 0) -> re.Pattern:
    pattern = pattern.replace("<SC>", _SP).replace("<S>", f"[{_SP}]").replace("<NS>", f"[^{_SP}]").replace("<DOT>", "[^\\n\\r\u2028\u2029]")
    return re.compile(pattern, flags | re.ASCII)


TAGS = ("address|article|aside|base|basefont|blockquote|body|caption|center|col|colgroup|dd|details|dialog|dir|div|dl|dt|fieldset|figcaption|figure|"
        "footer|form|frame|frameset|h[1-6]|head|header|hr|html|iframe|legend|li|link|main|menu|menuitem|meta|nav|noframes|ol|optgroup|option|p|param|"
        "search|section|summary|table|tbody|td|tfoot|th|thead|title|tr|track|ul")
BULL = r"(?:[*+-]|[0-9]{1,9}[.)])"
HR = r" {0,3}(?:(?:-[\t ]*){3,}|(?:_[ \t]*){3,}|(?:\*[ \t]*){3,})(?:\n+|\Z)"
PARAGRAPH = (r"[^\n]+(?:\n(?!" + "|".join([HR, r" {0,3}#{1,6}(?:<S>|\Z)", r" {0,3}>", r" {0,3}(?:`{3,}(?=[^`\n]*\n)|~{3,})[^\n]*\n",
                                           r" {0,3}(?:[*+-]|1[.)]) ", r"</?(?:" + TAGS + r")(?: +|\n|/?>)", r"<(?:script|pre|style|textarea|!--)",
                                           r" +\n"]) + r")[^\n]+)*")
ATTRIBUTE = r" +[a-zA-Z:_][\w.:-]*(?: *= *\"[^\"\n]*\"| *= *'[^'\n]*'| *= *[^<SC>\"'=<>`]+)?"
SPACE = _rx(r"(?:[ \t]*(?:\n|\Z))+")
CODE = _rx(r"(?:(?: {4}| {0,3}\t)[^\n]+(?:\n(?:[ \t]*(?:\n|\Z))*)?)+")
FENCES = _rx(r" {0,3}(?P<fence>`{3,}(?=[^`\n]*(?:\n|\Z))|~{3,})[^\n]*(?:\n|\Z)(?:|[\s\S]*?(?:\n|\Z))(?:(?P<close> {0,3}(?P=fence)[~`]* *(?=\n|\Z))|\Z)")
HEADING = _rx(r" {0,3}(#{1,6})(?=<S>|\Z)(<DOT>*)(?:\n+|\Z)")
HR_RULE = _rx(HR)
BLOCKQUOTE = _rx(r"( {0,3}> ?(" + PARAGRAPH + r"|[^\n]*)(?:\n|\Z))+")
LIST = _rx(r"( {0,3}" + BULL + r")([ \t][^\n]+?)?(?:\n|\Z)")
HTML = _rx(r" {0,3}(?:(?P<h1><(?P<raw>script|pre|style|textarea)[<SC>>][\s\S]*?(?:</(?P=raw)>[^\n]*\n+|\Z))"
           r"|(?P<h2><!--(?:-?>|[\s\S]*?(?:-->|\Z))[^\n]*(?:\n+|\Z))"
           r"|(?P<h3><\?[\s\S]*?(?:\?>\n*|\Z))"
           r"|(?P<h4><![A-Z][\s\S]*?(?:>\n*|\Z))"
           r"|(?P<h5><!\[CDATA\[[\s\S]*?(?:\]\]>\n*|\Z))"
           r"|(?P<h6></?(?:" + TAGS + r")(?: +|\n|/?>)[\s\S]*?(?:(?:\n[ \t]*)+\n|\Z))"
           r"|(?P<h7><(?!script|pre|style|textarea)[a-z][\w-]*(?:" + ATTRIBUTE + r")*? */?>(?=[ \t]*(?:\n|\Z))[\s\S]*?(?:(?:\n[ \t]*)+\n|\Z))"
           r"|(?P<h8></(?!script|pre|style|textarea)[a-z][\w-]*<S>*>(?=[ \t]*(?:\n|\Z))[\s\S]*?(?:(?:\n[ \t]*)+\n|\Z)))", re.I)
DEFINITION = _rx(r" {0,3}\[((?!<S>*\])(?:\\<DOT>|[^\[\]\\])+)\]: *(?:\n[ \t]*)?([^<<SC>][^<SC>]*|<<DOT>*?>)(?:(?: +(?:\n[ \t]*)?| *\n[ \t]*)"
                 r"(?:\"(?:\\\"?|[^\"\\])*\"|'[^'\n]*(?:\n[^'\n]+)*\n?'|\([^()]*\)))? *(?:\n+|\Z)")
OTHER_BLOCK = BULL + r" |(?: {4}| {0,3}\t)| {0,3}(?:`{3,}|~{3,})| {0,3}>| {0,3}#{1,6}| {0,3}<[^\n>]+>\n"
SETEXT = _rx(r"(?!" + OTHER_BLOCK + r")((?:<DOT>|\n(?!<S>*?\n|" + OTHER_BLOCK + r"))+?)\n {0,3}(=+|-+) *(?:\n+|\Z)")
LINE = _rx(r"[^\n]+")
QUOTE_START = _rx(r" {0,3}>")
QUOTE_PREFIX = _rx(r" {0,3}>[ \t]?")
QUOTE_UNDERLINE = _rx(r"\n {0,3}((?:=+|-+) *)(?=\n|\Z)")
INDENT = _rx(r"(?: {1,4}| {0,3}\t)")
BLANK = _rx(r"[ \t]*\Z")
FRONT = _rx(r"---<S>*\n([\s\S]*?)---<S>*\n?")


def _push(runs: list[tuple[int, int]], run: tuple[int, int]) -> None:
    if runs:
        (p, n), (q, m) = runs[-1], run
        if (p < 0 and q < 0) or (p >= 0 and q == p + n):
            runs[-1] = (p, n + m)
            return
    runs.append(run)


def _merge(a: list[tuple[int, int]], b: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out = list(a)
    for run in b:
        _push(out, run)
    return out


class _S:
    __slots__ = ("s", "runs")

    def __init__(self, s: str = "", runs: list[tuple[int, int]] | None = None):
        self.s = s
        self.runs = runs if runs is not None else ([(-1, len(s))] if s else [])

    @staticmethod
    def of(s: str) -> _S:
        return _S(s, [(0, len(s))] if s else [])

    def __len__(self) -> int:
        return len(self.s)

    def __add__(self, other: _S | str) -> _S:
        other = _S(other) if isinstance(other, str) else other
        return _S(self.s + other.s, _merge(self.runs, other.runs))

    def __getitem__(self, k: slice) -> _S:
        start, stop, _ = k.indices(len(self.s))
        out, at = [], 0
        for pos, length in self.runs:
            lo, hi = max(start, at), min(stop, at + length)
            if lo < hi:
                _push(out, (-1 if pos < 0 else pos + lo - at, hi - lo))
            at += length
            if at >= stop:
                break
        return _S(self.s[start:stop], out)

    def at(self, k: int) -> int:
        for pos, length in self.runs:
            if k < length:
                return -1 if pos < 0 else pos + k
            k -= length
        raise IndexError(k)

    def lines(self) -> list[_S]:
        out, runs, k, used = [], self.runs, 0, 0
        for line in self.s.split("\n"):
            need, part = len(line), []
            while need and k < len(runs):
                pos, length = runs[k]
                take = min(need, length - used)
                _push(part, (-1 if pos < 0 else pos + used, take))
                need, used = need - take, used + take
                if used == length:
                    k, used = k + 1, 0
            out.append(_S(line, part))
            if k < len(runs):
                pos, length = runs[k]
                used += 1
                if used == length:
                    k, used = k + 1, 0
        return out

    def rtrim(self, chars: str) -> _S:
        end = len(self.s)
        while end and self.s[end - 1] in chars:
            end -= 1
        return self[:end]

    def trim(self) -> _S:
        lo, hi = 0, len(self.s)
        while lo < hi and self.s[lo] in _SPACES:
            lo += 1
        while hi > lo and self.s[hi - 1] in _SPACES:
            hi -= 1
        return self[lo:hi]


def _join(parts: list[_S], sep: str = "\n") -> _S:
    runs: list[tuple[int, int]] = []
    for k, part in enumerate(parts):
        if k and sep:
            _push(runs, (-1, len(sep)))
        for run in part.runs:
            _push(runs, run)
    return _S(sep.join(p.s for p in parts), runs)


class _Tok:
    __slots__ = ("kind", "raw", "text", "kids", "fence", "closed", "html")

    def __init__(self, kind: str, raw: _S, text: _S | None = None, kids: list | None = None, fence: str = "", closed: bool = True, html: str = ""):
        self.kind, self.raw, self.text, self.kids, self.fence, self.closed, self.html = kind, raw, text, kids, fence, closed, html


def _dedent(raw: _S) -> _S:
    lines = []
    for line in raw.lines():
        m = INDENT.match(line.s)
        lines.append(line[m.end():] if m else line)
    return _join(lines).rtrim("\n")


def _tabs(line: _S, width: int) -> _S:
    if "\t" not in line.s:
        return line
    pieces = line.s.split("\t")
    out, at = _S(), 0
    for k, piece in enumerate(pieces):
        out = out + line[at:at + len(piece)] + (" " * width if k < len(pieces) - 1 else "")
        at += len(piece) + 1
    return out


def _first_other(s: str) -> int:
    return next((k for k, c in enumerate(s) if c != " "), -1)


class _Lexer:
    def __init__(self, text: str, deadline: float | None = None):
        self.deadline = deadline
        self.links: set[str] = set()
        self.top = True
        self.tokens = self.blocks(_S.of(text), [])

    def blocks(self, src: _S, toks: list[_Tok], clipped: bool = False) -> list[_Tok]:
        s, i, n = src.s, 0, len(src.s)
        while i < n:
            self.tick()
            last = toks[-1] if toks else None
            if (m := SPACE.match(s, i)) and m.end() > i:
                if m.end() - i == 1 and last is not None:
                    last.raw = last.raw + "\n"
                else:
                    toks.append(_Tok("space", src[i:m.end()]))
                i = m.end()
            elif m := CODE.match(s, i):
                raw, i = src[i:m.end()], m.end()
                if last is not None and last.kind in ("paragraph", "text"):
                    last.raw, last.text = last.raw + "\n" + raw, last.text + "\n" + _dedent(raw)
                else:
                    toks.append(_Tok("code", raw))
            elif m := FENCES.match(s, i):
                toks.append(_Tok("code", src[i:m.end()], fence=m.group("fence"), closed=m.group("close") is not None))
                i = m.end()
            elif m := HEADING.match(s, i):
                q = src[m.start(2):m.end(2)].trim()
                if q.s.endswith("#"):
                    cut = q.rtrim("#")
                    if not cut.s or cut.s.endswith(" "):
                        q = cut.trim()
                toks.append(_Tok("heading", src[i:m.end()], text=q))
                i = m.end()
            elif m := HR_RULE.match(s, i):
                raw = src[i:m.end()].rtrim("\n")
                toks.append(_Tok("hr", raw))
                i += len(raw)
            elif BLOCKQUOTE.match(s, i):
                tok = self.quotation(src[i:])
                toks.append(tok)
                i += len(tok.raw)
            elif LIST.match(s, i) and (tok := self.listing(src[i:])) is not None:
                toks.append(tok)
                i += len(tok.raw)
            elif m := HTML.match(s, i):
                kind = next(g for g in ("h1", "h2", "h3", "h4", "h5", "h6", "h7", "h8") if m.group(g) is not None)
                toks.append(_Tok("html", src[i:m.end()], html=kind))
                i = m.end()
            elif m := DEFINITION.match(s, i):
                raw, i = src[i:m.end()], m.end()
                if last is not None and last.kind in ("paragraph", "text"):
                    last.raw, last.text = last.raw + "\n" + raw, last.text + "\n" + raw
                else:
                    self.links.add(re.sub(f"[{_SP}]+", " ", m.group(1).lower()))
            elif m := SETEXT.match(s, i):
                toks.append(_Tok("heading", src[i:m.end()], text=src[m.start(1):m.end(1)]))
                i = m.end()
            elif self.top and (m := PARAGRAPH_RULE.match(s, i)):
                text = src[m.start(1):m.end(1)]
                if clipped and last is not None and last.kind == "paragraph":
                    last.raw, last.text = last.raw + "\n" + src[i:m.end()], last.text + "\n" + text
                else:
                    toks.append(_Tok("paragraph", src[i:m.end()], text=text))
                clipped, i = False, m.end()
            elif m := LINE.match(s, i):
                raw, i = src[i:m.end()], m.end()
                if last is not None and last.kind == "text":
                    last.raw, last.text = last.raw + "\n" + raw, last.text + "\n" + raw
                else:
                    toks.append(_Tok("text", raw, text=raw))
            else:
                break
        self.top = True
        return toks

    def tick(self) -> None:
        if self.deadline is not None and time.monotonic() > self.deadline:
            raise TimeoutError("a text took too long to read")

    def quotation(self, src: _S) -> _Tok:
        q = src[:BLOCKQUOTE.match(src.s).end()].rtrim("\n").lines()
        raw, text, kids = _S(), _S(), []
        while q:
            group, started, k = [], False, 0
            while k < len(q):
                if QUOTE_START.match(q[k].s):
                    group.append(q[k])
                    started = True
                elif not started:
                    group.append(q[k])
                else:
                    break
                k += 1
            q = q[k:]
            joined = _join(group)
            inner, last_end = _S(), 0
            for m in QUOTE_UNDERLINE.finditer(joined.s):
                inner = inner + joined[last_end:m.start() + 1] + "    " + joined[m.start(1):m.end(1)]
                last_end = m.end()
            inner = inner + joined[last_end:]
            inner = _join([line[QUOTE_PREFIX.match(line.s).end():] if QUOTE_PREFIX.match(line.s) else line for line in inner.lines()])
            raw = raw + "\n" + joined if raw.s else joined
            text = text + "\n" + inner if text.s else inner
            saved, self.top = self.top, True
            self.blocks(inner, kids, True)
            self.top = saved
            if not q:
                break
            last = kids[-1] if kids else None
            if last is not None and last.kind == "code":
                break
            if last is not None and last.kind in ("blockquote", "list"):
                rest = last.raw + "\n" + _join(q)
                again = self.quotation(rest) if last.kind == "blockquote" else self.listing(rest)
                if again is None:
                    break
                kids[-1] = again
                raw = raw[:len(raw) - len(last.raw)] + again.raw
                if last.kind == "blockquote":
                    text = text[:len(text) - len(last.text)] + again.text
                    break
                text = text[:len(text) - len(last.raw)] + again.raw
                q = rest[len(again.raw):].lines()
        return _Tok("blockquote", raw, text=text, kids=kids)

    def listing(self, src: _S) -> _Tok | None:
        first = LIST.match(src.s)
        if first is None:
            return None
        bullet = first.group(1).strip()
        ordered = len(bullet) > 1
        item_re = re.compile(r"( {0,3}" + ("[0-9]{1,9}" + re.escape(bullet[-1]) if ordered else re.escape(bullet)) + r")((?:[\t ][^\n]*)?(?:\n|\Z))", re.ASCII)
        raw, items, rest = _S(), [], src
        while len(rest):
            self.tick()
            m = item_re.match(rest.s)
            if not m or HR_RULE.match(rest.s):
                break
            mine, rest = rest[:m.end()], rest[m.end():]
            head = _leading_tabs(_item_head(m, mine))
            nxt = rest.s.split("\n", 1)[0]
            empty = not head.s.strip(_SPACES)
            if empty:
                indent, body = len(m.group(1)) + 1, _S()
            else:
                indent = _first_other(m.group(2))
                indent = 1 if indent > 4 else indent
                body = head[indent:]
                indent += len(m.group(1))
            ended = False
            if empty and BLANK.match(nxt):
                mine, rest, ended = mine + nxt + "\n", rest[len(nxt) + 1:], True
            if not ended:
                m_fence, m_head, m_html, m_bullet, m_hr = _stops(indent)
                while len(rest):
                    self.tick()
                    line = rest[:rest.s.find("\n")] if "\n" in rest.s else rest
                    j, wide = line.s, _tabs(line, 4)
                    if m_fence.match(j) or m_head.match(j) or m_html.match(j) or m_bullet.match(j) or m_hr.match(j):
                        break
                    if _first_other(wide.s) >= indent or not j.strip(_SPACES):
                        body = body + "\n" + wide[indent:]
                    else:
                        if empty:
                            break
                        if _first_other(_tabs(head, 4).s) >= 4 or m_fence.match(head.s) or m_head.match(head.s) or m_hr.match(head.s):
                            break
                        body = body + "\n" + line
                    if not empty and not j.strip(_SPACES):
                        empty = True
                    mine, rest, head = mine + line + "\n", rest[len(line) + 1:], wide[indent:]
            items.append(_Tok("item", mine, text=body))
            raw = raw + mine
        if not items:
            return None
        items[-1].raw, items[-1].text = items[-1].raw.rtrim(_SPACES), items[-1].text.rtrim(_SPACES)
        for item in items:
            self.top = False
            item.kids = self.blocks(item.text, [])
        return _Tok("list", raw.rtrim(_SPACES), kids=items)


@functools.lru_cache(maxsize=32)
def _stops(indent: int) -> tuple[re.Pattern, ...]:
    room = f" {{0,{min(3, indent - 1)}}}"
    return (re.compile(room + "(?:```|~~~)", re.ASCII), re.compile(room + "#", re.ASCII), re.compile(room + "<(?:[a-z].*>|!--)", re.ASCII | re.I),
            re.compile(room + r"(?:[*+-]|[0-9]{1,9}[.)])((?:[\t ][^\n]*)?(?:\n|\Z))", re.ASCII),
            re.compile(room + r"(?:(?:- *){3,}|(?:_ *){3,}|(?:\* *){3,})(?:\n+|\Z)", re.ASCII))


def _item_head(m: re.Match, mine: _S) -> _S:
    part = mine[m.start(2):m.end(2)]
    return part[:part.s.find("\n")] if "\n" in part.s else part


def _leading_tabs(line: _S) -> _S:
    n = 0
    while n < len(line.s) and line.s[n] == "\t":
        n += 1
    return _S(" " * (3 * n)) + line[n:]


PARAGRAPH_RULE = _rx(r"(" + PARAGRAPH + r")")


ESCAPE = _rx(r"\\[!\"#$%&'()*+,\-./:;<=>?@\[\]\\^_`{|}~]")
CODESPAN = _rx(r"(`+)([^`]|[^`][\s\S]*?[^`])\1(?!`)")
INLINE_TAG = _rx(r"<!--(?:-?>|[\s\S]*?-->)|</[a-zA-Z][\w:-]*<S>*>|<[a-zA-Z][\w-]*(?:<S>+[a-zA-Z:_][\w.:-]*(?:<S>*=<S>*\"[^\"]*\"|<S>*=<S>*'[^']*'"
                 r"|<S>*=<S>*[^<SC>\"'=<>`]+)?)*?<S>*/?>|<\?[\s\S]*?\?>|<![a-zA-Z]+<S>[\s\S]*?>|<!\[CDATA\[[\s\S]*?\]\]>")
AUTOLINK = _rx(r"<(?:[a-zA-Z][a-zA-Z0-9+.-]{1,31}:[^<SC>\x00-\x1f<>]*|[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
               r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)+(?![-_]))>")
LABEL = r"(?:\[(?:\\<DOT>|[^\[\]\\])*\]|\\<DOT>|`[^`]*`|[^\[\]\\`])*?"
REF = r"(?!<S>*\])(?:\\<DOT>|[^\[\]\\])+"
TITLE = r"\"(?:\\\"?|[^\"\\])*\"|'(?:\\'?|[^'\\])*'|\((?:\\\)?|[^)\\])*\)"
LINK = _rx(r"!?\[(" + LABEL + r")\]\(<S>*(<(?:\\<DOT>|[^\n<>\\])+>|[^<SC>\x00-\x1f]*)(?:<S>+(" + TITLE + r"))?<S>*\)")
REFLINK = _rx(r"!?\[(" + LABEL + r")\]\[(" + REF + r")\]")
NOLINK = _rx(r"!?\[(" + REF + r")\](?:\[\])?")
REFERENCE = _rx(r"!?\[(" + LABEL + r")\]\[(" + REF + r")\]|!?\[(" + REF + r")\](?:\[\])?(?!\()")
BREAK = _rx(r"(?: {2,}|\\)\n(?!<S>*\Z)")
TEXT = _rx(r"(`+|[^`])(?:(?= {2,}\n)|[\s\S]*?(?:(?=[\\<!\[`*_]|\b_|\Z)|[^ ](?= {2,}\n)))")
BLOCK_SKIP = _rx(r"\[[^\[\]]*?\]\((?:\\<DOT>|[^\\\(\)]|\((?:\\<DOT>|[^\\\(\)])*\))*\)|`[^`]*?`|<[^<>]*?>")
BRACKET_ESCAPE = _rx(r"\\([\[\]])")
BLANKS = _rx(f"[{_SP}]+")
MENTION = _rx(r"(?:\A|<S>)@((?:[^<SC>\\]|\\ )+)")
MAYBE = _rx(r"(?<=[*_\[\]])@((?:[^<SC>\\]|\\ )+)")
COMMENT_SPAN = _rx(r"<!--[\s\S]*?-->")


def _balance(href: str) -> int:
    if ")" not in href:
        return -1
    depth, k = 0, 0
    while k < len(href):
        if href[k] == "\\":
            k += 1
        elif href[k] == "(":
            depth += 1
        elif href[k] == ")":
            depth -= 1
            if depth < 0:
                return k
        k += 1
    return -1


class Mention(NamedTuple):
    start: int
    end: int
    token: str
    read: bool


def _bracket_free(label: _S) -> _S:
    out, at = _S(), 0
    for m in BRACKET_ESCAPE.finditer(label.s):
        out = out + label[at:m.start()] + label[m.start(1):m.end(1)]
        at = m.end()
    return out + label[at:]


@functools.cache
def _emphasis_rules() -> tuple[re.Pattern, re.Pattern, re.Pattern, re.Pattern, re.Pattern]:
    category, ranges, start = unicodedata.category, [], None
    for c in range(0x20000):
        found = c not in range(0xD800, 0xE000) and category(chr(c))[0] in "PS"
        if found and start is None:
            start = c
        elif not found and start is not None:
            ranges.append((start, c - 1))
            start = None
    punct = "".join(f"\\U{a:08x}" if a == b else f"\\U{a:08x}-\\U{b:08x}" for a, b in ranges)

    def make(pattern: str) -> re.Pattern:
        pattern = pattern.replace("<NPS>", f"[^{_SP}{punct}]").replace("<PS>", f"[{_SP}{punct}]").replace("<P>", f"[{punct}]")
        return _rx(pattern)
    left = make(r"\*+(?:((?!\*)<P>)|[^<SC>*])|_+(?:((?!_)<P>)|([^<SC>_]))")
    star = make(r"^[^_*]*?__[^_*]*?\*[^_*]*?(?=__)|[^*]+(?=[^*])|(?!\*)<P>(\*+)(?=<S>|\Z)|<NPS>(\*+)(?!\*)(?=<PS>|\Z)|(?!\*)<PS>(\*+)(?=<NPS>)"
                r"|<S>(\*+)(?!\*)(?=<P>)|(?!\*)<P>(\*+)(?!\*)(?=<P>)|<NPS>(\*+)(?=<NPS>)")
    under = make(r"^[^_*]*?\*\*[^_*]*?_[^_*]*?(?=\*\*)|[^_]+(?=[^_])|(?!_)<P>(_+)(?=<S>|\Z)|<NPS>(_+)(?!_)(?=<PS>|\Z)|(?!_)<PS>(_+)(?=<NPS>)"
                 r"|<S>(_+)(?!_)(?=<P>)|(?!_)<P>(_+)(?!_)(?=<P>)")
    return left, star, under, make(r"(?![*_])<PS>"), make(r"\\<P>")


def _masked(lexer: _Lexer, s: str) -> str:
    if lexer.links:
        s = REFERENCE.sub(lambda m: "[" + "a" * (len(m.group()) - 2) + "]" if m.group()[m.group().rfind("[") + 1:-1] in lexer.links else m.group(), s)
    return _emphasis_rules()[4].sub("++", BLOCK_SKIP.sub(lambda m: "[" + "a" * (len(m.group()) - 2) + "]", s))


def _emphasis(s: str, i: int, masked: str, prev: str) -> tuple[int, int, int] | None:
    left, rules, before = _emphasis_rules()[0], _emphasis_rules()[1:3], _emphasis_rules()[3]
    m = left.match(s, i)
    if not m or (m.group(3) and prev.isalnum()) or ((m.group(1) or m.group(2)) and prev and not before.match(prev)):
        return None
    run = len(m.group()) - 1
    depth, more = run, 0
    for k in rules[s[i] == "_"].finditer(masked[i + run:]):
        found = [g for g in k.groups() if g]
        if not found:
            continue
        size, closer = len(found[0]), k.lastindex
        if closer in (3, 4):
            depth += size
            continue
        if closer in (5, 6) and run % 3 and not (run + size) % 3:
            more += size
            continue
        depth -= size
        if depth > 0:
            continue
        size = min(size, size + depth + more)
        total = run + k.start() + 1 + size
        edge = 1 if min(run, size) % 2 else 2
        return i + total, i + edge, i + total - edge
    return None


def _segments(lexer: _Lexer, t: _S, out: list[_S]) -> None:
    s, n, i, run, prev, after_text, masked = t.s, len(t.s), 0, 0, "", False, None
    while i < n:
        lexer.tick()
        if not after_text:
            prev = ""
        after_text, c, end, inner, text = False, s[i], None, None, False
        if (c == "\\" and (m := ESCAPE.match(s, i))) or (c == "<" and (m := INLINE_TAG.match(s, i))):
            end = m.end()
        elif c in "[!":
            end, inner, text = _link(lexer, t, i)
        if end is None and not text:
            if c in "*_":
                masked = _masked(lexer, s) if masked is None else masked
                if found := _emphasis(s, i, masked, prev):
                    end, inner = found[0], t[found[1]:found[2]]
            elif (c == "`" and (m := CODESPAN.match(s, i))) or (c in " \\" and (m := BREAK.match(s, i))) or (c == "<" and (m := AUTOLINK.match(s, i))):
                end = m.end()
        if end is not None:
            if i > run:
                out.append(t[run:i])
            if inner is not None:
                _segments(lexer, inner, out)
            i = run = end
        elif text:
            i += 1
        else:
            i = TEXT.match(s, i).end()
            after_text = True
            if s[i - 1] != "_":
                prev = s[i - 1]
    if n > run:
        out.append(t[run:n])


def _link(lexer: _Lexer, t: _S, i: int) -> tuple[int | None, _S | None, bool]:
    s = t.s
    if m := LINK.match(s, i):
        href, end = m.group(2).strip(_SPACES), m.end()
        if href.startswith("<"):
            if not href.endswith(">") or (len(href) - len(href[:-1].rstrip("\\"))) % 2 == 0:
                m = None
        elif (cut := _balance(m.group(2))) > -1:
            end = i + len(m.group(0)[:(5 if m.group(0).startswith("!") else 4) + len(m.group(1)) + cut].strip(_SPACES))
        if m:
            return end, None if s[i] == "!" else _bracket_free(t[m.start(1):m.end(1)]), False
    if m := REFLINK.match(s, i) or NOLINK.match(s, i):
        if BLANKS.sub(" ", (m.group(2) if m.re.groups > 1 else "") or m.group(1)).lower() in lexer.links:
            return m.end(), None if s[i] == "!" else _bracket_free(t[m.start(1):m.end(1)]), False
        return None, None, True
    return None, None, False


def _path_of(token: str) -> str | None:
    path = token.split("#", 1)[0].replace("\\ ", " ")
    if path and (path.startswith(("./", "~/")) or (path.startswith("/") and path != "/")
                 or (not path.startswith("@") and not re.match(r"[#%^&*()]+", path) and re.match(r"[a-zA-Z0-9._-]", path))):
        return path
    return None


def _scan(text: _S, doc: str, found: dict[tuple[int, int], Mention], pattern: re.Pattern, read: bool) -> None:
    for m in pattern.finditer(text.s):
        if _path_of(m.group(1)) is None:
            continue
        start, end = text.at(m.start(1) - 1), text.at(m.end(1) - 1) + 1
        joined = end - start != len(m.group(1)) + 1
        if start < 0 or end <= start or doc[start] != "@" or doc[end - 1] != m.group(1)[-1] or (not joined and doc[start:end] != "@" + m.group(1)):
            raise ValueError("a path was not found where the lexer put it")
        if (start, end) not in found or (read and not found[start, end].read):
            found[start, end] = Mention(start, end, m.group(1), read)


def _walk(lexer: _Lexer, toks: list[_Tok], doc: str, found: dict[tuple[int, int], Mention]) -> None:
    for t in toks:
        if t.kind == "code":
            continue
        if t.kind == "html":
            raw = t.raw
            if raw.s.lstrip(_SPACES).startswith("<!--") and "-->" in raw.s.lstrip(_SPACES):
                rest, at = _S(), 0
                for m in COMMENT_SPAN.finditer(raw.s):
                    rest, at = rest + raw[at:m.start()], m.end()
                rest = rest + raw[at:]
                if rest.s.strip(_SPACES):
                    _scan(rest, doc, found, MENTION, True)
        elif t.kind in ("paragraph", "heading", "text"):
            if t.kind == "text":
                _scan(t.text, doc, found, MENTION, True)
            parts: list[_S] = []
            _segments(lexer, t.text, parts)
            for part in parts:
                _scan(part, doc, found, MENTION, True)
                _scan(part, doc, found, MAYBE, False)
        elif t.kind == "blockquote":
            _walk(lexer, t.kids, doc, found)
        elif t.kind == "list":
            for item in t.kids:
                _walk(lexer, item.kids, doc, found)


CLOSERS = {"h1": None, "h2": "-->", "h3": "?>", "h4": ">", "h5": "]]>"}


def mentions(text: str, deadline: float | None = None) -> tuple[list[Mention], str]:
    lexer = _Lexer(text, deadline)
    found: dict[tuple[int, int], Mention] = {}
    _walk(lexer, lexer.tokens, text, found)
    last = lexer.tokens[-1] if lexer.tokens else None
    closing = ""
    if last is not None and last.kind == "code" and not last.closed:
        closing = last.fence
    elif last is not None and last.kind == "html" and last.html in CLOSERS:
        raw = last.raw.s
        closed = {"h1": re.search(r"</(script|pre|style|textarea)>", raw, re.I) is not None, "h2": "-->" in raw[4:] or raw.startswith(("<!-->", "<!--->")),
                  "h3": "?>" in raw[2:], "h4": ">" in raw[2:], "h5": "]]>" in raw}[last.html]
        if not closed:
            closing = CLOSERS[last.html] or "</" + re.search(r"<(\w+)", raw).group(1).lower() + ">"
    return list(found.values()), closing


def _ticks(text: str) -> int:
    lengths = {len(run) for run in re.findall(r"`+", text)}
    n = 1
    while n in lengths:
        n += 1
    return n


def _span(raw: str, n: int) -> str:
    pad = " " if raw.startswith("`") or raw.endswith("`") else ""
    return "`" * n + pad + raw + pad + "`" * n


TRAILING = re.compile(r"[.,;:!?)\]}*_]+\Z")


def _spans(found: list[Mention]) -> list[tuple[int, int]]:
    spans = []
    for m in sorted(found):
        end = m.end
        if tail := TRAILING.search(m.token):
            end -= min(len(tail.group()), len(m.token) - 1)
        spans.append((m.start, end))
    out: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def _wrap(text: str, found: list[Mention], closing: str) -> str:
    n = _ticks(text)
    out, at = [], 0
    for start, end in _spans(found):
        out += [text[at:start], " " if text[start - 1:start] == "`" else "", _span(text[start:end], n), " " if text[end:end + 1] == "`" else ""]
        at = end
    out.append(text[at:])
    result = "".join(out)
    return result + ("" if not closing or result.endswith("\n") else "\n") + closing


BLUNT = _rx(r"(?<![A-Za-z0-9])@(?=<NS>)")


def _blunt(text: str) -> str:
    out, at = [], 0
    for m in BLUNT.finditer(text):
        slashes = 0
        while slashes < m.start() and text[m.start() - 1 - slashes] == "\\":
            slashes += 1
        if slashes % 2 == 0:
            out += [text[at:m.start()], "\\"]
            at = m.start()
    out.append(text[at:])
    return "".join(out)


def bare(text: str, deadline: float | None = None) -> bool:
    if "<!--" not in text:
        return not text.strip()
    try:
        for t in _Lexer(text, deadline).tokens:
            raw = t.raw.s.lstrip(_SPACES)
            if t.kind != "space" and not (t.kind == "html" and raw.startswith("<!--") and "-->" in raw and not COMMENT_SPAN.sub("", raw).strip(_SPACES)):
                return False
    except Exception:
        return False
    return True


def sealed(text: str, deadline: float | None = None) -> tuple[str, list[str]]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    try:
        found, closing = mentions(text, deadline)
        out = _wrap(text, found, closing)
        if out != text and any(m.read for m in mentions(out, deadline)[0]):
            raise ValueError("an import survived its wrapping")
        return out, [m.token for m in found if m.read]
    except Exception:
        return _blunt(text), []


TEXT_EXTENSIONS = frozenset({
    ".md", ".txt", ".text", ".json", ".yaml", ".yml", ".toml", ".xml", ".csv", ".html", ".htm", ".css", ".scss", ".sass", ".less", ".js",
    ".ts", ".tsx", ".jsx", ".mjs", ".cjs", ".mts", ".cts", ".py", ".pyi", ".pyw", ".rb", ".erb", ".rake", ".go", ".rs", ".java", ".kt",
    ".kts", ".scala", ".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hxx", ".cs", ".swift", ".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat",
    ".cmd", ".env", ".ini", ".cfg", ".conf", ".config", ".properties", ".sql", ".graphql", ".gql", ".proto", ".vue", ".svelte", ".astro",
    ".ejs", ".hbs", ".pug", ".jade", ".php", ".pl", ".pm", ".lua", ".r", ".dart", ".ex", ".exs", ".erl", ".hrl", ".clj", ".cljs", ".cljc",
    ".edn", ".hs", ".lhs", ".elm", ".ml", ".mli", ".f", ".f90", ".f95", ".for", ".cmake", ".make", ".makefile", ".gradle", ".sbt", ".rst",
    ".adoc", ".asciidoc", ".org", ".tex", ".latex", ".lock", ".log", ".diff", ".patch",
})


def import_path(token: str, base: str | Path) -> Path | None:
    path = _path_of(token)
    if path is None:
        return None
    path = path.strip(_SPACES)
    if path.startswith("~/") or os.path.isabs(path):
        if path.endswith("/"):
            return None
        home = os.path.expanduser("~")
        if path.startswith("~/") and home == "~":
            return None
        full = os.path.join(home, path[2:]) if path.startswith("~/") else path
    else:
        full = os.path.join(str(base), path)
    return Path(unicodedata.normalize("NFC", os.path.normpath(full)))


def _text_or_empty(path: Path) -> str:
    try:
        return read_text(path)
    except (OSError, UnicodeDecodeError):
        return ""


def _read(path: str) -> str:
    try:
        with open(path, "rb") as f:
            data = f.read(4 * PROJECT_MEMORY_CHARS + READ_SLACK)
        return codecs.getincrementaldecoder("utf-8")().decode(data).replace("\r\n", "\n").replace("\r", "\n")
    except (OSError, UnicodeDecodeError):
        return ""


def _unquote(item: str) -> str:
    item = item.strip()
    return item[1:-1] if len(item) > 1 and item[0] == item[-1] and item[0] in "'\"" else item


def _split(value: str) -> list[str]:
    out, depth, item = [], 0, ""
    for c in value:
        depth += (c == "{") - (c == "}")
        if c == "," and depth == 0:
            out.append(item)
            item = ""
        else:
            item += c
    return [_unquote(p) for p in [*out, item] if p.strip()]


def _globs(front: str) -> list[str]:
    lines = front.split("\n")
    for k, line in enumerate(lines):
        if not (m := re.match(r"paths[ \t]*:[ \t]*(.*)", line)):
            continue
        value = m.group(1).strip()
        if value[:1] not in ("'", '"'):
            value = re.sub(r"(^|[ \t]+)#.*", "", value).strip()
        if value:
            items = _split(_unquote(value[1:-1] if value.startswith("[") and value.endswith("]") else value))
        else:
            items = []
            for below in lines[k + 1:]:
                if item := re.match(r"[ \t]*-[ \t]+(.*)", below):
                    items.append(_unquote(re.sub(r"[ \t]+#.*", "", item.group(1))))
                elif below.strip() and not below.lstrip().startswith("#"):
                    break
        patterns = [i[:-3] if i.endswith("/**") else i for i in items]
        return [] if all(p == "**" for p in patterns) else items
    return []


def _front(raw: str) -> tuple[str, str]:
    m = FRONT.match(raw)
    return (raw[m.end():], m.group(1)) if m else (raw, "")


class Section(NamedTuple):
    heading: str
    path: Path | None
    text: str


class _Walk:
    def __init__(self, board: Board):
        self.board = board
        self.root = os.path.realpath(board.root)
        self.seen: set[str] = set()
        self.parts: list[Section] = []
        self.deadline = time.monotonic() + WORK_S
        places = {p for p in {*policy.protected(board), str(providers.CONFIG_DIR), *map(os.path.expanduser, policy.CLOSED_TO_LINKS)} if os.path.isabs(p)}
        self.barred = [p for p in places | {os.path.realpath(p) for p in places} if not policy.inside(self.root, p)]

    def text(self, heading: str, path: Path | None, text: str, base: str, depth: int, name: str, notice: str = "") -> None:
        body, tokens = sealed(text, self.deadline)
        self.parts.append(Section(sealed(f"# {heading}", self.deadline)[0][2:], path, body + notice))
        for token in tokens:
            if (target := import_path(token, base)) is not None:
                self.file(target, depth + 1, name)

    def file(self, path: Path, depth: int, parent: str | None, rule: bool = False) -> None:
        if depth >= IMPORT_DEPTH:
            return
        real = os.path.realpath(path)
        if policy.fold(real) in self.seen:
            return
        self.seen.add(policy.fold(real))
        ext = os.path.splitext(path.name)[1].lower()
        if (ext and ext not in TEXT_EXTENSIONS) or (parent is not None and not policy.inside(real, self.root)):
            return
        if any(policy.inside(real, p) for p in self.barred) or not os.path.isfile(real):
            return
        body, front = _front(_read(real))
        if rule and _globs(front):
            return
        body = body.strip()
        if bare(body, self.deadline):
            return
        shown = path if parent is None else self.shown(path, real)
        name = shown.relative_to(self.board.root).as_posix()
        heading = f"The project's {name}"
        if parent is not None:
            heading += f" (imported by {parent})"
        cut = body[:PROJECT_MEMORY_CHARS]
        notice = f"\n[... cut at {PROJECT_MEMORY_CHARS} characters]" if len(body) > len(cut) else ""
        self.text(heading, shown, cut, os.path.dirname(real), depth, name, notice)

    def shown(self, path: Path, real: str) -> Path:
        for found in (os.path.normpath(path), real):
            rel = os.path.relpath(found, self.root)
            if rel != ".." and not rel.startswith(".." + os.sep):
                return self.board.root / rel
        return self.board.root / os.path.relpath(real, self.root)


def _sources(board: Board) -> list[tuple[Path, bool]]:
    top = board.root / ".claude" / "rules"
    rules, visited = [], set()

    def walk(folder: Path) -> None:
        real = os.path.realpath(folder)
        if real in visited:
            return
        visited.add(real)
        try:
            names = sorted(os.listdir(real))
        except OSError:
            return
        for name in names:
            if (folder / name).is_dir():
                walk(folder / name)
            elif name.endswith(".md") and (folder / name).is_file():
                rules.append(folder / name)

    walk(top)
    return [(board.root / "CLAUDE.md", False), (board.root / ".claude" / "CLAUDE.md", False),
            *((p, True) for p in sorted(rules, key=lambda p: p.relative_to(top).as_posix()))]


def instructions(board: Board) -> tuple[str, str]:
    for where, path in (("team", board.dir / INSTRUCTIONS), ("default", providers.CONFIG_DIR / INSTRUCTIONS)):
        text = _text_or_empty(path).strip()
        if text:
            return text, where
    return "", "none"


def sections(board: Board) -> list[Section]:
    walk = _Walk(board)
    text, where = instructions(board)
    if text:
        walk.text(f"Instructions for team {board.cfg['team']} ({where} instructions)", None, text, walk.root, 0, f"the {where} instructions")
    if board.cfg.get("project_memory") is not False:
        for path, rule in _sources(board):
            walk.file(path, 0, None, rule)
    return walk.parts


def project_memory(board: Board) -> list[tuple[Path, str]]:
    return [(s.path, s.text) for s in sections(board) if s.path is not None]


def memory(board: Board) -> str:
    parts = [f"# {s.heading}\n\n{s.text}" for s in sections(board)]
    return "\n\n".join(parts) + ("\n" if parts else "")


def write_memory(board: Board) -> None:
    path = board.dir / "claude" / "CLAUDE.md"
    text = memory(board)
    if text:
        path.parent.mkdir(parents=True, exist_ok=True)
        if _text_or_empty(path) != text:
            path.write_text(text, encoding="utf-8")
    else:
        path.unlink(missing_ok=True)
