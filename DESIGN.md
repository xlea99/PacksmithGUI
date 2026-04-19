# PackSmith Design Document

> **Last updated:** 2026-04-16
>
> This is a living design document. It captures the full vision of PackSmith, including
> unbuilt systems, with the understanding that specifics will evolve as decisions are made.
> It is the single authoritative reference for what PackSmith is, why it exists, and how
> it works.

---

## 1. What PackSmith Is

PackSmith is an IDE for Minecraft **modpack** development. Not mod development — modPACK
development. The distinction is the entire thesis.

Minecraft modding has world-class tooling: IntelliJ plugins, Architectury, Fabric Loom,
ForgeGradle, ProbeJS. But modpack development — the act of curating, configuring, and
enforcing consistency across dozens or hundreds of mods simultaneously — is a horrific
frankenstack of 15+ disconnected tools, mountains of manual file editing, and prayer. The
current state of the art for most pack devs is Google Sheets and manual auditing.

PackSmith's core insight: **documentation and action are the same problem.** When a pack
dev decides to remove an item, they must remember that decision (documentation) AND
propagate it across five or six disparate files (action). These are currently handled by
completely different workflows — a spreadsheet over here, manual file edits over there —
and the inevitable result is that one drifts out of sync with the other.

PackSmith unifies them. Checking a box IS the documentation of intent AND the trigger for
actual file changes. One gesture, both purposes. Single Pane of Glass (SPOG).

---

## 2. The Two Halves

PackSmith is a two-part system.

### 2.1 Packsmith (The Forge Mod)

A Minecraft Forge mod (currently targeting 1.20.1, Forge 47+) that acts as a data
extraction bridge. On world load (or via `/packsmith dump`), it serializes the game's
complete state into JSON:

- **Registries** — every registry type (100+ in modded 1.20.1): items, blocks, entities,
  biomes, enchantments, mob effects, sound events, worldgen features, etc. Each gets its
  own file with a sorted `values` array.
- **Localization** — display names for items, blocks, entities, enchantments, and mob
  effects, resolved through the game's language system.
- **Metadata** — timestamp, MC version, Forge version, loader info, complete mod list
  with versions, and a registry manifest.

Output lands in `<gameDir>/packsmith/` as:
```
packsmith/
  meta.json
  registries/
    minecraft_item.json
    minecraft_block.json
    ... (one per registry type)
  attributes/
    localization.json
```

The mod also performs a **world datapack safety check** — if world-specific datapacks
(IDs starting with `file/`) are detected, the dump aborts to prevent contaminated
snapshots.

### 2.2 PackSmithGUI (The Desktop App)

A PySide6 desktop application that consumes Packsmith dumps and provides the full IDE
experience: registry browsing, tagging, blueprint management, job execution,
file editing, and KubeJS integration. This is where all three architectural layers
(Section 3) live.

---

## 3. The Three-Layer Architecture

The architecture is built on three layers. The separation between them is **load-bearing**
— not organizational convenience. Each layer has a strict contract about what it can and
cannot do.

```
Layer 1: Game Data     — static, immutable, read-only snapshot of game reality
Layer 2: User Data     — descriptive metadata the user attaches TO game data
Layer 3: Jobs          — scripts that read Layers 1+2 and produce/edit real files
```

### 3.1 Layer 1: Game Data

Game Data is the Packsmith dump. It is inherently **read-only, static, and a report, not
a whiteboard.** It represents the actual truth from the game itself. PackSmith reads it,
displays it, but critically never modifies it.

The game told you what exists. You don't get to argue.

#### Profiles

A **Profile** is the top-level organizational unit — one profile per modded Minecraft
instance. It tracks:

- Profile name
- Minecraft instance path
- MC version (e.g., `1.20.1`)
- Mod loader and version (e.g., Forge `47.4.16`) — frozen at creation
- Settings (e.g., `max_packdump_snapshot_count`)
- References to its packdumps and user database

Profiles are stored in `userdata/profiles/{name}/`.

#### Packdumps

A **Packdump** is an immutable snapshot of the game's registry state at a point in time.
Multiple packdumps are retained per profile (default ~5), with new ones created only when
the registry actually changes.

Key design decisions:

- **Automatic discovery.** When the user creates a profile, they point it at their
  Minecraft instance directory. The Packsmith mod always dumps to `<gameDir>/packsmith/`,
  so PackSmith knows exactly where to look — no user configuration needed for dump
  location. PackSmith won't let you do anything until it loads its first packdump, since
  without game data there's no registry to browse, no entries to tag, nothing to automate.
- **New dumps do not auto-merge.** When the user imports a new packdump, they review the
  diff (additions, removals, changes) before "blessing" it as the active snapshot. This
  prevents transient mod installs (install mod to test it, decide it sucks, uninstall)
  from polluting the tag/blueprint data.
- **Packdumps are diffable.** The `compare()` method generates a structured dict of
  differences between any two snapshots — useful for the review/blessing workflow.
- **Localization uses fallback.** If a display name isn't available in the current locale,
  the raw registry ID is shown.

Storage layout per profile:
```
packdumps/
  latest/           # The blessed/active snapshot
    meta.json
    registries/
    attributes/
  history/          # Previous snapshots, most recent N kept
```

#### Multi-Loader Awareness

The GUI itself is largely loader-agnostic — Forge and Fabric registries look the same
once dumped. The Packsmith Java mod will need per-loader versions, but the GUI just tracks
`loader` as metadata. Design for generality, don't over-engineer for it.

### 3.2 Layer 2: User Data

Layer 2 is where PackSmith becomes something. It consists of three primitives — **Tags**,
**Blueprints**, and **Views**. Layer 2's invariant: **no side effects on external state.**
It computes (filters, queries, sorts), but it never writes files, never triggers jobs,
never modifies anything outside the database. It is pure, idempotent, and observable-only.
That decoupling is critical: the same tag can drive multiple jobs, and changing a job
doesn't change the user's intent.

To be precise: the user *does* assign tags inside a view's table — but the view config
itself carries no assignment logic. The assignment happens through the table delegate,
which the view happens to be rendering. The view defines what you see; the table UI is
where you act.

All user data is stored in a per-profile SQLite database (`profile.db`).

#### 3.2.1 Tags

Tags are user-defined scalar values assigned to registry entries from Layer 1. They are
NOT game data. Each tag is **strictly scoped to a single registry type** — a `remove` tag
on `minecraft:item` is a completely separate definition from a `remove` tag on
`minecraft:block`. This is not a limitation; it's load-bearing. Registry types have
fundamentally different semantics, and a tag that means "remove this item from crafting"
is a different concept from "remove this block from worldgen." Separate definitions,
separate assignments, separate queries.

The ergonomic cost is real: tags that have genuinely identical semantics across registry
types (e.g., `mod_source` — "which mod introduced this?") must be defined N times, once
per registry type. This is accepted as the right tradeoff — the alternative (cross-registry
tags) would blur the boundary between fundamentally different domains and complicate every
query path.

Five types:

| Type        | Values                    | Example                          |
|-------------|---------------------------|----------------------------------|
| `bool`      | `true`, `false`, or unset | `remove`, `is_food`, `is_hidden` |
| `string`    | any text, or unset        | `notes`, `lang_override`         |
| `enum`      | one of N options, or unset| `tier` (early/mid/late/end)      |
| `number`    | int or float, or unset    | `weight`, `stack_size`           |
| `reference` | a registry entry ID from any registry type, or unset | `material` (diamond sword → `minecraft:diamond`) |

The `reference` type is a pointer to a specific registry entry. Unlike the other scalar
types, it stores a full registry ID (e.g., `minecraft:diamond`) and is validated against
the packdump. This enables lightweight cross-registry relationships without needing a full
blueprint — "what material is this tool made of?" is a single tag, not a schema.

Tags can have **default values**. An unset tag with a default displays and behaves as
the default in the UI (including copy operations), but the default is NOT written to the
database — the distinction between "explicitly set to X" and "defaulting to X" is
preserved.

Key operations:

- **Define/Undefine** — create or destroy a tag type (undefine cascades to all
  assignments)
- **Assign/Unassign** — set or clear a tag value, supports bulk operations
- **Query** — filter registry entries by tag values with operators: `eq`, `neq`, `in`,
  `not_in`, `gt`, `lt`, `gte`, `lte`, `exists`, `not_exists`
- **Orphan detection** — find tags referencing entries that no longer exist in the current
  packdump (important after registry changes)

#### 3.2.2 Blueprints

Blueprints solve the problem tags can't: **modeling relationships between registry
entries.**

Tags are flat — they describe a single entry in isolation. But modpack development
constantly requires reasoning about *groups* of related entries that the game's flat
registry doesn't formally connect. The canonical example is stone palettes.

##### The Stone Problem

In vanilla Minecraft, stone types (granite, andesite, diorite, deepslate...) can be
processed into various cuts (polished, bricks, tiles, chiseled...) and each cut exists in
multiple forms (base block, stairs, slab, wall, vertical slab). But the coverage is
**completely arbitrary** — you can craft polished granite but not polished tuff; deepslate
has a cobbled variant but granite doesn't; Quark adds granite bricks but via polished
granite, not raw.

Layer 300 mods on top and it becomes an incomprehensible mess of missing holes and bizarre
inconsistencies. To enforce consistency, you need to:

1. **See** every stone type × cut × form combination
2. **Identify** which slots are filled, which are empty, and which come from which mod
3. **Fill gaps** — generate textures, blocks, recipes for missing combinations
4. **Standardize recipes** — enforce that the same crafting pattern works across all
   stone types

This requires modeling the *relationship* between `minecraft:granite`,
`minecraft:granite_stairs`, `minecraft:polished_granite`, `quark:granite_bricks`, etc. —
entries from different mods that are conceptually one "stone type."

##### How Blueprints Work

A **Blueprint** is an instantiable schema — a struct, or a class without methods.

1. **Define** a blueprint schema: `StoneType` has inline groups (`polished_cut`,
   `brick_cut`, `chiseled_cut`...) and each group has slots (`base`, `stairs`,
   `slab`, `wall`, `vertical_slab`).

2. **Instantiate** the blueprint: create `StoneType:granite`, `StoneType:andesite`, etc.

3. **Bind** entries to slots: `StoneType:granite.polished_cut.base` →
   `minecraft:polished_granite`, `StoneType:granite.polished_cut.stairs` →
   `minecraft:polished_granite_stairs`, etc.

4. **Identify gaps**: empty slots = missing content that needs to be generated or sourced.

5. **Automate**: jobs can query blueprint instances to generate recipes, detect
   duplicates, compile "you need to go build these in your glue mod" lists, etc.

Blueprints are **purely descriptive** — they live on Layer 2, not Layer 3. They organize
information. They don't do anything. But jobs can access them, and that's where the power
comes from.

##### Nesting and Structure

Blueprints support two kinds of structure:

**Inline groups** — structural nesting within a single blueprint definition. The user
thinks of this as one blueprint with grouped fields, not multiple separate blueprints:

```
StoneType:
  base_block: minecraft:block
  polished:                         ← inline group
    base: minecraft:block
    stairs: minecraft:block
    slab: minecraft:block
    wall: minecraft:block
    vertical_slab: minecraft:block
  bricks:                           ← inline group
    base: minecraft:block
    stairs: minecraft:block
    ...
```

Nesting depth is **unlimited.** At the DB level it's parent-child rows in
`blueprint_slots`, but the user sees one cohesive schema definition.

**Blueprint references** — a slot can reference an instance of *another* blueprint. This
is a pointer, not a copy. If the referenced instance is updated, the reference sees the
change automatically.

```
VillageVariations:
  inhabitants: minecraft:entity_type
  wood_type: WoodType               ← reference to a WoodType instance
  stone_type: StoneType             ← reference to a StoneType instance
```

Binding: `VillageVariations:canyon_villagers.stone_type` → `StoneType:granite`

This composes arbitrarily. `VillageVariations` references `StoneType`. Some future
blueprint could reference `VillageVariations`. No depth limit — it's just pointers with
no data duplication.

**Schema-level cycles are allowed.** A `CladeNode` blueprint with a `descendants` slot of
type `CladeNode` is a legitimate and powerful pattern (recursive tree structures like
cladograms). The schema is just a type definition; cycles there are fine. **Instance-level
cycles** (A references B references A) are a harder question — they could create infinite
loops in traversal, rendering, or job queries. See Open Questions.

##### Slot Typing

All slots are **strictly typed.** Two kinds of slot types:

- **Registry type** (`minecraft:block`, `minecraft:item`, etc.) — binds to a registry
  entry ID from the packdump. Validated on bind, autocomplete filtered to the correct
  registry.
- **Blueprint type** (`StoneType`, `WoodType`, etc.) — binds to an instance of that
  blueprint. Validated on bind, autocomplete shows available instances.

No or-types for now. If a genuine need surfaces during dogfooding, it can be added. Start
strict, loosen if needed.

##### Cross-Reference

A single registry entry can appear in multiple blueprint instances without restriction.
`minecraft:granite` can be bound in both `StoneType:granite` and `BuildingBlocks:granite`.
No special tracking — queryable through `instance_bindings` if needed ("show me every
blueprint instance that references this entry").

##### Auto-Population

There is significant room for a job that auto-fills blueprint slot bindings based on
naming conventions and registry search (e.g., if the stone type is "granite" and the slot
is "polished_base", search for `*polished_granite*` in the item registry). This is one of
the most practically valuable features of the blueprint system — without it, manually
binding hundreds of slots across dozens of instances is tedious enough to undermine the
whole concept. But mod naming conventions are wildly inconsistent (`polished_granite` vs.
`granite_polished` vs. `create:polished_granite`), so the approach needs serious design
work. See Open Questions.

##### Database Schema

```
blueprints          — the schema definition (the "class")
blueprint_slots     — fields/groups per blueprint (parent_slot_id for nesting, type for
                      registry type or blueprint reference)
blueprint_instances — a named instantiation ("granite", "andesite")
instance_bindings   — slot → registry entry ID or blueprint instance ID
```

#### 3.2.3 Views

A **View** is a saved, reopenable table configuration. It defines what the user wants to
see and potentially edit. Nothing more.

Views are **strictly read-only as a concept** — the view *config* carries no assignment
logic and never triggers jobs. Tag assignment happens through the table delegate that
the view renders, not the view itself. Separation of church and state.

A View specifies:

- Which registry type(s) to display
- Which tag columns to show
- Filter criteria (which entries match)
- Sort order
- Which columns are editable vs. read-only display

Examples ranging from trivial to complex:

- "All `minecraft:item` entries where `remove` == true" (simple tag filter)
- "All `minecraft:item` entries, showing `remove`, `hide_only`, `lang_override` columns"
  (the main item browser)
- "All `minecraft:entity_type` where `is_creature` == true, showing `species_name`,
  `clade`, `diet` columns" (creature taxonomy view)
- "All `minecraft:item` where `is_food` == true, showing `food_group`, `nutrition_value`,
  `special_food` columns" (diet system)

Even the main item browser is itself a View. The system must handle **hundreds of views**
gracefully — most packs will accumulate 200-300, the vast majority being trivially simple
"show me X where Y" configurations.

##### Filtering

Every opened view has a **filter bar** at the top of its table. This is where queries
live — not in the sidebar, not in a separate dialog. The filter bar supports two modes:

- **Query builder** (visual): filter rows with dropdowns — pick a tag, pick an operator,
  pick a value, click plus to add another condition. AND/OR toggle between rows. Zero
  learning curve, discoverable, good for the common case.
- **Text query** (direct): a text representation of the same filter, editable inline.
  Fast for power users who know what they want.

The two modes stay in sync — editing the text updates the builder, using the builder
updates the text. The builder IS the documentation for the query syntax. Users learn the
text language by watching what the builder produces.

The query language supports:

- Tag value filters: `remove == true`, `tier IN ("early", "mid")`, `weight > 5`
- Tag existence: `HAS notes`, `NOT HAS tier`
- Entry properties: `id CONTAINS "granite"`, `mod == "quark"`,
  `display_name MATCHES ".*Brick.*"`
- Boolean logic: `AND`, `OR`, `NOT`, parenthetical grouping
- Registry type scoping: `minecraft:item WHERE ...`

Changing a filter updates the table live. If the user likes the result, they can save it
as a named view.

##### Creating Views

Three paths to the same result — all produce a named view config in the sidebar:

1. **Filter bar → save** (exploratory, bottom-up): open a blank registry table, play with
   filters, get a useful result, save it. The view already exists as a tab — saving just
   names it.

2. **New View wizard** (intentional, top-down): right-click in the Views sidebar panel →
   "New View." Minimal dialog: pick registry type, pick tag columns to show, optionally
   set an initial filter, name it. Four fields. Opens as a tab for further refinement.

3. **Tag shortcut** (quick single-tag view): from the Tags sidebar panel, click a tag →
   "Create View for this tag." Pre-fills the wizard with the tag's registry type and that
   one editable column. One click, view created.

### 3.3 Layer 3: Jobs

Jobs are the bridge between Layers 1+2 and the actual files in the modpack instance.
They are scripts that can read game data and user data to produce or edit output files.

#### Design Principles

1. **Modular and composable.** Jobs are small, focused units — not monolithic scripts.
   `item_obliterator` is one job. `emi_hide` is another. A user builds a meta-job that
   queries tags and feeds results into these sub-jobs.

2. **Nothing is built-in.** Even the concept of "removing an item" is not baked into
   PackSmith's core. PackSmith may ship with stock jobs for common cases, but the system
   is fundamentally modular. The UI provides the interface into a registry; what you DO
   with that is defined by jobs.

3. **Downloadable and shareable.** Users can browse, download, and configure
   community-published jobs through an in-app store/marketplace (free, not monetized).
   "Buttfucker69's Removal Script with Item Obliterator Compat v2.9.7" is a real thing
   someone publishes and someone else installs.

4. **Sandboxed.** Community scripts mean running other people's code. The job runtime must
   enforce strict capability-based security — scripts can only access what PackSmith
   explicitly grants (read game data, read tags, write to controlled output directories).
   No filesystem escape, no network access, no surprises.

5. **Mod-aware but mod-agnostic.** Many jobs interface with specific mods (KubeJS, Item
   Obliterator, InControl, EMI, Paxi). Some only touch vanilla datapacks. Some don't
   touch files at all. The framework doesn't care — it provides the API surface and lets
   scripts do their thing.

#### The Canonical Example: Item Removal

To "remove" an item from a modded Minecraft pack (say `quark:rope`), you can't literally
unregister it — that's destructive. Instead you need to:

1. Hide it in EMI/JEI/REI (the recipe viewer)
2. Add it to `item_obliterator.json`
3. Generate datapack files to remove all recipes that create or use it
4. Add it to a KubeJS server script that nukes all tag assignments
5. Add it to a common hide datapack
6. Ensure that if a player somehow obtains it, it instantly disappears

Six file edits across six formats, plus remembering to document the decision somewhere.

With PackSmith: open a View, check `remove` next to `quark:rope`, run your jobs. One
checkbox, six files updated, decision documented. SPOG.

#### File Versioning

Jobs produce and edit files in the modpack instance. These changes are **versioned and
tracked** through a built-in file viewer, with the ability to restore previous versions.
("Would you like to restore `supplementaries.toml` to its previous version before it was
managed by PackSmith?")

#### Job Language & API Boundary

The job runtime is deliberately designed as a **language-agnostic API boundary.**
The API defines a set of capabilities — what a script can read, what it can write, what
it can query — and any runtime can call into it. This is the load-bearing architectural
decision: the API is one API, regardless of what language sits on top.

**The Job API contract:**

- **Inputs (read-only):** game data (registries, localization, metadata), tags (query
  with full filter support), blueprint instances and bindings, profile metadata
- **Outputs (controlled writes):** files within the modpack instance, at paths the host
  explicitly permits
- **Boundary:** the host prepares a context, hands it to the runtime, gets results back.
  The script never reaches around this boundary.

**Current approach:**

- **Development / dogfooding:** Python. No sandboxing needed — the author is the user.
  Python jobs are local `.py` files that call into the API through Python bindings. This
  enables immediate, practical use of PackSmith for real modpack development while the
  broader runtime question is resolved.

- **Public release / community store:** Decision deferred. The leading candidate is
  **Daphnia**, a capability-based secure language being developed in parallel, where
  scripts physically cannot exceed the capabilities the host grants. This would make the
  community store safe by construction — no sandboxing hacks, no trust assumptions. The
  alternative is shipping with Python and accepting Curseforge-style "trust the author"
  norms, which the Minecraft ecosystem already tolerates.

- **At release, one language only.** If Daphnia: all Python job capability is removed. If
  Python: Daphnia is not used. There is no dual-runtime, no "Python for local, Daphnia
  for store," no escape hatches. One language, one surface, period. The indev Python phase
  is purely scaffolding that gets torn out.

- **If Daphnia ships:** PackSmith ships with a comprehensive set of stock jobs (item
  removal, recipe generation, EMI/JEI hiding, lang overrides, etc.) pre-written in Daphnia
  so day-one users never need to write a line of anything. The community store is
  Daphnia-only from birth with safety guaranteed by construction.

The critical discipline: every job written during indev must go through the API boundary.
No reaching into Python-specific features (arbitrary pip imports, raw `os` module access,
metaprogramming) that couldn't survive a runtime swap. If it can't be expressed through
the capability API, it's a design smell regardless of the final language choice.

> For the full context on why this decision is complicated — including the external
> project pressures shaping it — see [SHRIMP_GAMBIT.md](SHRIMP_GAMBIT.md).

#### Execution Model

There is no separate "runlist" or "run configuration" concept. **Jobs are the only unit of
execution**, and jobs can call other jobs. A "Removal Pipeline" is itself a job — one whose
body calls `item_obliterator`, `emi_hide`, `recipe_nuke`, etc. as sub-jobs. A "Full Run"
is a job that calls every other job in dependency order.

The Jobs sidebar panel lists all jobs. The user marks which jobs get a **play button** in
the sidebar (typically the top-level orchestrator jobs, not every leaf). Click to run.
Results appear in the bottom panel.

For MVP, all jobs are manually triggered — the user clicks a button when they're ready.
Reactive triggers (on tag change, on packdump blessing, on file save) are post-MVP and
would simply be alternative ways to invoke the same jobs.

#### API Surface

The job API provides the following capabilities:

**Read Layer 1 (Game Data):**
- Query any registry type (all items, all blocks, all entities, etc.)
- Read localization / display names
- Read mod list and metadata

**Read/Write Layer 2 (User Data):**
- Full read access to tags (query with all filter operators)
- Full read access to blueprint schemas, instances, and bindings
- Full read access to view configurations
- No direct DB access — all queries go through clean API methods, never raw SQL

**Read/Write Files (Controlled):**
- Read files within the profile's modpack instance scope only
- Write/edit files with ownership integration — writes go through the file ownership
  system, requiring user approval when PackSmith claims ownership of a new file. If the
  user denies, the write fails. All writes are versioned and tracked.
- Jobs declare intended file ownership in their manifest (see below), enabling pre-run
  preview of what will change.

**Invoke Other Jobs:**
- Jobs can call other jobs as functions within a single run. This is composition, not
  triggering — it enables the meta-job pattern where a "Remove Items" script queries tags
  and feeds results into `item_obliterator(items)`, `emi_hide(items)`,
  `recipe_nuke(items)` as sub-calls.
- Jobs cannot schedule future runs, react to events, or trigger other jobs asynchronously.
  Event-driven triggering is a separate system (post-MVP).

**Job Manifest:**
- Every job declares metadata: which tags it expects, which registry types it operates on,
  which files it intends to own. This powers pre-run previews, ownership declarations,
  conflict detection, and the "this job will modify these 47 files" UI. The manifest is
  not a runtime capability — it's a static declaration inspectable before the job runs.

#### Community Store (Post-MVP)

Eventually, PackSmith will have an in-app store where users can browse, download, and
configure community-published jobs. For MVP, this isn't needed — PackSmith ships with
stock jobs covering common use cases (item removal, recipe generation, EMI/JEI hiding,
lang overrides), users write custom jobs as local script files, and community sharing
happens through existing Minecraft channels (CurseForge, Modrinth, GitHub, Discord). The
store becomes valuable when the community is large enough that organic content production
outpaces informal sharing. Specifics (hosting, discovery, versioning, trust/reputation)
are deferred until that time comes.

### 3.4 Plugins (Post-MVP)

Plugins are UI extensions that teach PackSmith about specific mods. They sit outside the
three-layer architecture — they don't touch game data, user data, or job logic.
They modify the UI: adding smart folders to the file browser, context menu actions,
bottom panel tabs, Monaco file renderers, and similar conveniences.

Most mods don't need a plugin. Plugins exist for "tooly" mods that affect the development
workflow itself — datapack loaders (Paxi, OpenLoader, Moonlight/Global Packs), scripting
frameworks (KubeJS), guidebook mods (Patchouli), and similar.

#### Examples

- **Paxi plugin**: registers smart folders for `config/paxi/datapacks/` and
  `config/paxi/resourcepacks/`. Adds "New Datapack" and "New Asset Pack" context menu
  options in the file browser.
- **KubeJS plugin**: registers smart folders for `kubejs/server_scripts/`,
  `kubejs/client_scripts/`, `kubejs/startup_scripts/`. Adds "New Server Script" etc.
  context menu actions. Adds a "KubeJS Logs" tab to the bottom panel.
- **Patchouli plugin**: helps with pathing for book pages, possibly renders book page
  JSON as formatted previews in Monaco.

#### Plugin Runtime

Plugins face the same community trust question as jobs. A malicious plugin with arbitrary
host access is *more* dangerous than a malicious job (full UI access vs. controlled file
writes). The resolution: **plugins use the same runtime as jobs.** If jobs run in Daphnia,
plugins run in Daphnia. One runtime, one trust model, no
contradictions.

This works because plugins don't need to touch Qt directly. They describe what they want
through a declarative API — `register_smart_folder()`, `register_context_action()`,
`register_bottom_tab()` — and PackSmith builds the actual UI. The plugin never sees a
widget, never imports PySide6, never reaches into host internals. It just declares
capabilities and PackSmith renders them.

#### MVP Approach

For MVP, plugin functionality is **hardcoded directly into PackSmith** for the most common
mods (Paxi, KubeJS, OpenLoader, Moonlight). But the internal implementation uses the same
registration functions that a future plugin API would expose — `register_smart_folder()`,
`register_context_action()`, etc. The callers are just Python code inside PackSmith
instead of external scripts.

When the plugin system ships post-MVP, those registration functions become Daphnia ops,
and the hardcoded calls move into `.daph` plugin files. The refactor is mechanical, not
architectural — the API surface already exists because PackSmith has been using it
internally.

---

## 4. The GUI

PackSmith's GUI is a PySide6 desktop application aiming to be a true SPOG — everything
a pack dev needs, in one window.

### 4.1 Window Layout

Standard IDE-style layout with three zones: a left sidebar for navigation, a main
workspace for content (~3/4 of the area), and a collapsible bottom panel (~1/4) for
output, status, and the status bar.

```
┌──────────────────────────────────────────────────────┐
│  Header: profile name, MC version, loader            │
├─────┬────────────────────────────────────────────────┤
│  V  │  [View: Items] [Monaco: config.toml] [BP:Gran] │
│  B  │                                                 │
│  T  │            Main workspace (tabs)                │
│  F  │                                                 │
│  R  │               (~3/4 height)                     │
│  J  │                                                 │
│     ├─────────────────────────────────────────────────┤
│     │  [Logs] [Job Results] [Errors]                   │
│     │          Bottom panel (~1/4 height)             │
│     ├─────────────────────────────────────────────────┤
│     │  Status bar (item count, dirty files, job run)  │
└─────┴─────────────────────────────────────────────────┘
```

Dark mode UI throughout (dark grays, blue accent).

#### Left Sidebar

A vertical icon strip (like IntelliJ/VS Code) where each icon swaps the sidebar panel
content. These are **navigation** panels — how you find and open things. Clicking an item
in any panel typically opens a tab in the main workspace.

| Icon | Panel | Purpose |
|------|-------|---------|
| V | **Views** | Searchable list of saved view configurations. Double-click opens a registry table tab with those filters/columns/sort applied. Ships with sensible defaults (Item Registry, Block Registry, etc.). |
| B | **Blueprints** | Browse blueprint schemas and instances as a tree. Click a schema to see all instances. Click an instance to open an editor tab. |
| T | **Tags** | All declared tags. Click to edit a definition. Quick-action to spawn a minimal view (registry type + that one editable column). Shortcut into the Views system. |
| F | **Files** | The semantic file browser. Toggle between **smart mode** (datapacks/assets/configs/scripts organized by purpose, mod-aware path resolution) and **honest mode** (raw directory tree). Same panel, view toggle at the top. |
| R | **Registry** | Lightweight browse-only registry explorer. For quick lookups without configuring a whole view. |
| J | **Jobs** | All jobs, organized as a flat or grouped list. Jobs marked as "pinned" get a play button for one-click execution — typically the top-level orchestrator jobs. Expandable to see sub-job composition. "Run All Pinned" at the top for the common case. Right-click to edit, create new, pin/unpin. |

More panels can be added later (search/query, etc.) without any architectural changes —
the sidebar is just a list of panels, adding one is trivial.

#### Main Workspace

Horizontal tab bar. These are the things you're actually **working in.** All tab types
are peers — registry views, Monaco editors, blueprint editors, NBT viewers, anything.

Opening a View, double-clicking a file, clicking a blueprint instance — all spawn tabs
here. Multiple tabs of the same type are fine (three views open, two files open, etc.).
Tabs can be closed, reordered, and (eventually) split.

#### Bottom Panel

Collapsible (closed by default, drag up or hotkey to open). Takes roughly **1/4 of the
main area height** when open — enough to scan logs without stealing focus from the
workspace. The status bar lives at the very bottom of this panel, not as a separate
window-level strip. Home for **output and status** — things you want visible while working
but that aren't your primary focus.

| Tab | Purpose |
|-----|---------|
| **Logs** | Live log viewer. Essential for debugging job output, packdump import warnings, etc. |
| **Job Results** | Per-run summary: "Removal Job: 126 files modified, 3 skipped (user-owned)." Expandable to per-file diffs. This is where the file versioning system surfaces. |
| **Errors / Warnings** | Orphaned tags, broken blueprint bindings, ownership conflicts. Clickable to jump to the relevant view/file/tag. |
| **Packdump** | Packdump management and the blessing workflow. When a new dump is imported that differs from the current blessed snapshot, this tab auto-opens and pulls focus — not a modal dialog, not blocking, just a visible attention jerk. Shows diff summary (new entries, removed entries, changed registries, mod list changes), surfaces orphaned tags at risk, and provides a "Bless" action to accept the new snapshot as active. The user can keep working in the main area while this waits. |

Additional bottom tabs (search results, etc.) can be added as needed.

#### Menu Bar

Standard menu bar at the top: `File | Edit | View | Profiles | ...`

The **Profiles** menu handles profile management — create, switch, delete. Switching
profiles is a heavy action (reloads the entire packdump and database) and prompts for
confirmation. PackSmith remembers the last active profile and auto-loads it on startup.

#### Header & Status Bar

- **Header**: `{profile_name} — {mc_version} {loader} {loader_version}`
- **Status bar**: the bottom-most strip inside the bottom panel (always visible even when
  the panel's tab content is collapsed). Shows contextual information: item count in
  current view, dirty file indicators, job run status, etc.

### 4.2 Registry Table

The primary interface for browsing and editing. Built on Qt's Model/View/Delegate
architecture:

- **RegistryTableModel** — virtual/lazy-loading model, 2 fixed columns (ID, Display
  Name) + N dynamic tag columns
- **RegistrySortProxy** — type-aware sorting: numbers sort numerically, bools sort
  False→True, strings sort case-insensitive, empty values always sink to bottom
- **RegistryTableView** — keyboard-driven bulk editing
- **Cell delegates** — type-specific rendering and editing per tag type

#### Keyboard Shortcuts

| Key         | Action                                                              |
|-------------|---------------------------------------------------------------------|
| Space       | Gmail-style bool toggle (all True → all False, otherwise all True)  |
| Delete/Bksp | Clear selected cells (unset tags)                                   |
| Ctrl+C      | Copy as TSV (plain text) + HTML table                               |
| Ctrl+V      | Single-value paste fill with type validation                        |
| Enter       | Commit edit and move down one row                                   |
| Ctrl+Z      | Undo                                                                |
| Ctrl+Y      | Redo                                                                |

#### Copy Behavior

- Format: TSV (tab-separated values)
- Bools copy as literal `true`/`false` (not empty — empty means unset)
- Default values are included in copy even if not explicitly written to DB
- Multi-cell selection copies all selected cells, not just the anchor

#### Undo/Redo

All tag edits go through an **EditStack** using the command pattern:

- **TagEditCommand** — single cell edit, stores old/new value, can apply/undo
- **BatchEditCommand** — groups multiple TagEditCommands, optimizes bulk operations
- Redo stack is cleared when a new command is executed

#### Default Sort

First column, A→Z on open.

### 4.3 Tab Architecture

The main window uses a **single Qt-owned tab bar** (QTabWidget or QTabBar) where every
tab — regardless of content type — lives side by side as peers. Some tabs are pure Qt
widgets (registry table views, blueprint builders). Some tabs display files in the Monaco
editor. From the user's perspective, they're all just tabs:

```
Tab 1: [RegistryTableView]         ← pure Qt
Tab 2: [Monaco → config.toml]      ← shared WebEngine, model A
Tab 3: [BlueprintViewer]           ← pure Qt
Tab 4: [Monaco → startup.js]       ← same WebEngine, model B
Tab 5: [Monaco → loot_table.json]  ← same WebEngine, model C
```

**One Chromium process, one QWebEngineView, one Monaco instance.** Monaco natively
supports multiple "models" — lightweight text buffers that hold content, undo history,
cursor position, and dirty state independently. Switching between file tabs swaps the
active model via a JS call through QWebChannel. The swap is instant. The single
QWebEngineView is reparented or shown/hidden as the user switches between Monaco tabs
and Qt tabs.

This means 10 open files cost essentially nothing beyond their text buffer in JS memory.
No 10-Chromium nightmare. Monaco assets will be **bundled locally** (not loaded from CDN)
to eliminate network dependency.

### 4.4 File Editor (Monaco)

The Monaco editor provides a full code editing experience within PackSmith:

- Syntax highlighting with auto-detected language
- Dark theme (vs-dark)
- Dirty state tracking and save support (Ctrl+S)
- Minimap, line numbers, smooth scrolling

Target file types: KubeJS scripts (.js), datapack JSONs, mod configs (.toml, .cfg, .json5,
.txt), job scripts, and anything else in the modpack instance.

### 4.5 File Browser (Planned)

A collapsible file system panel for browsing files from the modpack instance. But NOT
a dumb directory listing — a **semantic file browser** that understands Minecraft folder
conventions and presents files by *purpose*, not just by path.

Minecraft instance directories are dense forests of wildly different file types scattered
across deeply nested structures. A pack dev constantly navigates two mental hierarchies
simultaneously — the physical file path AND the conceptual purpose — and they frequently
diverge. "Am I in asset packs or data packs right now?" "Am I in server scripts or
client scripts?" A raw tree view doesn't solve this.

PackSmith is uniquely positioned to do better because it already has the packdump — it
knows which mods are installed. It can detect which global datapack loader is present
(Paxi → `config/paxi/datapacks/`, Moonlight → `moonlight-global-datapacks/`,
OpenLoader → its own path) and resolve paths automatically. The file browser presents
files by purpose: "Here are your datapacks. Here are your KubeJS server scripts. Here
are your mod configs." The underlying paths might be wildly different per loader, per
mod, per user setup — but PackSmith resolves them.

This also directly supports the job contract: a job doesn't say "write to
`config/paxi/datapacks/my_pack/data/minecraft/recipes/stone.json`." It says "write this
recipe JSON for namespace `minecraft`, recipe `stone`." PackSmith resolves the actual path
based on the profile's detected configuration. Jobs become portable across loader
setups.

### 4.6 File Ownership System

Every file in the modpack instance that PackSmith interacts with has exactly one of three
ownership states:

| State | Meaning | Constraints |
|---|---|---|
| **Untouched** | Neither user nor job has modified this file through PackSmith. It's the mod's default, vanilla, or whatever shipped with it. | Anyone can claim it. |
| **User-owned** | The user has edited this file (either manually in Monaco or through an explicit claim). | Jobs are **blocked** from touching it. If a job wants this file, the user must explicitly release ownership. |
| **Job-owned** | A job created or claimed this file. | The user is **warned** before editing. If they edit anyway, ownership transfers to them and the job is blocked until they release it back. |

Ownership transfers are **explicit, visible, and loud.** Never silent overwrites. The user
always knows who owns what, and conflicts are prevented rather than resolved.

#### The Rust Philosophy

This is deliberately strict. The permissive approach ("jobs and users can both freely edit
anything") sounds flexible but creates invisible chaos — a job silently overwrites a manual
edit, or a manual edit silently invalidates something that's SPOG'd on
a view somewhere. The strict model is annoying for five minutes while you get used to it.
The permissive model is terrifying permanently because you can never trust that your files
are in the state you think they are.

#### Job Ownership Declaration

Jobs **explicitly declare** which files (or keys — see below) they intend to own. This
enables PackSmith to show the full picture *before anything runs*:

- "This job will create/modify these 47 files"
- "3 of these are currently user-owned — release them or the job will skip them"
- "These 12 are already owned by a different job — here's the conflict"

For any file in the browser, PackSmith can show not just who owns it *now* but who *will*
touch it on the next run. "This file is managed by the Removal Job. Last generated 3 days
ago. Will be regenerated on next run." Full provenance, full
predictability.

#### Per-Key Ownership for Structured Configs

For unstructured files (scripts, raw text), ownership is per-file. One owner, period.
Each job writes its own files. KubeJS loads all `.js` files in a directory, so the removal
job writes `server_scripts/packsmith_removal.js` and the recipe job writes
`server_scripts/packsmith_recipes.js`. No conflict.

But mod config files break the per-file model. `quark-common.toml` is one file. A job that
disables features, a job that configures Wraith Sounds entity lists, a job that manages the
Pettable allowlist, and the user who wants to tweak a UI
setting — all have legitimate reasons to touch the same file.

The solution: **per-key ownership for structured config formats.** PackSmith parses the
config, tracks ownership at the key/field level, and merges on write. Each job claims
specific keys. The user owns everything unclaimed. Multiple jobs can coexist
in the same file as long as they don't claim the same keys.

```
quark-common.toml
  ├── [Wraith Sounds].wraith_entities     ← owned by: Wraith Sound job
  ├── [Enchanting Stacks].stacks          ← owned by: Enchanting job  
  ├── [Enchantments To Begone].list       ← owned by: Removal job
  ├── [Pettable].allowlist                ← owned by: Pettable job
  └── everything else                     ← owned by: user (or untouched)
```

Key-level conflicts (two jobs claiming the same key) are detected at job
install/configuration time, not at runtime.

#### Monaco Integration

When a user opens a structured config file with mixed ownership in Monaco:

- **Job-owned keys** are visually decorated — subtle background tint, lock icon in the
  gutter. Edits to these ranges are intercepted and blocked. Tooltip: "Managed by:
  Removal Job. Release ownership in the Jobs panel to edit."
- **User-owned keys** show a subtle indicator in the gutter. Tooltip shows ownership
  provenance.
- **Untouched keys** look normal, fully editable. The moment the user saves a change,
  PackSmith automatically assigns user ownership to the keys they touched.

This is implemented via Monaco's decoration API (background styling, gutter icons) and edit
interception via `onDidChangeModelContent` (check if the edit touched a job-owned
range, revert if so).

#### Supported Structured Formats

Per-key ownership applies to any format that is parseable with addressable fields:

| Format | Key Granularity | Notes |
|--------|----------------|-------|
| JSON / JSONC / JSON5 | any key path | Comment preservation needed for json5/jsonc |
| TOML | `[section].key` | Comment preservation — solvable with round-trip libs |
| YAML / YML | any key path | Comment preservation, whitespace sensitivity |
| INI / .properties | `key` or `[section].key` | Java standard, used by MC for lang/some configs |
| .conf | `key` | Simple key=value (as seen in Forge ecosystem) |
| .cfg | Forge category + key | Forge-specific legacy format, still appears |
| CSS (e.g. `emi.css`) | selector block + property | EMI uses CSS-syntax for config — fully parseable |
| .mcmeta | any key path | It's JSON |

Per-file ownership only (unstructured):

| Format | Notes |
|--------|-------|
| .js / scripts | Each job writes its own file |
| .txt | Could be anything — per-file only |
| .dat / NBT | Binary, per-file, dedicated tree viewer (not Monaco) |

The critical engineering concern across all structured formats is **comment-preserving
round-trip parsing.** PackSmith must read, modify specific keys, and write back WITHOUT
destroying comments, formatting, or key ordering. This is a solved problem (round-trip
TOML/JSON5/YAML libraries exist) but requires deliberate parser selection.

#### File Versioning

Jobs create and modify files. These changes are **versioned** through a custom snapshot
system (not git — too heavyweight, wrong mental model).

Before a job touches a file, its current state is snapshotted. After the run, the result
is snapshotted. Each snapshot is tagged with which job produced it and when. This gives:

- **Per-file history**: "Show me every version of `quark-common.toml`"
- **Per-run grouping**: "Removal Job, April 15 — modified 126 files, added 6000 lines"
  as a single reviewable unit
- **Rollback**: "Restore `supplementaries.toml` to before the removal job touched it"
- **Diffing**: expand any run, see every file diff, approve or rollback the
  whole batch or individual files

The void gets a flashlight. "My script just touched 126 files and I hope it was right"
becomes "my script touched 126 files and here's exactly what changed in each one."

#### NBT / .dat Files

PackSmith will support viewing and editing `.dat` (NBT) files, but NOT through Monaco.
NBT is a binary tree structure that wants a **dedicated tree viewer** — expandable nodes
with typed values, similar to NBTExplorer. This is its own Qt widget type living in the
tab system alongside Monaco tabs and registry table tabs.

NBT files participate in the ownership system at the per-file level. A relevant use case:
a job that scrubs removed blocks from structure files (`.dat` containing structure NBT)
would claim ownership of those structure files. Structure NBT has addressable paths
internally, but per-file ownership is sufficient since jobs would typically own the
whole structure or not touch it.

### 4.7 KubeJS Integration (Planned)

Since PackSmith already has the full registry, it can provide **autocomplete and type
hints** for KubeJS scripts inside the Monaco editor — the same problem ProbeJS solves,
but PackSmith has the data natively.

Additionally, KubeJS scripts could potentially interface **directly with profile.db** at
game runtime, querying user data from within the game itself.

### 4.8 Blueprint Viewer (Planned)

A dedicated view for browsing, editing, and visualizing blueprint structures. Potentially
a node/tree-based viewer where you can see the schema hierarchy, browse instances, and
identify gaps (empty slots).

---

## 5. Data Storage

### 5.1 File Formats

| Data                | Format   | Location                                      |
|---------------------|----------|-----------------------------------------------|
| Packdump snapshots  | JSON     | `userdata/profiles/{name}/packdumps/`         |
| Profile metadata    | JSON     | `userdata/profiles/{name}/profile.json`       |
| User data (all L2)  | SQLite   | `userdata/profiles/{name}/profile.db`         |
| App configuration   | TOML     | `userdata/config/main.toml`                   |
| Logs                | text     | `userdata/logs/`                              |

### 5.2 Database Schema

The SQLite database stores all Layer 2 data:

```
tag_definitions       — tag name, type, enum values, default
tag_assignments       — registry_type, entry_id, tag_name, value

blueprints            — blueprint schema definitions
blueprint_slots       — fields/members per blueprint (supports nesting)
blueprint_instances   — named instantiations
instance_bindings     — slot → registry entry mappings

views                 — saved view configurations
jobs                  — job definitions, manifests, pinned status (see Section 3.3)
```

### 5.3 Design Principle

The packdump is **read-only, always.** It never gets modified by PackSmith. Anything the
user creates, decides, or configures lives in the SQLite database. The packdump is truth
from the game; the database is truth from the user.

---

## 6. Technology Stack

| Component        | Technology                                   |
|------------------|----------------------------------------------|
| GUI framework    | PySide6 (Qt for Python)                      |
| Database         | SQLite3                                      |
| Code editor      | Monaco (embedded via QWebEngineView)         |
| File formats     | JSON (registries), TOML (config), SQLite     |
| Python version   | 3.13                                         |
| Forge mod        | Java 17, Minecraft 1.20.1, Forge 47+        |
| Build system     | Gradle (mod), pip/manual (GUI — no build yet)|

---

## 7. Open Questions

### Job Language (Release-Time Decision)
Python-only, Daphnia-only, or dual-runtime with separate domains? The API boundary is
designed to support any of these. The decision point is when a public release with
community store features is being prepared — not before. See Section 3.3 and
[SHRIMP_GAMBIT.md](SHRIMP_GAMBIT.md) for the full decision framework.

### Round-Trip Parser Selection
Which libraries do we use for comment-preserving round-trip parsing of TOML, JSON5, YAML,
and the other structured formats? This is the core engineering challenge of the per-key
ownership system. Needs research per format.

### Data Migration / Schema Versioning
As the SQLite schema evolves during development, how do we handle migration of existing
profile databases? `profile.py` has a TODO about this. Not urgent for early dev (blow away
and recreate is fine), but needs a real answer before any release where users have data
they care about.

### Blueprint Instance Cycles
Schema-level cycles are explicitly allowed (e.g., recursive `CladeNode`). But what about
instance-level cycles — `A` references `B` references `A`? This could cause infinite loops
in traversal, rendering, or job queries. Options: detect and reject at bind time, allow but
with documented depth limits on traversal, or allow and let consuming code handle it.

### Blueprint Schema Evolution
When a user adds, removes, renames, or retypes a slot on a blueprint schema that already
has instances, what happens to existing bindings? This is the in-app equivalent of the DB
migration problem — except it happens during a session with no restart boundary. Needs a
real design sketch covering each mutation type.

### Blueprint Binding Orphan Detection
When a registry entry disappears from a new packdump and it's bound to blueprint instances,
what happens? Auto-clear the binding? Enter an "orphan" state (like tag orphans)? Surface
it during the packdump blessing workflow? The blessing pipeline needs blueprint-aware logic.

### Blueprint Auto-Population Strategy
Auto-population is one of the most practically valuable blueprint features — without it,
manually binding hundreds of slots is tedious enough to undermine the concept. But mod
naming conventions are wildly inconsistent. Needs real design: string-similarity matching?
User-trained patterns? Blueprint-level regex hints from the schema author? Or defer
entirely and accept manual binding for MVP?

### Views at Scale
The design estimates 200-300 views for a mature pack. A flat searchable list in the sidebar
will break down well before that. How do we organize views — folders? Tags on views?
Pinned favorites? Some combination? Needs UX design.

### Query Language Specification
The filter bar query language is sketched but not formally specified. Operator precedence,
regex dialect, edge cases (e.g., `IN` on a bool tag), error messages for malformed
queries — all need a grammar definition. The query-builder-as-documentation approach is
great UX but requires a real grammar underneath it.

### Job Runtime Management
Recursion limits, resource caps, timeouts, and error recovery for job execution. Not
pressing for single-user indev, but required before community store ships untrusted code.
What happens when a job calls itself? When a job hangs? When a job crashes mid-run with
half its files written?

### Global Search
"Find any item, block, entity, tag, view, blueprint, or file by name across the whole
profile" — there's no specified way to do this. Filter bars are per-view. A global
search / command palette (Ctrl+Shift+P pattern — fuzzy search over every action, entity,
and setting in the app) would be essential for a tool at this complexity level.

### Per-Key Ownership Edge Cases
Key rename in Monaco: user renames a key, parser sees delete-old + insert-new. Does
ownership transfer? Is the old key's ownership orphaned? What if it was job-owned?
Also: the Monaco edit-interception UX for job-owned ranges (block keystroke? revert after?
allow with pending-review banner?). Needs an explicit decision.

### Undo Scope
Ctrl+Z in a view — does undo cross tabs? Cross sessions? What about undoing a tag
definition change that cascaded to hundreds of unset assignments? The current per-view
EditStack is specified but cross-view and destructive-cascade undo is not.

### Settings / Preferences
Beyond `max_packdump_snapshot_count`, there's no user-settings system discussed. Themes,
keybinds, default behaviors, font size, etc. Not architecturally complex but needs a
design pass.

### Packaging and Distribution
How do users install PackSmith? PyInstaller bundle? Standard installer? How does it
update? How does the Forge mod version stay in sync with GUI changes?

### Community Sharing (Post-MVP)
The community store handles jobs. What about sharing blueprint schemas, view configs, tag
definitions? A unified store for jobs, plugins, schemas, and other shareable artifacts.
Not MVP, but the sharing surface is wider than just jobs.

### Testing Strategy
How does PackSmith get tested? The ownership model, job API, round-trip parsers, and
blueprint system all have complex invariants. Needs at minimum a strategy sketch — unit
tests, integration tests, what gets mocked vs. real.

---

## 8. Underspecified Sections

> Sections of the design that exist as concepts but lack enough detail to implement from.
> These need dedicated design passes before building.

- **Blueprint Viewer (§4.8)** — Central to the architecture, currently a one-paragraph
  placeholder. What does it look like to see a 4-level-nested StoneType schema with 15
  instances? Grid? Tree? Tabbed instances with a shared schema header?
- **File Browser (§4.5)** — Smart-mode/honest-mode toggle is a good concept but the
  mechanics are vague. How does file creation work in smart mode? What about files that
  don't fit any category?
- **KubeJS Integration (§4.7)** — Currently just "we have the registry so we can do
  ProbeJS's job." Monaco language server integration is a real project. Is this
  Monaco-native IntelliSense or a full LSP?
- **Packdump Pipeline** — Error recovery for the packdump import/blessing workflow. What
  happens on a corrupt dump? Mid-load failure? Partial registry data?
- **Per-Key Ownership Monaco UX** — The decoration and edit-interception model is sketched
  but the actual user interaction flow (what happens keystroke-by-keystroke when you try to
  edit a job-owned range) needs a concrete decision.
