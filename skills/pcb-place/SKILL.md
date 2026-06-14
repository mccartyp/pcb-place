---
name: pcb-place
description: Apply placement.ppl deterministically with the pcb-place CLI, then interpret its placement/reflow/congestion reports to recommend board.pln edits in the AI optimization loop. Use when executing a placement.ppl against a .kicad_pcb, validating placement/collisions/spacing/keepouts/regions, reading review reports, or deciding which board.pln change fixes a degraded placement.
---

# pcb-place

## Purpose

`pcb-place` is the **deterministic placement executor**. It consumes a
`placement.ppl` part-placement file and a KiCad `.kicad_pcb` board, applies
footprint-level placement, performs reflow and optimization, validates the
result (bounds, collisions, spacing, keepouts, regions), and writes a placed
`.kicad_pcb` while preserving KiCad structure (UUIDs, groups, tracks, zones,
etc.) via surgical patching.

In the new workflow, the highest-value use of this skill is **interpreting the
reports `pcb-place` produces and feeding the fixes back into `board.pln`**:

```
KiCad PCB + netlist + stackup + dimensions
  -> pcb-plan inspect -> planning-hints/
  -> Claude (pcb-bootstrap) generates board.pln    # design-intent authored by AI
  -> pcb-plan check / validate -> pcb-plan emit -> placement.ppl
  -> pcb-place -> reports                           # deterministic execution
  -> Claude optimization loop edits board.pln       # board.pln is the optimization surface
```

`board.pln` is the **authoritative design-intent document and THE optimization
surface**. `placement.ppl` is a derived artifact produced by `pcb-plan emit`
from `board.pln`. Preference order for changes is: **1) `board.pln`,
2) `placement.ppl` override, 3) manual `Anchor()`** — the second and third are
temporary last resorts, not the normal fix.

`pcb-place` does **not**:

- plan placements or infer architecture, power islands, or a final floorplan
  from netlists/roles — that intent lives in `board.pln` (authored by AI)
- hand-place components as the primary fix (editing `placement.ppl` directly is
  at best a temporary override)
- route traces, generate vias, or create copper zones
- replace engineering review

It **does** perform reflow and local optimization deterministically; that is
mechanical repacking, not architecture decisions.

## Prerequisites

- `pcb-place` installed (`pip install .` or `pip install -e '.[dev]'`).
  Console script: `pcb-place`. If unavailable, fall back to
  `python -m pcb_place` (referred to as `pcb_place` below — both spellings
  invoke the same implementation).
- A KiCad board file (`.kicad_pcb`).
- A `placement.ppl` file, normally produced by `pcb-plan emit` from `board.pln`.
- Optional: a Zener/`pcb` netlist artifact (e.g. `.pcb/build/default.net`) for
  semantic alias resolution.

## Inputs and Outputs

| Artifact | Role |
| --- | --- |
| `board.kicad_pcb` (input) | Board to place footprints on. |
| `placement.ppl` (input) | Placement DSL emitted from `board.pln`: `Board`, `Region`, `Keepout`, `Anchor`, `Cluster`, `Satellite`, etc. |
| `--netlist` (optional input) | Semantic alias map (e.g. `MCU.U_MCU` -> `U6`). |
| `placed.kicad_pcb` (output) | Placed board, written atomically. Defaults to `<input>.placed.kicad_pcb` unless `-o`/`--in-place`. |
| `--report-json` (output) | Machine-readable report: board geometry, regions, keepouts, violations, deltas, floorplan/reflow/congestion data, alias diagnostics. This is the primary feedback you interpret to edit `board.pln`. |

## Primary Commands

```bash
# Inspect footprint/board geometry without applying placement
pcb-place board.kicad_pcb placement.ppl --print-bounds
pcb-place board.kicad_pcb placement.ppl --print-board
pcb-place board.kicad_pcb placement.ppl --print-regions
pcb-place board.kicad_pcb placement.ppl --print-clusters

# Preview placement without writing, and capture the report to interpret
pcb-place board.kicad_pcb placement.ppl --dry-run \
  --report-json pcb-place-report.json

# Apply placement and write a placed copy
pcb-place board.kicad_pcb placement.ppl \
  -o placed.kicad_pcb \
  --report-json pcb-place-report.json

# Validate a board against placement.ppl (lightweight checks, no write)
pcb-place placed.kicad_pcb placement.ppl --validate

# CI: fail if applying placement.ppl would change the board, with strict refs
pcb-place placed.kicad_pcb placement.ppl --check --strict
```

`--print-bounds`, `--print-board`, `--print-regions`, `--print-clusters`,
`--list-refs`, and `--list-aliases` work without writing output; `--ppl` is
optional for `--print-bounds`/`--print-board`/`--list-refs`/`--list-aliases`
but required for `--print-regions`/`--print-clusters` (regions/clusters are
declared in the `.ppl` file).

Performance and explanation flags: `--optimization-level`,
`--time-budget-seconds`, `--explain-placement REF`, `--profile-placement`, and
`--progress` tune or narrate the executor's run — useful when interpreting why
a placement degraded or how long optimization is taking.

Fallback command spelling (same implementation):

```bash
pcb_place board.kicad_pcb placement.ppl --dry-run
```

> **Note on `--validate-structure`:** there is no separate
> `--validate-structure` flag. Use `--validate` for placement-level structural
> checks (board bounds, keepout/region containment, near-coincident
> footprints, locked-footprint moves), and `--check --strict` for CI
> staleness/reference checks. Every write also re-parses the output file and
> verifies footprint count and parenthesis balance automatically — no extra
> flag is needed for that.

## Skill Behavior

The core loop is: **run the executor, interpret the report, edit `board.pln`,
re-emit, re-run.** Hand-editing `placement.ppl` or adding `Anchor()` is only a
last resort.

1. **Run `--dry-run` before writing.** Always preview with `--dry-run
   --report-json <file>` first and review per-footprint deltas, "outside
   board" flags, floorplan/reflow data, and any warnings before producing real
   output.
2. **Check board bounds and `Edge.Cuts`.** Run `--print-board` to confirm
   which board geometry source is in effect (`Edge.Cuts`, `Board(...)`, or
   footprint-bounds fallback) — this affects where board-local coordinates
   land.
3. **Validate collisions/spacing/keepouts/regions.** Use `--dry-run
   --report-json` or `--validate` and inspect `region_violations`,
   `keepout_violations`, collision/spacing counts, and
   `priority_conflicts`/`overridden_rules`/`locked_move_attempts`.
4. **Interpret the report, then edit `board.pln`.** Map each degraded
   placement, parked component, congestion hotspot, or violation to a concrete
   `board.pln` change (region size/position, keepout, side selection, array
   strategy, power island, high-speed path). Re-run `pcb-plan emit` and
   `pcb-place` to confirm. Do not paper over failures by hand-editing
   `placement.ppl`.
5. **Diagnose unresolved references.** Run with `--strict` (and `--check
   --strict` in CI) to fail loudly on missing/ambiguous references rather
   than silently skipping rules. Use `--list-refs` to see available KiCad
   references.
6. **Diagnose aliases/netlist semantic paths.** Use `--netlist <file>
   --list-aliases` to confirm semantic names (e.g. `MCU.U_MCU`) resolve to the
   expected KiCad reference. Explicit `Alias("name", "ref")` rules in
   `placement.ppl` override imported netlist aliases.
7. **Use `--explain-placement REF`** to understand why a specific component
   landed where it did (which rule won, what reflow moves happened) before
   deciding which `board.pln` primitive to adjust.
8. **Recommend `pcbnew placed.kicad_pcb`** to open a standalone, separately
   written placed board copy — do not assume it auto-opens in an existing
   KiCad project.
9. **Warn about project-loader vs `pcbnew` differences.** If the board
   filename or KiCad project (`.kicad_pro`) association changes, the full
   KiCad project loader can behave differently than opening the `.kicad_pcb`
   directly with `pcbnew`. Call this out explicitly when renaming/copying
   board files.
10. **Preserve KiCad UUIDs/groups.** Don't suggest workflows that regenerate or
    strip UUIDs on existing footprints/pads/groups/tracks/vias/zones — only
    newly created objects (e.g. emitted `Edge.Cuts` outlines) get new UUIDs.
11. **Use `--warn-overlap` only for exploratory visualization**, not as a
    final validation gate — it demotes collision/spacing violations to
    warnings. Final validation should pass without `--warn-overlap` /
    `--allow-overlap`.
12. **Recommend incremental testing for a failing `.ppl`.** Bisect which rule
    causes a failure; use `--print-clusters`/`--print-regions` to confirm
    declarations parsed as expected; re-run `--dry-run` after each change. When
    `placement.ppl` was emitted from `board.pln`, fix the cause in `board.pln`
    rather than mutating the emitted file.

## Iterative Floorplanner: No-Abort Placement and Reflow

`pcb-place` behaves like a constraint-driven floorplanner during execution, not
a collection of independent placement primitives. A single component that cannot
place legally is treated as evidence that the surrounding floorplan (expressed
in `board.pln`) is over-constrained, not as a hard failure. The executor's
reflow/optimization is deterministic mechanical repacking — it does **not**
decide architecture, power islands, or the floorplan; that reasoning belongs in
`board.pln` (authored by AI).

- **No-abort default (`best_effort`, on by default).** When a primitive cannot
  satisfy its constraints, the engine runs a reflow ladder before, as a last
  resort, *parking* the component (leaving it at its current position with a
  `DEGRADED PLACEMENT … [review-required]` warning and a quality penalty).
  Placement never aborts in this mode.
- **Reflow ladder.** Level 1 repacks within the array (spacing/stagger/rows/
  sides/slide — built into the array primitive). Levels 2–4 repack nearby
  movable support parts (test points, LEDs, pull-ups/straps) outward. Level 5
  moves a *movable* parent (e.g. nudges a regulator/IC) if doing so lets the
  group place. Level 7 is parking.
- **Mechanical authority is absolute.** Locked parts, edge-required connectors,
  and mechanical anchors (mounting holes) are never moved by reflow.
- **Global optimization.** After the first pass the floorplanner iterates
  (`--max-floorplan-iterations`, default 10), re-scoring and re-attempting the
  worst constraints, keeping the best-scoring floorplan.
- **Quality scoring.** The report's `floorplan` section carries
  `placement_quality_score` (lower is better; parking dominates, then
  collisions/out-of-bounds/spacing, then congestion), `iterations` (the
  per-iteration score trajectory), `reflow_attempts` (with per-attempt
  `score_delta`), `moved_parents`, `moved_components`, `parked`,
  `parking_fallback_used`, `degraded_warnings`, and `congestion_map`.

Flags:

- `--strict` / `--fail-fast` — restore legacy behavior: abort on the first
  unsatisfiable placement (the only modes that may fail).
- `--no-best-effort` — disable no-abort; parked components become errors.
- `--max-floorplan-iterations N` — cap global optimization iterations.

When you see parked components, moved parents, or congestion in the report,
**fix the floorplan in `board.pln`, not just the emitted rule**: enlarge the
relevant region, free space near the parent, adjust the power island /
high-speed path, or relax spacing for the neighbourhood — all expressed in
`board.pln`, then re-emit. `board.pln` is the optimization surface. The
`ai-edit-hints.md` "Floorplan health" section lists these `[review-required]`
items with concrete suggestions.

## Placement Ownership, Edge Connectors, and Review Reports

The report's `ownership` section records the unique winning rule per ref;
cluster moves later refined by higher-priority rules (ESD, arrays, near-pad)
are expected, not errors. `Edge(...)` supports `rot="auto"` (rotation from
`access_side`: top 0°, right 90°, bottom 180°, left 270°, assuming the mating
face points toward the top edge at rot=0) and `allow_body_outside_board=True`
(connector body may extend past the outline while its anchor stays on-board;
reported in `allowed_outside_board_refs`, not as a violation). Spacing defaults
are conservative (`passive_to_passive=0.25`, `passive_to_ic=0.40`,
`ic_to_ic=0.75`, `connector=1.0`, `mechanical=1.0`); arrays escalate spacing
rather than emit touching parts. Emit markdown reviews with
`--high-speed-review`, `--power-review`, `--mechanical-review`, and
`--ai-edit-hints` to score `HighSpeedPath(...)` directness/ESD position/corridor
intruders, `PowerIsland(...)` hot-loop compactness and high-speed separation,
mounting-hole distribution, and to collect suggested edits for AI-assisted
iteration. Treat every review finding as a candidate `board.pln` edit, not a
`placement.ppl` patch.

## AI-Assisted Placement Report Review

After every `pcb-place ... --dry-run --report-json pcb-place-report.json`,
Claude should inspect the report before recommending a write. Treat the report
as **feedback to improve `board.pln` and regenerate `placement.ppl`**, not as a
reason to hide failures with ad-hoc placement edits.

Inspect:

- collisions and near-collisions;
- spacing violations;
- keepout violations;
- region violations;
- outside-board placements and board-geometry source;
- degraded/parked components, moved parents, and the `congestion_map`;
- array slide/clamp diagnostics, including candidate `PlacementRegion`,
  capacity, original/slid array bboxes, slide deltas, and rejected candidates;
- placement ownership conflicts, duplicate owners, priority conflicts, and
  overridden rules;
- attempts to move locked, fixed, mechanical, or edge-required components;
- unplaced references;
- candidate search failures for `DecouplingArray`, `PullupArray`, `NearPad`,
  `Satellite`, `Between`, `Inline`, rows/columns, and other automatic rules.

Propose `board.pln` changes (the optimization surface) such as:

- region size/position changes to give clusters and arrays legal room;
- keepout additions, shrinkage, expansion, or relocation when the report shows
  missing or over-broad exclusions;
- side / `effective_side` overrides when automatic side selection chooses a bad
  side, especially near edges or high-speed corridors;
- `DecouplingArray` spacing, stagger, rows, distance, side, and
  `effective_side` updates;
- `PullupArray` or strap-array owner, side, row/spacing, and
  `effective_side` updates;
- explicit `edge_required` and `access_side` corrections for connectors,
  buttons, antenna modules, mounting holes, and other access/mechanical parts;
- cluster/keepout/high-speed/power-region changes when ownership conflicts or
  region violations show the floorplan is wrong.

Do **not** recommend disabling `DecouplingArray` or `PullupArray` just because
placement failed. Keep those primitives and adjust their owner, side, region,
spacing, stagger, rows, or `effective_side` in `board.pln` unless the primitive
itself is broken. A temporary `placement.ppl` override is acceptable only to
unblock a single run, and manual `Anchor` placement is the final fallback —
both come after planner-intent (`board.pln`), regions, keepouts, side
selection, and array strategy have been reviewed. Architecture, power-island,
and floorplan decisions are never made in `placement.ppl`; they belong in
`board.pln`.

## Troubleshooting

- **Missing `Edge.Cuts`** — `--print-board` falls back to the `Board(...)`
  declaration or footprint-bounds. If the generator didn't emit an outline,
  declare `Board(width=..., height=..., origin_x=..., origin_y=...,
  emit_outline=True)` (in `board.pln`, so the emitted `placement.ppl` carries
  it), or use `--emit-outline-only outline.kicad_pcb` to debug outline
  generation separately.
- **Footprint bounds vs board bounds** — footprint bounds (from
  `--print-bounds`) describe the area occupied by components, not the board
  outline. A board can be larger than its footprint bounds; don't treat
  footprint bounds as `Board(...)` dimensions.
- **Project-loader vs `pcbnew` behavior** — opening a placed `.kicad_pcb`
  directly with `pcbnew placed.kicad_pcb` is the recommended check. If the
  file is part of a `.kicad_pro` project and the project's board reference
  doesn't match the new filename, KiCad's project loader may show stale or
  unexpected state.
- **Malformed UUIDs** — `pcb-place` validates any UUIDs it generates (for new
  objects only) before writing, and fails rather than emit a malformed
  identifier. If you see a UUID-related write failure, check for hand-edited
  or corrupted UUID fields in the source `.kicad_pcb`.
- **Collision/spacing failures** — inspect `--report-json` for collision
  pairs and spacing violations, then adjust the corresponding intent in
  `board.pln` (spacing/part-class, region room, or array strategy) and
  re-emit. The executor will search nearby legal positions for relative/
  automatic primitives (`Satellite`, `Orbit`, `DecouplingArray`,
  `PullupArray`, `Row`, `Column`, `Array`, `Between`, `Inline`) when
  `avoid_overlap` is enabled; explicit `Anchor` placements are not
  auto-adjusted unless `soft=True`.
- **Cluster strategy** — clusters move a functional neighborhood as a unit
  (anchor delta applied to all members). Mechanical objects (mounting holes,
  fiducials, board outline, mechanical keepouts) should be placed
  independently, not inside a cluster. `--print-clusters` shows parsed cluster
  declarations; a later rule that moves a cluster member individually emits a
  "moved by cluster ... later refined by ..." warning. Fix recurring cluster
  problems in the cluster's `board.pln` definition.
- **`NearPad` failures** — confirm the `parent` reference resolves (check
  `--list-refs`/`--list-aliases`) and that the named `pad` (or list of pads)
  exists on that footprint in the board file. `side="auto"` requires enough
  board/region space to find a legal candidate; in `board.pln`, set an
  explicit side or increase the distance if it can't find a placement.
- **Keepout failures** — a placement that overlaps a `Keepout(...)` rectangle
  fails by default. Either move the rule's target out of the keepout, narrow
  the keepout (in `board.pln`), or set `allow_keepout_overlap=True` on the rule
  (or `--allow-keepout-overlap` globally) only after reviewing why the overlap
  is acceptable.
- **Region violations** — a `region=...` placement must keep the footprint
  bounding box inside that `Region(...)` rectangle. Check `--print-regions`
  for the rectangle definition and `region_violations` in the report; widen the
  region or move the target in `board.pln`, or set `allow_outside_region=True`
  / `--allow-outside-region` only after review.

## Limitations

- `pcb-place` does **not** plan placements or infer architecture, power
  islands, clusters, or floorplans — that intent lives in `board.pln`
  (authored by AI from `pcb-plan inspect` hints).
- `pcb-place` does **not** route traces, generate vias, or emit copper zones.
- `pcb-place` only executes the rules in the emitted `placement.ppl`; it cannot
  infer intent that isn't expressed there (or upstream in `board.pln`).
- Reflow/optimization is deterministic mechanical repacking, not architecture
  decision-making.
- Engineering review in KiCad is required before routing and fabrication —
  placement output is a starting point, not a sign-off.

## Examples

Standard apply-and-review flow (executor + report interpretation):

```bash
pcb-place layout.kicad_pcb placement.ppl --print-board
pcb-place layout.kicad_pcb placement.ppl --dry-run --report-json pcb-place-report.json
# interpret pcb-place-report.json -> edit board.pln -> pcb-plan emit -> re-run
pcb-place layout.kicad_pcb placement.ppl -o layout.placed.kicad_pcb --report-json pcb-place-report.json
pcbnew layout.placed.kicad_pcb
```

Review reports for AI optimization (each finding maps to a `board.pln` edit):

```bash
pcb-place layout.kicad_pcb placement.ppl --dry-run \
  --high-speed-review --power-review --mechanical-review --ai-edit-hints \
  --report-json pcb-place-report.json
```

Explain a single degraded placement before adjusting `board.pln`:

```bash
pcb-place layout.kicad_pcb placement.ppl --dry-run --explain-placement U6
```

CI gate:

```bash
pcb-place layout.kicad_pcb placement.ppl --check --strict --report-json pcb-place-report.json
```

With a Zener/`pcb` netlist for semantic aliases:

```bash
pcb-place --netlist .pcb/build/default.net --list-aliases
pcb-place layout.kicad_pcb placement.ppl --netlist .pcb/build/default.net --dry-run
```

See [`src/pcb_place/README.md`](../../src/pcb_place/README.md) for the full
placement DSL, rule reference, and safety/validation model.
