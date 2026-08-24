"""The filter-bar language — text in, AST out, and back again (design 3.2.3, 3.2.4).

This is the **surface**, not the engine. §3.2.3: *"The filter bar is a user-facing language
that lowers into the query engine's AST — it is not itself the engine. Everything typed
here (including the `t:`/`a:`/`b:` shorthand) is builder-side sugar; the engine only ever
sees the fully-resolved nodes."*

Two directions, and the second is the one with teeth:

* :func:`parse` lowers text into AST nodes.
* :func:`format` lifts AST nodes back into text.

The round trip is a hard requirement, not a nicety. §3.2.4 rejected SQL partly *because*
arbitrary SQL cannot be lifted back into a visual builder — *"the two-way binding dies."*
Everything here has to survive being printed and re-parsed, which is why the sugar is kept
deliberately shallow and why every desugaring below has a matching pattern in the printer.

The grammar::

    filter     := or
    or         := and (OR and)*
    and        := unary (AND unary)*
    unary      := NOT unary | primary
    primary    := '(' filter ')' | HAS field | field op value | bare
    field      := 'id' | 'mod' | 't:'NAME | 'a:'NAME | 'b:'PATH
    op         := == != > < >= <= IN 'NOT IN' CONTAINS MATCHES MATCHES_TOKENS ~
    value      := STRING | NUMBER | TRUE | FALSE | '(' value, ... ')'
    bare       := WORD+ | STRING

**Bare words are the candidate finder.** Typing `granite brick wall` with no operator
lowers to a token match across the entry's id *and* its display name — so the most common
gesture in the bar does the most valuable thing (design 5.3), finding
`quark:granite_bricks_wall` regardless of word order or plurals. Precedence is the usual
``NOT`` > ``AND`` > ``OR``.
"""
import re

from packsmith.core.query.ast import (
    And, Attribute, BoundIn, Cmp, Has, Id, Mentions, Mod, Not, Or, Slot, Tag, QueryError,
    _Id, _Mod,
)
from packsmith.core.query.tokens import tokenize

# Spelled both ways on purpose (a user decision): `~` to type, MATCHES_TOKENS to read. The
# printer always emits the long form, so a saved query stays self-documenting.
_WORD_OPS = {
    "contains": "contains", "matches": "matches", "matches_tokens": "matches_tokens",
    "in": "in",
}
# The case-insensitive spellings of the text operators; they carry ci=True.
_CI_WORD_OPS = {"contains_i": "contains", "matches_i": "matches",
                "matches_tokens_i": "matches_tokens"}
_SYMBOL_OPS = {"==": "eq", "!=": "neq", ">=": "gte", "<=": "lte", ">": "gt", "<": "lt",
               "~": "matches_tokens"}
_OP_TEXT = {"eq": "==", "neq": "!=", "gte": ">=", "lte": "<=", "gt": ">", "lt": "<",
            "contains": "CONTAINS", "matches": "MATCHES",
            "matches_tokens": "MATCHES_TOKENS", "in": "IN", "not_in": "NOT IN"}
_KEYWORDS = {"and", "or", "not", "has", "in", "contains", "matches", "matches_tokens",
             "mentions", "bound_in",
             "true", "false"} | set(_CI_WORD_OPS)

# `Cmp.ci` is engine-legal but had no spelling, so `format` silently dropped it and a
# case-insensitive filter came back case-sensitive — quietly changing which rows match,
# including through the ⚙ dialog's text fallback.
#
# Spelled as a distinct operator (`CONTAINS_I`) rather than a `/i` suffix: `_WORD` swallows
# `/` on purpose, because ids look like `minecraft:worldgen/biome`, so `CONTAINS/i` would
# lex as a single word. One more keyword costs nothing and round-trips cleanly.
_CI_OPS = {"contains": "CONTAINS_I", "matches": "MATCHES_I",
           "matches_tokens": "MATCHES_TOKENS_I"}
_LIST_OPS = {"in", "not_in"}

# A bare word: ids carry colons, dots, slashes and dashes, so they all belong in one token.
# A leading `@` makes it a recommendation-template parameter (`@slot`, `@b:base_block`) —
# still just a token as far as the grammar is concerned, which is what keeps a template
# plain serializable data (see query.template).
_WORD = re.compile(r"[@A-Za-z0-9_][A-Za-z0-9_:/.\-*+#]*")
_NUMBER = re.compile(r"-?\d+(\.\d+)?$")


class QuerySyntaxError(QueryError):
    """Malformed filter text, with the offset so the bar can point at it."""

    def __init__(self, message, position=None, text=None):
        self.position = position
        self.text = text
        if position is not None and text is not None:
            message = f"{message}\n  {text}\n  {' ' * position}^"
        super().__init__(message)


# --- lexing -----------------------------------------------------------------

class _Token:
    __slots__ = ("kind", "value", "pos")

    def __init__(self, kind, value, pos):
        self.kind, self.value, self.pos = kind, value, pos

    def __repr__(self):
        return f"<{self.kind} {self.value!r}>"


def _lex(text):
    tokens, i, n = [], 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch in "()," :
            tokens.append(_Token(ch, ch, i))
            i += 1
            continue
        if ch in "\"'":
            # Backslash escaping, so a value containing the quote character is sayable at
            # all. Without it `format` emits a string the tokenizer then cuts short, which
            # breaks the round-trip the ⚙ dialog relies on to edit a filter as text.
            chars, j = [], i + 1
            while j < len(text) and text[j] != ch:
                if text[j] == "\\" and j + 1 < len(text):
                    j += 1                      # take the next character literally
                chars.append(text[j])
                j += 1
            if j >= len(text):
                raise QuerySyntaxError("unterminated string", i, text)
            tokens.append(_Token("string", "".join(chars), i))
            i = j + 1
            continue
        two = text[i:i + 2]
        if two in _SYMBOL_OPS:
            tokens.append(_Token("op", _SYMBOL_OPS[two], i))
            i += 2
            continue
        if ch in _SYMBOL_OPS:
            tokens.append(_Token("op", _SYMBOL_OPS[ch], i))
            i += 1
            continue
        if ch == "=":
            raise QuerySyntaxError("use '==' to compare", i, text)
        match = _WORD.match(text, i)
        if not match:
            raise QuerySyntaxError(f"unexpected character {ch!r}", i, text)
        word = match.group(0)
        lowered = word.lower()
        kind = "keyword" if lowered in _KEYWORDS else "word"
        tokens.append(_Token(kind, lowered if kind == "keyword" else word, i))
        i = match.end()
    return tokens


# --- parsing ----------------------------------------------------------------

class _Parser:
    def __init__(self, text, typing=False):
        self.text = text
        self.tokens = _lex(text)
        self.i = 0
        # While typing, the final word is half-written and should match as a prefix — see
        # tokens.matches_tokens for why that can't just be the rule everywhere. A trailing
        # space means the word is finished, so the marker is dropped.
        self.typing = typing and bool(text) and not text[-1].isspace()

    # -- helpers
    def peek(self, offset=0):
        index = self.i + offset
        return self.tokens[index] if index < len(self.tokens) else None

    def next(self):
        token = self.peek()
        self.i += 1
        return token

    def at_keyword(self, *words):
        token = self.peek()
        return token is not None and token.kind == "keyword" and token.value in words

    def fail(self, message, token=None):
        token = token or self.peek()
        raise QuerySyntaxError(message, token.pos if token else len(self.text), self.text)

    # -- grammar
    def parse(self):
        if not self.tokens:
            return None
        node = self.parse_or()
        if self.peek() is not None:
            self.fail(f"unexpected {self.peek().value!r}")
        return node

    def parse_or(self):
        clauses = [self.parse_and()]
        while self.at_keyword("or"):
            self.next()
            clauses.append(self.parse_and())
        return clauses[0] if len(clauses) == 1 else Or(clauses)

    def parse_and(self):
        clauses = [self.parse_unary()]
        while self.at_keyword("and"):
            self.next()
            clauses.append(self.parse_unary())
        return clauses[0] if len(clauses) == 1 else And(clauses)

    def parse_unary(self):
        # `NOT IN` belongs to the comparison, not here — only NOT-as-negation gets this far.
        if self.at_keyword("not") and not self._not_starts_an_operator():
            self.next()
            return Not(self.parse_unary())
        return self.parse_primary()

    def _not_starts_an_operator(self):
        after = self.peek(1)
        return after is not None and after.kind == "keyword" and after.value == "in"

    def parse_primary(self):
        token = self.peek()
        if token is None:
            self.fail("expected a condition")
        if token.kind == "(":
            self.next()
            node = self.parse_or()
            if self.peek() is None or self.peek().kind != ")":
                self.fail("expected ')'")
            self.next()
            return node
        if self.at_keyword("has"):
            self.next()
            return Has(self.parse_field())
        if self.at_keyword("mentions", "bound_in"):
            return self.parse_crossing()
        if self._starts_comparison():
            return self.parse_comparison()
        return self.parse_bare()

    def _starts_comparison(self):
        """A field reference followed by an operator. One token of lookahead is all it
        takes to tell `mod == quark` from the bare phrase `mod quark`."""
        token = self.peek()
        if token is None or token.kind not in ("word", "keyword"):
            return False
        if not _looks_like_field(token.value):
            return False
        after = self.peek(1)
        if after is None:
            return False
        if after.kind == "op":
            return True
        # Derived from the operator tables rather than re-listed: a spelling added there
        # and forgotten here parses as a bare phrase instead, which fails as a confusing
        # "unexpected 'contains_i'" rather than as an unknown operator.
        return after.kind == "keyword" and after.value in (
            set(_WORD_OPS) | set(_CI_WORD_OPS) | {"not"})

    def parse_comparison(self):
        field = self.parse_field()
        ci = False
        token = self.next()
        if token.kind == "op":
            op = token.value
        elif token.kind == "keyword" and token.value == "not":
            following = self.next()
            if following is None or following.kind != "keyword" or following.value != "in":
                self.fail("expected 'IN' after 'NOT'", following)
            op = "not_in"
        elif token.kind == "keyword" and token.value in _WORD_OPS:
            op = _WORD_OPS[token.value]
        elif token.kind == "keyword" and token.value in _CI_WORD_OPS:
            op, ci = _CI_WORD_OPS[token.value], True
        else:
            self.fail("expected a comparison operator", token)
        return Cmp(field, op, self.parse_value(op), ci=ci)

    def parse_field(self):
        token = self.next()
        if token is None or token.kind not in ("word", "keyword"):
            self.fail("expected a field", token)
        return _field_from(token.value, lambda m: self.fail(m, token))

    def parse_value(self, op):
        token = self.peek()
        if token is None:
            self.fail("expected a value")
        if op in _LIST_OPS:
            if token.kind != "(":
                self.fail("expected a list, e.g. (\"early\", \"late\")")
            self.next()
            values = []
            while True:
                values.append(self._scalar())
                separator = self.peek()
                if separator is not None and separator.kind == ",":
                    self.next()
                    continue
                break
            if self.peek() is None or self.peek().kind != ")":
                self.fail("expected ')'")
            self.next()
            return values
        if op == "matches_tokens":
            # A phrase or a list, both lowering to the same token set.
            if token.kind == "(":
                self.next()
                words = []
                while True:
                    words.append(str(self._scalar()))
                    separator = self.peek()
                    if separator is not None and separator.kind == ",":
                        self.next()
                        continue
                    break
                if self.peek() is None or self.peek().kind != ")":
                    self.fail("expected ')'")
                self.next()
                return canonical_tokens(words)
            return canonical_tokens([self._scalar()])
        return self._scalar()

    def _scalar(self):
        token = self.next()
        if token is None:
            self.fail("expected a value")
        if token.kind == "string":
            return token.value
        if token.kind == "keyword" and token.value in ("true", "false"):
            return token.value == "true"
        if token.kind == "word":
            if _NUMBER.match(token.value):
                return float(token.value) if "." in token.value else int(token.value)
            return token.value
        self.fail("expected a value", token)

    def parse_crossing(self):
        """``MENTIONS "StoneType"`` / ``BOUND_IN "StoneType"``, each taking an optional
        second argument in the list form the language already uses for arguments:

            MENTIONS "StoneType"                      instance names only
            MENTIONS ("StoneType", "search_terms")    plus an alias slot
            BOUND_IN "StoneType"                      bound anywhere
            BOUND_IN ("StoneType", "pillar.base")     bound in that one slot

        A whole condition rather than a field with an operator, because there is nothing to
        compare — the answer is already yes or no, the same shape as HAS.
        """
        node = BoundIn if self.next().value == "bound_in" else Mentions
        token = self.peek()
        if token is not None and token.kind == "(":
            self.next()
            args = [str(self._scalar())]
            if self.peek() is not None and self.peek().kind == ",":
                self.next()
                args.append(str(self._scalar()))
            if self.peek() is None or self.peek().kind != ")":
                self.fail("expected ')'")
            self.next()
            if len(args) > 2:
                self.fail("expected at most a blueprint and one slot")
            return node(*args)
        return node(str(self._scalar()))

    def parse_bare(self):
        """A run of words with no operator: the candidate finder (design 5.3).

        Greedy up to the next keyword, operator or bracket, so `granite brick wall` is ONE
        term of three tokens rather than three terms — which is what makes it find
        `quark:granite_bricks_wall` instead of anything containing "wall".
        """
        words = []
        while True:
            token = self.peek()
            if token is None or token.kind in ("(", ")", ",", "op"):
                break
            if token.kind == "keyword":
                break
            words.append(str(self.next().value))
        if not words:
            self.fail("expected a condition")
        if not tokenize(" ".join(words)):
            self.fail("expected a condition")
        if (self.typing and self.peek() is None and not words[-1].endswith("*")
                and not words[-1].startswith("@")):
            words = words[:-1] + [words[-1] + "*"]
        return _bare(words)


def _looks_like_field(word) -> bool:
    lowered = str(word).lower()
    return lowered in ("id", "mod") or lowered[:2] in ("t:", "a:", "b:")


def _field_from(word, fail):
    lowered = str(word).lower()
    if lowered == "id":
        return Id
    if lowered == "mod":
        return Mod
    prefix, _, name = str(word).partition(":")
    if not name:
        fail(f"'{word}' is not a field — use id, mod, t:tag, a:attribute or b:slot")
    kind = prefix.lower()
    if kind == "t":
        return Tag(name)
    if kind == "a":
        return Attribute(name)
    if kind == "b":
        return Slot(name)
    fail(f"'{word}' is not a field — use id, mod, t:tag, a:attribute or b:slot")


def canonical_tokens(words) -> list:
    """Sorted, de-duplicated, phrase-split — the canonical form of a token list.

    `granite brick`, `"granite brick"` and `("brick", "granite")` are the same query, and
    matching is set-based, so they must all lower to the *same* AST. Otherwise two
    identical queries serialize differently in saved Views and nothing compares equal.

    Spelling is deliberately preserved: `bricks` stays `bricks`. Singularisation happens
    at match time (see ``query.tokens``), where it belongs — printing back a word the user
    didn't type would be a surprise.
    """
    parts = []
    for word in words:
        parts.extend(str(word).split())
    return sorted({p for p in parts if p})


def _bare(words):
    """Bare words search the id AND the display name — an entry is worth finding by either
    name it goes by."""
    tokens = canonical_tokens(words)
    return Or([Cmp(Id, "matches_tokens", tokens),
               Cmp(Attribute("localization"), "matches_tokens", tokens)])


def _as_bare(node):
    """Recognise the shape :func:`_bare` produces, so the printer can lift it back. This
    pairing is the round trip: every desugaring above has its match here."""
    if not isinstance(node, Or) or len(node.clauses) != 2:
        return None
    left, right = node.clauses
    if not (isinstance(left, Cmp) and isinstance(right, Cmp)):
        return None
    if left.op != "matches_tokens" or right.op != "matches_tokens":
        return None
    if not (isinstance(left.field, _Id) and isinstance(right.field, Attribute)
            and right.field.name == "localization"):
        return None
    return list(left.value) if list(left.value) == list(right.value) else None


def parse(text, *, typing=False):
    """Filter text -> AST, or None for an empty bar.

    ``typing=True`` is what a live filter bar passes: the word still under the cursor is
    matched as a prefix, so `granit` finds granite instead of finding nothing. Finished
    words (anything before a space) always match exactly. The marker is a real ``*`` in the
    resulting AST rather than hidden state, so what you filtered by is what gets saved.
    """
    return _Parser(text or "", typing=typing).parse()


# --- printing ---------------------------------------------------------------

_PRECEDENCE = {Or: 1, And: 2}


def format(node) -> str:
    """AST -> filter text. The inverse of :func:`parse` for anything parse produced."""
    if node is None:
        return ""
    return _format(node, 0)


def _format(node, parent_precedence):
    bare = _as_bare(node)
    if bare is not None:
        return " ".join(_quote_word(w) for w in bare)
    if isinstance(node, Or):
        return _join(node.clauses, " OR ", _PRECEDENCE[Or], parent_precedence)
    if isinstance(node, And):
        return _join(node.clauses, " AND ", _PRECEDENCE[And], parent_precedence)
    if isinstance(node, Not):
        inner = node.clause
        if isinstance(inner, Has):
            return f"NOT HAS {_field_text(inner.field)}"
        return f"NOT {_format(inner, 3)}"
    if isinstance(node, Has):
        return f"HAS {_field_text(node.field)}"
    if isinstance(node, (BoundIn, Mentions)):
        return _crossing_text(node)
    if isinstance(node, Cmp):
        ci = getattr(node, "ci", False)
        operator = (_CI_OPS[node.op] if ci and node.op in _CI_OPS
                    else _OP_TEXT[node.op])
        return f"{_field_text(node.field)} {operator} {_value_text(node)}"
    raise QueryError(f"cannot format {node!r}")


def _crossing_text(node) -> str:
    keyword = "BOUND_IN" if isinstance(node, BoundIn) else "MENTIONS"
    second = node.slot if isinstance(node, BoundIn) else node.alias_slot
    if second:
        return f'{keyword} ("{node.blueprint}", "{second}")'
    return f'{keyword} "{node.blueprint}"'


def _join(clauses, separator, precedence, parent_precedence):
    text = separator.join(_format(c, precedence) for c in clauses)
    return f"({text})" if precedence < parent_precedence else text


def _field_text(field) -> str:
    if isinstance(field, _Id):
        return "id"
    if isinstance(field, _Mod):
        return "mod"
    if isinstance(field, Tag):
        return f"t:{field.name}"
    if isinstance(field, Attribute):
        return f"a:{field.name}"
    if isinstance(field, Slot):
        return f"b:{field.name}"
    raise QueryError(f"cannot format field {field!r}")


def _value_text(node) -> str:
    value = node.value
    if node.op in _LIST_OPS:
        return "(" + ", ".join(_scalar_text(v) for v in value) + ")"
    if node.op == "matches_tokens":
        return "(" + ", ".join(_scalar_text(str(v)) for v in value) + ")"
    return _scalar_text(value)


def _scalar_text(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _quote_word(word) -> str:
    return word if _WORD.fullmatch(word) and word.lower() not in _KEYWORDS else f'"{word}"'
