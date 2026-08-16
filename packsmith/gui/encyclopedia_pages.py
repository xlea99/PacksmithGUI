"""Content for the Encyclopedia Packsmithia — the in-app guide.

Pages are **data**, not widgets: an id, a title, and a body of simple HTML. That keeps the
reader (`encyclopedia.py`) ignorant of what it renders, and it means a page can be linked
to from anywhere in the app by id — which is the whole point of the ? beside the filter bar.

Most of this is a skeleton. Exactly one page is written for real — `query-language` —
because a guide's shape is only worth judging against one finished page, and a language
reference is the page most likely to be wanted mid-task.

Link between pages with ``<a href="page:some-id">``; the reader resolves those itself and
never touches the network.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Page:
    id: str
    title: str
    body: str
    summary: str = ""


@dataclass(frozen=True)
class Category:
    title: str
    pages: list = field(default_factory=list)


_LOREM = """
<p><i>Not written yet.</i></p>
<p>Lorem ipsum dolor sit amet, consectetur adipiscing elit. Sed do eiusmod tempor
incididunt ut labore et dolore magna aliqua. Ut enim ad minim veniam, quis nostrud
exercitation ullamco laboris nisi ut aliquip ex ea commodo consequat.</p>
<p>Duis aute irure dolor in reprehenderit in voluptate velit esse cillum dolore eu fugiat
nulla pariatur. Excepteur sint occaecat cupidatat non proident, sunt in culpa qui officia
deserunt mollit anim id est laborum.</p>
"""


def _stub(page_id, title, summary=""):
    return Page(page_id, title, _LOREM, summary)


# --- the one real page -------------------------------------------------------------------
#
# Written from `packsmith/core/query/language.py` — its grammar block and the printer's
# actual spellings — rather than from memory. A reference that disagrees with the parser is
# worse than no reference: it is confidently wrong at the moment someone is stuck.

_QUERY_LANGUAGE = """
<h1>Writing filters</h1>

<p>The <b>Filter</b> box above a table narrows what you are looking at <i>right now</i>.
It never changes the View itself — that is what
<a href="page:views">Edit view…</a> is for. Clear the box and the View is exactly as it
was. When a filter turns out to be worth keeping, <b>Keep</b> folds it into the View's own
query.</p>

<h2>Just type words</h2>

<p>The most useful thing you can do here is type words with no syntax at all:</p>

<pre>granite brick wall</pre>

<p>That matches across an entry's <b>id and its display name</b>, ignoring word order and
plurals — so it finds <code>quark:granite_bricks_wall</code> even though you typed the
words in a different order and without the <code>s</code>. Reach for the operators below
only when plain words are not enough.</p>

<h2>Fields</h2>

<table>
  <tr><td><code>id</code></td><td>the entry's namespaced id, e.g.
      <code>alexscaves:galena</code></td></tr>
  <tr><td><code>mod</code></td><td>the part before the colon —
      <code>alexscaves</code></td></tr>
  <tr><td><code>t:</code><i>name</i></td><td>a <b>tag</b> you defined, e.g.
      <code>t:remove</code></td></tr>
  <tr><td><code>a:</code><i>name</i></td><td>an <b>attribute</b> from the packdump, e.g.
      <code>a:localization</code></td></tr>
  <tr><td><code>b:</code><i>path</i></td><td>a <b>blueprint slot</b>, in a blueprint
      view</td></tr>
</table>

<h2>Comparing</h2>

<pre>t:remove == true
mod != "minecraft"
t:weight &gt;= 10</pre>

<p><code>==</code> <code>!=</code> <code>&gt;</code> <code>&lt;</code> <code>&gt;=</code>
<code>&lt;=</code> work as you would expect. Quote anything with a space in it.</p>

<p>For text there are three more:</p>

<table>
  <tr><td><code>CONTAINS</code></td><td>the value appears somewhere in the field</td></tr>
  <tr><td><code>MATCHES</code></td><td>a regular expression</td></tr>
  <tr><td><code>MATCHES_TOKENS</code></td><td>word-order-insensitive matching — what bare
      words use. Also spelled <code>~</code>.</td></tr>
</table>

<p>Each has a case-insensitive twin: <code>CONTAINS_I</code>, <code>MATCHES_I</code>,
<code>MATCHES_TOKENS_I</code>.</p>

<pre>a:localization CONTAINS_I "quartz"
id ~ galena ore</pre>

<h2>Lists</h2>

<pre>t:tier IN ("early", "mid")
mod NOT IN ("minecraft", "forge")</pre>

<h2>Has it been decided at all?</h2>

<p>This is the one that repays learning. A tag with a default <i>shows</i> that default in
every cell — but a cell nobody has touched is <b>not the same</b> as one you deliberately
set to the default value. <code>HAS</code> asks whether a decision was ever made:</p>

<pre>HAS t:tier
NOT HAS t:tier</pre>

<p><code>NOT HAS</code> is how you find the gaps — the entries you have not got to yet.
It is the single most useful query in the app, and it is invisible if you only ever
compare values.</p>

<h2>Combining</h2>

<pre>t:remove == true AND mod == "quark"
NOT HAS t:tier AND mod != "minecraft"
(mod == "quark" OR mod == "create") AND HAS t:remove</pre>

<p><code>AND</code>, <code>OR</code>, <code>NOT</code>, and brackets. <code>NOT</code>
binds tightest, then <code>AND</code>, then <code>OR</code> — so
<code>a AND b OR c</code> means <code>(a AND b) OR c</code>. Bracket it when in doubt; the
brackets are kept when the filter is saved.</p>

<h2>Nulls</h2>

<p>When a field has no value for an entry, <b>every comparison is false</b> — including
<code>!=</code>. That is deliberate: a missing value is not "different from", it is
absent. Ask about absence with <code>HAS</code> rather than by comparing.</p>
"""


CATEGORIES = [
    Category("Getting started", [
        _stub("what-is-packsmith", "What Packsmith is", "The one-paragraph version."),
        _stub("profiles", "Profiles and packs", "Pointing Packsmith at an instance."),
        _stub("packdump", "The packdump", "Where the game data comes from."),
    ]),
    Category("Working with data", [
        _stub("registries", "Registries", "Layer 1: what the game has."),
        _stub("tags", "Tags", "Layer 2: what you decide about it."),
        _stub("views", "Views", "A saved query, rendered."),
        Page("query-language", "Writing filters", _QUERY_LANGUAGE,
             "The filter bar's language, end to end."),
        _stub("blueprints", "Blueprints", "Schemas and their instances."),
    ]),
    Category("Files and ownership", [
        _stub("ownership", "Who owns what", "The rule the file engine turns on."),
        _stub("editing", "Editing files", "The text editor and its locks."),
    ]),
    Category("Automation", [
        _stub("actions", "Actions", "Installed, reusable operations."),
        _stub("jobs", "Jobs", "What you actually run."),
    ]),
]

PAGES = {page.id: page for category in CATEGORIES for page in category.pages}

# Where the ? beside the filter bar goes. Named rather than inlined at the call site, so
# the link and the page cannot drift apart silently.
FILTER_HELP = "query-language"
