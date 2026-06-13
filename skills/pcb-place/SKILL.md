---
name: pcb-place
description: Apply and debug deterministic KiCad footprint placement with the pcb-place CLI. Use when executing a placement.ppl file against a .kicad_pcb board, validating placement/collisions/spacing/keepouts/regions, diagnosing alias or reference errors, or reviewing a placed board before routing.
---

# pcb-place

## Purpose

`pcb-place` is the deterministic placement executor. It consumes a
`placement.ppl` part-placement file and a KiCad `.kicad_pcb` board, applies
footprint-level placement, validates the result (bounds, collisions, spacing,
keepouts, regions), and writes a placed `.kicad_pcb` while preserving KiCad
structure (UUIDs, groups, tracks, zones, etc.) via surgical patching.

`pcb-place` does **not**:

- plan placements (no inference from netlists/roles — that's `pcb-plan`)
- route traces, generate vias, or create copper zones
- replace engineering review

## Prerequisites

- `pcb-place` installed (`pip install .` or `pip install -e '.[dev]'`).
  Console script: `pcb-place`. If unavailable, fall back to
  `python -m pcb_place` (referred to as `pcb_place` below — both spellings
  invoke the same implementation).
- A KiCad board file (`.kicad_pcb`).
- A `placement.ppl` file (hand-written or from `pcb-plan emit`).
- Optional: a Zener/`pcb` netlist artifact (e.g. `.pcb/build/default.net`) for
  semantic alias resolution.

## Inputs and Outputs

| Artifact | Role |
| --- | --- |
| `board.kicad_pcb` (input) | Board to place footprints on. |
| `placement.ppl` (input) | Placement DSL: `Board`, `Region`, `Keepout`, `Anchor`, `Cluster`, `Satellite`, etc. |
| `--netlist` (optional input) | Semantic alias map (e.g. `MCU.U_MCU` -> `U6`). |
| `placed.kicad_pcb` (output) | Placed board, written atomically. Defaults to `<input>.placed.kicad_pcb` unless `-o`/`--in-place`. |
| `--report-json` (output) | Machine-readable report: board geometry, regions, keepouts, violations, deltas, alias diagnostics. |

## Primary Commands

```bash
# Inspect footprint/board geometry without applying placement
pcb-place board.kicad_pcb placement.ppl --print-bounds
pcb-place board.kicad_pcb placement.ppl --print-board
pcb-place board.kicad_pcb placement.ppl --print-regions
pcb-place board.kicad_pcb placement.ppl --print-clusters

# Preview placement without writing
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

When asked to apply or debug a `placement.ppl`:

1. **Run `--dry-run` before writing.** Always preview with `--dry-run
   --report-json <file>` first and review per-footprint deltas, "outside
   board" flags, and any warnings before producing real output.
2. **Check board bounds and `Edge.Cuts`.** Run `--print-board` to confirm
   which board geometry source is in effect (`Edge.Cuts`, `Board(...)`, or
   footprint-bounds fallback) — this affects where board-local coordinates
   land.
3. **Validate collisions/spacing/keepouts/regions.** Use `--dry-run
   --report-json` or `--validate` and inspect `region_violations`,
   `keepout_violations`, collision/spacing counts, and
   `priority_conflicts`/`overridden_rules`/`locked_move_attempts`.
4. **Diagnose unresolved references.** Run with `--strict` (and `--check
   --strict` in CI) to fail loudly on missing/ambiguous references rather
   than silently skipping rules. Use `--list-refs` to see available KiCad
   references.
5. **Diagnose aliases/netlist semantic paths.** Use `--netlist <file>
   --list-aliases` to confirm semantic names (e.g. `MCU.U_MCU`) resolve to the
   expected KiCad reference before relying on them in `placement.ppl`.
   Explicit `Alias("name", "ref")` rules in `placement.ppl` override imported
   netlist aliases.
6. **Recommend `pcbnew placed.kicad_pcb`** to open a standalone, separately
   written placed board copy — do not assume it auto-opens in an existing
   KiCad project.
7. **Warn about project-loader vs `pcbnew` differences.** If the board
   filename or KiCad project (`.kicad_pro`) association changes, the full
   KiCad project loader can behave differently than opening the `.kicad_pcb`
   directly with `pcbnew`. Call this out explicitly when renaming/copying
   board files.
8. **Preserve KiCad UUIDs/groups.** Don't suggest workflows that regenerate or
   strip UUIDs on existing footprints/pads/groups/tracks/vias/zones — only
   newly created objects (e.g. emitted `Edge.Cuts` outlines) get new UUIDs.
9. **Use `--warn-overlap` only for exploratory visualization**, not as a
   final validation gate — it demotes collision/spacing violations to
   warnings. Final validation should pass without `--warn-overlap` /
   `--allow-overlap`.
10. **Recommend incremental testing for a failing `.ppl`.** Comment out or
    remove rules to bisect which rule causes a failure; use
    `--print-clusters`/`--print-regions` to confirm declarations parsed as
    expected; re-run `--dry-run` after each change.
11. **Prefer planner intent fixes over executor hacks.** When `placement.ppl`
    was emitted by `pcb-plan`, fix recurring dry-run failures by proposing
    `board.pln` changes first. Use manual `Anchor` edits only as a last resort
    when the planning primitive cannot express the needed intent.

## Iterative Floorplanner: No-Abort Placement and Reflow

`pcb-place` behaves like a constraint-driven floorplanner, not a collection of
independent placement primitives. A single component that cannot place legally
is treated as evidence that the surrounding floorplan is over-constrained, not
as a hard failure.

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

When you see parked components or moved parents in the report, **fix the
floorplan, not just the rule**: enlarge the relevant `Region()`, free space near
the parent, adjust the power island / high-speed path, or relax spacing for the
neighbourhood. `board.pln` remains the primary optimization artifact. The
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
mounting-hole distribution, and to collect suggested `.ppl` edits for
AI-assisted iteration.

## AI-Assisted Placement Report Review

After every `pcb-place ... --dry-run --report-json pcb-place-report.json`,
Claude should inspect the report before recommending a write. Treat the report
as feedback to improve `board.pln` and regenerate `placement.ppl`, not as a
reason to hide failures with ad-hoc placement edits.

Inspect:

- collisions and near-collisions;
- spacing violations;
- keepout violations;
- region violations;
- outside-board placements and board-geometry source;
- array slide/clamp diagnostics, including candidate `PlacementRegion`,
  capacity, original/slid array bboxes, slide deltas, and rejected candidates;
- placement ownership conflicts, duplicate owners, priority conflicts, and
  overridden rules;
- attempts to move locked, fixed, mechanical, or edge-required components;
- unplaced references;
- candidate search failures for `DecouplingArray`, `PullupArray`, `NearPad`,
  `Satellite`, `Between`, `Inline`, rows/columns, and other automatic rules.

Propose `board.pln` changes such as:

- region size/position changes to give clusters and arrays legal room;
- keepout additions, shrinkage, expansion, or relocation when the report shows
  missing or over-broad exclusions;
- `effective_side` overrides when automatic side selection chooses a bad
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
spacing, stagger, rows, or `effective_side` unless the primitive itself is
broken. Manual `Anchor` placement is the last resort after planner intent,
regions, keepouts, side selection, and array strategy have been reviewed.

## Troubleshooting

- **Missing `Edge.Cuts`** — `--print-board` falls back to the `Board(...)`
  declaration or footprint-bounds. If the generator didn't emit an outline,
  declare `Board(width=..., height=..., origin_x=..., origin_y=...,
  emit_outline=True)` in `placement.ppl`, or use `--emit-outline-only
  outline.kicad_pcb` to debug outline generation separately.
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
  pairs and spacing violations. Adjust `Spacing(...)`/`PartClass(...)` or
  rule-level `clearance=...`, or allow `PlacementPolicy(avoid_overlap=True,
  ...)` to search nearby legal positions for relative/automatic primitives
  (`Satellite`, `Orbit`, `DecouplingArray`, `PullupArray`, `Row`, `Column`,
  `Array`, `Between`, `Inline`).
  Explicit `Anchor` placements are not auto-adjusted unless `soft=True`.
- **Cluster strategy** — use `Cluster(name, anchor=..., members=[...],
  placement=...)` to move a functional neighborhood as a unit (anchor delta
  applied to all members). Mechanical objects (mounting holes, fiducials,
  board outline, mechanical keepouts) should be placed independently, not
  inside a cluster. `--print-clusters` shows parsed cluster declarations; a
  later rule that moves a cluster member individually emits a "moved by
  cluster ... later refined by ..." warning.
- **`NearPad` failures** — confirm the `parent` reference resolves (check
  `--list-refs`/`--list-aliases`) and that the named `pad` (or list of pads)
  exists on that footprint in the board file. `side="auto"` requires enough
  board/region space to find a legal candidate; try an explicit `side=` or
  increase `distance=` if it can't find a placement.
- **Keepout failures** — a placement that overlaps a `Keepout(...)` rectangle
  fails by default. Either move the rule's target out of the keepout, narrow
  the keepout, or set `allow_keepout_overlap=True` on the rule (or
  `--allow-keepout-overlap` globally) only after reviewing why the overlap is
  acceptable.
- **Region violations** — a `region=...` placement must keep the footprint
  bounding box inside that `Region(...)` rectangle. Check `--print-regions`
  for the rectangle definition and `region_violations` in the report; widen
  the region, move the target, or set `allow_outside_region=True` /
  `--allow-outside-region` only after review.

## Limitations

- `pcb-place` does **not** plan placements — it has no netlist-based
  inference of roles, clusters, or floorplans (use `pcb-plan` for that).
- `pcb-place` does **not** route traces, generate vias, or emit copper zones.
- `pcb-place` only executes the rules in `placement.ppl`; it cannot infer
  intent that isn't expressed there.
- Engineering review in KiCad is required before routing and fabrication —
  placement output is a starting point, not a sign-off.

## Examples

Standard apply-and-review flow:

```bash
pcb-place layout.kicad_pcb placement.ppl --print-board
pcb-place layout.kicad_pcb placement.ppl --dry-run --report-json pcb-place-report.json
pcb-place layout.kicad_pcb placement.ppl -o layout.placed.kicad_pcb --report-json pcb-place-report.json
pcbnew layout.placed.kicad_pcb
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
