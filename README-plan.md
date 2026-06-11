# pcb-plan

`pcb-plan` is an experimental, heuristic placement planner for `pcb-place`.

- `pcb-plan` **generates** a readable `.ppl` part-placement file from board, netlist, pin, footprint, and optional `.pln` board-plan information.
- `pcb-place` **executes** that explicit `.ppl` plan deterministically against a KiCad `.kicad_pcb` file.

The separation is intentional: `pcb-plan` is knowledge-based and may make imperfect inferences, while `pcb-place` remains a deterministic and relatively simple placement executor. Together they are intended to extend Zener board designs into a complete board-as-code build definition workflow.

## Workflow

```bash
pcb build board.zen
pcb layout board.zen
pcb-plan --board layout.kicad_pcb --netlist default.net --intent board.pln -o placement.ppl
pcb-place layout.kicad_pcb placement.ppl -o layout.placed.kicad_pcb
pcbnew layout.placed.kicad_pcb
```

For early use, only the board is required:

```bash
pcb-plan --board layout.kicad_pcb -o placement.ppl
```

Netlist and `.pln` board-plan inputs improve planner quality but are optional.

## CLI

```bash
pcb-plan \
  --board layout.kicad_pcb \
  --netlist default.net \
  --intent board.pln \
  -o placement.ppl \
  --report-json pcb-plan-report.json
```

Useful inspection command:

```bash
pcb-plan --board layout.kicad_pcb --netlist default.net --explain C5
```

The explanation output summarizes the inferred role, connected nets, parent candidate, and generated rule for the requested reference.

## Inputs

### KiCad board file

Required. The MVP parser reads:

- footprint references, footprint names, UUIDs, positions, rotations, layers, and values
- pads, pad numbers/names, pad positions, pad layers, pad shapes/sizes, and pad nets
- approximate footprint bounding boxes from pads and footprint graphics
- rectangular `Edge.Cuts` board geometry when present

`pcb-plan` never writes to the board file.

### Zener/pcb netlist

Optional. `pcb-plan` reuses the alias parser from `pcb-place` to recover semantic instance path aliases such as `MCU.U_MCU -> U1`. JSON netlists can also augment component values, footprints, and pin-to-net connectivity.

### Board plan (`.pln`)

Optional. Board-plan files should use the `.pln` extension. They contain YAML-like planner intent (the dependency-free YAML subset shown below) so the project has `.pln` plan files as planner inputs and `.ppl` part-placement files as executor inputs. JSON content is also supported for tooling that prefers machine-generated plan data.

```yaml
board:
  width: 74
  height: 74
  origin_x: 140.23
  origin_y: 52.465

roles:
  J1: hdmi_input_connector
  U10: hdmi_retimer

regions:
  HIGH_SPEED: { x: 0, y: 0, w: 74, h: 26 }
  CONTROL:    { x: 0, y: 26, w: 74, h: 30 }
  POWER:      { x: 0, y: 56, w: 74, h: 18 }

keepouts:
  - name: WIFI_ANTENNA
    x: 55
    y: 5
    w: 14
    h: 18
    role: rf
```

For complex YAML, convert the `.pln` file content to JSON before invoking `pcb-plan`, or keep the file within the documented YAML subset.

## `.pln` syntax reference

A `.pln` file is a planner-input file, not an executable placement file. It describes board-level planning hints that help `pcb-plan` generate a concrete `.ppl` part-placement file for `pcb-place`. Keep `.pln` files in version control alongside the Zener board design so the board-as-code build definition includes both electrical intent and physical planning intent.

The MVP `.pln` reader accepts either JSON or a small YAML-like subset. The YAML-like form supports:

- indentation-based maps
- scalar strings, numbers, and booleans
- simple lists with `- item` entries
- inline maps such as `{ x: 0, y: 0, w: 74, h: 26 }`
- comments introduced with `#`

Avoid advanced YAML features such as anchors, aliases, multi-document streams, folded block scalars, custom tags, and complex quoting. If you need those features, generate JSON content instead while keeping the `.pln` extension. Unknown top-level keys are ignored by the current planner, so future metadata can be added conservatively without breaking older planner versions.

### Top-level keys

| Key | Type | Purpose |
| --- | --- | --- |
| `board` | map | Declares board dimensions and board-local origin. Overrides geometry inferred from `Edge.Cuts` for planning output. |
| `fixed` | map of refs | Declares fixed mechanical placement, currently corner placement for mounting holes and similar features. |
| `roles` | map of refs to strings | Overrides heuristic role inference for specific references. |
| `regions` | map of names to rectangles | Declares named floorplan regions that become `Region(...)` rules in generated `.ppl`. |
| `keepouts` | list of maps | Declares rectangular placement keepouts that become `Keepout(...)` rules in generated `.ppl`. |

### `board`

`board` is a map with dimensions in millimeters:

```yaml
board:
  width: 74
  height: 74
  origin_x: 140.23
  origin_y: 52.465
```

Fields:

- `width`: board width in millimeters.
- `height`: board height in millimeters.
- `origin_x`: KiCad x-coordinate corresponding to board-local x=0.
- `origin_y`: KiCad y-coordinate corresponding to board-local y=0.

### `fixed`

`fixed` maps footprint references to fixed-placement hints. The current supported fixed placement type is `corner`:

```yaml
fixed:
  H1: { type: corner, corner: top_left, inset: 3 }
  H2: { type: corner, corner: top_right, inset: 3 }
```

Fields for `type: corner`:

- `corner`: one of `top_left`, `top_right`, `bottom_left`, or `bottom_right`.
- `inset`: distance from the board edges in millimeters.

### `roles`

`roles` maps KiCad references to semantic roles. These values override automatic role inference and are copied into planner decisions and reports:

```yaml
roles:
  J1: hdmi_input_connector
  J2: hdmi_output_connector
  U10: hdmi_retimer
  U6: mcu
  U12: rf_module
  U7: power_regulator
```

Role strings are intentionally lightweight. Use stable names that make sense to reviewers and downstream scripts. Current planner heuristics understand common concepts such as connectors, ICs, RF modules, power regulators, ESD/protection devices, decoupling capacitors, pullups, and series components.

### `regions`

`regions` maps names to board-local rectangles. Each rectangle is emitted as a `.ppl` `Region(...)` rule:

```yaml
regions:
  HIGH_SPEED: { x: 0, y: 0,  w: 74, h: 26 }
  CONTROL:    { x: 0, y: 26, w: 74, h: 30 }
  POWER:      { x: 0, y: 56, w: 74, h: 18 }
```

Fields:

- `x`, `y`: board-local rectangle origin in millimeters.
- `w`, `h`: rectangle width and height in millimeters.

Use regions to reserve high-speed, control, power, RF, connector, or mechanical planning areas before lower-priority placement rules are emitted.

### `keepouts`

`keepouts` is a list of board-local rectangles. Each entry is emitted as a `.ppl` `Keepout(...)` rule:

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

- `name`: stable keepout name used in generated `.ppl` and reports.
- `x`, `y`: board-local rectangle origin in millimeters.
- `w`, `h`: rectangle width and height in millimeters.
- `role`: optional label such as `rf`, `mechanical`, `cable_clearance`, or `switch_node_noise`.

Use keepouts for antenna areas, cable clearances, high-voltage clearances, switching-node noise areas, and other locations where footprints should not be planned.


## `.pln` Routing Constraints

`.pln` files may also carry optional routing and signal-integrity intent. `pcb-plan` parses, validates, reports, and preserves this data for downstream tools, but it does **not** route traces and does not emit executable routing primitives in `placement.ppl`.

Supported top-level routing/SI keys are:

| Key | Purpose |
| --- | --- |
| `stackup` | Declares copper layers, plane layers, copper weight, and dielectric relationships. |
| `routing` | Declares routing mode, defaults, and named routing classes. |
| `differential_pairs` | Names P/N net pairs and associates them with routing classes. |
| `net_classes` | Maps net-name patterns to routing classes. |
| `routing_overrides` | Applies per-net routing overrides such as preferred layer or max vias. |
| `simulation` | Captures OpenEMS/simulation handoff hints. |

Valid routing modes are `low_speed_only`, `all_nets_constrained`, and `experimental_high_speed`. Valid via policies are `avoid`, `allow`, `constrained`, and `forbid`.

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

When routing constraints are present, `placement.ppl` remains placement-focused and only receives review comments such as:

```text
# Routing constraints from board.pln:
# HDMI_TMDS0: 100 ohm differential, F.Cu over In1.GND, max skew 0.25 mm
# Routing performed later by orchestrator/KiCadRoutingTools.
```

Use `--emit-routing-policy routing-policy.yaml` to generate a YAML handoff containing the routing classes, stackup, differential pairs, net classes, overrides, and validation warnings for automation/orchestrator/KiCadRoutingTools consumers.

## Stackup

`stackup.layers` is a list of copper layers. Signal layers normally set `type: signal`; plane layers set `type: plane` and often include the associated `net`.

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

The validator checks that referenced layers exist, dielectric `between` entries name known layers, dielectric thickness is positive, and reference planes used by routing classes are present in the stackup.

## Differential Pairs

`differential_pairs` declares named P/N nets and optionally binds each pair to a routing class:

```yaml
differential_pairs:
  HDMI_TMDS0:
    p: TMDS0_P
    n: TMDS0_N
    class: high_speed_diff
```

When board or netlist connectivity is available, `pcb-plan` warns if the declared P or N net is missing. It also warns when a pair references an unknown routing class.

## High-Speed Routing Parameters

Routing classes may define impedance, width, spacing, clearance, layer, skew, length matching, return plane, and via policy constraints:

```yaml
routing:
  classes:
    clock:
      trace_width_mm: 0.15
      preferred_layer: F.Cu
      reference_plane: In1.GND
      via_policy: avoid
      max_vias: 0
```

Validation includes:

- referenced preferred layers and reference planes exist in `stackup`
- impedance targets are numeric
- width, spacing, clearance, skew, length-mismatch, and dielectric values are positive
- high-speed classes using `via_policy: allow` specify `max_vias`
- high-speed routing intent includes a stackup

The `pcb-plan-report.json` output includes:

```json
{
  "routing": {
    "mode": "all_nets_constrained",
    "classes": {},
    "differential_pairs": {},
    "warnings": [],
    "high_speed_constraints_complete": true
  },
  "stackup": {
    "layers": [],
    "reference_planes": [],
    "warnings": []
  }
}
```

## OpenEMS Simulation Hooks

OpenEMS hints live under `simulation.openems`:

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
```

Use `--emit-openems-plan simulation/openems/openems-plan.yaml` to create an OpenEMS handoff plan. The file is emitted when `enabled: true`, or when `enabled: auto` and triggers are present. It contains the source board file, stackup summary, critical nets, differential pairs, regions of interest, simulation goal, and an advisory warning that `pcb-plan` does not run simulation.

`pcb-plan` warns if OpenEMS is enabled but the trigger list contains no high-speed, RF, or switching-power trigger.

## Relationship to pcb-place and KiCadRoutingTools

`pcb-plan` owns intent parsing, validation, reports, placement-plan generation, and optional routing/simulation handoff files. `pcb-place` remains a deterministic placement executor for `.ppl` primitives and should not be expected to understand routing constraints unless support is added there later. KiCadRoutingTools or an automation orchestrator can consume `routing-policy.yaml` and `openems-plan.yaml` to perform routing or simulation in later workflow stages.

## Output

`pcb-plan` emits a `.ppl` part-placement file organized for review:

```python
# Generated by pcb-plan
# Generated placement plan.
# Requires engineering review before fabrication.
# High-speed routing, impedance, return path, and EMI compliance must be verified.

Board(...)
Spacing(...)
PlacementPolicy(...)

Region(...)
Keepout(...)
Corner(...)
Cluster(...)
Corridor(...)
Decoupling(...)
ESD(...)
Pullup(...)
Series(...)
```

The generated `.ppl` includes comments explaining important inferences, for example why a capacitor was classified as decoupling or why an ESD device was placed between a connector and protected IC.

## Built-in placement strategy

Rules are emitted in a safe review order:

1. Board geometry
2. Regions and keepouts
3. Fixed mechanical placement
4. Connector and high-speed clusters
5. High-speed corridors
6. IC/power/RF support clusters
7. Decoupling and ESD refinements
8. Pullups, straps, series passives, and lower-priority refinements

## Heuristics

The first version infers roles from:

- reference prefixes: `C`, `R`, `L`, `FB`, `D`, `U`, `J`, `P`, `H`, `MH`, `TP`, and `SW`
- footprint/value text such as HDMI, USB, ESP32/WiFi, regulator, buck, TVS/ESD, crystal, oscillator, and resonator
- net names such as `TMDS`, `HDMI`, `USB`, `DP`, `DN`, `D+`, `D-`, `SSTX`, `SSRX`, `PCIe`, `LVDS`, `CLK`, `MIPI`, `ETH`, `RX`, and `TX`
- power/ground net names such as `3V3`, `5V`, `VCC`, `VDD`, `VBAT`, `VIN`, `GND`, `AGND`, and `DGND`
- differential suffixes such as `_P/_N`, `+/-`, `P/N`, and `DP/DN`

Implemented planner outputs include connector clusters, IC support clusters, default or intent-provided regions, RF keepouts, differential-pair corridors, decoupling rules, ESD rules, pullup rules, and series-component rules.

## Limitations and safety

`pcb-plan` does **not** route traces, generate vias, tune differential pairs, validate impedance, verify return paths, or certify EMI/SI behavior. The `.ppl` output is an engineering starting point that must be reviewed, edited if needed, executed by `pcb-place`, and then inspected in KiCad before routing and fabrication.

Never treat generated placement plans as production-ready without engineering review.
