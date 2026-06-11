# pcb-place repository

This repository contains two complementary command-line tools for reviewable PCB
planning and placement workflows:

- [`pcb-plan`](src/pcb_plan/README.md) owns `board.pln` planning intent. It can
  initialize, update, review, and explain `board.pln`; infer placement, routing,
  and simulation intent; and emit deterministic `placement.ppl` files for
  execution.
- [`pcb-place`](src/pcb_place/README.md) is the deterministic placement executor.
  It consumes `placement.ppl`, validates placement rules, applies footprint
  placement to KiCad `.kicad_pcb` files, and writes placed boards.

The split is intentional: planning is knowledge-based and heuristic, while
execution is deterministic, reviewable, and CI-friendly. `pcb-plan` must not move
footprints or route traces; `pcb-place` must not infer planning strategy from
netlists or own `board.pln`.

## Repository structure

```text
.
├── README.md                  # Repository overview and structure
├── pyproject.toml             # Packaging and console-script metadata
├── src/
│   ├── pcb_plan/
│   │   ├── README.md          # pcb-plan and board.pln documentation
│   │   ├── __init__.py        # Import package for console scripts
│   │   ├── pcb_plan.py        # pcb-plan implementation
│   │   └── pcb_plan_facades/  # Internal migration/facade modules
│   └── pcb_place/
│       ├── README.md          # pcb-place DSL and executor documentation
│       ├── __init__.py        # Import package for console scripts
│       └── pcb_place.py       # pcb-place implementation
├── examples/                  # Example placement workflows and inputs
└── tests/                     # Unit and CLI integration tests
```

There are no root-level `pcb_plan.py` or `pcb_place.py` compatibility entry
points and no hyphenated tool directories under `src/`. The canonical
executable implementations live beside their READMEs in the importable packages
`src/pcb_plan/` and `src/pcb_place/`, which also provide the normal Python entry
point targets for installed console scripts.

## Overall workflow

`pcb-plan` and `pcb-place` fit after generated board creation and before manual
routing/review:

```text
Zener / Diode pcb design
    ↓
pcb build
    ↓
pcb layout
    ↓
pcb-plan init/update/review
    ↓
board.pln
    ↓
pcb-plan emit
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

pcb-plan init \
  --board layout.kicad_pcb \
  --netlist default.net \
  -o board.pln

pcb-plan emit \
  --pln board.pln \
  --board layout.kicad_pcb \
  --netlist default.net \
  -o placement.ppl

pcb-place layout.kicad_pcb placement.ppl --dry-run
pcb-place layout.kicad_pcb placement.ppl -o layout.placed.kicad_pcb
pcbnew layout.placed.kicad_pcb
```

For early use, you can skip the planner and write `placement.ppl` by hand, or
run the planner with only a KiCad board:

```bash
pcb-plan emit --board layout.kicad_pcb -o placement.ppl
pcb-place layout.kicad_pcb placement.ppl -o layout.placed.kicad_pcb
```

Legacy one-shot planner invocation is still supported for compatibility:

```bash
pcb-plan --board layout.kicad_pcb --netlist default.net --intent board.pln -o placement.ppl
```

## Tool responsibilities

### pcb-plan: own planning intent

`pcb-plan` owns the `board.pln` lifecycle and emits a readable `.ppl` placement
plan. It may infer roles such as connectors, ICs, decoupling capacitors, ESD
devices, high-speed differential interfaces, RF modules, power regulators,
pullups, and series passives.

Planner output is meant to be reviewed, edited, diffed, and re-run. It does
**not** route traces, tune differential pairs, validate impedance, certify EMI
behavior, or claim production readiness.

Planner quality depends on real board geometry and connectivity. Explicit
`board.pln` geometry overrides KiCad `Edge.Cuts`, and `Edge.Cuts` overrides the
last-resort footprint-extents fallback. Supplying a netlist lets `pcb-plan` build
multi-member connectivity clusters, infer differential pairs, and place support
passives with semantic helpers such as `Decoupling`, `NearPad`, `ESD`, `Pullup`,
`Series`, `Satellite`, and `Between`; review any `unplaced_components` in the
report.

See [`src/pcb_plan/README.md`](src/pcb_plan/README.md) for planner CLI details,
`board.pln` syntax, provenance, reports, routing/SI hooks, simulation hooks,
heuristics, and limitations.

### pcb-place: execute placement intent

`pcb-place` reads an explicit `.ppl` part-placement file and applies
deterministic footprint-level placement updates to a KiCad `.kicad_pcb` file. It
provides dry-run, check, validation, reporting, alias import, board-geometry
handling, keepout/region validation, and safe atomic writes.

Executor behavior should remain predictable and independent of planner
heuristics. It does **not** infer placement strategy from netlists and does
**not** route traces.

See [`src/pcb_place/README.md`](src/pcb_place/README.md) for the placement DSL,
safety checks, CLI reference, and executor-specific examples.

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

- `pcb-plan` never writes directly to `.kicad_pcb`; it reads or writes
  `board.pln`, emits `placement.ppl`, and can emit JSON/Markdown reports plus
  routing/simulation planning artifacts.
- `pcb-place` rewrites only matched footprint-level placement records and
  defaults to writing a separate output file unless `--in-place` is explicitly
  requested.
- Generated or executed placement must be reviewed in KiCad before routing and
  fabrication.
- High-speed routing, impedance, return paths, reference planes, DRC/ERC,
  manufacturability, thermal behavior, RF behavior, and EMI compliance remain
  engineering responsibilities.

## Documentation map

- [`src/pcb_plan/README.md`](src/pcb_plan/README.md): planner-specific
  documentation for `board.pln`, lifecycle commands, routing/SI constraints,
  simulation hooks, reports, and `.ppl` generation.
- [`src/pcb_place/README.md`](src/pcb_place/README.md): executor-specific
  documentation for applying `.ppl` files to KiCad boards.
- [`examples/`](examples): example placement workflows and input files.
- [`tests/`](tests): unit and CLI integration tests for both tools.

## Claude Skills

[`skills/`](skills) contains Claude Code skills that teach Claude how to drive
this repository's tools:

- [`skills/pcb-plan/SKILL.md`](skills/pcb-plan/SKILL.md) — `board.pln` and
  `placement.ppl` lifecycle: init/review/explain/update/emit, routing/SI
  constraints, stackup, differential pairs, simulation hooks, provenance.
- [`skills/pcb-place/SKILL.md`](skills/pcb-place/SKILL.md) — applying and
  debugging `placement.ppl` against a `.kicad_pcb` board: dry-run,
  validation, collisions/spacing/keepouts/regions, troubleshooting.
- [`skills/pcb-automation-orchestrator/SKILL.md`](skills/pcb-automation-orchestrator/SKILL.md) —
  end-to-end iteration across `pcb-plan`, `pcb-place`, routing, DRC/ERC, and
  optional OpenEMS/ngspice simulation, including all-net and high-speed
  routing modes.

Short workflow: use `pcb-plan` to own `board.pln` and emit `placement.ppl`,
use `pcb-place` to apply/debug that `placement.ppl` against a KiCad board, and
use `pcb-automation-orchestrator` to coordinate the full
plan/place/route/verify/simulate loop. See [`skills/README.md`](skills/README.md)
for details on when to use each.
