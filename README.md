# pcb-place

This repository provides two complementary command-line tools for reviewable PCB placement workflows. The intended direction is to build from Zener board designs into a complete **board-as-code build definition** workflow: electrical design, generated layout artifacts, planner floorplan intent, deterministic placement execution, and final KiCad engineering review.

- [`pcb-plan`](README-plan.md) is an experimental, heuristic planner. It reads a KiCad board plus optional Zener/pcb netlist and `.pln` board-plan files, infers placement intent from pins, nets, footprints, and roles, then generates an explicit `.ppl` part-placement file.
- [`pcb-place`](README-place.md) is the deterministic placement executor. It reads a `.ppl` placement plan and a KiCad `.kicad_pcb` file, then writes a placed KiCad board.

The split is intentional: planning is knowledge-based and heuristic, while execution is deterministic, reviewable, and CI-friendly.

## Overall workflow

`pcb-plan` and `pcb-place` are designed to fit after generated board creation and before manual routing/review:

```text
Zener / Diode pcb design
    ↓
pcb build
    ↓
pcb layout
    ↓
pcb-plan
    ↓
placement.ppl
    ↓
pcb-place
    ↓
placed KiCad board
    ↓
manual high-speed review / routing / DRC
```

A conservative command-line workflow is:

```bash
pcb build board.zen
pcb layout board.zen
pcb-plan --board layout.kicad_pcb --netlist default.net --intent board.pln -o placement.ppl
pcb-place layout.kicad_pcb placement.ppl --dry-run
pcb-place layout.kicad_pcb placement.ppl -o layout.placed.kicad_pcb
pcbnew layout.placed.kicad_pcb
```

For early use, you can skip the planner and write `placement.ppl` by hand, or run the planner with only a KiCad board:

```bash
pcb-plan --board layout.kicad_pcb -o placement.ppl
pcb-place layout.kicad_pcb placement.ppl -o layout.placed.kicad_pcb
```

## Tool responsibilities

### pcb-plan: generate placement intent

`pcb-plan` reads board/netlist/`.pln` artifacts and emits a readable `.ppl` file. It may infer roles such as connectors, ICs, decoupling capacitors, ESD devices, high-speed differential interfaces, RF modules, power regulators, pullups, and series passives.

Planner output is meant to be reviewed, edited, diffed, and re-run. It does **not** route traces, tune differential pairs, validate impedance, certify EMI behavior, or claim production readiness.

See [`README-plan.md`](README-plan.md) for planner CLI details, supported inputs, generated reports, `--explain REF`, heuristics, and limitations.

### pcb-place: execute placement intent

`pcb-place` reads an explicit `.ppl` part-placement file and applies deterministic footprint-level placement updates to a KiCad `.kicad_pcb` file. It provides dry-run, check, validation, reporting, alias import, board-geometry handling, keepout/region validation, and safe atomic writes.

Executor behavior should remain predictable and independent of planner heuristics. It does **not** infer placement strategy from netlists and does **not** route traces.

See [`README-place.md`](README-place.md) for the placement DSL, safety checks, CLI reference, and executor-specific examples.

## Installation

From the repository root:

```bash
pip install .
```

For development:

```bash
pip install -e '.[dev]'
pytest
```

The installed console scripts are:

```bash
pcb-plan --help
pcb-place --help
```

## Safety model

Both tools are sidecars around KiCad files:

- `pcb-plan` never writes directly to `.kicad_pcb`; it reads optional `.pln` board-plan files and only emits `.ppl` part-placement files plus optional JSON reports.
- `pcb-place` rewrites only matched footprint-level placement records and defaults to writing a separate output file unless `--in-place` is explicitly requested.
- Generated or executed placement must be reviewed in KiCad before routing and fabrication.
- High-speed routing, impedance, return paths, reference planes, DRC/ERC, manufacturability, thermal behavior, RF behavior, and EMI compliance remain engineering responsibilities.

## Documentation map

- [`README-plan.md`](README-plan.md): planner-specific documentation for generating `.ppl` files.
- [`README-place.md`](README-place.md): executor-specific documentation for applying `.ppl` files to KiCad boards.
- [`examples/`](examples): example placement workflows and input files.
