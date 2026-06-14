---
name: pcb-bootstrap
description: Generate the initial authoritative board.pln from extracted planning hints. Use as the primary planning skill when asked to bootstrap, initialize, or create a board.pln or floorplan intent for a KiCad-based PCB project — it runs pcb-plan inspect to produce planning-hints/, reasons about each candidate hint, and authors a complete board.pln that pcb-plan check/review then validates.
---

# pcb-bootstrap

## Purpose

`pcb-bootstrap` is the **primary planning skill**. Its job is to turn extracted
board-intelligence hints into the authoritative `board.pln` design-intent
document that drives everything downstream.

The architecture separates extraction, planning, and execution:

- `pcb-plan inspect` is a board-intelligence **extractor**. It reads a KiCad
  board, netlist, stackup, and board dimensions, and writes `planning-hints/` —
  **facts and candidate hints only**. It does **not** place components, does
  **not** create placement ownership, and does **not** infer a final floorplan.
- **Claude (this skill)** is the primary planning engine. It reads
  `planning-hints/`, resolves every candidate hint into an explicit decision,
  and authors `board.pln`. `board.pln` is the **single authoritative
  design-intent document**, generated primarily by AI.
- `pcb-place` is the deterministic **executor** (apply placement, reflow,
  optimize, validate, report). It does **not** infer architecture, power
  islands, or floorplan.

`pcb-bootstrap`:

- runs `pcb-plan inspect` to produce `planning-hints/`
- reads the hints, especially `board-hints.json`, `ai-pln-prompt.md`, and
  `ai-placement-review.md`
- reasons about each candidate hint and resolves it into an explicit
  `board.pln` decision (or drops it with a recorded rationale)
- authors a complete `board.pln` (YAML)
- validates the result with `pcb-plan check` and `pcb-plan review`

`pcb-bootstrap` does **not**:

- run placement (that is `pcb-place`)
- floorplan from `inspect` (inspect only extracts facts/candidates)
- replace engineering review (heuristic hints cannot verify impedance, return
  paths, plane splits, thermal, or EMI)

## Where this fits

```
KiCad PCB + netlist + stackup + board dimensions
  -> pcb-plan inspect          -> planning-hints/
  -> Claude (pcb-bootstrap)    -> board.pln          (authoritative intent)
  -> pcb-plan check / review   -> validate intent
  -> pcb-place                 -> reports
  -> Claude optimization loop  -> edits board.pln
```

The bootstrap skill owns the first two arrows: extract hints, then generate the
initial `board.pln`. After bootstrap, the `pcb-plan` and `pcb-place` skills own
the validate/execute/optimize loop.

## Prerequisites

- `pcb-plan` installed (`pip install .` or `pip install -e '.[dev]'` from the
  repo root). Console script: `pcb-plan`. If the console script is unavailable,
  fall back to `python -m pcb_plan`.
- A KiCad board file (`.kicad_pcb`), normally produced by `pcb build` /
  `pcb layout` (Zener) or exported from KiCad.
- A netlist (e.g. `default.net`) for semantic alias resolution and
  connectivity-based candidate inference.
- Known board dimensions (width/height in mm). Stackup layer count is helpful
  but optional.

## Inputs and Outputs

| Artifact | Role |
| --- | --- |
| `layout.kicad_pcb` (input) | KiCad board geometry and footprints. |
| `default.net` (input) | Netlist for connectivity/alias candidate inference. |
| Board width/height/origin (input) | Geometry passed to `inspect`. |
| `planning-hints/` (output of `inspect`) | Extracted facts and candidate hints (15 files, see below). |
| `board.pln` (authored by this skill) | The authoritative design-intent document. |
| `pcb-plan-check-report.json` (output of `check`) | Plan-quality report: low-confidence/missing constraints, warnings, review-required items. |

### planning-hints/ contents

`pcb-plan inspect` writes 15 files. The most important for bootstrap are
`board-hints.json` (the master file), `ai-pln-prompt.md` (a prompt that guides
Claude to author `board.pln`), and `ai-placement-review.md` (a review
checklist).

- `board-hints.json` — master file: `board_geometry`, counts, `candidate_roles`,
  `candidate_role_counts`, `candidate_functional_paths`,
  `candidate_high_speed_paths`, `candidate_power_islands`,
  `candidate_edge_connectors`, `candidate_mechanicals`, `candidate_rf_zones`,
  `differential_pairs`, `routing_classes`, `stackup_assumptions`,
  `simulation_candidates`, `imported_kicad_groups`, `kicad_groups_note`,
  `aliases_recovered`, `warnings`.
- `board-hints.md` — human-readable summary.
- `component-table.csv` — per-component table.
- `connectivity-graph.json` — net/connectivity graph.
- `footprint-bboxes.json` — footprint bounding boxes.
- `pad-locations.json` — pad coordinates.
- `candidate-functional-paths.json`
- `candidate-power-islands.json`
- `candidate-high-speed-paths.json`
- `candidate-edge-connectors.json`
- `candidate-mechanicals.json`
- `candidate-rf-zones.json`
- `routing-classes.json`
- `ai-pln-prompt.md` — a prompt that guides Claude to generate `board.pln`.
- `ai-placement-review.md` — a review checklist.

## Primary Commands

```bash
# 1. Extract facts and candidate hints into planning-hints/
pcb-plan inspect \
  --board layout.kicad_pcb \
  --netlist default.net \
  --width 75 \
  --height 75 \
  --out planning-hints

# (optional flags: --origin-x, --origin-y, --stackup-layers N)

# 2. Claude reads planning-hints/ and authors board.pln (no CLI step)

# 3. Validate the authored plan
pcb-plan check \
  --pln board.pln \
  --board layout.kicad_pcb \
  --netlist default.net \
  --report-json pcb-plan-check-report.json

pcb-plan review \
  --pln board.pln
```

`--out` and `--board` are required for `inspect`; `--netlist`, `--width`, and
`--height` should always be supplied so candidate inference and geometry are
correct. Optional `--origin-x` / `--origin-y` set the board origin and
`--stackup-layers N` records the assumed layer count.

> **`pcb-plan init` is legacy.** `init` still exists as a heuristic
> generator/fallback that synthesizes a `board.pln` without AI reasoning. The
> AI bootstrap from hints documented here is the **preferred** way to create
> `board.pln`. Reach for `init` only as a fallback when you cannot author the
> plan from hints.

Real `pcb-plan` subcommands: `inspect`, `init`, `update`, `review`, `explain`,
`emit`, `check`. Do not invent other subcommands or flags.

## Skill Behavior

When asked to bootstrap, initialize, or create a `board.pln` / floorplan intent:

1. **Locate artifacts.** Find the KiCad board (`.kicad_pcb`) and netlist
   (`.net`/`default.net`). Confirm board width/height (and origin/stackup if
   known). Ask if ambiguous.
2. **Run `pcb-plan inspect`.** Produce `planning-hints/` with the board,
   netlist, width, height, and `--out`. Add `--origin-x`/`--origin-y` and
   `--stackup-layers` when known.
3. **Read the hints.** Read `board-hints.json` for the full candidate set, and
   read `ai-pln-prompt.md` and `ai-placement-review.md` to ground your authoring
   and review. Skim `board-hints.md` and the per-category candidate JSON
   (functional paths, power islands, high-speed paths, edge connectors,
   mechanicals, RF zones, routing classes) for detail. Surface `warnings` to
   the user.
4. **Resolve every candidate into an explicit decision.** For each
   `candidate_*` entry, decide deliberately: encode it into `board.pln` (with a
   rationale), or drop it (with a recorded rationale for why it does not apply).
   Do not silently ignore candidates. Candidates are hints, not commitments —
   simulation candidate booleans
   (`openems_candidate`, `ngspice_candidate`, `si_candidate`) are hints, not
   results, and must be confirmed by your reasoning before becoming triggers.
5. **Author a complete `board.pln`.** Write valid YAML covering the required
   sections (see below). Assign exactly **one placement owner per ref** — a ref
   may appear in several semantic groups, but exactly one rule must own its
   final placement.
6. **Treat KiCad groups as metadata only.** `inspect` exports KiCad groups under
   `imported_kicad_groups` with `metadata_only: true`. They are informational;
   they MUST NOT drive placement. Read `kicad_groups_note`, but author
   placement intent from the candidate hints and your reasoning, not from
   groups.
7. **Encode intent in `board.pln`, not in later overrides.** `board.pln` is the
   authoritative artifact. Prefer expressing every decision here. The change
   preference order is: 1) `board.pln`, 2) a `placement.ppl` override, 3) a
   manual `Anchor()`. Use the lower-preference mechanisms only when the higher
   one cannot express the intent.
8. **Validate.** Run `pcb-plan check` with `--report-json` and inspect
   plan confidence, low-confidence/missing constraints, warnings, and
   review-required items. Run `pcb-plan review --pln board.pln` to see the
   authored intent and its provenance. Address gaps in `board.pln` and re-check,
   or explain to the user why the plan remains low-confidence.
9. **Hand off, don't execute.** Bootstrap stops at a validated `board.pln`.
   Placement (`pcb-place`) and the optimization loop are owned by the `pcb-plan`
   and `pcb-place` skills. Do not run placement from this skill.
10. **Keep human review in scope.** State clearly that heuristic hints cannot
    verify impedance, return paths, plane splits, thermal, or EMI. The authored
    `board.pln` is an engineering starting point that still requires review.

## What board.pln Must Contain

The authoritative `board.pln` should express, as YAML:

- **mechanical constraints** — board outline/geometry, mounting holes,
  keepouts, fixed parts;
- **edge-required components** — connectors and other parts that must sit on a
  board edge;
- **connector orientation** — rotation / mating-face direction;
- **access sides** — `access_side` for connectors and access parts;
- **placement regions** — named rectangles that constrain where groups go;
- **power islands** — regulator / input-cap / inductor / output-cap / feedback
  groupings (load decouplers stay at their loads);
- **high-speed corridors** — routing corridors for high-speed/diff signals;
- **functional paths** — connector -> protection -> IC signal-path topology;
- **ownership** — exactly one owner per ref;
- **routing classes** — named classes with geometry/impedance/layer/via intent;
- **SI constraints** — impedance targets, skew/length tolerance, reference
  planes, trace width/spacing;
- **stackup** — layer/dielectric definition used for impedance/reference
  reasoning;
- **simulation triggers** — OpenEMS/ngspice hooks, set from confirmed reasoning
  about the simulation candidate hints.

Resolve each candidate hint from `board-hints.json` into one or more of these
sections. Follow the structure and key names in `ai-pln-prompt.md`; that prompt
is generated specifically to guide this authoring step.

## Validation

After authoring `board.pln`, validate before handing off:

```bash
pcb-plan check \
  --pln board.pln \
  --board layout.kicad_pcb \
  --netlist default.net \
  --report-json pcb-plan-check-report.json

pcb-plan review \
  --pln board.pln
```

- `check` catches low-confidence or missing constraints (e.g. missing geometry,
  incomplete differential-pair constraints, unresolved references, duplicate
  ownership). Read the report's plan confidence, reasons, and warnings, fix the
  underlying `board.pln`, and re-run rather than proceeding silently.
- `review` prints a human-readable view of the authored intent and its
  provenance, so assumptions stay visible.

Use `ai-placement-review.md` as a checklist while reviewing: confirm each
candidate was either encoded or deliberately dropped, every ref has exactly one
owner, edge/access metadata is set, and simulation triggers reflect real intent.

## Boundaries and Limitations

- **`inspect` does not floorplan.** It extracts facts and candidate hints only;
  it never places components, creates ownership, or infers a final floorplan.
- **`pcb-bootstrap` does not run placement.** Placement is `pcb-place`'s job;
  architecture/power-island/floorplan inference is never delegated to the
  executor.
- **KiCad groups never drive placement.** They are imported as
  `metadata_only: true` and are informational only.
- **Human review is required.** Heuristic candidate hints cannot verify
  impedance, return paths, plane splits, thermal performance, or EMI. Never
  claim FCC/interface compliance, SI/impedance verification, EMI verification,
  or production readiness. The authored `board.pln` is a reviewable starting
  point.

## Examples

Bootstrap a 75x75 mm board end-to-end up to a validated plan:

```bash
pcb-plan inspect --board layout.kicad_pcb --netlist default.net \
  --width 75 --height 75 --out planning-hints
# Claude reads planning-hints/ (board-hints.json, ai-pln-prompt.md,
# ai-placement-review.md) and authors board.pln, resolving each candidate.
pcb-plan check --pln board.pln --board layout.kicad_pcb --netlist default.net \
  --report-json pcb-plan-check-report.json
pcb-plan review --pln board.pln
```

With an explicit origin and a known 6-layer stackup:

```bash
pcb-plan inspect --board layout.kicad_pcb --netlist default.net \
  --width 100 --height 80 --origin-x 0 --origin-y 0 --stackup-layers 6 \
  --out planning-hints
```

After bootstrap, hand off to the `pcb-plan`/`pcb-place` skills to emit/apply
placement and run the optimization loop that edits `board.pln`.

See [`src/pcb_plan/README.md`](../../src/pcb_plan/README.md) for the full
`board.pln` syntax reference, CLI flags, and reports.
