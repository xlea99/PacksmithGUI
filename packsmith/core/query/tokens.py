"""Tokenising registry ids and display names (design 5.3).

The hardest part of the stone problem isn't applying a change, it's **finding the id**:
`alexscaves:galena` has stairs and a slab but no wall, yet `quark:ac_galena_wall` exists.
Substring matching fails on that — the `ac_` infix and the namespace break it — which is
why §5.3 specifies token matching instead:

    id MATCHES_TOKENS("galena", "wall")   ->  quark:ac_galena_wall

Two rules, and both are chosen for a reason measured against a real 304-mod pack:

**Split on every separator, keep the namespace.** `quark:ac_galena_wall` becomes
{quark, ac, galena, wall}. Keeping the namespace as an ordinary token means "quark" is a
searchable term for free, and costs nothing, because matching asks whether the wanted
tokens are *present*, not whether the sets are equal.

**Singularise symmetrically.** This one is not cosmetic. In the reference pack `brick`
appears 978 times and `bricks` 550, and the six candidates for a granite brick wall are
split across both spellings — so exact token matching finds 1 of 6 or 5 of 6 depending on
which the user happens to type, and never all six. Normalising both sides finds all six
either way.

The rule is deliberately naive (drop a trailing "s"), and its correctness does not depend
on being linguistically right — only on being applied *identically* to the query and the
data. `glass` -> `glas` on both sides still matches itself. Erring toward more candidates
is also the safe direction here: §5.3 is explicit that a recommendation "is an *open hint*
— it must never restrict, or you lock yourself out of exactly the weirdness that makes the
problem hard."
"""
import re

_SEPARATORS = re.compile(r"[^0-9a-z]+")


def normalise(token: str) -> str:
    """One token, lowercased and singularised. Applied to both sides of a match."""
    token = token.lower()
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def tokenize(text) -> set:
    """The normalised token set of an id or phrase.

    ``quark:ac_galena_wall`` and ``"Galena Wall"`` both reach {galena, wall} (plus the
    namespace), which is what lets a recommendation query be written against either.
    """
    if text is None:
        return set()
    return {normalise(part) for part in _SEPARATORS.split(str(text).lower()) if part}


def wanted_tokens(wanted) -> list:
    """Split a query side into tokens, preserving a trailing ``*`` as a prefix marker.

    ``tokenize`` deliberately drops punctuation, so the marker has to survive separately.
    """
    terms = [wanted] if isinstance(wanted, str) else list(wanted)
    out = []
    for term in terms:
        for part in str(term).split():
            prefix = part.endswith("*")
            core = part[:-1] if prefix else part
            for token in tokenize(core):
                out.append((token, prefix))
    return out


def matches_tokens(text, wanted) -> bool:
    """Does ``text`` contain every wanted token? Extra tokens are fine — that is the
    point; `quark:ac_galena_wall` carries `quark` and `ac` and still matches.

    A token ending in ``*`` matches by prefix. That exists because a *finished* word and a
    *half-typed* one want opposite things, and the difference is stark in real data: in the
    reference pack `stone` is 228 blocks but `stone*` is 1907 (it swallows `stonezone` and
    `stonework`), while `granit` is **0** and `granit*` is 96. Exact-only makes a search bar
    look broken until the last keystroke; prefix-only makes finished words useless. So the
    caller says which it means, and the filter bar marks only the word still under the
    cursor.
    """
    have = tokenize(text)
    for token, prefix in wanted_tokens(wanted):
        if prefix:
            if not any(t.startswith(token) for t in have):
                return False
        elif token not in have:
            return False
    return True
