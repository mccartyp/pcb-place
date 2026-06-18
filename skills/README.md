# Claude Skills

This directory contains [Claude Code skills](https://code.claude.com/docs)
that teach Claude how to use the `pcb-plan` and `pcb-place` tools in this
repository correctly, plus a bootstrap planning skill and an orchestrator skill
for end-to-end workflows.

The architecture separates concerns: `pcb-plan` **extracts facts**, the AI
(Claude) **generates design intent** in `board.pln`, and `pcb-place`
**executes intent**.

```text
KiCad PCB + netlist + stackup + board dimensions
  -> pcb-plan inspect      (extract facts -> planning-hints/)
  -> Claude / pcb-bootstrap (generate authoritative board.pln)
  -> pcb-plan check         (validate intent)
  -> pcb-plan emit          (board.pln -> placement.ppl)
  -> pcb-place              (deterministic execute -> reports)
  -> Claude optimization    (analyze reports, edit board.pln, repeat)
```

## Available skills

- [`pcb-bootstrap/SKILL.md`](pcb-bootstrap/SKILL.md) — the **primary planning
  skill**. Runs `pcb-plan inspect` to extract `planning-hints/`, then reasons
  about the candidate hints to author the authoritative `board.pln`
  (mechanical/edge/region/power-island/high-speed/ownership/routing/stackup/sim
  intent), and validates it with `pcb-plan check`/`review`.
- [`pcb-plan/SKILL.md`](pcb-plan/SKILL.md) — board-intelligence extraction with
  `pcb-plan inspect`, plus reviewing, validating, updating, and emitting
  `board.pln` / `placement.ppl`. `pcb-plan` no longer invents the floorplan;
  it extracts facts and reviews AI-authored intent.
- [`pcb-place/SKILL.md`](pcb-place/SKILL.md) — apply `placement.ppl`
  deterministically and interpret placement/reflow/congestion reports to
  recommend `board.pln` edits (the optimization surface), rather than
  hand-placing components.
- [`pcb-schgen/SKILL.md`](pcb-schgen/SKILL.md) — generate a readable KiCad
  `.kicad_sch` from the netlist, placed board, and `board.pln` with the
  deterministic `pcb-schgen` CLI, and interpret its generation/symbol-map
  reports. Use when a Zener/pcb flow produced a netlist and PCB but no schematic.
- [`pcb-automation-orchestrator/SKILL.md`](pcb-automation-orchestrator/SKILL.md) —
  coordinate the full inspect -> bootstrap board.pln -> validate -> place ->
  analyze -> update loop across `pcb-plan`, `pcb-place`, KiCadRoutingTools,
  KiCad DRC/ERC, and optional OpenEMS/ngspice.

## When to use each

- Use **`pcb-bootstrap`** to create an initial `board.pln`: extract hints with
  `pcb-plan inspect`, then turn them into authoritative design intent. This is
  the entry point for planning a board.
- Use **`pcb-plan`** to extract facts (`inspect`) and to review, validate
  (`check`/`review`/`explain`), update, and emit an existing `board.pln`.
- Use **`pcb-place`** when applying or debugging `placement.ppl` against a
  `.kicad_pcb` board, especially to turn report failures into `board.pln`
  improvements.
- Use **`pcb-automation-orchestrator`** for end-to-end iteration spanning
  inspect, bootstrap, validate, emit, place, route, verify, update, and
  (optionally) simulate.

## How they compose

```text
pcb-automation-orchestrator
  ├── calls pcb-plan inspect   (facts -> planning-hints/)
  ├── calls pcb-bootstrap      (AI generates authoritative board.pln)
  ├── calls pcb-plan           (check/review/emit board.pln, placement.ppl)
  ├── calls pcb-place          (deterministic execute/debug placement.ppl)
  └── calls external tools     (KiCadRoutingTools, KiCad DRC/ERC, OpenEMS, ngspice)
```

The orchestrator skill does not duplicate the other skills' behavior — it
sequences them and decides (in review mode), applies on request (in assisted
mode), or runs a bounded loop (in autonomous mode, default max 3 iterations)
for the resulting `board.pln` updates. AI is the primary planning engine and
the optimization loop modifies `board.pln` (preferred) over `placement.ppl`
overrides or manual `Anchor()`. `pcb-plan inspect` extracts facts only (no
placement, no ownership, no floorplan; KiCad groups are metadata only),
`pcb-place` stays deterministic, and no skill claims SI/EMI/compliance or
production readiness. For single-tool tasks, use the relevant skill directly.
