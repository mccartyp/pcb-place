# pcb-place repository

This repository contains two complementary command-line tools for reviewable PCB
planning and placement workflows. The architecture separates concerns:
**`pcb-plan` extracts facts, the AI (Claude) generates design intent, and
`pcb-place` executes intent.**

```text
KiCad PCB + netlist + stackup + board dimensions
  -> pcb-plan inspect       (extract board intelligence -> planning-hints/)
  -> Claude generates board.pln   (authoritative design intent, AI-authored)
  -> pcb-plan check / validate
  -> pcb-plan emit          (board.pln -> placement.ppl)
  -> pcb-place              (deterministic execute -> reports)
  -> Claude optimization loop     (analyze reports, edit board.pln, repeat)
```

- [`pcb-plan`](src/pcb_plan/README.md) is a **board-intelligence extractor** and
  `board.pln` reviewer. Its `inspect` command extracts facts (geometry,
  connectivity) and *candidate hints* (roles, functional paths, power islands,
  edge connectors, mechanicals, RF zones) into a `planning-hints/` directory for
  AI planning. It also reviews, validates (`check`), updates, and emits
  deterministic `placement.ppl` from `board.pln`.
- The AI (Claude) is the **primary planning engine**: it turns `planning-hints/`
  into `board.pln`, the authoritative design-intent document, and iterates on it
  using placement/routing/simulation reports.
- [`pcb-place`](src/pcb_place/README.md) is the **deterministic placement
  executor**. It consumes `placement.ppl`, applies/reflows/optimizes footprint
  placement, validates placement rules, writes placed KiCad `.kicad_pcb` files,
  and reports.

The split is intentional. `pcb-plan inspect` must **not** place components,
create placement ownership, build topology clusters heuristically, or infer a
final floorplan — those are AI planning problems solved in `board.pln`.
`pcb-plan` must not move footprints or route traces; `pcb-place` must not infer
planning strategy, power islands, or a final floorplan. KiCad groups are exported
as metadata only and must never drive placement.

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
pcb-plan inspect        (extract facts -> planning-hints/)
    ↓
Claude generates board.pln   (AI is the primary planning engine)
    ↓
pcb-plan check          (validate intent)
    ↓
Claude review/optimization
    ↓
pcb-plan emit
    ↓
placement.ppl
    ↓
pcb-place
    ↓
placed KiCad board + reports
    ↓
Claude optimization loop (edit board.pln, repeat)
    ↓
manual high-speed review / routing / DRC
```

A conservative command-line workflow is:

```bash
pcb build board.zen
pcb layout board.zen

# 1. Extract board intelligence into planning-hints/ (facts + candidate hints).
pcb-plan inspect \
  --board layout.kicad_pcb \
  --netlist default.net \
  --width 75 --height 75 \
  --out planning-hints

# 2. Let Claude generate board.pln from planning-hints/ (see planning-hints/
#    ai-pln-prompt.md). board.pln is the authoritative design-intent document:
#    mechanical/edge constraints, regions, power islands, high-speed corridors,
#    functional paths, ownership, routing/SI, stackup, and simulation triggers.
#    The pcb-bootstrap Claude skill drives this step.

# (Legacy heuristic alternative: pcb-plan init writes a first-pass board.pln.)
pcb-plan init \
  --board layout.kicad_pcb \
  --netlist default.net \
  -o board.pln \
  --report-json pcb-plan-init-report.json

# Let Claude review/optimize board.pln before emit. Typical fixes include
# board geometry, regions, keepouts, edge_required/access_side metadata,
# DecouplingArray/PullupArray strategy, differential pairs, routing/SI
# constraints, and OpenEMS/ngspice triggers.

pcb-plan check \
  --pln board.pln \
  --board layout.kicad_pcb \
  --netlist default.net \
  --report-json pcb-plan-check-report.json

# Let Claude review/optimize pcb-plan-check-report.json.

pcb-plan emit \
  --pln board.pln \
  --board layout.kicad_pcb \
  --netlist default.net \
  -o placement.ppl

pcb-place layout.kicad_pcb placement.ppl --dry-run \
  --report-json pcb-place-report.json
# Let Claude review/optimize pcb-place-report.json and feed fixes back to
# board.pln rather than hand-editing placement.ppl when possible.
pcb-place layout.kicad_pcb placement.ppl -o layout.placed.kicad_pcb
pcbnew layout.placed.kicad_pcb
```

Concise AI-assisted workflow:

1. Extract facts and candidate hints with `pcb-plan inspect` into
   `planning-hints/`.
2. Let Claude generate `board.pln` from `planning-hints/` (the authoritative,
   AI-authored design-intent document). The `pcb-bootstrap` skill drives this.
3. Validate intent with `pcb-plan check`, then generate `placement.ppl` with
   `pcb-plan emit`.
4. Apply with `pcb-place` after reviewing the dry-run report.
5. Iterate using placement, routing, DRC/ERC, OpenEMS, and ngspice reports,
   feeding fixes back into `board.pln` (the optimization surface).

Run `pcb-plan check` after `init`/`update` and before `emit` to catch
low-confidence plans (missing nets, mostly-singleton clusters, mostly-unplaced
components, duplicate rules, missing differential pairs, rejected `Series(...)`
inferences, or footprint-extents board geometry) before generating
`placement.ppl`. AI review should prefer `board.pln` intent edits over manual
`placement.ppl` hacks and should preserve provenance for inferred changes. See
[`src/pcb_plan/README.md`](src/pcb_plan/README.md) for the `plan_confidence`
model and the `--strict-confidence`/`--allow-low-confidence` flags on `emit`.

For early use, you can skip the planner and write `placement.ppl` by hand, or
run the planner with only a KiCad board:

```bash
pcb-plan emit --board layout.kicad_pcb -o placement.ppl
pcb-place layout.kicad_pcb placement.ppl -o layout.placed.kicad_pcb
```

When initializing `board.pln` before Edge.Cuts are reliable, pass explicit board geometry so generated regions and edge-aware placements use the intended outline:

```bash
pcb-plan init --board layout.kicad_pcb --width 75 --height 75 -o board.pln
```

Legacy one-shot planner invocation is still supported for compatibility:

```bash
pcb-plan --board layout.kicad_pcb --netlist default.net --intent board.pln -o placement.ppl
```

## Board intelligence extraction: `pcb-plan inspect`

`pcb-plan inspect` is the entry point of the AI planning workflow. It is a
board-intelligence **extractor**, not a floorplanner: it reads facts and emits
*candidate hints* for an AI (or human) to turn into an authoritative
`board.pln`.

```bash
pcb-plan inspect \
  --board layout.kicad_pcb \
  --netlist default.net \
  --width 75 --height 75 \
  --out planning-hints
```

It writes a `planning-hints/` directory:

```text
planning-hints/
  board-hints.json                 # master facts + candidate hints
  board-hints.md                   # human-readable summary
  component-table.csv              # per-component facts (role, bbox, nets)
  connectivity-graph.json          # component/net graph + ownership hints
  footprint-bboxes.json            # bbox, centroid, area per component
  pad-locations.json               # absolute/local pad coordinates and nets
  candidate-functional-paths.json  # connector -> protection -> IC paths
  candidate-power-islands.json     # regulator + hot-loop topology
  candidate-high-speed-paths.json  # high-speed subset of functional paths
  candidate-edge-connectors.json   # access side / auto-rotation / edge_required hints
  candidate-mechanicals.json       # mounting-hole corner assignments
  candidate-rf-zones.json          # RF antenna keepout candidates
  routing-classes.json             # candidate routing classes (impedance needs fab stackup)
  ai-pln-prompt.md                 # prompt to help Claude generate board.pln
  ai-placement-review.md           # checklist for reviewing board.pln vs hints
```

`pcb-plan inspect` must **not** attempt final placement, create topology
clusters heuristically, create placement ownership automatically, or infer a
final floorplan. Everything it emits is a candidate hint and is marked as such.
KiCad groups are exported under `imported_kicad_groups` with `metadata_only:
true` and **must not drive placement** — they are preserved for information only.

`board.pln` is the authoritative design-intent document. It is generated
primarily by AI from `planning-hints/` and contains mechanical constraints,
edge-required components, connector orientation/access sides, placement regions,
power islands, high-speed corridors, functional paths, ownership, routing
classes, SI constraints, stackup, and simulation triggers. The AI optimization
loop primarily edits `board.pln` (preferred), then `placement.ppl` overrides,
then manual `Anchor()` as a last resort. See the
[`pcb-bootstrap`](skills/pcb-bootstrap/SKILL.md) skill for generating it.

## Tool responsibilities

### pcb-plan: extract facts and review intent

`pcb-plan` extracts board intelligence (`inspect`), reviews and validates the
AI-authored `board.pln`, and emits a readable `.ppl` placement plan (`emit`). It
may infer *candidate* roles such as connectors, ICs, decoupling capacitors, ESD
devices, high-speed differential interfaces, RF modules, power regulators,
pullups, and series passives — as hints, not authoritative placement decisions.

`pcb-plan` no longer invents the floorplan: the AI generates `board.pln` from
the hints. `pcb-plan inspect` output and `board.pln` are meant to be reviewed,
AI-optimized, edited, diffed, validated with `check`, and emitted. `pcb-plan`
does **not** place components, route traces, tune differential pairs, validate
impedance, certify EMI behavior, or claim production readiness.

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

## Topology-aware placement model

Placement planning and execution follow a fixed priority model:

```text
mechanical constraints first
functional signal path topology second
power topology third
support passives fourth
spacing/clearance always enforced
high-speed/EMI best practices baked into scoring
```

### Groups vs clusters vs ownership

Groups are metadata. A component may belong to several semantic groups at once
(a retimer cluster, an HDMI path, a 1V2 power domain, a decoupling group), but
**final placement ownership is explicit and unique**: exactly one rule owns each
ref's final coordinates. Clusters only preserve coarse neighborhoods; they are
never atomic placement units, and a later higher-priority rule (ESD, power
island member, decoupling array, near-pad) refines members out of the cluster
move. `pcb-plan` reports this as `ownership_model` / `semantic_groups` /
`multi_group_components`; `pcb-place` reports the final winner per ref in
`ownership` and notes every cluster move that a refinement overrode.

Ownership priority (highest first): explicit fixed/locked mechanical,
edge-required connector, explicit anchor, functional-path placement
(ESD/Between/Series), DecouplingArray/NearPad, PullupArray, Satellite, cluster
fallback.

### Mechanical constraints first

Mounting holes are fixed/corner mechanical objects, never cluster members. If
`board.pln` defines `mechanical.mounting_holes`, those corners/locations win;
otherwise `pcb-plan` keeps holes that already sit at distinct corners and
redistributes clustered/colocated holes to distinct corners (`inset` 3 mm) with
a review warning. Board outline, keepouts (RF antenna, cable access), and edge
connectors are planned before any other component.

### Edge-required components

Connectors that leave the board (HDMI, USB, RJ45/Ethernet, DC jacks, card
slots) are inferred — or declared in `board.pln` — as `edge_required`. They are
edge-locked, never pulled inward by optimization, and rotated by access side:

```yaml
components:
  J1:
    role: hdmi_connector
    edge_required: true
    access_side: left
    allow_body_outside_board: true
    locked: true
    rotation: auto
```

`rotation: auto` uses the convention that at rot=0 the footprint's mating face
points toward the top board edge (-y), so auto maps top→0°, right→90°,
bottom→180°, left→270°; supply an explicit rotation when a footprint uses a
different convention. With `allow_body_outside_board: true` the connector body
or courtyard may extend past the board outline (an expected mechanical
condition, not an ordinary outside-board violation) while the footprint anchor
must stay on the board. Support parts stay inside the board unless explicitly
allowed.

### Functional paths and high-speed/EMI scoring

`pcb-plan` builds functional paths from the connectivity graph
(connector → ESD/protection → receiver/retimer) and emits them as
`HighSpeedPath(...)` declarations; `board.pln` `functional_paths:` entries pin
them explicitly. Paths reserve direct corridors before low-speed support parts
are placed: unrelated components are penalized out of corridors during
candidate scoring, and `pcb-place --high-speed-review` writes
`high-speed-placement-review.md` scoring each path for directness/straightness,
flow-through ESD position (protection belongs near the connector), corridor
intruders, and warnings requiring human review.

### Power islands and power/EMI scoring

Regulator sections are planned as topology-aware power islands, not generic
"power component piles": input capacitor tight to the regulator power pins,
inductor and output capacitor compact against the regulator, feedback network
at the FB pin away from the switch node — while downstream load decouplers stay
owned by their loads. `board.pln` `power_islands:` entries pin membership
explicitly. `pcb-place --power-review` writes `power-placement-review.md`
scoring cap/inductor distances, estimated hot-loop area, feedback proximity,
and separation from high-speed paths.

### Spacing profiles

Both tools default to a conservative, non-touching spacing profile and accept a
`board.pln` `spacing:` override:

```yaml
spacing:
  passive_to_passive: 0.25
  passive_to_ic: 0.40
  ic_to_ic: 0.75
  connector_to_component: 1.00
  mechanical_to_component: 1.00
```

Spacing is enforced during candidate scoring and validation. Arrays whose
requested pitch cannot satisfy clearance escalate their spacing instead of
emitting touching parts.

### Stackup profiles

`board.pln` supports reusable stackup templates for 2/4/6/8/10 layers:
`2_layer_basic`, `4_layer_signal_gnd_pwr_signal`, `6_layer_high_speed`,
`8_layer_high_speed`, `10_layer_high_speed`. A bare `stackup: { layers: 6 }`
expands to the conservative 6-layer profile and is marked
`source: stackup_template`, `confidence: medium`, `requires_review: true`.
Templates set layer roles, likely reference planes, and preferred high-speed
layers to improve placement/routing intent only — **controlled impedance always
requires the actual fabricator stackup**, and the tools emit a warning saying
so.

### AI-adjustable .pln and .ppl files

The `.pln` and `.ppl` files are intended to be edited by humans and AI
assistants as part of an iterative layout optimization workflow:

```text
generate board.pln  ->  AI reviews/edits board.pln  ->  pcb-plan emits placement.ppl
->  AI reviews/edits placement.ppl if needed  ->  pcb-place applies placement
->  reports feed back into board.pln
```

Generated files carry stable ordering for diffs, section comments explaining
inferred constraints (effective sides, edge requirements, paths, islands,
arrays), and provenance/`requires_review` markers. Both CLIs emit
`ai-edit-hints.md` (`pcb-plan emit --ai-edit-hints`, `pcb-place
--ai-edit-hints`) listing uncertain placement choices, low-confidence
constraints, multi-group components, placement owners, suggested `.pln`/`.ppl`
edits, and risks requiring engineering review.

### Why human review remains required

Placement-level heuristics and scoring cannot verify impedance, return paths,
reference-plane continuity, plane splits, thermal behavior, RF detuning, or
EMI compliance. The review reports exist to focus engineering attention, not to
replace it.

## Performance and runtime budgets

`pcb-place` uses bounded, staged search so placement stays fast even on dense
boards. A ~100-component board completes in a few seconds in the default
`normal` mode; it never runs unbounded.

### Optimization levels

Choose the search breadth/quality trade-off with `--optimization-level`:

| Level    | Candidates/rule | Reflow depth        | Post-legal probing | Use when |
|----------|-----------------|---------------------|--------------------|----------|
| `fast`   | 150             | local + group only  | accept first legal | quick iteration, CI smoke runs |
| `normal` | 500 (default)   | up to support region| small extra probe  | default placement |
| `deep`   | 2000            | up to parent move   | full search        | debug / exhaustive optimization |

`fast` does no parent reflow and accepts the first legal layout; `normal` is
balanced; `deep` searches harder and reflows more (and is slower by design).

### Time budget and best-effort timeout

`--time-budget-seconds N` (default `60`, `0` disables) bounds wall-clock time.
When the budget expires the placer does **not** abort: it keeps placing the
remaining components with the best known candidate, marks them
`requires_review`, records `timed_out: true`, and continues. Only `--strict`
turns a timeout into a hard error.

```bash
pcb-place board.kicad_pcb placement.ppl \
  --optimization-level normal \
  --time-budget-seconds 60 \
  --max-candidates-per-rule 500 \
  --max-floorplan-iterations 5
```

The placement report includes a `runtime` block:

```json
"runtime": {
  "optimization_level": "normal",
  "elapsed_seconds": 2.17,
  "budget_seconds": 60.0,
  "timed_out": false,
  "candidates_evaluated": 196,
  "cache_hits": 201176,
  "max_candidates_per_rule": 500,
  "degraded_refs": []
}
```

### Spatial index

Collision and clearance checks query a uniform-grid spatial index (stdlib only)
so each candidate is compared against only the handful of footprints near it
instead of every part on the board. The index updates as parts move and is
reported under `spatial_index`:

```json
"spatial_index": {
  "enabled": true,
  "cells": 93,
  "queries": 6382,
  "average_candidates_checked": 0.85
}
```

`average_candidates_checked` stays well below the part count — that is the
difference between local queries and a naive all-pairs scan.

### Bounded, staged search

For each placement primitive the search is bounded and staged:

- **Coarse → fine.** A limited set of high-quality candidates is generated and
  ranked; expensive checks run only on finalists.
- **Pruning.** Candidates that obviously cannot place (outside board/region,
  overlapping the expanded parent bbox, violating a hard keepout) are rejected
  before collision scoring.
- **Early success.** Once an *excellent* legal candidate is found (score under
  the level's threshold), the search stops instead of chasing a marginally
  better one.
- **Caps.** `--max-candidates-per-rule` caps evaluations per primitive so a
  single rule can never explode into thousands of checks. Legality always wins
  over the cap: the spacing-escalation ladder is never cut short before a legal
  placement is found.

### Reflow levels

Reflow is local by default, escalating only as needed (low-priority blockers —
testpoints, LEDs, straps, pullups, passives — move before critical parts; edge-
required and mechanically locked parts never move):

```text
level 0  local primitive only
level 1  same array/group
level 2  same parent support region
level 3  same functional island/path
level 4  adjacent regions
level 5  parent IC movement
```

`fast` uses levels 0–2, `normal` 0–3 (parent movement only when local placement
is impossible and the parent is movable), `deep` 0–5.

### Profiling

`--profile-placement profile.json` writes per-rule runtime, candidate counts,
collision-check counts, spatial-index stats, cache hit/miss, reflow attempts,
and the slowest rules — the data needed to tune a slow board:

```bash
pcb-place board.kicad_pcb placement.ppl --profile-placement profile.json
```

### Progress output

`--progress` prints concise, throttled per-rule progress to stderr for long
runs, e.g. `placing 34/102 C19 DecouplingArray candidates=120 elapsed=8.2s`.

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
  `placement.ppl` lifecycle: init, AI-assisted optimization, check, review,
  explain, update, emit, routing/SI constraints, stackup, differential pairs,
  simulation hooks, provenance.
- [`skills/pcb-place/SKILL.md`](skills/pcb-place/SKILL.md) — applying and
  debugging `placement.ppl` against a `.kicad_pcb` board: dry-run report
  review, validation, collisions/spacing/keepouts/regions, array diagnostics,
  edge-required movement attempts, troubleshooting.
- [`skills/pcb-automation-orchestrator/SKILL.md`](skills/pcb-automation-orchestrator/SKILL.md) —
  end-to-end AI iteration across `pcb-plan`, `pcb-place`, routing, DRC/ERC,
  and optional OpenEMS/ngspice simulation, including review, assisted, and
  bounded autonomous modes plus all-net and high-speed routing modes.

Short workflow: generate `board.pln`, let Claude review/optimize it, generate
`placement.ppl`, apply with `pcb-place`, and iterate using reports. Use
`pcb-plan` to own `board.pln`, use `pcb-place` to apply/debug the emitted
`placement.ppl` against a KiCad board, and use `pcb-automation-orchestrator`
to coordinate the full init/check/edit/emit/place/route/verify/simulate/update
loop. See [`skills/README.md`](skills/README.md) for details on when to use
each.

### Placement regions, support arrays, and edge-required parts

`pcb-place` models grouped support-part placement with an internal `PlacementRegion`: a legal strip on the left, right, top, or bottom of the expanded parent footprint bbox, clipped to board bounds and any strict `Region(...)` requested by the rule. `DecouplingArray(...)` and `PullupArray(...)` use these regions for capacity planning before selecting a scored candidate.

For arrays near a board edge, pcb-place computes the full array bounding box and slides it along the side tangent before rejecting it. Top/bottom arrays slide in X; left/right arrays slide in Y. Placement search reports include `placement_region`, capacity fields, `original_array_bbox`, `slid_array_bbox`, `slide_applied`, `slide_dx`, and `slide_dy`. Use `--explain-placement REF` to print the recorded winning score, candidate regions, slide data, and top rejected candidates for a specific part.

Board, region, keepout, and coordinate geometry accepts integral or floating-point values. For example, `Board(width=75, height=75)` and `Board(width=75.0, height=75.0)` are equivalent internally.

Edge-access components can be declared with metadata such as `edge_required=True`, `mechanical=True`, `locked=True`, and `access_side="left"` on `Edge(...)` placements. Edge-required anchors are treated as locked/high-priority owners; support components must move around them instead of pulling the connector or mechanical body inward. The generated report lists `edge_required_refs` separately from other locked refs.

Placement ownership precedence is enforced by priority and locking: fixed/locked/edge-required placements win over explicit anchors, pin-aware arrays, near-pad/satellite rules, and cluster fallback. When a cluster gives a coarse placement and a later DecouplingArray/PullupArray refines a support part, diagnostics report the refinement rather than silently fighting the rules.
