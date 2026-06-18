---
name: pcb-schgen
description: Generate a readable KiCad .kicad_sch schematic from a Zener/pcb netlist, a placed KiCad board, and board.pln, using the deterministic pcb-schgen CLI. Use when a Zener/pcb flow produced a netlist and PCB but no schematic, when you need a reviewable schematic with local circuits wired and rails labeled, or when interpreting the pcb-schgen generation/symbol-map reports.
---

# pcb-schgen

## Purpose

`pcb-schgen` is the **deterministic schematic generator**. It reconstructs a
readable KiCad `.kicad_sch` from artifacts the Zener/pcb flow already produces:

```
netlist (default.net) + placed board (.kicad_pcb) + board.pln  ->  pcb-schgen  ->  .kicad_sch
```

It closes the gap where Zener/pcb can emit a netlist and a placed PCB but **no
schematic capture file**. It is **connectivity-correct first**: every netlist net
is reproduced exactly, validated by re-extracting the generated schematic's
connectivity the way KiCad resolves it (shared wire endpoints, junctions, labels,
power symbols) and comparing it to the input netlist.

`pcb-schgen` does **not**:

- modify `pcb-plan`, `pcb-place`, the input PCB, or any input;
- invent connectivity — the netlist is authoritative;
- guarantee pin-perfect symbols (it falls back to generic symbols, flagged for
  review, rather than failing).

It **is** deterministic: same inputs produce a byte-identical `.kicad_sch`
(stable ordering, `uuid5`-derived UUIDs) that diffs cleanly.

## When to use

- A Zener/pcb design has `default.net` + `layout.*.kicad_pcb` + `board.pln` but
  needs a schematic for review, documentation, or import into KiCad.
- You want a schematic where local circuits (feedback dividers, LED chains,
  reset circuits, connector→ESD→IC chains) are drawn with **real wires**, while
  `GND`/power rails and high-fanout/global nets use **power symbols and labels**.
- You need to audit symbol mapping (which refs fell back to generic symbols) or
  connectivity coverage via the JSON/YAML reports.

## Command

```
pcb-schgen \
  --netlist layout/Board/default.net \
  --board   layout/Board/layout.ai-placed.kicad_pcb \
  --pln     ai-placement-run/board.pln \
  --hints   ai-placement-run/planning-hints \
  -o        layout/Board/Board.kicad_sch \
  --report-json ai-placement-run/pcb-schgen-report.json \
  --symbol-map  ai-placement-run/symbol-map.yaml
```

Required: `--netlist`, `-o`. Optional: `--board`, `--pln`, `--hints`,
`--place-report`, `--project`, `--report-json`, `--symbol-map`,
`--paper {A0..A4}`, `--label-fanout N`, `--random-uuids`, `--strict`.

`--pln` is what makes the schematic readable rather than a netlist dump: roles,
`functional_paths`, `power_islands`, and `differential_pairs` drive functional
grouping (connectors at edges, ESD between connector and IC, retimer/MCU/Wi-Fi/
power blocks) and the wire-vs-label policy.

## Reading the reports

`--report-json` fields:

- `refs_total` / `refs_emitted`, `missing_refs` — every component should be
  emitted; investigate any `missing_refs`.
- `nets_total` / `nets_matched`, `missing_nets`, `connectivity_ok` — the gate.
  `connectivity_ok: true` means the schematic matches the netlist exactly. Use
  `--strict` to fail the run otherwise.
- `wired_nets` vs `global_label_nets` — which nets were drawn as wires vs
  labels/power symbols. Nets in dense areas may be *demoted* from wire to label;
  this is reported and is still connectivity-correct.
- `symbol_fallbacks` / `requires_review` — refs given a generic rectangular
  symbol (real pin names/numbers preserved, marked with a `schgen_review`
  property). Verify these pinouts before manufacturing.

`--symbol-map` (YAML) lists each ref's `lib_id`, value, part, footprint,
fallback flag, and reason — the place to confirm or correct symbol choices.

## How it fits the workflow

`pcb-schgen` runs after placement, alongside review:

```
pcb build / pcb layout         (Zener/pcb -> netlist + PCB)
  -> pcb-plan inspect          (planning-hints/)
  -> Claude / pcb-bootstrap     (board.pln)
  -> pcb-plan emit -> pcb-place (placed .kicad_pcb)
  -> pcb-schgen                 (readable .kicad_sch + reports)   <-- here
  -> human review in KiCad (Eeschema), iterate board.pln/symbols
```

Prefer fixing the upstream Zener design or `board.pln` intent (better roles,
functional paths, power islands) over hand-editing generated symbols — the same
inputs will regenerate the schematic deterministically.

## Limitations / review

- Emits hierarchical sheets by default (one sub-sheet per functional block,
  cross-sheet nets carried by global labels/power); `--single-sheet` forces one
  flat page.
- Generic symbols require pinout review.
- Connectivity is guaranteed; layout aesthetics are best-effort — some local
  nets fall back to labels rather than risk an incorrect wire. The generated
  schematic must be reviewed in KiCad before fabrication.
