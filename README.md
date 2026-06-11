# pcb-place

`pcb-place` is a deterministic footprint placement layer for KiCad `.kicad_pcb` files.

It bridges the gap between generated electrical design and physical PCB layout by letting placement intent be expressed as code. The core idea is simple: schematic hierarchy is not placement hierarchy. A microcontroller, its decoupling capacitors, its crystal, and its debug connector may belong to the same logical module, but each has a different physical placement requirement.

`pcb-place` does **not** route traces. It produces repeatable, reviewable component placement so that routing, DRC, and engineering review can happen in KiCad or another downstream PCB workflow.

## Intended workflow with pcb / Zener

`pcb-place` is designed to complement hardware-as-code flows such as [`pcb`](https://github.com/diodeinc/pcb) and Zener:

```text
Zener
  ↓
pcb build
  ↓
pcb layout
  ↓
pcb-place --netlist .pcb/build/default.net
  ↓
KiCad
  ↓
routing / review
```

A recommended conservative workflow is:

```bash
pcb build board.zen
pcb layout board.zen
pcb-place layout.kicad_pcb --print-board
pcb-place layout.kicad_pcb placement.ppl --dry-run
pcb-place layout.kicad_pcb placement.ppl -o layout.placed.kicad_pcb
# then open layout.placed.kicad_pcb in KiCad
```

Avoid `--in-place` until the placed file has opened successfully in KiCad. The default write path is atomic and writes to a separate `.placed.kicad_pcb` file unless you explicitly opt into in-place editing.

In this workflow:

- Zener expresses electrical intent: components, nets, hierarchy, interfaces, and constraints.
- `pcb build` emits a netlist artifact that may preserve semantic instance paths such as `MCU.U_MCU` or `PWR.U_BUCK`.
- `pcb layout` creates or updates the KiCad PCB artifact.
- KiCad footprints may have generated raw references such as `U1`, `U2`, `C1`, and `C2`. Those references are valid, but they are not stable semantic placement names.
- `pcb-place --netlist ...` imports a semantic alias map so placement intent can use names from the design hierarchy.
- KiCad remains the editable board file and review environment.

Netlist aliasing matters because physical intent often belongs to semantic instances, not generated references. For example, a placement file can now say:

```python
Anchor("MCU.U_MCU", x=35, y=20)
Satellite("MCU.C_VDD_1.C", parent="MCU.U_MCU", side="top")
```

Raw KiCad references still work unchanged, so `Anchor("U6", x=35, y=20)` remains valid. Explicit `Alias("name", "ref")` rules in `placement.ppl` override imported netlist aliases when both define the same semantic name.

`pcb-place` can also be used without `pcb`:

```text
KiCad schematic / imported netlist
              ↓
KiCad PCB with footprints
              ↓
pcb-place placement.ppl
              ↓
Floorplanned KiCad PCB
```

## Why this exists

Most generated PCB workflows can produce components and nets. Placement is often still manual, heuristic, or tied too closely to schematic hierarchy.

`pcb-place` models physical roles instead:

- Mounting holes belong in corners.
- Connectors belong on edges.
- Primary ICs are anchors.
- Decoupling capacitors are satellites of power pins or ICs.
- ESD devices belong between connectors and sensitive ICs.
- Ferrites and current monitors are inline with power or signal paths.
- RF antennas and mechanical features create keepouts.
- Repeated channels should be copied or mirrored deterministically.

This allows placement to be version-controlled, regenerated, reviewed, and checked in CI.

## Cluster-Based Floorplanning

`Cluster()` is the preferred mechanism for moving an existing functional PCB neighborhood. A cluster has a named anchor footprint, a set of member footprints, and an unbound placement primitive that positions the anchor. `pcb-place` computes the anchor delta and applies that same `dx`/`dy` to every cluster member, preserving each member's local geometry and rotation.

Use clusters for electrical neighborhoods such as HDMI channels, MCU subsystems, WiFi sections, power regulators, and user I/O groups. Mechanical objects should remain independent primitives: place mounting holes, fiducials, tooling holes, board outlines, and mechanical keepouts directly with `Corner()`, `Edge()`, `Anchor()`, `Keepout()`, or board geometry rather than putting them into clusters.

Recommended floorplanning flow:

1. Place fixed mechanical objects.
2. Place clusters.
3. Refine cluster internals with individual primitives.
4. Validate bounds, overlaps, spacing, and keepouts.
5. Route downstream.

Example: move an HDMI input neighborhood by placing connector `J1` on the left edge, carrying its ESD, passives, test points, and switch along with it:

```python
Cluster(
    "HDMI_IN",
    anchor = "J1",
    members = ["J1", "D3", "D4", "R21", "R22", "R23", "C28", "C31", "C32", "C33", "C34", "C36", "TP4", "TP5", "TP6", "SW1"],
    placement = Edge(edge = "left", y = 20, inset = 2.5),
)
```

Example: move an MCU neighborhood to a board coordinate while keeping decouplers and pull resistors in their imported relative positions:

```python
Cluster(
    "MCU",
    anchor = "U6",
    members = ["U6", "C5", "C6", "C7", "C8", "C9", "C10", "C11", "C12", "C13", "R1", "R2", "R3"],
    placement = Anchor(x = 36, y = 30),
)
```

Clusters do not lock members. Later rules execute after earlier rules and can refine any member. This lets you place a neighborhood first, then tighten important parts:

```python
Cluster("MCU", anchor = "U6", members = ["U6", "C5", "C6"], placement = Anchor(x = 36, y = 30))
Satellite("C5", parent = "U6", side = "top", distance = 2)
```

When a later rule refines a member already moved by a cluster, `pcb-place` emits a warning such as `C5 moved by cluster MCU later refined by Satellite`. Cluster placement is deterministic and does not auto-spread members; run validation to catch board-boundary, overlap, spacing, and keepout issues after placement.

Useful cluster reporting commands:

```bash
pcb-place board.kicad_pcb placement.ppl --print-clusters
pcb-place board.kicad_pcb placement.ppl --report-json report.json --dry-run
```

## Scope

Implemented today:

- KiCad `.kicad_pcb` footprint parsing and rewriting
- Deterministic footprint placement
- Cluster-based floorplanning for moving functional PCB neighborhoods while preserving internal geometry
- Python/Starlark-like `.ppl` placement DSL
- Exact and hierarchical suffix reference matching
- Optional Zener/pcb netlist alias imports for semantic instance paths
- JSON reports with board geometry, geometry source, footprint bounds, placement bounds, and per-placement delta information
- Dry-run, check, validation, print-bounds, print-board, emit-outline-only, and list-refs modes
- Conservative KiCad rewriting that only changes matched footprint-level `(at ...)` expressions
- Rotation preservation by default, with explicit opt-in for path-aligned/computed rotations
- Safe-by-default pre-write validation for non-finite coordinates, large moves, outside-board placement, duplicate targets, unresolved aliases, and locked footprint movement
- Atomic output writes and post-write parser sanity checks
- Validation for board-boundary, keepout-origin, footprint bounding-box collisions, spacing rules, and near-coincident placed footprints

Not implemented yet:

- Trace routing
- Via generation
- Copper zones
- Differential-pair tuning
- KiCad keepout-zone emission
- KiCad locked-footprint flags

## Install

From the repository root:

```bash
pip install .
```

For development:

```bash
pip install -e '.[dev]'
pytest
```

## CLI quick start

Write a placed copy of a board:

```bash
pcb-place board.kicad_pcb placement.ppl -o board.placed.kicad_pcb
```

Edit the input board in place, writing a `.bak` backup:

```bash
pcb-place board.kicad_pcb placement.ppl --in-place
```

Print existing footprint coordinate bounds and any Edge.Cuts-derived board bounds. Footprint bounds are reported separately because they are not board bounds:

```bash
pcb-place board.kicad_pcb --print-bounds
```

Print the authoritative board geometry that `pcb-place` can see:

```bash
pcb-place board.kicad_pcb --print-board
pcb-place board.kicad_pcb placement.ppl --print-board
```

Preview placement without writing. Dry-run output lists each changed footprint, original and new x/y/rotation, x/y delta, rotation-change status, and whether the new point is outside the declared board:

```bash
pcb-place board.kicad_pcb placement.ppl --dry-run
```

Infer the board origin before applying board-local placement rules. The source is printed explicitly. `pcb-place` prefers Edge.Cuts, then the `Board(...)` definition, then a footprint-bounds fallback:

```bash
pcb-place board.kicad_pcb placement.ppl --infer-origin --dry-run
```

Run lightweight validation without writing:

```bash
pcb-place board.kicad_pcb placement.ppl --validate
```

Fail CI if placement is stale:

```bash
pcb-place board.kicad_pcb placement.ppl --check
```

Use strict mode to fail on missing or ambiguous references:

```bash
pcb-place board.kicad_pcb placement.ppl --check --strict
```

List footprint references:

```bash
pcb-place board.kicad_pcb --list-refs
pcb-place board.kicad_pcb --list-refs --format json
```

List semantic aliases imported from a netlist:

```bash
pcb-place --netlist .pcb/build/default.net --list-aliases
pcb-place --netlist .pcb/build/default.net --list-aliases --format json
```

Emit a machine-readable report, including alias diagnostics when `--netlist` is used. Reports include the resolved board origin, input footprint bounds, board bounds, and per-placement delta/rotation/outside-board fields:

```bash
pcb-place board.kicad_pcb placement.ppl --netlist .pcb/build/default.net --report-json report.json
```

`--safe` is enabled by default. Use the explicit override flags only after reviewing the dry-run/report output:

```bash
pcb-place board.kicad_pcb placement.ppl --allow-outside-board
pcb-place board.kicad_pcb placement.ppl --allow-large-move
pcb-place board.kicad_pcb placement.ppl --no-safe
```

If you want mechanically simple rotations, `--cardinal-rotations` rounds explicit rotations to the nearest `0`, `90`, `180`, or `270` degrees unless an individual rule sets `allow_arbitrary_rotation=True`.


## Placement Safety

`pcb-place` checks placed footprint bounding boxes instead of treating footprint center points as sufficient physical geometry.  It parses common KiCad footprint primitives such as pads and footprint graphics to estimate each footprint's occupied area, reports fallback bounding boxes when exact geometry is unavailable, and validates both direct overlaps and minimum spacing.

Spacing is configurable globally:

```python
Spacing(
    default=0.25,
    passive_to_ic=0.50,
    connector=1.00,
)

PartClass("U1", "ic")
PartClass("R1", "passive")
```

If `PartClass(...)` is omitted, `pcb-place` infers classes from reference prefixes such as `R`/`C`/`L` for passive components, `U` for ICs, `J`/`P` for connectors, and `H`/`MH` for mechanical parts.  Individual placement rules can also request a one-off clearance, for example `Satellite("R1", parent="U1", side="top", distance=2, clearance=0.5)`.

Explicit absolute anchors remain deterministic and are not automatically moved by default.  Relative and automatic placement primitives (`Satellite`, `Orbit`, `Row`, `Column`, `Array`, `Between`, and `Inline`) use the placement policy to search nearby legal candidate locations when the requested location would collide or violate spacing:

```python
PlacementPolicy(
    avoid_overlap=True,
    allow_anchor_move=False,
    max_search_radius=5,
    search_step=0.5,
)
```

Writes fail on collisions by default.  Use `--warn-overlap` to demote overlap/spacing diagnostics to warnings, or `--allow-overlap` only after reviewing the output.  Dry runs and JSON reports include collision counts, spacing violations, fallback bounding-box warnings, and any auto-adjusted placements.

For dense PCB work, run the safety report before writing output:

```bash
pcb-place board.kicad_pcb placement.ppl --dry-run --report-json report.json
```

## Board Geometry

`pcb-place` treats board geometry as a first-class model rather than assuming that footprint extents define the PCB. This matters for generated boards: a 70 mm × 70 mm board may have footprints occupying only a 54 mm × 61 mm area, so footprint bounds are useful diagnostics but are not the board outline.

Board geometry is resolved in this priority order:

1. KiCad `Edge.Cuts` geometry (`gr_rect` or a rectangular outline made from `gr_line` segments).
2. The placement file `Board(width=..., height=..., origin_x=..., origin_y=...)` definition.
3. Footprint bounds as an explicit fallback when no board geometry is available.

Use `--print-board` after generating a board to confirm what source will be used:

```bash
pcb build board.zen
pcb layout board.zen
pcb-place layout.kicad_pcb --print-board
pcb-place layout.kicad_pcb placement.ppl --dry-run
pcb-place layout.kicad_pcb placement.ppl -o placed.kicad_pcb
```

If the source generator emits the `Edge.Cuts` layer but not an actual outline, declare the board in the placement DSL and request outline emission:

```python
Board(
    width=70,
    height=70,
    origin_x=140,
    origin_y=52,
    emit_outline=True,
)
```

When `emit_outline=True`, `pcb-place` writes a rectangular `Edge.Cuts` outline only if no outline already exists. If an existing outline conflicts with the declared board, the run fails with a clear error instead of duplicating or silently replacing geometry. For debugging, you can emit just the outline without applying placements:

```bash
pcb-place board.kicad_pcb placement.ppl --emit-outline-only outline.kicad_pcb
```

## UUID Preservation

pcb-place preserves all existing KiCad UUID text byte-for-byte, including UUIDs on footprints, pads, groups, tracks, vias, zones, drawings, graphics, text, and dimensions. It is a placement tool, not a UUID management tool, so existing KiCad metadata identifiers are immutable during placement rewrites.

Only newly-created objects receive UUID identifiers. Generated objects such as emitted `Edge.Cuts` graphics are assigned canonical UUIDv4 strings from Python's standard `uuid.uuid4()` path, and those generated UUIDs are validated before writing so pcb-place fails rather than emitting malformed identifiers. This minimizes risk of KiCad metadata corruption.

## Placement DSL example

```python
Board(
    width = 70,
    height = 40,
    origin_x = 140,
    origin_y = 60,
    units = "mm",
)

Region("CONTROL", x = 20, y = 10, w = 30, h = 20)
Region("POWER",   x = 0,  y = 25, w = 20, h = 15)

Corner("H1", corner = "top_left",     inset = 3)
Corner("H2", corner = "top_right",    inset = 3)
Corner("H3", corner = "bottom_left",  inset = 3)
Corner("H4", corner = "bottom_right", inset = 3)

Edge("J1", edge = "left",  y = 20, rot = 270, role = "input_connector")
Edge("J2", edge = "right", y = 20, rot = 90,  role = "output_connector")

Anchor("U1", region = "CONTROL", role = "primary_controller")
Lock("U1", reason = "primary floorplan anchor")

Anchor("U2", relative_to = "U1", dx = 15, dy = 0, role = "secondary_ic")

Between("D1", a = "J1", b = "U1", t = 0.20, offset = -2, role = "protection")
Inline("FB1", a = "J1", b = "U1", t = 0.45, align = "path", role = "filter")

Satellite("C1", parent = "U1", side = "top", distance = 2, role = "decoupling")
Orbit(refs = ["C2", "C3", "C4"], parent = "U1", radius = 4, start_angle = 210, step_angle = 30)

Row(["LED1", "LED2", "LED3"], start = (10, 36), pitch = 4)
Column(["TP1", "TP2", "TP3"], start = (60, 10), pitch = 3)
Grid(refs = ["R1", "R2", "R3", "R4"], start = (20, 30), columns = 2, pitch = (3, 2))

Mirror("J3", source = "J1", axis = "vertical")

CopyPlacement(
    source_prefix = "CH1.",
    target_prefix = "CH2.",
    dx = 20,
    dy = 0,
)

Keepout("ANTENNA", x = 55, y = 5, w = 12, h = 16, layers = "all")
Corridor("HIGH_SPEED", a = "J1", b = "U1", width = 8, clearance = 1)
```

The DSL also supports a fluent component style for users who prefer to group placement intent by footprint:

```python
Component("U1").anchor(region = "CONTROL").lock(reason = "main controller")
Component("C1").satellite(parent = "U1", side = "top", distance = 2, role = "decoupling")
Component("D1").between(a = "J1", b = "U1", t = 0.2, role = "protection")
```

Both styles produce the same internal placement model.

### Board origins and generated layouts

KiCad boards generated by `pcb layout` / Zener do not always use `(0, 0)` as the practical board-local placement origin. Footprints may already live in a generated coordinate space such as x≈140 mm and y≈60 mm. `pcb-place` treats DSL placement coordinates as **board-local** and resolves them as:

```text
absolute_x = origin_x + local_x
absolute_y = origin_y + local_y
```

Existing placement files continue to work because `origin_x` and `origin_y` default to `0`. For generated layouts, either declare the origin explicitly in `Board(...)` or inspect/infer it from the input board:

```bash
pcb-place layout.kicad_pcb placement.ppl --print-bounds
pcb-place layout.kicad_pcb placement.ppl --infer-origin --dry-run
```

The first origin inference implementation uses the current minimum footprint x/y as `origin_x`/`origin_y` and reports the inferred values. It is only applied when `--infer-origin` is supplied.

### Rotation behavior

`pcb-place` is conservative about rotation. If a rule changes only x/y and does not explicitly set `rot`, the original footprint `(at ...)` rotation form is preserved exactly: footprints that had no rotation remain without a rotation field, and footprints that had a rotation keep it. A new rotation is written only when the placement rule explicitly sets `rot`, `rot = "path"`, or `align = "path"`.

For path-based helpers such as `Between` and `Inline`, computed path rotation is opt-in:

```python
Inline("FB1", a = "J1", b = "U1", t = 0.45)                 # preserve rotation
Inline("FB1", a = "J1", b = "U1", t = 0.45, rot = "path")  # align to path
Inline("FB1", a = "J1", b = "U1", t = 0.45, align = "path")
```

Explicit rotations are normalized into `[0, 360)`. With `--cardinal-rotations`, explicit rotations are rounded to the nearest cardinal direction unless the rule includes `allow_arbitrary_rotation=True`.

## Rule reference

| Rule | Purpose |
|---|---|
| `Board` | Defines board width, height, units, origin, and optional name. |
| `Region` | Defines a named rectangular placement area. |
| `Alias` | Maps a stable DSL name to a KiCad reference. |
| `Anchor` | Places a footprint at an explicit coordinate, region center, or relative coordinate. |
| `Fixed` | Same as `Anchor`, but semantically fixed/locked. |
| `Lock` | Prevents later rules from moving a footprint. |
| `Corner` | Places a footprint in a board corner. |
| `Edge` | Places a footprint along a board edge. |
| `Between` | Places a footprint between two references. |
| `Inline` | Like `Between`; preserves rotation by default and aligns to the path only with `rot="path"` or `align="path"`. |
| `Satellite` | Places a support part near a parent footprint. |
| `Orbit` | Distributes support parts around a parent footprint. |
| `Array` | Places references with arbitrary pitch. |
| `Row` / `Column` | Convenience linear placement rules. |
| `Grid` | Row-major rectangular placement. |
| `Mirror` | Places one footprint as a mirror of another. |
| `CopyPlacement` | Copies repeated-channel placement from one hierarchical prefix to another. |
| `Keepout` | Parsed, reported, and used by basic validation; KiCad geometry not emitted yet. |
| `Corridor` | Parsed and reported; KiCad geometry not emitted yet. |
| `Component` | Fluent wrapper around the rule functions. |

## Reference matching

`pcb-place` prefers exact KiCad footprint references. It can also resolve suffixes for hierarchical references, so a rule for `U1` can match `MCU.U1` when unambiguous.

Disable suffix matching with:

```bash
pcb-place board.kicad_pcb placement.ppl --no-suffix-match
```

Use strict mode in CI to fail on missing or ambiguous references:

```bash
pcb-place board.kicad_pcb placement.ppl --strict --check
```

## Validation

`--validate` runs lightweight checks after applying the placement model in memory:

- placed footprint origins outside `Board(...)` bounds
- placed footprint origins inside declared `Keepout(...)` rectangles
- near-coincident origins among footprints touched by the current placement run
- locked-footprint movement violations
- missing or ambiguous references when used with `--strict`

`--safe` is enabled by default before writes and fails earlier for dangerous output conditions:

- NaN or infinite placed coordinates/rotations
- placements outside `Board(origin_x, origin_y, width, height)` unless `--allow-outside-board` is supplied
- placements far outside the original footprint bounds unless `--allow-large-move` is supplied
- duplicate resolved placement targets
- unresolved aliases/references
- locked footprints that would be moved

After writing, `pcb-place` re-parses the output with its own KiCad parser, verifies footprint count/reference preservation, and checks parentheses balance.

Example CI command:

```bash
pcb-place board.kicad_pcb placement.ppl --check --strict --report-json report.json
```

A successful `--check` means the board is already consistent with the placement file. A failing `--check` means generated placement changes are pending.

## Design philosophy

`pcb-place` is intentionally a placement tool, not an autorouter.

Good PCB layout starts with good floorplanning. By making placement intent explicit, repeatable, and reviewable, `pcb-place` aims to reduce the amount of manual dragging needed after board generation while preserving the engineer's ability to route and review the board in KiCad.

The project is built around three principles:

1. **Electrical intent and physical intent are different artifacts.**
2. **Placement rules should express relationships, not just coordinates.**
3. **Generated boards should remain ordinary KiCad files.**

## Development

Run tests:

```bash
pytest
```

Run the example:

```bash
pcb-place examples/simple-mcu/board.kicad_pcb examples/simple-mcu/placement.ppl --dry-run
```

Build a wheel/sdist:

```bash
python -m build
```

## Roadmap

Near-term:

- true footprint bounding-box / courtyard overlap checks
- optional KiCad locked-footprint emission
- keepout-zone drawing/emission
- richer region placement policies
- better reports for CI and generated-board review

Longer-term:

- placement constraint solving
- power-domain-aware placement helpers
- differential-pair corridor visualization
- RF-aware placement helpers
- integration examples for generated `pcb`/Zener projects

## License

MIT.
