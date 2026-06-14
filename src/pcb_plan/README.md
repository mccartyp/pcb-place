# pcb-plan

`pcb-plan` is the **board-intelligence extractor** and `board.pln` reviewer for
a PCB design. Its `inspect` command extracts facts and candidate hints into a
`planning-hints/` directory; an AI (Claude) turns those hints into `board.pln`,
the authoritative design-intent document; and `pcb-plan` then reviews, validates,
updates, and emits the deterministic `placement.ppl` consumed by `pcb-place`.

```text
pcb-plan inspect  ->  planning-hints/  ->  AI generates board.pln  ->  pcb-plan check/emit  ->  pcb-place
```

The boundary is intentional:

- `pcb-plan inspect` extracts facts (geometry, connectivity) and *candidate
  hints* (roles, functional paths, power islands, edge connectors, mechanicals,
  RF zones). It does **not** place components, create placement ownership, build
  topology clusters heuristically, or infer a final floorplan — those are AI
  planning problems solved in `board.pln`.
- The AI (Claude) is the primary planning engine: it authors `board.pln` from
  `planning-hints/` (see the `pcb-bootstrap` skill).
- `pcb-plan` reviews/validates `board.pln` (`check`, `review`, `explain`),
  updates it from feedback (`update`), and emits `placement.ppl` (`emit`).
- `pcb-place` consumes `placement.ppl`, applies/reflows/optimizes placement,
  validates it, and writes KiCad board files.
- `pcb-plan` does **not** move footprints in a KiCad board and does **not**
  route traces. KiCad groups are exported as metadata only and must not drive
  placement.

## pcb-plan inspect

`pcb-plan inspect` extracts board intelligence into a `planning-hints/`
directory for AI-driven `board.pln` generation:

```bash
pcb-plan inspect \
  --board layout.kicad_pcb \
  --netlist default.net \
  --width 75 --height 75 \
  --out planning-hints
```

| Flag | Purpose |
| --- | --- |
| `--board` (required) | Input KiCad `.kicad_pcb` board. |
| `--netlist` | Optional Zener/pcb netlist for connectivity. |
| `--width` / `--height` | Override board geometry in mm (use together). |
| `--origin-x` / `--origin-y` | Override board origin in mm. |
| `--stackup-layers` | Optional known layer count to inform stackup assumptions. |
| `--out` (required) | Output `planning-hints/` directory. |

It writes facts and *candidate hints* (never authoritative placement):

| File | Contents |
| --- | --- |
| `board-hints.json` | Master facts + candidate hints (geometry, counts, candidate roles, paths, power islands, edge connectors, mechanicals, RF zones, differential pairs, routing classes, stackup assumptions, simulation candidates, imported KiCad groups). |
| `board-hints.md` | Human-readable summary. |
| `component-table.csv` | Per-component facts (role, candidate role, bbox, area, nets). |
| `connectivity-graph.json` | Component/net connectivity plus candidate ownership hints. |
| `footprint-bboxes.json` | Bounding box, centroid, and area per component. |
| `pad-locations.json` | Absolute/local pad coordinates and nets. |
| `candidate-functional-paths.json` | connector → protection → IC signal paths. |
| `candidate-power-islands.json` | Regulator + hot-loop topology. |
| `candidate-high-speed-paths.json` | High-speed subset of functional paths. |
| `candidate-edge-connectors.json` | Access side, auto-rotation, and `edge_required` hints. |
| `candidate-mechanicals.json` | Mounting-hole corner assignments. |
| `candidate-rf-zones.json` | RF antenna keepout candidates. |
| `routing-classes.json` | Candidate routing classes (impedance still requires the fab stackup). |
| `ai-pln-prompt.md` | Prompt to help Claude generate `board.pln`. |
| `ai-placement-review.md` | Checklist for reviewing `board.pln` against the hints. |

KiCad `(group ...)` blocks are exported under `imported_kicad_groups` with
`metadata_only: true`; they are preserved for information only and **must not
drive placement**. Simulation opportunities are surfaced as hints
(`openems_candidate`, `ngspice_candidate`, `si_candidate`), not simulation
results.

## board.pln Overview

`board.pln` is the single source of planning intent. It is a human-reviewable
YAML-like file (JSON is also accepted) containing board geometry, floorplanning,
placement constraints, routing intent, simulation hooks, and provenance for
inferred assumptions.

Supported top-level sections are:

| Section | Purpose |
| --- | --- |
| `board` | Board size, origin, units, and geometry provenance. |
| `fixed` | Fixed mechanical placement hints. |
| `regions` | Named floorplan areas used during placement-plan generation. |
| `keepouts` | Board-local exclusion rectangles. |
| `roles` | Semantic component roles inferred or supplied by the user. |
| `clusters` | Connectivity or interface clusters. |
| `high_speed` | High-speed nets and interface summaries. |
| `routing` | Routing mode, defaults, routing classes, and constraints. |
| `stackup` | Candidate or explicit copper/plane/dielectric definitions. |
| `differential_pairs` | Named P/N net pairs and related routing class. |
| `net_classes` | Net-pattern to routing-class assignments. |
| `routing_overrides` | Per-net routing exceptions. |
| `simulation` | OpenEMS and ngspice planning hooks. |
| `provenance` | Generator metadata and update proposals. |

Inferred scalar values are represented with visible provenance where practical:

```yaml
impedance_ohms:
  value: 100
  source: inferred_from_high_speed_diff
  confidence: medium
  requires_review: true
```

Before generating `placement.ppl`, `pcb-plan emit` unwraps these provenance
objects into ordinary values for the existing placement generator.


## Connectivity-aware placement planning notes

`pcb-plan init` creates a starting point for engineering review, not final
engineering truth. The generated `board.pln` records inferred values with
provenance wherever practical so reviewers can keep, override, or delete them.

Board geometry is resolved in a strict priority order during planning:

1. explicit `board.width`, `board.height`, and origin fields in `board.pln`;
2. KiCad `Edge.Cuts` rectangular geometry;
3. footprint extents as a last-resort fallback.

The footprint-extents fallback is intentionally noisy because it is not a real
board outline. If that warning appears, add explicit `board` geometry to
`board.pln` or fix the KiCad outline before trusting region sizes.

Good placement plans require connectivity. Supplying Zener/pcb `default.net` (or
another supported JSON, XML, or S-expression netlist artifact) lets the planner
recover component refs, pin-to-net connectivity, semantic aliases, differential
pairs, and support-part relationships. Without this graph, clusters and passive
placement rules are necessarily weaker.

The planner uses connectivity, proximity, role inference, existing physical
neighborhoods, and semantic/module aliases to build multi-member clusters such
as connector/ESD/interface groups, MCU support islands, regulator power stages,
and RF-module neighborhoods. These clusters preserve useful generated-layout
neighborhoods while allowing pin-aware refinements inside them.

Support passives are emitted with semantic placement helpers where possible:
`Decoupling(...)`, `NearPad(...)`, `ESD(...)`, `Pullup(...)`, `Series(...)`,
`Satellite(...)`, `Between(...)`, `Cluster(...)`, and `Orbit(...)`. Review
`unplaced_components` and the planning metrics in `pcb-plan` reports; unplaced
support parts should be treated as warnings and either given better connectivity
or explicit placement intent.

The planner builds a lightweight connectivity graph from parsed components and
nets to validate primitive selection rather than relying on shared-net
heuristics alone:

- **Decoupling capacitors** that share the same IC and power pin are grouped
  into a single `DecouplingArray(...)` rule that places the whole group as one
  row near the IC (or its power pad), instead of one `Decoupling(...)`/
  `NearPad(...)` pair per capacitor. A lone decoupling capacitor for a pin
  still gets a single `Decoupling(...)` rule.
- **Pullup/pulldown resistors** are assigned to the component that owns the
  signal they bias (connector, MCU, transceiver/RF module, other IC, or
  regulator, in that priority order), determined via the connectivity graph.
  Multiple pullups/pulldowns owned by the same component are grouped into a
  single `PullupArray(...)` rule near that owner instead of context-less
  `Pullup(...)` rules.
- **Series components** (e.g. damping or filter resistors on a signal path)
  are only emitted as `Series(...)` if the connectivity graph finds a true
  A -> component -> B topology: exactly two non-ground nets, each connecting
  to exactly one other non-passive component. Components that only share nets
  with other passive support parts (such as two decoupling capacitors) are
  rejected with a warning and listed in `failed_topology_inference` rather
  than emitted as a misleading `Series(...)` rule.

Inferred clusters carry a `category` (a semantic label such as "HDMI cluster"
or "MCU cluster") and a `confidence` ("high" for multi-member clusters, "low"
for single-member clusters that likely indicate missing connectivity).
Generated rules are also validated for board-bounds violations, duplicate
rule text, and duplicate placement ownership of the same component; problems
surface as `duplicate_rules` and additional `warnings` in `pcb-plan` reports.

## board.pln Syntax Reference

A `.pln` file is a planner-input file, not an executable placement file. It
captures board-level intent that `pcb-plan` turns into a concrete `.ppl`
part-placement file for `pcb-place` and into advisory routing/simulation handoff
artifacts for downstream tools.

The reader accepts either JSON or a dependency-free YAML-like subset. The subset
supports:

- indentation-based maps
- scalar strings, numbers, booleans, and `null`
- simple lists with `- item` and nested map entries introduced with `-`
- inline maps such as `{ x: 0, y: 0, w: 74, h: 26 }`
- inline lists such as `[F.Cu, B.Cu]`
- comments introduced with `#`

Avoid YAML anchors, aliases, folded/literal block scalars, custom tags,
multi-document streams, and complex escaping. If tooling needs those features,
write JSON content while keeping the `.pln` extension.

### Complete top-level key reference

| Key | Type | Consumed by | Purpose |
| --- | --- | --- | --- |
| `board` | map | `init`, `emit`, reports | Board dimensions, board-local origin, and units. Overrides inferred `Edge.Cuts` geometry during planning. |
| `fixed` | map of refs to maps | `emit` | Fixed mechanical placement hints such as mounting holes at board corners. |
| `regions` | map of names to rectangles | `emit`, reports | Floorplan areas emitted as `Region(...)` placement rules. |
| `keepouts` | list of rectangles | `emit`, reports | Placement exclusion areas emitted as `Keepout(...)` placement rules. |
| `roles` | map of refs to strings | `init`, `emit`, reports | Semantic component role overrides or inferred role records. |
| `clusters` | list of maps | reports, future emit logic | Inferred or user-reviewed logical groups of related components. |
| `high_speed` | map | reports, simulation inference | High-speed nets and interface summaries inferred from names/connectivity. |
| `stackup` | map | routing/SI reports, routing-policy emit, OpenEMS plan emit | Copper, plane, and dielectric definitions or candidates. |
| `routing` | map | routing/SI reports, routing-policy emit, placement comments | Routing mode, defaults, and named routing classes. |
| `differential_pairs` | map of names to maps | `emit`, routing/SI reports, routing-policy emit, OpenEMS plan emit | P/N net-pair definitions and class bindings. |
| `net_classes` | map | routing/SI reports, routing-policy emit, OpenEMS plan emit | Net-name patterns assigned to routing classes. |
| `routing_overrides` | map of net names to maps | routing/SI reports, routing-policy emit | Per-net routing exceptions. |
| `simulation` | map | reports, OpenEMS plan emit, update | OpenEMS and ngspice enablement, triggers, export locations, and feedback notes. |
| `provenance` | map | `review`, `explain`, reports, update | Generator metadata, confidence/review data, and update proposals. |
| `spacing` | map | `emit`, reports | Part-to-part clearance profile emitted as `Spacing(...)`; conservative non-touching defaults. |
| `components` | map of refs to maps | `emit`, reports | Per-component overrides: `role`, `edge_required`, `access_side`, `allow_body_outside_board`, `locked`, `rotation`. |
| `mechanical` | map | `emit`, reports | Mechanical constraints, currently `mounting_holes` corner/location assignments. |
| `functional_paths` | map of names to maps | `emit`, reports | Explicit functional signal paths (connector -> protection -> IC) emitted as `HighSpeedPath(...)`. |
| `power_islands` | map of names to maps | `emit`, reports | Explicit regulator topology (input caps, inductor, output caps, feedback, switch node) emitted as island rules + `PowerIsland(...)`. |

Unknown top-level keys are reported by validation and should be avoided unless a
future `pcb-plan` version documents them.

### `board`

`board` declares dimensions in millimeters and a KiCad-to-board-local origin:

```yaml
board:
  width: 74
  height: 40
  origin_x: 0
  origin_y: 0
  units: mm
```

Fields:

- `width`, `height`: positive board dimensions in millimeters.
- `origin_x`, `origin_y`: KiCad coordinates corresponding to board-local `(0, 0)`.
- `units`: currently expected to be `mm`.

### `fixed`

`fixed` maps footprint references to fixed-placement hints. The supported fixed
placement type is currently `corner`:

```yaml
fixed:
  H1: { type: corner, corner: top_left, inset: 3 }
  H2: { type: corner, corner: top_right, inset: 3 }
```

Fields for `type: corner`:

- `corner`: one of `top_left`, `top_right`, `bottom_left`, or `bottom_right`.
- `inset`: distance from the board edges in millimeters.

### `mechanical`

`mechanical.mounting_holes` assigns mounting holes to explicit corners or
coordinates. Mounting holes are fixed mechanical objects, never clusters; holes
without explicit assignments keep their positions when already at distinct
corners and are otherwise redistributed to distinct corners with a review
warning.

```yaml
mechanical:
  mounting_holes:
    H1: { corner: top_left, inset: 3 }
    H2: { corner: top_right, inset: 3 }
    H3: { corner: bottom_left, inset: 3 }
    H4: { corner: bottom_right, inset: 3 }
```

### `components`

`components` carries per-component overrides consumed during role inference and
edge-connector planning:

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

Fields:

- `role`: overrides the inferred semantic role (spec-level names such as
  `regulator`, `oscillator`, `retimer_redriver`, `hdmi_connector` are accepted).
- `edge_required`: forces (or suppresses) edge placement. Edge-required
  connectors are locked at the board edge; optimization never pulls them inward.
- `access_side`: `left`/`right`/`top`/`bottom`; determines the target edge and
  the automatic rotation.
- `allow_body_outside_board`: lets the connector body/courtyard extend past the
  board outline (the anchor must stay on the board). Defaults to true for
  edge-required connectors.
- `locked`: lock the connector after placement (default true at edges).
- `rotation`: `auto` (rotate from `access_side`; convention: at rot=0 the mating
  face points toward the top edge), an explicit angle, or `keep`.

### `spacing`

`spacing` overrides the conservative default clearance profile used in
`Spacing(...)` emission, candidate scoring, and validation:

```yaml
spacing:
  passive_to_passive: 0.25
  passive_to_ic: 0.40
  ic_to_ic: 0.75
  connector_to_component: 1.00
  mechanical_to_component: 1.00
```

Overrides are allowed, but the tools never default to essentially-touching
parts.

### `functional_paths`

`functional_paths` declares functional signal paths explicitly. Each path is
emitted as a `HighSpeedPath(...)` declaration that reserves a corridor before
support parts are placed and is scored by the high-speed placement review.
Explicit entries suppress the inferred path for the same connector.

```yaml
functional_paths:
  HDMI_IN:
    type: high_speed_diff
    sequence: [J1, U2, U10]
    corridor_width_mm: 8
    protect_first: true
```

### `power_islands`

`power_islands` pins regulator topology membership. Members get compact
topology placement (input caps tight to the regulator, inductor/output caps
compact, feedback at the FB pin) and the island is scored by the power
placement review. Inferred islands only claim capacitors whose closest
plausible owner is the regulator/inductor — downstream load decouplers stay
with their loads.

```yaml
power_islands:
  BUCK_3V3:
    regulator: U7
    input_caps: [C11]
    inductor: L1
    output_caps: [C12]
    feedback: [R13]
    switch_node: SW_NODE
```

### `regions`

`regions` maps names to board-local rectangles:

```yaml
regions:
  HIGH_SPEED: { x: 0, y: 0, w: 74, h: 13.3 }
  CONTROL: { x: 0, y: 13.3, w: 74, h: 13.3 }
  POWER: { x: 0, y: 26.6, w: 74, h: 13.4 }
```

Each rectangle supports `x`, `y`, `w`, and `h` in millimeters. Regions become
`Region(...)` rules in `placement.ppl`.

### `keepouts`

`keepouts` is a list of named board-local rectangles:

```yaml
keepouts:
  - name: WIFI_ANTENNA
    x: 55
    y: 5
    w: 14
    h: 18
    role: rf
```

Fields:

- `name`: stable keepout name.
- `x`, `y`, `w`, `h`: board-local rectangle in millimeters.
- `role`: optional label such as `rf`, `mechanical`, `cable_clearance`,
  `high_voltage`, or `switch_node_noise`.

### `roles`

`roles` maps KiCad references to semantic role strings:

```yaml
roles:
  J1: hdmi_input_connector
  U10: hdmi_retimer
  U7: power_regulator
```

Role overrides take precedence over heuristic inference and appear in reports,
explanations, and generated placement rules.

### `clusters`

`clusters` records logical component groups. `init` may infer clusters from
connectivity; users may keep, edit, or remove them before later emission:

```yaml
clusters:
  - name: U10_SUPPORT
    anchor: U10
    members: [C5, C6, R8]
    role: support
```

Common fields are `name`, `anchor`, `members`, and `role`. This section is
primarily report/provenance data today and leaves room for future placement
policy refinements.

### `high_speed`

`high_speed` summarizes inferred high-speed nets and interfaces:

```yaml
high_speed:
  nets: [TMDS0_P, TMDS0_N, USB_DP, USB_DN]
  interfaces: [TMDS0, USB]
```

This section helps reviewers see why routing constraints or OpenEMS triggers
were inferred. Use `differential_pairs`, `routing`, and `stackup` for concrete
constraints.

### Top-level routing/SI keys

Routing and signal-integrity intent is split across several top-level sections
so reviewers can distinguish physical stackup, class policy, net membership,
per-net exceptions, and simulation hooks:

| Top-level key | Purpose |
| --- | --- |
| `stackup` | Declares copper layers, plane layers, copper weights, dielectric relationships, materials, thicknesses, and dielectric constants. |
| `routing` | Declares routing mode, default rules, and named routing classes. |
| `differential_pairs` | Names P/N net pairs and associates each pair with routing constraints or a routing class. |
| `net_classes` | Maps net names or net-name patterns to routing classes. |
| `routing_overrides` | Applies per-net exceptions such as preferred layer, via policy, length/skew tolerance, or max vias. |
| `simulation` | Captures OpenEMS and ngspice enablement, triggers, output locations, and feedback from simulation runs. |

These sections are advisory planning data. `pcb-plan` validates, reports,
explains, and preserves them, but routing is performed later by an orchestrator,
KiCad tooling, or another routing engine.

### `stackup`

`stackup.layers` is a list of copper layers. Signal layers normally set
`type: signal`; plane layers set `type: plane` and usually include `net`:

```yaml
stackup:
  layers:
    - name: F.Cu
      type: signal
      copper_oz: 1
    - name: In1.GND
      type: plane
      net: GND
    - name: In2.PWR
      type: plane
      net: 3V3
    - name: B.Cu
      type: signal
  dielectric:
    - between: [F.Cu, In1.GND]
      material: FR4
      thickness_mm: 0.18
      er: 4.2
```

Layer fields include `name`, `type`, `net`, and `copper_oz`. Dielectric entries
include `between`, `material`, `thickness_mm`, and `er`. Validation checks that
referenced layers exist and that numeric dielectric values are positive.

#### Stackup profiles

Reusable conservative templates are available for common layer counts:

| Profile | Layers |
| --- | --- |
| `2_layer_basic` | 2 |
| `4_layer_signal_gnd_pwr_signal` | 4 |
| `6_layer_high_speed` | 6 |
| `8_layer_high_speed` | 8 |
| `10_layer_high_speed` | 10 |

```yaml
stackup:
  profile: 6_layer_high_speed
  layers:
    - F.Cu
    - In1.GND
    - In2.PWR
    - In3.SIG
    - In4.GND
    - B.Cu
```

The `layers` list is optional with a profile (plain layer names are accepted
and normalized). A bare layer count also works:

```yaml
stackup:
  layers: 6
```

and expands to the conservative profile for that count, marked
`source: stackup_template`, `confidence: medium`, `requires_review: true`.
Each profile records layer names/types, likely reference planes, default
high-speed preferred layers, and power-plane assumptions. Templates improve
placement/routing intent only: they never claim impedance accuracy, and a
warning is emitted reminding that controlled impedance requires the actual
fabricator stackup.

### `routing`

`routing` contains a mode, optional defaults, and named classes:

```yaml
routing:
  mode: all_nets_constrained
  defaults:
    trace_width_mm: 0.15
    clearance_mm: 0.15
    via_policy: allow
    preferred_layers: [F.Cu, B.Cu]
  classes:
    high_speed_diff:
      differential: true
      impedance_ohms: 100
      trace_width_mm: 0.12
      trace_spacing_mm: 0.15
      preferred_layer: F.Cu
      reference_plane: In1.GND
      max_skew_mm: 0.25
      max_length_mismatch_mm: 0.25
      via_policy: avoid
      max_vias: 0
```

Valid routing modes are:

- `low_speed_only`
- `all_nets_constrained`
- `experimental_high_speed`

Valid via policies are:

- `avoid`
- `allow`
- `constrained`
- `forbid`

Routing class/default fields include:

- geometry: `trace_width_mm`, `clearance_mm`, `trace_spacing_mm`
- impedance: `impedance_ohms`, `differential`
- layer policy: `preferred_layer`, `preferred_layers`, `reference_plane`
- matching/timing: `max_skew_mm`, `max_length_mismatch_mm`
- via policy: `via_policy`, `max_vias`
- optional region hints: `avoid_regions`, `prefer_regions`

### `differential_pairs`

`differential_pairs` declares named P/N nets and optional pair-local constraints:

```yaml
differential_pairs:
  HDMI_TMDS0:
    p: TMDS0_P
    n: TMDS0_N
    class: high_speed_diff
    max_skew_mm: 0.25
```

Required fields are `p` and `n`. `class` should reference a key under
`routing.classes`. Pair-local routing fields may override or supplement class
fields.

### `net_classes`

`net_classes` maps net names or simple patterns to routing classes:

```yaml
net_classes:
  USB_*:
    class: high_speed_diff
  3V3:
    class: power
```

Values are maps so future tools can add annotations without changing the syntax.
The common field is `class`.

### `routing_overrides`

`routing_overrides` applies exceptions to individual nets:

```yaml
routing_overrides:
  TMDS0_CLK_P:
    preferred_layer: F.Cu
    via_policy: forbid
    max_vias: 0
    max_length_mismatch_mm: 0.1
```

Supported fields match routing-class fields: width, clearance, spacing,
preferred layer(s), reference plane, via policy, max vias, skew tolerance, and
length-mismatch tolerance.

### `simulation`

`simulation` contains planning hooks for OpenEMS and ngspice:

```yaml
simulation:
  openems:
    enabled: auto
    trigger_on:
      - high_speed_diff
      - rf
      - switching_power_near_high_speed
    export_dir: simulation/openems
    notes: advisory_only
  ngspice:
    enabled: auto
    trigger_on:
      - regulator
      - reset_circuit
      - analog_filter
```

`enabled` may be `true`, `false`, or `auto`. For `auto`, triggers determine
whether a handoff artifact is emitted. `openems` is intended for HDMI, USB,
Ethernet, RF, differential-pair, and switching-power-near-high-speed concerns.
`ngspice` is intended for regulators, reset circuits, filters, and analog
networks.

### `provenance`

`provenance` stores generator metadata and reviewable update proposals:

```yaml
provenance:
  generator: pcb-plan init
  schema_version: "0.1"
  update_proposals:
    - type: openems_feedback
      value: review routing corridors and reference planes
      source: openems_report
      confidence: medium
      requires_review: true
```

`review` and `explain` surface provenance fields so assumptions and feedback are
visible instead of hidden in generated files.

## pcb-plan init

> **Note:** `init` is a legacy heuristic generator kept for quick starts and
> compatibility. The preferred path is `pcb-plan inspect` followed by
> AI-authored `board.pln` (the `pcb-bootstrap` skill), which keeps fact
> extraction and design-intent generation cleanly separated.

`init` generates an initial `board.pln` from a KiCad board and optional netlist:

```bash
pcb-plan init \
  --board layout.kicad_pcb \
  --netlist default.net \
  -o board.pln \
  --report-json pcb-plan-init-report.json
```

It infers board geometry, default regions, keepouts, component roles, clusters,
differential pairs, routing classes, stackup candidates, and simulation triggers.
The report includes inferred roles, interfaces, constraints, warnings,
provenance, confidence, and review-required items.

## pcb-plan update

`update` reads an existing `board.pln` and optional feedback reports, then writes
an updated plan and an optional reviewable patch:

```bash
pcb-plan update \
  --pln board.pln \
  --board layout.kicad_pcb \
  --place-report pcb-place-report.json \
  --routing-report routing-report.json \
  --openems-report openems-report.json \
  --ngspice-report ngspice-report.json \
  -o board.updated.pln \
  --patch board.pln.patch \
  --report-json pcb-plan-update-report.json
```

Feedback can propose new keepouts, routing-policy changes, region changes,
simulation changes, differential-pair definitions, stackup annotations, and via
policy adjustments. Updates are written as visible proposals under provenance so
reviewers can accept, edit, or reject them without hiding assumptions.

## pcb-plan review

`review` prints a human-readable summary of an existing `board.pln`:

```bash
pcb-plan review --pln board.pln
```

The output includes inferred constraints, provenance, confidence, warnings,
missing information, and review-required items.

## pcb-plan explain

`explain` describes why a specific object exists in `board.pln`:

```bash
pcb-plan explain HDMI_TMDS0 --pln board.pln
pcb-plan explain U10 --pln board.pln
pcb-plan explain routing.classes.high_speed_diff --pln board.pln
```

When a board is also supplied, component explanations can include the legacy
placement inference explanation:

```bash
pcb-plan explain U10 --pln board.pln --board layout.kicad_pcb --netlist default.net
```

## pcb-plan emit

`emit` generates `placement.ppl`; this is the existing placement-planning
workflow moved behind an explicit subcommand:

```bash
pcb-plan emit \
  --pln board.pln \
  --board layout.kicad_pcb \
  --netlist default.net \
  -o placement.ppl \
  --report-json pcb-plan-summary.json
```

`emit` owns placement-plan generation and placement reports only. The resulting
`placement.ppl` is still executed by `pcb-place`.

`--ai-edit-hints ai-edit-hints.md` writes an AI-adjustment report listing
uncertain placement choices, low-confidence inferred constraints, components
with multiple semantic groups, placement owners per ref, suggested
`board.pln`/`placement.ppl` edits, and risks requiring engineering review. The
generated `.pln` and `.ppl` files are intended to be edited by humans and AI
assistants as part of an iterative layout optimization workflow; both carry
stable ordering, section comments, and provenance markers to support that loop.

Legacy one-shot invocation remains available for compatibility:

```bash
pcb-plan --board layout.kicad_pcb --netlist default.net --intent board.pln -o placement.ppl
```

## pcb-plan check

`check` evaluates plan quality without writing `placement.ppl`:

```bash
pcb-plan check \
  --pln board.pln \
  --board layout.kicad_pcb \
  --netlist default.net \
  --report-json pcb-plan-check-report.json
```

See "Plan quality gates" below for the report fields and the `plan_confidence`
model shared with `emit`.

## Routing Constraints

Routing intent is optional and advisory. `pcb-plan` parses, validates, explains,
reports, and can emit handoff artifacts, but it does not route traces.

```yaml
routing:
  mode: all_nets_constrained
  classes:
    high_speed_diff:
      impedance_ohms: 100
      trace_width_mm: 0.12
      trace_spacing_mm: 0.15
      reference_plane: In1.GND
      preferred_layer: F.Cu
      max_skew_mm: 0.25
      max_length_mismatch_mm: 0.25
      via_policy: avoid
```

Supported data includes stackup references, impedance, trace width, trace
spacing, preferred layers, reference planes, via policies, skew tolerances, and
length tolerances. Use `--emit-routing-policy routing-policy.yaml` during `emit`
to create a downstream routing-policy artifact.

## Differential Pairs

Differential pairs may be inferred from net names or declared explicitly:

```yaml
differential_pairs:
  HDMI_TMDS0:
    p: TMDS0_P
    n: TMDS0_N
    class: high_speed_diff
```

When board/netlist connectivity is available, `pcb-plan` reports missing P/N
nets and unknown routing-class references.

## Stackup Definitions

`stackup` captures candidate or explicit layer information:

```yaml
stackup:
  layers:
    - name: F.Cu
      type: signal
      copper_oz: 1
    - name: In1.GND
      type: plane
      net: GND
    - name: In2.PWR
      type: plane
      net: 3V3
    - name: B.Cu
      type: signal
  dielectric:
    - between: [F.Cu, In1.GND]
      material: FR4
      thickness_mm: 0.18
      er: 4.2
```

The validator checks referenced layers, reference planes, dielectric thickness,
and dielectric constants where enough data is present.

## Simulation Hooks

Simulation intent lives under `simulation`:

```yaml
simulation:
  openems:
    enabled: auto
  ngspice:
    enabled: auto
```

OpenEMS triggers are inferred for HDMI, USB, Ethernet, RF, differential pairs,
and switching power near high-speed circuitry. ngspice triggers are inferred for
regulators, reset circuits, filters, and analog networks. Use
`--emit-openems-plan simulation/openems/openems-plan.yaml` during `emit` to
produce an OpenEMS planning artifact when enabled.

## Provenance Model

Inferred items should expose:

- `value`: the inferred or candidate value.
- `source`: why the value exists.
- `confidence`: `low`, `medium`, or `high`.
- `requires_review`: whether an engineer must review the assumption.

This model intentionally avoids hiding assumptions. Generated reports and
`pcb-plan review` surface warnings and review-required items.

## Iterative Workflow

Current workflow:

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

pcb-place \
  layout.kicad_pcb \
  placement.ppl \
  -o placed.kicad_pcb
```

Future orchestrator feedback loop:

```bash
pcb-plan update \
  --pln board.pln \
  --openems-report report.json \
  -o board.updated.pln \
  --patch board.pln.patch
```

## Reports

`pcb-plan` can generate:

- `pcb-plan-init-report.json` from `init --report-json`.
- `pcb-plan-update-report.json` from `update --report-json`.
- `pcb-plan-summary.md` from `--summary-md` on `init`, `update`, or `emit`.

Reports include inferred roles, inferred interfaces, inferred constraints,
provenance, confidence, warnings, and review-required items.

`pcb-plan` reports also include placement-quality metrics:

- `primitive_counts`: number of generated rules per placement primitive (e.g.
  `Decoupling`, `DecouplingArray`, `Pullup`, `PullupArray`, `Series`, `Cluster`).
- `decoupling_groups` / `pullup_groups`: per-IC/owner grouping summaries
  showing which components were grouped into a single `DecouplingArray(...)`/
  `PullupArray(...)` rule (`"grouped": true`) versus given a single
  `Decoupling(...)`/`Pullup(...)` rule (`"grouped": false`).
- `series_components_validated`: count of `Series(...)` rules that passed
  connectivity-graph topology validation.
- `failed_topology_inference`: components where a series/pullup inference was
  rejected because the connectivity graph could not establish the required
  topology.
- `duplicate_rules`: duplicate rule text or duplicate placement ownership
  detected across generated rules.
- `cluster_quality_score`: fraction of inferred clusters that are multi-member
  (higher is better; single-member clusters likely indicate missing
  connectivity).
- `plan_confidence`: an overall `{score, level, reasons}` summary of how
  trustworthy the plan is (see "Plan quality gates" below).

## Plan quality gates

`pcb-plan check` evaluates plan quality **without** writing `placement.ppl`:

```bash
pcb-plan check \
  --pln board.pln \
  --board layout.kicad_pcb \
  --netlist default.net \
  --report-json pcb-plan-check-report.json
```

It prints (and optionally writes via `--report-json`) a JSON report containing:

- `nets_parsed`, `components_parsed`, `components_planned`,
  `unplaced_components`
- `clusters_total`, `single_member_clusters`, `multi_member_clusters`
- `duplicate_rule_refs`
- `decoupling_groups`, `pullup_groups`
- `series_components_validated`, `invalid_series_candidates`
- `differential_pairs_inferred`, `high_speed_constraints_complete`
- `simulation_triggers`
- `warnings`
- `plan_confidence`

Use `pcb-plan check` between `init`/`update` and `emit` to catch weak plans
before generating a placement file.

### Plan confidence

Every `pcb-plan` report (including `check` and `emit`) includes a
`plan_confidence` block:

```json
{
  "score": 100,
  "level": "high",
  "reasons": []
}
```

`level` is `"high"` (score >= 80), `"medium"` (score >= 50), or `"low"`
(score < 50). The score starts at 100 and is reduced when any of the
following conditions are detected, each adding a human-readable entry to
`reasons`:

- no nets were parsed from the board/netlist
- more than half of the inferred clusters are single-member
- more than 25% of components have no placement rule
- duplicate placement rules were detected
- high-speed connector(s) are present but no differential pairs were inferred
- one or more `Series(...)` inferences were rejected as invalid topology
- board geometry was inferred from footprint extents (no `board.pln` or
  `Edge.Cuts` geometry was available)

`pcb-plan emit` always prints `WARNING:` lines to stderr (and includes a
`# Plan confidence: low` / `# Reasons:` / `# - ...` header comment block at
the top of `placement.ppl`) when the plan is low-confidence, but otherwise
continues by default. Pass `--strict-confidence` to make `emit` fail on a
low-confidence plan, and `--allow-low-confidence` to acknowledge the warning
and proceed anyway despite `--strict-confidence`:

```bash
pcb-plan emit --pln board.pln --board layout.kicad_pcb --netlist default.net \
  -o placement.ppl --strict-confidence
# fails with a non-zero exit code if plan confidence is low

pcb-plan emit --pln board.pln --board layout.kicad_pcb --netlist default.net \
  -o placement.ppl --strict-confidence --allow-low-confidence
# proceeds anyway, still printing the WARNING lines
```

Recommended workflow:

```bash
pcb-plan init --board layout.kicad_pcb --netlist default.net -o board.pln
pcb-plan check --pln board.pln --board layout.kicad_pcb --netlist default.net \
  --report-json pcb-plan-check-report.json
pcb-plan emit --pln board.pln --board layout.kicad_pcb --netlist default.net \
  -o placement.ppl
pcb-place layout.kicad_pcb placement.ppl --dry-run
```

## Syntax and safety

The dependency-free `.pln` reader accepts JSON or a small YAML-like subset:
indentation-based maps, scalar strings/numbers/booleans, simple lists, inline
maps, inline lists, and comments. Avoid advanced YAML features such as anchors,
custom tags, and block scalars unless the file is emitted as JSON.

`pcb-plan` output is an engineering starting point. Review `board.pln`, review
`placement.ppl`, execute placement with `pcb-place`, inspect the KiCad board, and
verify routing, impedance, return paths, SI/EMI behavior, and simulation results
before fabrication.
