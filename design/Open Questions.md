## Open Questions

These are questions that don't have a clean home in any specific section — they're
cross-cutting, genuinely new surfaces, or process-level concerns that sit outside the
architecture proper. Section-scoped questions live in each section's own "Live
Questions" subsection.

### Global Search

"Find any item, block, entity, tag, view, blueprint, or file by name across the whole
profile" — there's no specified way to do this. Filter bars are per-view. A global
search / command palette (Ctrl+Shift+P pattern — fuzzy search over every action, entity,
and setting in the app) would be essential for a tool at this complexity level. Not
covered by any existing section — it would be a new top-level UI surface.

### Settings / Preferences

Beyond `max_packdump_snapshot_count`, there's no user-settings system discussed. Themes,
keybinds, default behaviors, font size, etc. Not architecturally complex but needs a
design pass. No clear home in existing sections — would likely be its own §4.x.

### Standalone Action Runs — Will This Ever Be Useful? (relegated 2026-08-12)

§4.1 and §3.3.2 both describe running an action directly from the Actions panel, creating
an "ephemeral one-step job with a *Save as job?* prompt". **Relegated rather than built**,
because the case for it did not survive being looked at:

- **It saves almost nothing.** Creating a job is name → add step → bind → run. A standalone
  run is pick → bind → run. Binding is the entire cost and it is identical either way; the
  delta is a name and one click.
- **It is actively worse for iteration**, which is the real workflow. Ephemeral means the
  bindings do not persist, so tweak-and-rerun would re-bind every time. A saved job keeps
  them, and a *pinned* job already has a one-click play button in the Jobs panel — so the
  fast path exists and is better than this would be.
- **It is thinner than it reads.** "Ephemeral one-step job" is a job with different UI
  treatment, not a second execution path — so it was never buying an architectural
  simplification either.

**The problem it was actually solving is Jobs-panel pollution** — experiments named `test`,
`test2`, `asdf` accumulating in what §4.1 calls the primary automation surface. If that
turns out to hurt in real use, the cheap fix is auto-naming new jobs and one-click delete,
not a second way to run things.

Revisit only if real use produces the specific symptom: avoiding experiments because you
don't want to name them.

### Asynchronous Job Runs — Maybe Someday, and Only for Snappiness (2026-08-12)

§3.3.2 describes running jobs on a background thread with a global queue. **Not built, and
not currently wanted**, for a reason that only became clear once the alternative was costed:

**An async run would spend its first half making the UI responsive and its second half
making it unusable.** A run in flight cannot safely allow a packdump adopt (it swaps the
registry under a running action, and its fallback path closes the database), a profile
switch, a second job, or — per §3.2.1's ownership reasoning — L2 edits, since "the user
overwrites a cell an in-flight action is about to claim" is unspecified. What remains is
scrolling. A responsive-but-inert window is arguably *worse* than a blocked one: blocked is
unambiguous, inert invites clicks that silently do nothing.

Synchronous is also the more honest expression of what §3.3 already promises. Runs are
**globally serialized**, and a blocked window is that guarantee enforced by physics rather
than by a lock somebody has to remember to hold. It brings a real safety property too:
killing Packsmith mid-run is survivable, because the in-flight step's writes were staged and
never committed while completed steps are in history and individually rollback-able.

**What blocking actually costs is one thing, and it is not "you can't click".** After a few
seconds without pumping events the OS marks the window Not Responding, and at that moment
*working* and *crashed* look identical to the user. That is solved — `run_job` takes an
`on_progress` callback, the GUI streams each step's log lines as it finishes, names the step
about to run, and pumps the event loop. An hour's work rather than half a day, single
threaded throughout, and it needs none of the concurrency design above.

So async survives only as a **snappiness** want: the window could be scrolled and read
during a long run. Worth revisiting when a single *step* routinely takes more than a few
seconds on a real pack — which will be felt long before any metric shows it. Even then the
first move is the action reporting its own progress, not threading the runner. If it is ever
built, note the two things that dominate the work: SQLite connections are per-thread
(`db.py` opens without `check_same_thread`, and `transaction()`'s re-entrancy counter is not
thread-aware, so sharing one connection risks committing at the wrong moment), and
`starlark-pyo3` has been spiked and *does* run actions correctly off the main thread.

### Testing Strategy

How does Packsmith get tested? The ownership model, action API, round-trip parsers, and
blueprint system all have complex invariants. Needs at minimum a strategy sketch — unit
tests, integration tests, what gets mocked vs. real. Process-level concern, not a
feature of any one section.
