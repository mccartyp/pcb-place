---
name: pcb-automation-orchestrator
description: Coordinate the end-to-end PCB automation workflow across pcb (Zener), pcb-plan, pcb-place, KiCad routing tools, KiCad DRC/ERC, and optional OpenEMS/ngspice simulation. Use for full board iterations (build -> plan -> place -> route -> verify -> simulate -> update plan), all-net or high-speed routing with constraints, and bounded autonomous iteration loops.
---

# pcb-automation-orchestrator

## Purpose

This skill coordinates the full PCB automation workflow. It does **not**
replace any individual tool — it sequences them, passes artifacts between
them, and decides (in review mode) or applies (in autonomous mode) the
resulting `board.pln` updates:

- **`pcb` (Zener)** — hardware-as-code board build/layout (`pcb build`,
  `pcb layout`).
- **`pcb-plan`** — owns `board.pln`, emits `placement.ppl` and routing/SI
  handoff artifacts. See [`../pcb-plan/SKILL.md`](../pcb-plan/SKILL.md).
- **`pcb-place`** — deterministic placement executor. See
  [`../pcb-place/SKILL.md`](../pcb-place/SKILL.md).
- **KiCadRoutingTools** — external autorouter/router invoked using
  `routing-policy.yaml` from `pcb-plan emit`.
- **KiCad DRC/ERC** — design-rule and electrical-rule checks on the routed
  board.
- **OpenEMS** (optional) — electromagnetic simulation for high-speed/RF
  concerns.
- **ngspice** (optional) — circuit simulation for regulators, resets,
  filters, and analog sections.

## Prerequisites

- `pcb-plan` and `pcb-place` installed (see their skills for details).
- `pcb` (Zener) CLI for board build/layout, if starting from a `.zen` design.
- KiCadRoutingTools (or another routing tool that consumes
  `routing-policy.yaml`) and a KiCad DRC/ERC runner available on `PATH` for
  routing/verification stages.
- OpenEMS and/or ngspice available if simulation stages are requested or
  triggered.

## Inputs and Outputs

Primary generated artifacts (paths are conventions; adjust to project
layout):

```text
board.pln                      # owned by pcb-plan
placement.ppl                  # pcb-plan emit -> pcb-place input
pcb-plan-report.json           # pcb-plan emit report
pcb-place-report.json          # pcb-place dry-run/write report
routing-policy.yaml            # pcb-plan emit --emit-routing-policy
routing-report.json            # from KiCadRoutingTools
high-speed-routing-review.md   # generated when high-speed nets are auto-routed
openems-plan.yaml              # pcb-plan emit --emit-openems-plan
ngspice-summary.md             # ngspice run summary
design-score.json              # orchestrator-computed iteration score
iteration-summary.md           # per-iteration summary for review/autonomous mode
```

## Workflow

```bash
# 1. Build/layout (Zener)
pcb build board.zen
pcb layout board.zen

# 2. Plan
pcb-plan init --board layout.kicad_pcb --netlist default.net -o board.pln \
  --report-json pcb-plan-init-report.json
pcb-plan emit --pln board.pln --board layout.kicad_pcb --netlist default.net \
  -o placement.ppl --report-json pcb-plan-report.json \
  --emit-routing-policy routing-policy.yaml \
  --emit-openems-plan simulation/openems/openems-plan.yaml

# 3. Place
pcb-place layout.kicad_pcb placement.ppl --dry-run --report-json pcb-place-report.json
pcb-place layout.kicad_pcb placement.ppl -o layout.placed.kicad_pcb --report-json pcb-place-report.json

# 4. Route (external tool, driven by routing-policy.yaml)
KiCadRoutingTools route layout.placed.kicad_pcb \
  --policy routing-policy.yaml \
  --report-json routing-report.json

# 5. Verify
# Run KiCad DRC/ERC on the routed board (via kicad-cli or equivalent)

# 6. Optional simulation
# OpenEMS using simulation/openems/openems-plan.yaml
# ngspice for regulators/reset/filter/analog sections

# 7. Feed results back into the plan
pcb-plan update --pln board.pln --board layout.placed.kicad_pcb \
  --place-report pcb-place-report.json \
  --routing-report routing-report.json \
  --openems-report openems-report.json \
  --ngspice-report ngspice-report.json \
  -o board.updated.pln --patch board.pln.patch
```

Each stage's output feeds the next: `board.pln` -> `placement.ppl` ->
`layout.placed.kicad_pcb` -> routed board -> DRC/ERC + simulation reports ->
`board.pln` update proposals.

## Routing Modes

`routing.mode` in `board.pln` (see `pcb-plan` skill) selects the routing
strategy:

| Mode | Meaning |
| --- | --- |
| `low_speed_only` | Only low-speed/general nets are routed automatically; high-speed/differential nets are left for manual routing. |
| `all_nets_constrained` | All nets, including high-speed differential pairs, are routed automatically using the constraints declared in `routing`/`differential_pairs`/`stackup`. |
| `experimental_high_speed` | High-speed nets are routed automatically even where constraints are incomplete or low-confidence; requires extra review (see High-Speed Routing). |

The orchestrator **must support all-net routing, including high-speed nets**,
when either:

- `board.pln` provides sufficient `routing`/`differential_pairs`/`stackup`
  constraints (`routing.mode: all_nets_constrained`), or
- the user explicitly chooses `experimental_high_speed` routing.

If neither condition holds and high-speed nets exist, default to
`low_speed_only` and tell the user what's missing (e.g. impedance target,
reference plane, stackup) before offering to route high-speed nets.

### Constraint vocabulary (from `board.pln` / `routing-policy.yaml`)

- **stackup** — copper/plane layer stack and dielectric definitions; needed
  for impedance and reference-plane reasoning.
- **impedance target** — `impedance_ohms` per routing class / differential
  pair.
- **trace width/spacing** — `trace_width_mm`, `trace_spacing_mm` /
  `clearance_mm`.
- **preferred layer** — `preferred_layer` / `preferred_layers`.
- **reference plane** — `reference_plane` (must match a `type: plane` layer).
- **via policy** — `avoid` | `allow` | `constrained` | `forbid`, plus
  `max_vias`.
- **skew/length tolerance** — `max_skew_mm`, `max_length_mismatch_mm`.
- **differential pair classes** — named pairs (`p`/`n` nets) bound to a
  routing class via `differential_pairs.<name>.class`.

These pass from `board.pln` -> `pcb-plan emit --emit-routing-policy` ->
`routing-policy.yaml` -> KiCadRoutingTools.

## High-Speed Routing

If high-speed nets (including differential pairs) are routed automatically
(`all_nets_constrained` or `experimental_high_speed`):

1. Generate `high-speed-routing-review.md` summarizing: which nets/pairs were
   routed, the constraints applied (impedance, width/spacing, reference
   plane, via policy, skew/length tolerance), and any constraints that were
   missing/inferred and require review.
2. Run KiCad DRC after routing.
3. Where reports exist, check skew, length mismatch, and via count against
   `max_skew_mm`, `max_length_mismatch_mm`, and `max_vias` from `board.pln`.
4. Flag items that need human review (e.g. low-confidence/inferred
   constraints, DRC violations, missing reference planes, unmatched length
   pairs).
5. **Do not claim compliance** of any kind (see Safety / Limits).

## Simulation Hooks

### OpenEMS

Document/trigger an OpenEMS plan (`simulation.openems` in `board.pln`, emitted
via `pcb-plan emit --emit-openems-plan`) when:

- high-speed differential pairs exist (e.g. HDMI, USB, Ethernet)
- RF/antenna modules exist
- switching regulators are placed near sensitive/high-speed regions
- the user requests EMI/SI iteration

### ngspice

Document/trigger ngspice analysis for:

- voltage regulators
- reset circuits
- filters
- analog sections
- power sequencing

Feed `openems-report.json` / `ngspice-summary.md` (or equivalent) back via
`pcb-plan update --openems-report ... --ngspice-report ...` so feedback
becomes visible `provenance.update_proposals`, not silent edits.

## Iteration

### Review mode (default)

- Run plan/place/route/verify/simulate as requested.
- Propose `board.pln` changes via `pcb-plan update -o board.updated.pln
  --patch board.pln.patch`.
- **Do not apply** `board.updated.pln` over `board.pln` automatically —
  present the patch and report to the user for approval.

### Autonomous mode

- Apply `board.updated.pln` as the new `board.pln` (after `pcb-plan update`).
- Re-run plan -> place -> route -> verify -> (simulate) for the next
  iteration.
- **Bounded**: default `max_iterations = 3`. Stop early if DRC passes with no
  open `review_required_items`, or if an iteration makes no further changes
  to `board.pln`.
- Write `iteration-summary.md` after each iteration: what changed, current
  `design-score.json`, remaining warnings/review items, and whether another
  iteration is planned.
- If `max_iterations` is reached without a clean result, stop and report
  outstanding issues — do not silently continue or claim success.

Autonomous mode should still surface every `requires_review: true` item from
`board.pln` provenance to the user at the end of the run, even if iteration
completed.

## Safety / Limits

Never claim, in reports, summaries, or chat responses:

- EMI compliance
- SI verification
- FCC compliance
- HDMI/USB/Ethernet or other interface compliance
- production readiness

Allowed phrasing:

- "routed under current constraints"
- "passes current DRC"
- "current placement has no known collisions"
- "simulation suggests ..."
- "requires engineering review"

Every iteration summary and high-speed routing review should end with an
explicit list of items requiring engineering review (open
`requires_review: true` provenance entries, DRC/ERC findings, and any
constraint that was inferred rather than user-specified).

## Examples

Single review-mode pass with all-net routing using existing constraints:

```bash
pcb build board.zen
pcb layout board.zen

pcb-plan init --board layout.kicad_pcb --netlist default.net -o board.pln
pcb-plan emit --pln board.pln --board layout.kicad_pcb --netlist default.net \
  -o placement.ppl --emit-routing-policy routing-policy.yaml \
  --emit-openems-plan simulation/openems/openems-plan.yaml

pcb-place layout.kicad_pcb placement.ppl -o layout.placed.kicad_pcb --report-json pcb-place-report.json

KiCadRoutingTools route layout.placed.kicad_pcb --policy routing-policy.yaml --report-json routing-report.json
# run KiCad DRC/ERC

pcb-plan update --pln board.pln --board layout.placed.kicad_pcb \
  --place-report pcb-place-report.json --routing-report routing-report.json \
  -o board.updated.pln --patch board.pln.patch
# present board.pln.patch to the user; do not apply automatically
```

Bounded autonomous loop (3 iterations max), only when explicitly requested:

```text
for i in 1..3:
  emit placement.ppl from board.pln
  pcb-place ... -o layout.placed.kicad_pcb
  route + DRC/ERC (+ optional simulation)
  pcb-plan update ... -o board.updated.pln --patch board.pln.patch
  if no review_required_items and DRC clean: break
  board.pln <- board.updated.pln
  write iteration-summary.md
report final status + outstanding review items
```

See [`../pcb-plan/SKILL.md`](../pcb-plan/SKILL.md) and
[`../pcb-place/SKILL.md`](../pcb-place/SKILL.md) for tool-specific details, and
the root [`README.md`](../../README.md) for the repository-level workflow
diagram.
