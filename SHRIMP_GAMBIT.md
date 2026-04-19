# The Great Shrimp Gambit

> This document exists so that future conversations have full context on WHY
> PackSmith's automation language decision is complicated, and what external
> pressures are shaping it. Read this if you need to understand the Daphnia
> question. Skip it if you're just working on tags or the table UI.

---

## The Setup

PackSmith is one of four projects being developed solo. Three of them are
interconnected in a deliberate, load-bearing way:

1. **Daphnia** — a programming language
2. **PackSmith** — an IDE for Minecraft modpack development
3. **EvoStone** — an artificial life / evolutionary simulator

The fourth project (Shaman) is unrelated — it's a phone ordering automation tool
for work. Ignore it.

The Great Shrimp Gambit is the strategy that ties 1-3 together. It's a
self-imposed discipline machine, an adoption strategy for the language, and a
career play — all built out of the structural relationships between the projects.

---

## Daphnia: The Language

Daphnia is a small, typed, embeddable scripting language built in Rust. It is
designed around two properties:

1. **Sandboxed embedded scripting.** The host application is the god-king. Daphnia
   scripts are guests that can only do what the host explicitly permits. Authority
   is not ambient — it's granted through registered "ops" (host-defined functions).
   A script physically cannot access the filesystem, network, or anything else
   unless the host hands it that capability. Fuel quotas enforce termination.

2. **Genetic program manipulation.** Programs compile to a fixed-width 16-byte
   instruction format designed for zero-copy serialization, trivial diffing
   (memcmp), delta storage, and structural mutation. This makes programs cheap to
   copy, compare, serialize, and evolve at massive scale.

These sound orthogonal. They aren't. Both require programs to be compact,
deterministic, fully inspectable, and completely owned by the host.

The surface syntax is called **SexyDaphnia** — designed to be readable and
learnable for non-language-nerds. Code can be materialized in five modes:

- `do` — run inline
- `eval` — run as subprogram, return result
- `dict` (`#{}`) — run as subprogram, return context as dictionary
- `quote` (`&{}`) — don't run, return as a Program object (data, not execution)
- `state` — run and return a Fiber (resumable execution state)

**Current status:** Very early. The VM just achieved simple multiplication.
SexyDaphnia's surface syntax is being designed but is far from stable.

---

## EvoStone: The Left Branch

EvoStone is an artificial life simulator where organisms are Daphnia bytecode
programs that compete by playing a Hearthstone-like card game against each other.

The pitch: instead of vague blob creatures with physics engines (Species ALRE) or
assembly-code organisms in a petri dish (Avida), organisms play literal card games.
The winner reproduces. The loser dies. This creates a genuine Red Queen dynamic —
organisms are always competing against the shifting meta that other organisms
create.

Why Hearthstone specifically:

- **Performance** — fully deterministic, turn-based, no physics. Just resolve
  board states.
- **Red Queen** — Hearthstone's meta already exhibits exactly the evolutionary
  dynamics you want. Rock-paper-scissors at the archetype level, with constant
  churn.
- **Accessibility** — you can WATCH two organisms play and immediately understand
  their strategies. A 12-year-old can say "oh, that one's running aggro."
- **Bounded infinity** — organisms evolve any code they want, but it can only
  affect the bounded rules of a card game.
- **Cheap storage** — since organisms are Daphnia bytecode, every child can be
  stored as a delta from its parent. Billions of organisms, full lineage, feasible
  storage. Enables cladistics/taxonomy tracking of every organism that ever lived.
- **Replayability** — deterministic + stored organisms + epoch-based RNG seeds =
  perfect time travel to any match that ever happened.

**What EvoStone stress-tests in Daphnia:** The 16-byte IR, copy/diff/serialize
performance, type system robustness against adversarial mutations, fuel model under
millions of concurrent programs. Evolution will find every crack in the bytecode
format.

---

## PackSmith: The Right Branch

PackSmith is an IDE for Minecraft modpack development where the automation layer
runs user-authored and community-published scripts.

**What PackSmith stress-tests in Daphnia:** Surface syntax ergonomics — is
SexyDaphnia learnable? Are error messages clear? Does the capability model feel
natural to someone who just wants to remove duplicate items from their modpack?
Modpack developers have zero patience for tools that waste their time.

The community automation store is the critical feature: users download and run
other people's scripts. This is where Daphnia's sandboxing becomes essential —
scripts run safely not because you trust the author, but because the language
makes unsafe behavior structurally impossible.

---

## The Tension That IS The Discipline

If only PackSmith existed, the temptation would be to make Daphnia's surface syntax
maximally flexible and forgiving — and the IR would rot because nobody's pushing it.

If only EvoStone existed, the temptation would be to make the bytecode maximally
tight and machine-optimized — and the surface syntax would be an afterthought
because organisms don't care if the language is pleasant to write.

With both, every design decision must satisfy two masters:

- Make the IR too rigid for EvoStone → PackSmith scripting becomes painful
- Make the syntax too loose for PackSmith → the IR loses uniformity EvoStone needs

This tension forces the right abstraction boundary between surface language and
internal representation. It's not discipline through willpower — it's discipline
through architecture. The incentive structure makes half-assing either side cause
visible, concrete failures in a real application.

---

## The Trojan Horse

Neither application asks anyone to "learn Daphnia." They ask people to use a tool.

**PackSmith path:** Casual user downloads a community automation, checks boxes,
things happen. They never see Daphnia. Power user wants a custom automation, opens
a stock script, sees something readable, changes some numbers, eventually writes
their own. Need-driven onboarding.

**EvoStone path:** User runs simulations, watches organisms evolve. Wants to design
their own cards, opens `warsong_commander.daph`, sees something readable, tweaks
it. Eventually realizes the organisms are written in the same language as the
cards. Starts inspecting evolved bytecode.

Two completely independent communities, two entry points, both funneling curious
power users toward the same language — not through marketing, but because it was
already under the hood.

---

## The Career Play

The Gambit also serves as a career bootstrapping strategy. The portfolio tells a
story: "I designed a programming language and built two production applications on
top of it in completely different domains." That's not a resume line — it's a
narrative that gets in the room with people who'd never look at a traditional
entry-level resume.

The language sells itself through the applications. The applications sell themselves
through solving problems nothing else solves. The whole thing bootstraps from zero.

---

## How This Affects PackSmith's Design

The Gambit creates one specific pressure on PackSmith: the automation language
choice isn't purely a PackSmith decision. Choosing Python permanently would work
fine for PackSmith in isolation, but it removes PackSmith's role as Daphnia's
surface-language stress test and breaks the Gambit's structure.

The resolution (as of 2026-04-15): **design the automation API as a clean,
language-agnostic boundary.** Python for local development and dogfooding. The
runtime decision (Python vs. Daphnia vs. both with separate domains) is deferred
to when a public release with community features is being prepared. The API design
is the load-bearing part — it must work identically regardless of what language
calls into it. See DESIGN.md Section 3.3 for the full automation architecture.
