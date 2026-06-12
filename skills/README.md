# Claude Skills

This directory contains [Claude Code skills](https://code.claude.com/docs)
that teach Claude how to use the `pcb-plan` and `pcb-place` tools in this
repository correctly, plus an orchestrator skill for end-to-end workflows.

## Available skills

- [`pcb-plan/SKILL.md`](pcb-plan/SKILL.md) — create, AI-review, optimize,
  update, and emit `board.pln` / `placement.ppl` with the `pcb-plan` CLI:
  regions, keepouts, edge-required metadata, support arrays, routing/SI
  constraints, stackup, differential pairs, simulation hooks, and provenance.
- [`pcb-place/SKILL.md`](pcb-place/SKILL.md) — apply and debug
  `placement.ppl` with the `pcb-place` CLI: dry-run report review,
  validation, collisions, spacing, keepouts, regions, array diagnostics,
  edge-required movement attempts, and KiCad structure preservation.
- [`pcb-automation-orchestrator/SKILL.md`](pcb-automation-orchestrator/SKILL.md) —
  coordinate the full AI-assisted init -> check -> edit -> emit -> place ->
  route -> verify -> simulate -> update loop across `pcb-plan`, `pcb-place`,
  KiCadRoutingTools, KiCad DRC/ERC, and optional OpenEMS/ngspice.

## When to use each

- Use **`pcb-plan`** when generating, reviewing, optimizing, or editing
  `board.pln`, especially after `pcb-plan init` and before `pcb-plan emit`.
- Use **`pcb-place`** when applying or debugging `placement.ppl` against a
  `.kicad_pcb` board, especially to turn dry-run report failures into
  `board.pln` improvements.
- Use **`pcb-automation-orchestrator`** for end-to-end iteration spanning
  build, init, AI optimization, check, emit, place, route, verify, update, and
  (optionally) simulate.

## How they compose

```text
pcb-automation-orchestrator
  ├── calls pcb-plan   (board.pln lifecycle, placement.ppl, routing/SI/sim hooks)
  ├── calls pcb-place  (apply/debug placement.ppl)
  └── calls external tools (KiCadRoutingTools, KiCad DRC/ERC, OpenEMS, ngspice)
```

The orchestrator skill does not duplicate `pcb-plan`/`pcb-place` behavior — it
sequences them and decides (in review mode), applies on request (in assisted
mode), or runs a bounded loop (in autonomous mode, default max 3 iterations)
for the resulting `board.pln` updates. All skills prefer `board.pln` intent
edits over manual `placement.ppl` hacks, preserve provenance, surface
`requires_review` uncertainty, and never claim SI/EMI/compliance or production
readiness. For single-tool tasks, use the `pcb-plan` or `pcb-place` skill
directly.
