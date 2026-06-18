# pcb-schgen

`pcb-schgen` generates a **readable KiCad `.kicad_sch` schematic** from a
Zener/pcb netlist, a placed KiCad board, and a `board.pln` design-intent file.

It closes a real gap: Zener/pcb can emit a netlist and a placed `.kicad_pcb`, but
it does not emit schematic capture files. `pcb-schgen` reconstructs a schematic
that is **connectivity-correct first** and **readable second** — local circuits
are drawn with real wires, global rails use power symbols and labels, and parts
are grouped by the functional intent declared in `board.pln`.

```
netlist (default.net) ─┐
placed board (.kicad_pcb) ─┼─►  pcb-schgen  ─►  generated.kicad_sch
board.pln (intent) ─┘                         + schematic report (JSON)
planning-hints/ (optional)                    + symbol map (YAML)
pcb-place report (optional)
```

`pcb-schgen` is **deterministic** and **non-destructive**: it never modifies
`pcb-plan`, `pcb-place`, the input PCB, or any input. Re-running with the same
inputs produces a byte-identical schematic (stable ordering, `uuid5`-derived
UUIDs) so it diffs cleanly in version control.

## Usage

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

### Inputs

| Flag | Required | Purpose |
|------|----------|---------|
| `--netlist` | yes | Zener/pcb netlist (`default.net`) — the authoritative connectivity. |
| `--board` | no | Placed `.kicad_pcb` — recovered placement (advisory; never modified). |
| `--pln` | no | `board.pln` — functional grouping, roles, paths, power islands, diff pairs. |
| `--hints` | no | `planning-hints/` directory (advisory). |
| `--place-report` | no | `pcb-place` report JSON (advisory). |
| `--project` | no | `*.kicad_pro` used only for naming the project in instances. |
| `-o/--output` | yes | Output `.kicad_sch` path. |
| `--report-json` | no | Write the schematic generation report. |
| `--symbol-map` | no | Write the symbol-mapping report (YAML). |

### Options

- `--paper {A0..A4}` — sheet size (default `A3`).
- `--label-fanout N` — nets with `N`+ nodes use labels instead of wires (default `6`).
- `--random-uuids` — use random `uuid4` instead of deterministic `uuid5`.
- `--strict` — exit non-zero if connectivity validation fails.

## What it does

1. **Parse the netlist** — components (ref, value, footprint, libsource), nets
   (name + nodes), and `libparts` (pin names/numbers).
2. **Parse the placed board** — footprint positions (used for ordering hints).
3. **Parse `board.pln`** — roles, `functional_paths`, `power_islands`,
   `differential_pairs`, high-speed nets.
4. **Map symbols** — `R*→Device:R`-style builtins for passives, test points,
   switches, diodes/LEDs; **generic rectangular symbols** that preserve pin
   names/numbers for unknown ICs and connectors (flagged `requires_review`).
   *Unknown symbols never cause a failure* — connectivity matters more than
   pin-perfect symbols.
5. **Group into functional blocks** — connectors, ESD, retimer, MCU, Wi-Fi,
   power, etc., driven by `board.pln` intent (with netlist sheetpath and ref
   prefixes as fallbacks).
6. **Lay out deterministically** — connector→ESD→IC ordered along a functional
   path, components packed without overlap, related parts clustered so local
   wiring stays short.
7. **Wire vs label** — local circuits (feedback dividers, LED chains, reset
   circuits, connector→ESD→IC chains) are drawn with **real orthogonal wires**;
   ground/power rails use **power symbols**; high-fanout/global/cross-block nets
   use **labels**.
8. **Validate** — the generated schematic's connectivity is re-extracted (the
   way KiCad resolves it: shared wire endpoints, junctions, labels, power
   symbols) and compared against the input netlist.

## Wire vs label policy

- **Power symbols**: `GND`, `AGND`, `DGND`, `+3V3`, `+5V`, `+1V2_CORE`,
  `VBUS`, `VCC`, … (anything matching a power/ground rail). Custom power
  symbols are generated in the schematic's own `lib_symbols` so there are never
  missing-library errors.
- **Global labels**: high-fanout nets, named buses (`I2C_*`, `RESET_N`, …) that
  cross blocks, and any net at or above `--label-fanout`. Differential-pair
  members (`TMDS0_P`/`TMDS0_N`) are labelled and kept adjacent.
- **Wires**: everything else that is local and low-fanout. The router draws
  short orthogonal wires (two-pin doglegs and daisy-chains). When no
  collision-free route exists in a dense area, the net is *demoted to a label* —
  the schematic stays connectivity-correct, and the demotion is reported.

## Reports

`--report-json` emits:

```json
{
  "refs_total": 102, "refs_emitted": 102,
  "nets_total": 115, "nets_matched": 115,
  "missing_refs": [], "missing_nets": [],
  "global_label_nets": ["+3V3_SYS", "GND", ...],
  "wired_nets": ["PWR._BUCK_FB", ...],
  "symbol_fallbacks": ["U6", "U10", ...],
  "requires_review": ["U6", "U10", ...],
  "connectivity_ok": true,
  "connectivity_mismatches": []
}
```

`--symbol-map` emits a YAML mapping of each ref to its chosen `lib_id`, value,
part, footprint, fallback flag, and review reason.

## Design notes / limitations

- **MVP is single-sheet.** The architecture (functional blocks, symbol map,
  connectivity model) is designed for hierarchical sheets
  (`power.kicad_sch`, `hdmi_in.kicad_sch`, …) as a follow-up.
- **Generic symbols require review.** Where an exact KiCad symbol is unknown a
  generic rectangle is generated with the real pin names/numbers; verify the
  pinout/gate assignment before manufacturing.
- **Connectivity is guaranteed; aesthetics are best-effort.** In dense regions
  some local nets fall back to labels rather than risk an incorrect wire.

## KiCad compatibility

Output targets the KiCad 7/8/9/10 `.kicad_sch` s-expression format
(`version 20231120`). All generated objects carry UUIDs and stable ordering.
