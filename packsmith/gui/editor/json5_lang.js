/*
 * JSON5, as its own Monaco language (design 3.3.1).
 *
 * Packsmith's action manifests are `.json5`, and Monaco has no JSON5 support at all. Its
 * JSON service is really JSONC: it tolerates comments, rates a trailing comma an ERROR,
 * and rejects unquoted keys and single-quoted strings outright. Pointing `.json5` at
 * `json` therefore drew red underlines through a perfectly valid manifest — through
 * exactly the shorthand this format was chosen for.
 *
 * Relaxing the JSON service's diagnostics was tried first and is not enough. Comments and
 * trailing commas can be silenced with `setDiagnosticsOptions`; unquoted keys cannot. And
 * doing it there would have relaxed real `.json` files too, which should stay strict —
 * a trailing comma in a datapack is worth knowing about.
 *
 * So JSON5 gets its own language id, a Monarch grammar, and no language service. What it
 * gives up is schema validation it never had — nothing publishes a schema for a Packsmith
 * manifest — and a malformed one is still caught loudly where it counts, by `PackageIndex`
 * refusing to load the package and saying why (§3.3.1).
 *
 * In its own file rather than inline in the host page for the same reason
 * `starlark_lang.js` is: a Monarch grammar is a wall of regex literals, and every layer of
 * shell or string escaping between here and the browser is a chance to turn one into
 * something that silently fails to compile — which is exactly what happened when this was
 * written inline.
 */

function registerJson5() {
    monaco.languages.register({ id: 'json5', extensions: ['.json5'] });

    monaco.languages.setLanguageConfiguration('json5', {
        comments: { lineComment: '//', blockComment: ['/*', '*/'] },
        brackets: [['{', '}'], ['[', ']']],
        autoClosingPairs: [
            { open: '{', close: '}' },
            { open: '[', close: ']' },
            { open: '"', close: '"', notIn: ['string'] },
            { open: "'", close: "'", notIn: ['string'] },
        ],
        surroundingPairs: [
            { open: '{', close: '}' },
            { open: '[', close: ']' },
            { open: '"', close: '"' },
            { open: "'", close: "'" },
        ],
    });

    monaco.languages.setMonarchTokensProvider('json5', {
        defaultToken: '',
        tokenPostfix: '.json5',
        keywords: ['true', 'false', 'null', 'Infinity', 'NaN', 'undefined'],

        tokenizer: {
            root: [
                // Keys first, in all three spellings JSON5 allows, each recognised by the
                // `:` that follows. Matching them separately is what lets a key and a
                // string *value* colour differently, which is most of what makes a nested
                // manifest readable at a glance.
                [/[a-zA-Z_$][\w$]*(?=\s*:)/, 'type.identifier'],
                [/"(?:[^"\\]|\\.)*"(?=\s*:)/, 'type.identifier'],
                [/'(?:[^'\\]|\\.)*'(?=\s*:)/, 'type.identifier'],

                { include: '@whitespace' },

                [/[{}\[\]]/, '@brackets'],
                [/[,:]/, 'delimiter'],

                // JSON5 numbers: hex, leading/trailing dot, explicit sign, exponents.
                [/[+-]?(?:0[xX][0-9a-fA-F]+|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)/, 'number'],

                [/[a-zA-Z_$][\w$]*/, {
                    cases: { '@keywords': 'keyword', '@default': 'identifier' },
                }],

                [/"/, 'string', '@string_double'],
                [/'/, 'string', '@string_single'],
            ],

            whitespace: [
                [/[ \t\r\n]+/, ''],
                [/\/\*/, 'comment', '@comment'],
                [/\/\/.*$/, 'comment'],
            ],

            comment: [
                [/[^\/*]+/, 'comment'],
                [/\*\//, 'comment', '@pop'],
                [/[\/*]/, 'comment'],
            ],

            string_double: [
                [/[^\\"]+/, 'string'],
                [/\\./, 'string.escape'],
                [/"/, 'string', '@pop'],
            ],

            string_single: [
                [/[^\\']+/, 'string'],
                [/\\./, 'string.escape'],
                [/'/, 'string', '@pop'],
            ],
        },
    });
}
