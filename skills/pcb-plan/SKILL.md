---
name: pcb-plan
description: Extract board intelligence with `pcb-plan inspect` (planning-hints/), and review, validate, update, and emit board.pln with the pcb-plan CLI. Use when inspecting a KiCad board to seed AI planning, reviewing/validating an existing board.pln, managing routing/signal-integrity/stackup/differential-pair/simulation constraints, or emitting placement.ppl for a KiCad-based PCB project.
---

# pcb-plan

## Purpose

`pcb-plan` is the board-intelligence **extractor** and `board.pln` **reviewer**
for a PCB design. It reads a KiCad board (`.kicad_pcb`), netlist, stackup, and
board dimensions, extracts facts and candidate hints into a `planning-hints/`
directory, and reviews/validates/emits the authoritative `board.pln` that drives
deterministic placement.

`pcb-plan` does **not** generate the major placement strategy. That is now the
job of Claude via the **pcb-bootstrap** skill, which turns `planning-hints/`
into `board.pln`. `pcb-plan` extracts the facts and then reviews, validates, and
emits.

`pcb-plan`:

- runs `pcb-plan inspect` to extract facts and candidate hints into
  `planning-hints/` (for the pcb-bootstrap skill to consume)
- reviews and validates an existing `board.pln` (`check`, `review`, `explain`)
- identifies risks and missing constraints in `board.pln`
- emits deterministic `placement.ppl` for `pcb-place` (`emit`)
- updates `board.pln` from feedback reports (`update`)
- emits advisory routing-policy and OpenEMS planning artifacts
- generates JSON/Markdown reports

`pcb-plan` does **not**:

- place components, create placement ownership, or infer a final floorplan
  during `inspect` (it extracts facts and *candidate* hints only)
- invent the architecture/power islands/floorplan (that is AI's job in
  `board.pln`, via pcb-bootstrap)
- move footprints in a `.kicad_pcb` file
- route traces
- run simulations itself (it only emits hooks/handoff artifacts)

## Where pcb-plan fits

```
KiCad PCB + netlist + stackup + board dimensions
  -> pcb-plan inspect           -> planning-hints/
  -> Claude (pcb-bootstrap skill) generates board.pln  (AUTHORITATIVE design intent)
  -> pcb-plan check / review     (validate the plan)
  -> pcb-plan emit               -> placement.ppl
  -> pcb-place                   -> reports
  -> Claude optimization loop    (edits board.pln; pcb-plan update / re-emit)
```

Key principles:

- `board.pln` is the **authoritative** design-intent document, generated
  **primarily by AI** (Claude, via the pcb-bootstrap skill) from
  `planning-hints/`. `pcb-plan inspect` does not author it.
- `pcb-place` is the deterministic executor. It does not infer
  architecture/power islands/floorplan.
- AI is the primary planning engine and primarily edits `board.pln`. Preference
  order for changes: **1) `board.pln`, 2) `placement.ppl` override, 3) manual
  `Anchor()`**.
- KiCad groups are **metadata only** (`imported_kicad_groups`,
  `metadata_only: true`); they must **not** drive placement.

## Prerequisites

- `pcb-plan` installed (`pip install .` or `pip install -e '.[dev]'` from the
  repo root). Console script: `pcb-plan`. If the console script is
  unavailable, fall back to `python -m pcb_plan`.
- A KiCad board file (`.kicad_pcb`), normally produced by `pcb build` /
  `pcb layout` (Zener) or exported from KiCad.
- An optional netlist (e.g. `default.net`) for semantic alias resolution and
  connectivity-based inference.
- Known board dimensions (width/height) and, where relevant, stackup layer
  count for `inspect`.

## Inputs and Outputs

| Artifact | Role |
| --- | --- |
| `layout.kicad_pcb` (input) | KiCad board geometry and footprints. |
| `default.net` (input, optional) | Netlist for connectivity/alias inference. |
| `planning-hints/` (output of `inspect`) | Facts + candidate hints for AI planning. **Not authoritative.** See below. |
| `board.pln` (authoritative, AI-generated via pcb-bootstrap) | Planning intent: floorplan, routing/SI, stackup, simulation hooks, provenance. `pcb-plan` reviews/validates/emits it. |
| `placement.ppl` (output of `emit`) | Deterministic placement DSL consumed by `pcb-place`. |
| `pcb-plan-check-report.json` / `pcb-plan-update-report.json` / `pcb-plan-report.json` | Machine-readable reports: roles/interfaces/constraints, warnings, provenance, review-required items. |
| `routing-policy.yaml` (optional) | Routing/SI handoff for a routing tool. |
| `simulation/openems/openems-plan.yaml` (optional) | OpenEMS planning handoff. |
| `board.updated.pln` + `board.pln.patch` (from `update`) | Updated plan plus reviewable unified diff. |
| `*.summary.md` (optional) | Human-readable summary of any command via `--summary-md`. |

## Extracting Board Intelligence: `pcb-plan inspect`

`pcb-plan inspect` is the entry point of the new workflow. It reads the board,
netlist, stackup, and dimensions and writes a `planning-hints/` directory of
**facts and candidate hints**. It does **not** place components, does **not**
create placement ownership, and does **not** infer a final floorplan.

```bash
pcb-plan inspect \
  --board layout.kicad_pcb \
  --netlist default.net \
  --width 75 \
  --height 75 \
  --out planning-hints
```

`--out` and `--board` are required. Optional flags: `--origin-x`, `--origin-y`,
`--stackup-layers N`.

`planning-hints/` contains 15 files:

| File | Contents |
| --- | --- |
| `board-hints.json` | Master hints document (machine-readable). |
| `board-hints.md` | Human-readable summary of the hints. |
| `component-table.csv` | Per-component facts (ref, footprint, value, etc.). |
| `connectivity-graph.json` | Net/connectivity graph derived from the netlist. |
| `footprint-bboxes.json` | Footprint bounding boxes. |
| `pad-locations.json` | Pad locations. |
| `candidate-functional-paths.json` | Candidate functional signal paths. |
| `candidate-power-islands.json` | Candidate power islands. |
| `candidate-high-speed-paths.json` | Candidate high-speed paths. |
| `candidate-edge-connectors.json` | Candidate edge connectors. |
| `candidate-mechanicals.json` | Candidate mechanical/fixed parts. |
| `candidate-rf-zones.json` | Candidate RF zones. |
| `routing-classes.json` | Candidate routing classes. |
| `ai-pln-prompt.md` | Prompt scaffolding for the pcb-bootstrap skill. |
| `ai-placement-review.md` | AI placement-review scaffolding. |

Important about `planning-hints/`:

- These are **facts** (geometry, pads, connectivity, component table) plus
  **candidate hints** (the `candidate-*.json` files). The `candidate-*` files
  are suggestions, not decisions.
- Nothing in `planning-hints/` is authoritative. The authoritative document is
  `board.pln`, which Claude generates from these hints via the **pcb-bootstrap**
  skill.
- KiCad groups, if present, are surfaced as **metadata only**
  (`imported_kicad_groups`, `metadata_only: true`) and must not drive
  placement.

After running `inspect`, hand off to the **pcb-bootstrap** skill to turn the
hints into `board.pln`. Do not author the floorplan from `pcb-plan` itself.

## Primary Commands

```bash
# Extract facts + candidate hints into planning-hints/ (entry point)
pcb-plan inspect \
  --board layout.kicad_pcb \
  --netlist default.net \
  --width 75 --height 75 \
  --out planning-hints

# Evaluate plan quality of an existing board.pln (no placement.ppl written)
pcb-plan check \
  --pln board.pln \
  --board layout.kicad_pcb \
  --netlist default.net \
  --report-json pcb-plan-check-report.json

# Print a human-readable review of an existing board.pln
pcb-plan review \
  --pln board.pln

# Explain why an object/constraint exists in board.pln
pcb-plan explain \
  --pln board.pln \
  <object-or-path>

# Generate placement.ppl (and optional handoff artifacts)
pcb-plan emit \
  --pln board.pln \
  --board layout.kicad_pcb \
  --netlist default.net \
  -o placement.ppl \
  --report-json pcb-plan-report.json \
  --emit-routing-policy routing-policy.yaml \
  --emit-openems-plan simulation/openems/openems-plan.yaml

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
```

`explain` accepts a footprint reference, net name, or dotted `board.pln` path:

```bash
pcb-plan explain HDMI_TMDS0 --pln board.pln
pcb-plan explain U10 --pln board.pln
pcb-plan explain routing.classes.high_speed_diff --pln board.pln
```

### Legacy: `pcb-plan init`

`pcb-plan init` still exists, but it is now a **legacy heuristic generator /
fallback** for quick starts. It seeds a `board.pln` from heuristics rather than
from AI reasoning over `planning-hints/`. The **preferred** way to create
`board.pln` is the AI bootstrap path: `pcb-plan inspect` -> pcb-bootstrap skill.

Use `init` only when a quick heuristic starting point is needed and AI bootstrap
is unavailable; otherwise prefer `inspect` + pcb-bootstrap.

```bash
# Legacy heuristic fallback only — prefer inspect + pcb-bootstrap
pcb-plan init \
  --board layout.kicad_pcb \
  --netlist default.net \
  -o board.pln \
  --report-json pcb-plan-init-report.json
```

A legacy one-shot form remains for compatibility but should be avoided in new
workflows:

```bash
pcb-plan --board layout.kicad_pcb --netlist default.net --intent board.pln -o placement.ppl
```

## Skill Behavior

When asked to inspect, review, validate, or emit for a PCB, follow this
sequence:

1. **Locate artifacts.** Find the KiCad board (`.kicad_pcb`), netlist
   (`.net`/`default.net`), board dimensions, and stackup the user is working
   with. Ask if ambiguous.
2. **Extract facts if hints are missing.** Run `pcb-plan inspect` with
   `--width`/`--height` (and `--stackup-layers`/`--origin-*` as needed) to
   produce `planning-hints/`. Then point the user to the **pcb-bootstrap**
   skill to generate `board.pln` from those hints. Do not author the floorplan
   yourself in `pcb-plan`.
3. **Review `board.pln` if present.** Run `pcb-plan review --pln board.pln`
   before making edits, so existing intent and provenance are visible.
4. **Validate the plan and identify risks.** Run `pcb-plan check` and inspect
   `plan_confidence`. Surface missing constraints and risks from the report's
   `reasons` / `review_required_items` / `warnings` (e.g. missing netlist, bad
   board geometry, duplicate rules, incomplete differential-pair constraints).
   Address them in `board.pln` (preferably via the pcb-bootstrap loop) or
   explain to the user why the plan remains low-confidence.
5. **Suggest routing/SI constraints when missing.** If `routing`, `stackup`,
   `differential_pairs`, or `net_classes` are absent or incomplete for
   high-speed interfaces, propose additions to `board.pln` — but mark anything
   you add as inferred (see Provenance below) and explain the reasoning.
6. **Emit `placement.ppl`.** Run `pcb-plan emit` only after the reviewed
   `board.pln` reflects the desired intent. `emit` reports `plan_confidence`
   and prints `WARNING:` lines (and a `# Plan confidence: low` header in
   `placement.ppl`) for low-confidence plans; use `--strict-confidence` in CI
   to fail on low-confidence plans (paired with `--allow-low-confidence` to
   intentionally proceed anyway).
7. **Interpret reports.** Read `pcb-plan-*-report.json` and any `--summary-md`
   output; surface warnings and `review_required_items` to the user instead of
   silently proceeding.
8. **Feed back with `update`.** After `pcb-place` runs, use `pcb-plan update`
   with the place/route/sim reports to propose `board.pln` changes, reviewable
   as a `--patch` diff.
9. **Prefer `board.pln` edits over `placement.ppl` hacks.** Per the preference
   order (board.pln, then placement.ppl override, then manual `Anchor()`),
   route AI feedback into `board.pln`. Never overwrite explicit user-authored
   sections without calling it out; keep changes reviewable as a diff.
10. **Avoid silently inventing undocumented constraints.** Only use the
    documented top-level keys (see Important Concepts). Do not add unknown
    keys — `pcb-plan` validation reports unknown top-level keys.
11. **Mark inferred values as `inferred` / `requires_review`.** Any constraint
    you add or change that isn't explicitly requested by the user should use
    provenance (`source: ai_review`, `confidence`, `requires_review`, and
    `rationale`) so assumptions are not hidden.

## Reviewing and Validating board.pln

`pcb-plan`'s job in the new workflow is to make AI-authored intent trustworthy:
extract facts, then review, validate, and emit. It does not invent the
floorplan. When reviewing a `board.pln` (authored by Claude via pcb-bootstrap,
or hand-edited), inspect these together:

1. `board.pln`.
2. `planning-hints/board-hints.json` (the facts the plan should be consistent
   with).
3. `pcb-plan-check-report.json` after running `pcb-plan check`.
4. KiCad board geometry summary (`pcb-place --print-board`, `pcb-plan review`,
   or equivalent board-outline/Edge.Cuts details).
5. Netlist summary and connectivity-derived roles.
6. High-speed and differential-pair intent.
7. Region and keepout definitions.
8. Edge-required components and access sides.
9. Simulation triggers for OpenEMS and ngspice.

Look for and report risks and missing constraints, with proposed `board.pln`
changes when useful:

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
  analog filters, or power sequencing;
- placement being driven by KiCad groups (it must not be — groups are metadata
  only).

When the user asks Claude to fix or refine `board.pln`, prefer visible, minimal
edits:

- show the proposed diff or patch before/after the edit when in review mode;
- preserve user comments where the file format and editing method allow it;
- preserve existing provenance and add new provenance for inferred changes;
- mark uncertain inferred values as `requires_review: true`;
- rerun `pcb-plan review` and `pcb-plan check` after editing.

Allowed AI edits to `board.pln` include board geometry correction, region
size/position, keepout additions, `edge_required` metadata, `access_side`
metadata, `effective_side` overrides, decoupling group strategy, pullup/strap
group strategy, stackup/routing constraints, simulation triggers, and
OpenEMS/ngspice settings. Use provenance fields such as:

```yaml
source: ai_review
confidence: medium
requires_review: true
rationale: "Connector footprint and HDMI differential nets indicate this part needs edge access."
```

For routing and simulation review, check stackup completeness, impedance
targets, trace width/spacing, preferred layer, reference plane, via policy,
skew/length tolerances, differential pairs, and simulation triggers. Claude may
set `simulation.openems.enabled: auto` when high-speed/RF/differential pairs are
present, and `simulation.ngspice.enabled: auto` when regulators/reset/analog
sections are present. Never claim that a simulation plan or result proves
compliance.

### Recommended workflow

```bash
# 1. Extract facts + candidate hints
pcb-plan inspect \
  --board layout.kicad_pcb \
  --netlist default.net \
  --width 75 --height 75 \
  --out planning-hints

# 2. Claude generates board.pln from planning-hints/ via the pcb-bootstrap skill
#    (board.pln is the authoritative, AI-authored design intent)

# 3. Validate the plan
pcb-plan check \
  --pln board.pln \
  --board layout.kicad_pcb \
  --netlist default.net \
  --report-json pcb-plan-check-report.json

# AI review of pcb-plan-check-report.json; refine board.pln (prefer board.pln edits)

# 4. Emit deterministic placement
pcb-plan emit \
  --pln board.pln \
  --board layout.kicad_pcb \
  --netlist default.net \
  -o placement.ppl \
  --report-json pcb-plan-report.json

# 5. Run the deterministic executor
pcb-place \
  layout.kicad_pcb \
  placement.ppl \
  --dry-run \
  --report-json pcb-place-report.json

# 6. Feed results back into board.pln (reviewable patch)
pcb-plan update \
  --pln board.pln \
  --place-report pcb-place-report.json \
  --routing-report routing-report.json \
  --openems-report openems-report.json \
  -o board.updated.pln \
  --patch board.pln.patch
```

### Example review narrative

1. Run `pcb-plan inspect ...` to produce `planning-hints/`; hand off to the
   pcb-bootstrap skill to author `board.pln`.
2. Claude reviews `board.pln` against the hints and sees HDMI connectors that
   should be edge-located, then edits `edge_required: true` and
   `access_side: right` with `source: ai_review`.
3. Run `pcb-plan check ...`; Claude sees the U10 decoupling group near a board
   edge, then sets `effective_side: right`, enables stagger, and increases rows
   rather than disabling `DecouplingArray`.
4. Run `pcb-plan emit ...` and `pcb-place ... --dry-run`; Claude reviews the
   placement report.
5. Claude adjusts POWER and HIGH_SPEED regions/keepouts in `board.pln` to reduce
   overlap and improve routing corridors, then reruns check/emit/place.

## Placement Model (read by emit)

`emit` translates `board.pln` intent into deterministic placement following a
fixed priority: mechanical constraints first (board outline, mounting holes,
edge connectors, keepouts), functional signal-path topology second
(`functional_paths` / connector -> ESD -> IC paths emitted as
`HighSpeedPath(...)`), power topology third (`power_islands` with compact
regulator/input-cap/inductor/output-cap/feedback placement; load decouplers stay
at their loads), support passives fourth; spacing is always enforced from the
`spacing:` profile (conservative non-touching defaults). This priority is
*executed* from the AI-authored `board.pln`; `pcb-plan` does not invent the
floorplan.

Groups are metadata, clusters are not atomic: a ref can belong to several
semantic groups, but exactly one rule owns its final placement (see
`ownership_model`, `semantic_groups`, `multi_group_components`, and
`clusters_are_metadata` in the reports). Imported **KiCad groups are metadata
only** (`imported_kicad_groups`, `metadata_only: true`) and never own placement.
Edge-required connectors are edge-locked, rotated by `access_side`
(`rotation: auto`), and may extend their body outside the board with
`allow_body_outside_board: true`. Mounting holes go to distinct corners or
explicit `mechanical.mounting_holes` entries — never into clusters. Stackup
templates (`2_layer_basic` ... `10_layer_high_speed`, or
`stackup: { layers: 6 }`) improve intent only and never claim impedance
accuracy. Use `pcb-plan emit --ai-edit-hints ai-edit-hints.md` to get the
uncertain inferences, owners, and suggested `.pln`/`.ppl` edits for AI-assisted
iteration.

## Important Concepts

- **`board.pln`** — the authoritative source of planning intent (JSON or a
  dependency-free YAML-like subset), generated primarily by AI (Claude, via the
  pcb-bootstrap skill) from `planning-hints/`. Top-level sections: `board`,
  `fixed`, `regions`, `keepouts`, `roles`, `clusters`, `high_speed`, `routing`,
  `stackup`, `differential_pairs`, `net_classes`, `routing_overrides`,
  `simulation`, `provenance`, `spacing`, `components`, `mechanical`,
  `functional_paths`, `power_islands`. Avoid YAML
  anchors/aliases/block scalars/custom tags — use JSON if those features are
  needed.
- **`planning-hints/`** — the `inspect` output: facts (geometry, pads,
  connectivity, component table) plus `candidate-*` hints. **Not
  authoritative**; it is the input the pcb-bootstrap skill reasons over.
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

Output is an engineering starting point. The `candidate-*` hints from `inspect`
are suggestions, not decisions, and `board.pln` is AI-authored design intent
requiring review. Routing intent under `routing`, `differential_pairs`,
`stackup`, and `simulation` is **advisory**: `pcb-plan` validates, reports, and
explains it, but does not route traces or run simulations. All routing, SI, EMI,
and compliance claims require engineering review and, where applicable, lab
measurement.

## Troubleshooting

- **`board.pln` fails to parse** — check for unsupported YAML features
  (anchors, aliases, block scalars, custom tags, multi-document streams).
  Rewrite the file as JSON if needed.
- **Unknown top-level key warning** — remove or rename the key to one of the
  documented top-level sections; `pcb-plan` does not support undocumented
  keys.
- **`inspect` produces sparse hints** — confirm `--width`/`--height` are
  correct, supply `--netlist` for connectivity, and set `--stackup-layers` /
  `--origin-x` / `--origin-y` when the defaults do not match the board.
- **Missing P/N nets for a differential pair** — confirm both nets exist in
  the board/netlist; `pcb-plan` reports missing nets and unknown
  `routing.classes` references when board/netlist data is supplied.
- **`emit` produces an empty or minimal `placement.ppl`** — confirm
  `board.pln` has `regions`/`roles`/`fixed`/`clusters` populated. If
  `board.pln` is missing entirely, run `pcb-plan inspect` and use the
  pcb-bootstrap skill to author it (or, as a legacy fallback, `pcb-plan init`).
- **Stackup/reference-plane validation errors** — ensure `reference_plane`
  values match a `type: plane` layer name in `stackup.layers`, and that
  dielectric `thickness_mm`/`er` values are positive numbers.
- **Update produces unexpected proposals** — inspect
  `provenance.update_proposals` and the `--patch` diff before accepting; reject
  or edit proposals you disagree with rather than discarding the whole update.

## Examples

Extract board intelligence, hand off to pcb-bootstrap, then validate and emit:

```bash
pcb-plan inspect --board layout.kicad_pcb --netlist default.net \
  --width 75 --height 75 --out planning-hints
# Claude authors board.pln from planning-hints/ via the pcb-bootstrap skill
pcb-plan check --pln board.pln --board layout.kicad_pcb --netlist default.net \
  --report-json pcb-plan-check-report.json
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
