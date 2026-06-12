---
name: pcb-plan
description: Create, review, update, and emit PCB planning artifacts with the pcb-plan CLI. Use when initializing or editing board.pln, generating placement.ppl, managing routing/signal-integrity constraints, stackup, differential pairs, and OpenEMS/ngspice simulation hooks for a KiCad-based PCB project.
---

# pcb-plan

## Purpose

`pcb-plan` owns the `board.pln` planning-intent lifecycle for a PCB design. It
reads a KiCad board (`.kicad_pcb`) and optional netlist, infers or accepts
floorplan/routing/simulation intent, and emits the deterministic
`placement.ppl` file that `pcb-place` executes.

`pcb-plan`:

- creates and updates `board.pln` (planning intent, human-reviewable)
- infers placement, routing, and simulation intent with visible provenance
- emits `placement.ppl` for `pcb-place`
- emits advisory routing-policy and OpenEMS planning artifacts
- generates JSON/Markdown reports

`pcb-plan` does **not**:

- move footprints in a `.kicad_pcb` file
- route traces
- run simulations itself (it only emits hooks/handoff artifacts)

## Prerequisites

- `pcb-plan` installed (`pip install .` or `pip install -e '.[dev]'` from the
  repo root). Console script: `pcb-plan`. If the console script is
  unavailable, fall back to `python -m pcb_plan`.
- A KiCad board file (`.kicad_pcb`), normally produced by `pcb build` /
  `pcb layout` (Zener) or exported from KiCad.
- An optional netlist (e.g. `default.net`) for semantic alias resolution and
  connectivity-based inference.

## Inputs and Outputs

| Artifact | Role |
| --- | --- |
| `layout.kicad_pcb` (input) | KiCad board geometry and footprints. |
| `default.net` (input, optional) | Netlist for connectivity/alias inference. |
| `board.pln` (owned by pcb-plan) | Planning intent: floorplan, routing/SI, stackup, simulation hooks, provenance. |
| `placement.ppl` (output) | Deterministic placement DSL consumed by `pcb-place`. |
| `pcb-plan-init-report.json` / `pcb-plan-check-report.json` / `pcb-plan-update-report.json` / `pcb-plan-report.json` | Machine-readable reports: inferred roles/interfaces/constraints, warnings, provenance, review-required items. |
| `routing-policy.yaml` (optional) | Routing/SI handoff for a routing tool. |
| `simulation/openems/openems-plan.yaml` (optional) | OpenEMS planning handoff. |
| `board.updated.pln` + `board.pln.patch` (from `update`) | Updated plan plus reviewable unified diff. |
| `*.summary.md` (optional) | Human-readable summary of any command via `--summary-md`. |

## Primary Commands

```bash
# Create board.pln from a board (and optional netlist)
pcb-plan init \
  --board layout.kicad_pcb \
  --netlist default.net \
  -o board.pln \
  --report-json pcb-plan-init-report.json

# Print a human-readable review of an existing board.pln
pcb-plan review \
  --pln board.pln

# Explain why an object/constraint exists in board.pln
pcb-plan explain \
  --pln board.pln \
  <object-or-path>

# Update board.pln from feedback reports (place/route/sim)
pcb-plan update \
  --pln board.pln \
  --board layout.kicad_pcb \
  --place-report pcb-place-report.json \
  --routing-report routing-report.json \
  --openems-report openems-report.json \
  --ngspice-report ngspice-report.json \
  -o board.updated.pln \
  --patch board.pln.patch

# Evaluate plan quality without writing placement.ppl
pcb-plan check \
  --pln board.pln \
  --board layout.kicad_pcb \
  --netlist default.net \
  --report-json pcb-plan-check-report.json

# Generate placement.ppl (and optional handoff artifacts)
pcb-plan emit \
  --pln board.pln \
  --board layout.kicad_pcb \
  --netlist default.net \
  -o placement.ppl \
  --report-json pcb-plan-report.json \
  --emit-routing-policy routing-policy.yaml \
  --emit-openems-plan simulation/openems/openems-plan.yaml
```

`explain` accepts a footprint reference, net name, or dotted `board.pln` path:

```bash
pcb-plan explain HDMI_TMDS0 --pln board.pln
pcb-plan explain U10 --pln board.pln
pcb-plan explain routing.classes.high_speed_diff --pln board.pln
```

A legacy one-shot form remains for compatibility but should be avoided in new
workflows:

```bash
pcb-plan --board layout.kicad_pcb --netlist default.net --intent board.pln -o placement.ppl
```

## Skill Behavior

When asked to plan or update a PCB, follow this sequence:

1. **Locate artifacts.** Find the KiCad board (`.kicad_pcb`) and netlist
   (`.net`/`default.net`) the user is working with. Ask if ambiguous.
2. **Generate `board.pln` if missing.** Run `pcb-plan init` with
   `--report-json` and review the report's `inferred_roles`,
   `review_required_items`, and `warnings`.
3. **Perform AI-assisted optimization before emit.** Inspect `board.pln`, the
   init report, board geometry, netlist-derived connectivity, regions,
   keepouts, edge-required metadata, high-speed/routing/SI constraints,
   support arrays, and simulation triggers. Propose visible `board.pln` edits
   before `pcb-plan emit`; apply them only when the user requests optimization
   or an assisted/autonomous workflow.
4. **Review `board.pln` if present.** Run `pcb-plan review --pln board.pln`
   before making edits, so existing intent and provenance are visible.
5. **Suggest routing/SI constraints when missing.** If `routing`, `stackup`,
   `differential_pairs`, or `net_classes` are absent or incomplete for
   high-speed interfaces, propose additions — but mark anything you add as
   inferred (see Provenance below) and explain the reasoning.
6. **Check plan quality before emitting.** Run `pcb-plan check` and inspect
   `plan_confidence`. AI-review the check report and address the listed
   `reasons` (e.g. add a netlist, fix board geometry, resolve duplicate
   rules, complete differential-pair constraints) before generating
   `placement.ppl`, or explain to the user why the plan remains
   low-confidence.
7. **Generate `placement.ppl`.** Run `pcb-plan emit` only after the
   AI-reviewed `board.pln` reflects the desired intent. `emit` also reports
   `plan_confidence` and prints `WARNING:` lines (and a
   `# Plan confidence: low` header in `placement.ppl`) for low-confidence
   plans; use `--strict-confidence` in CI to fail on low-confidence plans
   (paired with `--allow-low-confidence` to intentionally proceed anyway).
8. **Interpret reports.** Read `pcb-plan-*-report.json` and any
   `--summary-md` output; surface warnings and `review_required_items` to the
   user instead of silently proceeding.
9. **Preserve user intent and provenance.** Never overwrite explicit
   user-authored sections of `board.pln` without calling it out. Prefer
   `board.pln` edits over hand-written `placement.ppl` hacks, and make
   changes reviewable as a diff.
10. **Avoid silently inventing undocumented constraints.** Only use the
    documented top-level keys (see Important Concepts). Do not add unknown
    keys — `pcb-plan` validation reports unknown top-level keys.
11. **Mark inferred values as `inferred` / `requires_review`.** Any constraint
    you add or change that isn't explicitly requested by the user should use
    provenance (`source: ai_review`, `confidence`, `requires_review`, and
    `rationale`) so assumptions are not hidden.

## AI-Assisted board.pln Optimization

After `pcb-plan init` and before `pcb-plan emit`, Claude should perform an
explicit AI review/optimization pass. The goal is to improve planning intent in
`board.pln`, not to hide tool failures in generated `placement.ppl`.

Inspect these inputs together:

1. `board.pln`.
2. `pcb-plan-init-report.json`.
3. `pcb-plan-check-report.json` after running `pcb-plan check`.
4. KiCad board geometry summary (`pcb-place --print-board`, `pcb-plan review`,
   or equivalent board-outline/Edge.Cuts details).
5. Netlist summary and connectivity-derived roles.
6. High-speed and differential-pair inference.
7. Region and keepout definitions.
8. Edge-required components and access sides.
9. Simulation triggers for OpenEMS and ngspice.

Look for and report, with proposed `board.pln` changes when useful:

- incorrect board dimensions or origin, including integer board geometry that
  should be represented exactly;
- missing `Edge.Cuts` or a bad fallback to footprint-extents geometry;
- missing or incorrect fixed mechanical parts;
- connectors that are not marked `edge_required`;
- missing `access_side` for connectors;
- high-speed regions that are too small, on the wrong side, or missing
  practical routing corridors;
- power regions too close to high-speed/RF regions;
- RF keepouts that are missing or too small;
- `DecouplingArray` groups without appropriate spacing, stagger, rows, or
  `effective_side`;
- `PullupArray`/strap groups assigned to the wrong owner, side, or spacing;
- false `Series(...)` inference;
- missing differential pairs, missing pair classes, or incomplete
  skew/length/impedance intent;
- missing stackup, routing constraints, reference planes, trace width/spacing,
  via policy, or preferred layers;
- OpenEMS triggers that should be enabled for high-speed, RF, differential-pair,
  or switching-power-near-high-speed boards;
- ngspice triggers that should be enabled for regulators, reset circuits,
  analog filters, or power sequencing.

When the user asks Claude to optimize, Claude may edit `board.pln` directly.
Prefer visible, minimal edits:

- show the proposed diff or patch before/after the edit when in review mode;
- preserve user comments where the file format and editing method allow it;
- preserve existing provenance and add new provenance for inferred changes;
- mark uncertain inferred values as `requires_review: true`;
- rerun `pcb-plan review` and `pcb-plan check` after editing.

Allowed AI edits include board geometry correction, region size/position,
keepout additions, `edge_required` metadata, `access_side` metadata,
`effective_side` overrides, decoupling group strategy, pullup/strap group
strategy, stackup/routing constraints, simulation triggers, and OpenEMS/ngspice
settings. Use provenance fields such as:

```yaml
source: ai_review
confidence: medium
requires_review: true
rationale: "Connector footprint and HDMI differential nets indicate this part needs edge access."
```

For routing and simulation optimization, review stackup completeness, impedance
targets, trace width/spacing, preferred layer, reference plane, via policy,
skew/length tolerances, differential pairs, and simulation triggers. Claude may
set `simulation.openems.enabled: auto` when high-speed/RF/differential pairs are
present, and `simulation.ngspice.enabled: auto` when regulators/reset/analog
sections are present. Never claim that a simulation plan or result proves
compliance.

### Recommended AI-reviewed workflow

```bash
pcb-plan init \
  --board layout.kicad_pcb \
  --netlist default.net \
  -o board.pln \
  --report-json pcb-plan-init-report.json

# AI review/optimization of board.pln

pcb-plan check \
  --pln board.pln \
  --board layout.kicad_pcb \
  --netlist default.net \
  --report-json pcb-plan-check-report.json

# AI review/optimization of pcb-plan-check-report.json

pcb-plan emit \
  --pln board.pln \
  --board layout.kicad_pcb \
  --netlist default.net \
  -o placement.ppl \
  --report-json pcb-plan-report.json

pcb-place \
  layout.kicad_pcb \
  placement.ppl \
  --dry-run \
  --report-json pcb-place-report.json

# AI review/optimization of pcb-place-report.json, followed by optional feedback
pcb-plan update \
  --pln board.pln \
  --place-report pcb-place-report.json \
  --routing-report routing-report.json \
  --openems-report openems-report.json \
  -o board.updated.pln \
  --patch board.pln.patch
```

### Example AI optimization narrative

1. Run `pcb-plan init ...` to seed `board.pln`.
2. Claude reviews `board.pln` and sees HDMI connectors inferred as ordinary
   connectors, then edits `edge_required: true` and `access_side: right` with
   `source: ai_review`.
3. Run `pcb-plan check ...`; Claude sees the U10 decoupling group near a board
   edge, then sets `effective_side: right`, enables stagger, and increases rows
   rather than disabling `DecouplingArray`.
4. Run `pcb-plan emit ...` and `pcb-place ... --dry-run`; Claude reviews the
   placement report.
5. Claude adjusts POWER and HIGH_SPEED regions/keepouts to reduce overlap and
   improve routing corridors, then reruns check/emit/place.

## Topology-Aware Placement Model

Planning follows a fixed priority: mechanical constraints first (board outline,
mounting holes, edge connectors, keepouts), functional signal-path topology
second (`functional_paths` / inferred connector -> ESD -> IC paths emitted as
`HighSpeedPath(...)`), power topology third (`power_islands` with compact
regulator/input-cap/inductor/output-cap/feedback placement; load decouplers stay
at their loads), support passives fourth; spacing is always enforced from the
`spacing:` profile (conservative non-touching defaults).

Groups are metadata, clusters are not atomic: a ref can belong to several
semantic groups, but exactly one rule owns its final placement (see
`ownership_model`, `semantic_groups`, `multi_group_components`, and
`clusters_are_metadata` in the reports). Edge-required connectors are
edge-locked, rotated by `access_side` (`rotation: auto`), and may extend their
body outside the board with `allow_body_outside_board: true`. Mounting holes go
to distinct corners or explicit `mechanical.mounting_holes` entries — never into
clusters. Stackup templates (`2_layer_basic` ... `10_layer_high_speed`, or
`stackup: { layers: 6 }`) improve intent only and never claim impedance
accuracy. Use `pcb-plan emit --ai-edit-hints ai-edit-hints.md` to get the
uncertain inferences, owners, and suggested `.pln`/`.ppl` edits for AI-assisted
iteration.

## Important Concepts

- **`board.pln`** — the single source of planning intent (JSON or a
  dependency-free YAML-like subset). Top-level sections: `board`, `fixed`,
  `regions`, `keepouts`, `roles`, `clusters`, `high_speed`, `routing`,
  `stackup`, `differential_pairs`, `net_classes`, `routing_overrides`,
  `simulation`, `provenance`, `spacing`, `components`, `mechanical`,
  `functional_paths`, `power_islands`. Avoid YAML anchors/aliases/block scalars/custom
  tags — use JSON if those features are needed.
- **`placement.ppl`** — the deterministic placement DSL consumed by
  `pcb-place`. `pcb-plan emit` unwraps provenance-wrapped values into plain
  values before generating it.
- **`stackup`** — `stackup.layers` (signal/plane layers, `copper_oz`, plane
  `net`) and `stackup.dielectric` (`between`, `material`, `thickness_mm`,
  `er`). Used for impedance/reference-plane reasoning.
- **`routing`** — `routing.mode` (one of `low_speed_only`,
  `all_nets_constrained`, `experimental_high_speed`), `routing.defaults`, and
  `routing.classes` (named routing classes with geometry/impedance/layer/via
  policy fields).
- **`differential_pairs`** — named P/N net pairs (`p`, `n`, `class`, optional
  pair-local overrides like `max_skew_mm`).
- **`impedance target`** — `impedance_ohms` on a routing class or
  differential pair; paired with `stackup` and `reference_plane` for
  meaningful SI reasoning.
- **`trace width/spacing`** — `trace_width_mm`, `trace_spacing_mm` /
  `clearance_mm` on `routing.defaults`, `routing.classes`, or
  `routing_overrides`.
- **`preferred layer`** — `preferred_layer` / `preferred_layers` on routing
  defaults, classes, or overrides.
- **`reference plane`** — `reference_plane` (e.g. `In1.GND`) — must reference
  a plane layer declared in `stackup.layers`.
- **`via policy`** — one of `avoid`, `allow`, `constrained`, `forbid`, plus
  `max_vias`.
- **`skew/length tolerance`** — `max_skew_mm`, `max_length_mismatch_mm` on
  routing classes, differential pairs, or overrides.
- **`OpenEMS hooks`** — `simulation.openems` (`enabled: true|false|auto`,
  `trigger_on: [high_speed_diff, rf, switching_power_near_high_speed, ...]`,
  `export_dir`). Use `--emit-openems-plan` during `emit` to write a planning
  artifact when enabled.
- **`ngspice hooks`** — `simulation.ngspice` (`enabled`, `trigger_on:
  [regulator, reset_circuit, analog_filter, ...]`).
- **`provenance`** — `generator`, `schema_version`, and
  `update_proposals: [{type, value, source, confidence, requires_review}]`.
  `review` and `explain` surface this so assumptions are never hidden.

## Limitations

`pcb-plan` (and any skill built on it) must **not** claim:

- FCC compliance
- HDMI/USB/Ethernet or other interface compliance
- production readiness
- EMI verification
- SI/impedance verification

Output is an engineering starting point. Routing intent under `routing`,
`differential_pairs`, `stackup`, and `simulation` is **advisory**: `pcb-plan`
validates, reports, and explains it, but does not route traces or run
simulations. All routing, SI, EMI, and compliance claims require engineering
review and, where applicable, lab measurement.

## Troubleshooting

- **`board.pln` fails to parse** — check for unsupported YAML features
  (anchors, aliases, block scalars, custom tags, multi-document streams).
  Rewrite the file as JSON if needed.
- **Unknown top-level key warning** — remove or rename the key to one of the
  documented top-level sections; `pcb-plan` does not support undocumented
  keys.
- **Missing P/N nets for a differential pair** — confirm both nets exist in
  the board/netlist; `pcb-plan` reports missing nets and unknown
  `routing.classes` references when board/netlist data is supplied.
- **`emit` produces an empty or minimal `placement.ppl`** — confirm
  `board.pln` has `regions`/`roles`/`fixed`/`clusters` populated, or run
  `pcb-plan init` first to seed inferred structure.
- **Stackup/reference-plane validation errors** — ensure `reference_plane`
  values match a `type: plane` layer name in `stackup.layers`, and that
  dielectric `thickness_mm`/`er` values are positive numbers.
- **Update produces unexpected proposals** — inspect
  `provenance.update_proposals` and the `--patch` diff before accepting; reject
  or edit proposals you disagree with rather than discarding the whole update.

## Examples

Bootstrap planning for a new board and emit placement:

```bash
pcb-plan init --board layout.kicad_pcb --netlist default.net -o board.pln \
  --report-json pcb-plan-init-report.json
pcb-plan review --pln board.pln
pcb-plan emit --pln board.pln --board layout.kicad_pcb --netlist default.net \
  -o placement.ppl --report-json pcb-plan-report.json
```

Feed placement/routing/simulation feedback back into the plan:

```bash
pcb-plan update --pln board.pln --board layout.kicad_pcb \
  --place-report pcb-place-report.json \
  --routing-report routing-report.json \
  -o board.updated.pln --patch board.pln.patch
# review board.pln.patch with the user before applying
```

See [`src/pcb_plan/README.md`](../../src/pcb_plan/README.md) for the full
`board.pln` syntax reference, CLI flags, and reports.
