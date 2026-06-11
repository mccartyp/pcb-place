# Claude Skills

This directory contains [Claude Code skills](https://code.claude.com/docs)
that teach Claude how to use the `pcb-plan` and `pcb-place` tools in this
repository correctly, plus an orchestrator skill for end-to-end workflows.

## Available skills

- [`pcb-plan/SKILL.md`](pcb-plan/SKILL.md) — create, review, update, and emit
  `board.pln` / `placement.ppl` with the `pcb-plan` CLI: routing/SI
  constraints, stackup, differential pairs, simulation hooks, and provenance.
- [`pcb-place/SKILL.md`](pcb-place/SKILL.md) — apply and debug
  `placement.ppl` with the `pcb-place` CLI: dry-run, validation, collisions,
  spacing, keepouts, regions, and KiCad structure preservation.
- [`pcb-automation-orchestrator/SKILL.md`](pcb-automation-orchestrator/SKILL.md) —
  coordinate the full plan -> place -> route -> verify -> simulate -> update
  loop across `pcb-plan`, `pcb-place`, KiCadRoutingTools, KiCad DRC/ERC, and
  optional OpenEMS/ngspice.

## When to use each

- Use **`pcb-plan`** when editing `board.pln` or generating `placement.ppl`.
- Use **`pcb-place`** when applying or debugging `placement.ppl` against a
  `.kicad_pcb` board.
- Use **`pcb-automation-orchestrator`** for end-to-end iteration spanning
  build, plan, place, route, verify, and (optionally) simulate.

## How they compose

```text
pcb-automation-orchestrator
  ├── calls pcb-plan   (board.pln lifecycle, placement.ppl, routing/SI/sim hooks)
  ├── calls pcb-place  (apply/debug placement.ppl)
  └── calls external tools (KiCadRoutingTools, KiCad DRC/ERC, OpenEMS, ngspice)
```

The orchestrator skill does not duplicate `pcb-plan`/`pcb-place` behavior — it
sequences them and decides (in review mode) or applies (in autonomous mode)
the resulting `board.pln` updates. For single-tool tasks, use the `pcb-plan`
or `pcb-place` skill directly.
