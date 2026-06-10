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

A typical command is:

```bash
pcb build board.zen
pcb layout board.zen
pcb-place layout/SomeBoard/layout.kicad_pcb placement.ppl \
  --netlist .pcb/build/default.net \
  -o layout/SomeBoard/layout.placed.kicad_pcb
```

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

## Scope

Implemented today:

- KiCad `.kicad_pcb` footprint parsing and rewriting
- Deterministic footprint placement
- Python/Starlark-like `.ppl` placement DSL
- Exact and hierarchical suffix reference matching
- Optional Zener/pcb netlist alias imports for semantic instance paths
- JSON reports
- Dry-run, check, validation, and list-refs modes
- Basic validation for board-boundary, keepout-origin, and near-coincident placed footprints

Not implemented yet:

- Trace routing
- Via generation
- Copper zones
- Differential-pair tuning
- KiCad keepout-zone emission
- True footprint courtyard/collision geometry
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

Preview placement without writing:

```bash
pcb-place board.kicad_pcb placement.ppl --dry-run
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

Emit a machine-readable report, including alias diagnostics when `--netlist` is used:

```bash
pcb-place board.kicad_pcb placement.ppl --netlist .pcb/build/default.net --report-json report.json
```

## Placement DSL example

```python
Board(width = 70, height = 40, units = "mm")

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
Inline("FB1", a = "J1", b = "U1", t = 0.45, role = "filter")

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
| `Inline` | Like `Between`, defaulting rotation along the path. |
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
