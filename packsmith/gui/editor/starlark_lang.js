/*
 * Working out what an author is asking about, in a `.star` file (design 6.3).
 *
 * Split out of `monaco_host.html` for one reason: everything here is pure text analysis
 * with no Monaco in it, so it can be run — and tested — without a browser. The providers
 * that wrap these functions live in the host page, because those genuinely need `monaco`.
 * `tests/test_starlark_completions.py` evaluates this file in a QJSEngine and drives the
 * functions directly, which is the only way the regexes below get checked at all; a text
 * assertion that the page "contains a completion provider" would prove nothing about
 * whether `pack.tags.` resolves to the tag members.
 *
 * The catalog (`api`) is passed in rather than read from a global, so nothing here holds
 * state and each function can be called with a fixture.
 */

/** Monaco ships no Starlark grammar, so `.star` opens as `python` and a provider must
 *  identify the buffer by path or it would fire on ordinary Python files too. */
function pathIsStarlark(path) {
    return typeof path === 'string' && path.toLowerCase().endsWith('.star');
}

function findMember(list, name) {
    if (!list) return null;
    for (var i = 0; i < list.length; i++) if (list[i].name === name) return list[i];
    return null;
}

/* A handle is produced by CALLING a resolver and is never named, so the only way to spot
 * one is the shape of the text: `…resolve(…)` immediately before the dot. Deliberately
 * does not cope with a call nested inside the arguments — an action's resolver arguments
 * are strings and subscripts, and being wrong here costs a missing suggestion, not a bug. */
var AFTER_RESOLVE = /\.resolve\s*\([^()]*\)\s*\.\s*[A-Za-z_]?\w*$/;
var PACK_CHAIN = /(?:^|[^\w.])(pack(?:\.[A-Za-z_]\w*)*)\.(?:[A-Za-z_]\w*)?$/;
var CHAIN_ENDING = /([A-Za-z_][\w.]*)$/;
var AFTER_CALL = /\)\s*\.\s*[A-Za-z_]\w*$/;

/**
 * Which list of members to offer, given the text before the cursor.
 * Returns null when the cursor isn't somewhere we know anything about.
 */
function membersFor(api, prefix) {
    if (!api) return null;
    if (AFTER_RESOLVE.test(prefix)) return api.handle;
    // Tolerates a partial word after the dot (`pack.ta`) so the list survives typing.
    var m = PACK_CHAIN.exec(prefix);
    if (!m) return null;
    var parts = m[1].split('.');
    if (parts.length === 1) return api.members[''];
    if (parts.length === 2) return api.members[parts[1]] || null;
    return null;
}

/** The catalog entry for the dotted chain ending at `text`, or null. */
function memberAt(api, text) {
    if (!api) return null;
    var m = CHAIN_ENDING.exec(text);
    if (!m) return null;
    var parts = m[1].split('.');
    if (parts[0] === 'pack') {
        if (parts.length === 1)
            return { name: 'pack', kind: 'namespace', detail: '',
                     doc: api.rootDoc || '', params: [] };
        if (parts.length === 2) return findMember(api.members[''], parts[1]);
        if (parts.length === 3) return findMember(api.members[parts[1]], parts[2]);
        return null;
    }
    // A bare name is only ours when it hangs off a call, which stops an author's own
    // helper named `write` from being described as a file handle's method.
    if (parts.length === 1 && AFTER_CALL.test(text))
        return findMember(api.handle, parts[0]);
    return null;
}

/**
 * The innermost unclosed `(` before the cursor, and how many arguments precede it.
 *
 * Scanned FORWARD rather than backward: quote state is only unambiguous read in the
 * direction it was written — going backwards, a `'` is equally likely to be an opening or
 * a closing quote, and an escaped one cannot be told from a real one without re-deriving
 * the whole line anyway.
 *
 * Returns `{before, argIndex}` where `before` is the text preceding the `(` — i.e. the
 * chain naming the function — or null if the cursor is not inside a call.
 */
function openCall(prefix) {
    var stack = [];
    var quote = null;
    for (var i = 0; i < prefix.length; i++) {
        var ch = prefix[i];
        if (quote) {
            if (ch === '\\') { i++; continue; }
            if (ch === quote) quote = null;
            continue;
        }
        if (ch === '"' || ch === "'") quote = ch;
        else if (ch === '(') stack.push({ open: i, commas: 0 });
        else if (ch === ')') stack.pop();
        else if (ch === ',' && stack.length) stack[stack.length - 1].commas++;
    }
    if (!stack.length) return null;
    var frame = stack[stack.length - 1];
    return { before: prefix.slice(0, frame.open), argIndex: frame.commas };
}
