"""Tests for the constraint-driven iterative floorplanner.

These cover the no-abort default, the reflow ladder (repack support parts, move
movable parents), mechanical authority during reflow, parking as a last resort,
global quality scoring, and floorplan-iteration convergence.
"""

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pcb_place import (  # noqa: E402
    PlacementError,
    PlacementUnsatisfiable,
    apply_placements,
    load_ppl,
)


# ---------------------------------------------------------------------------
# Board builders
# ---------------------------------------------------------------------------


def _ic(ref: str, x: float, y: float, half: float = 2.0, pad_at=(-1.8, 0.0)) -> str:
    return f'''  (footprint "Test:{ref}" (layer "F.Cu")
    (at {x} {y} 0)
    (property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at {pad_at[0]} {pad_at[1]}) (size 0.6 0.6) (layers "F.Cu"))
    (fp_rect (start {-half} {-half}) (end {half} {half}) (stroke (width 0.1) (type solid)) (fill none) (layer "F.CrtYd"))
  )'''


def _passive(ref: str, x: float, y: float, size: float = 1.0) -> str:
    return f'''  (footprint "Test:{ref}" (layer "F.Cu")
    (at {x} {y} 0)
    (property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size {size} {size}) (layers "F.Cu"))
  )'''


def _board(*footprints: str) -> str:
    body = "\n".join(footprints)
    return f'(kicad_pcb (version 20240108) (generator "pcb-place-test")\n{body}\n)'


def _caps(n: int, start_x: float = 50.0) -> str:
    return "\n".join(_passive(f"C{i}", start_x + i, 5.0) for i in range(1, n + 1))


def _load(tmp_path, body: str):
    p = tmp_path / "fp.ppl"
    p.write_text(body)
    return load_ppl(p)


# ---------------------------------------------------------------------------
# No-abort placement
# ---------------------------------------------------------------------------


def _impossible_decoupling_board() -> str:
    # The whole board is a keepout: no legal area exists for the array.
    return _board(_ic("U1", 4, 4, half=1.0, pad_at=(-0.8, 0.0)), _caps(2))


def _impossible_decoupling_ppl() -> str:
    return (
        "Board(width=8, height=8)\n"
        "Keepout(\"ALL\", x=0, y=0, w=8, h=8)\n"
        "Anchor(\"U1\", x=4, y=4, rot=0)\n"
        "DecouplingArray(refs=[\"C1\", \"C2\"], parent=\"U1\", pad=\"1\", "
        "side=\"auto\", distance=1.0, spacing=1.5)\n"
    )


def test_no_abort_default_parks_instead_of_raising(tmp_path):
    model = _load(tmp_path, _impossible_decoupling_ppl())
    # Must not raise even though the array genuinely cannot place.
    _out, messages, report = apply_placements(_impossible_decoupling_board(), model)
    floorplan = report["floorplan"]
    assert floorplan["best_effort"] is True
    assert floorplan["parking_fallback_used"] is True
    assert floorplan["parked_count"] == 2
    assert {p["ref"] for p in floorplan["parked"]} == {"C1", "C2"}
    # Degraded-placement warnings with a review marker are emitted.
    degraded = [m for m in messages if "DEGRADED PLACEMENT" in m.text]
    assert len(degraded) == 2
    assert all("[review-required]" in m.text for m in degraded)


def test_strict_mode_still_aborts(tmp_path):
    model = _load(tmp_path, _impossible_decoupling_ppl())
    with pytest.raises(PlacementError):
        apply_placements(_impossible_decoupling_board(), model, strict=True)


def test_fail_fast_aborts_without_reflow(tmp_path):
    model = _load(tmp_path, _impossible_decoupling_ppl())
    with pytest.raises(PlacementUnsatisfiable):
        apply_placements(_impossible_decoupling_board(), model, fail_fast=True)


def test_no_best_effort_aborts(tmp_path):
    model = _load(tmp_path, _impossible_decoupling_ppl())
    with pytest.raises(PlacementError):
        apply_placements(_impossible_decoupling_board(), model, best_effort=False)


# ---------------------------------------------------------------------------
# Reflow ladder
# ---------------------------------------------------------------------------


def _movable_parent_board() -> str:
    # U1 fully occupies the small DECAP region; only moving U1 frees room.
    return _board(_ic("U1", 19, 20, half=3.0, pad_at=(-2.8, 0.0)), _caps(3))


def _movable_parent_ppl() -> str:
    return (
        "Board(width=40, height=40)\n"
        "Region(\"DECAP\", x=16, y=18, w=4, h=4)\n"
        "Anchor(\"U1\", x=19, y=20, rot=0)\n"
        "DecouplingArray(refs=[\"C1\", \"C2\", \"C3\"], parent=\"U1\", pad=\"1\", "
        "side=\"left\", distance=1.0, spacing=1.2, region=\"DECAP\")\n"
    )


def test_reflow_moves_movable_parent_instead_of_parking(tmp_path):
    """U7-like / movable-regulator case: move the parent rather than park caps."""
    model = _load(tmp_path, _movable_parent_ppl())
    _out, _messages, report = apply_placements(_movable_parent_board(), model)
    floorplan = report["floorplan"]
    assert floorplan["parked_count"] == 0
    assert len(floorplan["moved_parents"]) == 1
    moved = floorplan["moved_parents"][0]
    assert moved["ref"] == "U1"
    assert moved["delta"] != [0.0, 0.0]
    assert any(a["action"] == "move_parent" and a["success"] for a in floorplan["reflow_attempts"])


def test_locked_parent_is_not_moved_and_caps_park(tmp_path):
    """Mechanical authority: a locked parent must never be reflowed."""
    ppl = (
        "Board(width=40, height=40)\n"
        "Region(\"DECAP\", x=16, y=18, w=4, h=4)\n"
        "Anchor(\"U1\", x=19, y=20, rot=0)\n"
        "Lock(\"U1\", reason=\"mechanical\")\n"
        "DecouplingArray(refs=[\"C1\", \"C2\", \"C3\"], parent=\"U1\", pad=\"1\", "
        "side=\"left\", distance=1.0, spacing=1.2, region=\"DECAP\")\n"
    )
    model = _load(tmp_path, ppl)
    _out, _messages, report = apply_placements(_movable_parent_board(), model)
    floorplan = report["floorplan"]
    assert floorplan["moved_parents"] == []
    assert floorplan["parked_count"] == 3


def _support_repack_board() -> str:
    # TP1 (a movable test point) blocks the only legal cap region; U1 is clear.
    return _board(
        _ic("U1", 24, 20, half=2.0, pad_at=(-1.8, 0.0)),
        _passive("TP1", 17, 20, size=1.2),
        _caps(3),
    )


def _support_repack_ppl() -> str:
    return (
        "Board(width=40, height=40)\n"
        "Region(\"DECAP\", x=16, y=16, w=2.4, h=8)\n"
        "Anchor(\"U1\", x=24, y=20, rot=0)\n"
        "Anchor(\"TP1\", x=17, y=20, rot=0)\n"
        "DecouplingArray(refs=[\"C1\", \"C2\", \"C3\"], parent=\"U1\", pad=\"1\", "
        "side=\"left\", distance=1.0, spacing=1.5, region=\"DECAP\")\n"
    )


def test_reflow_repacks_support_region(tmp_path):
    model = _load(tmp_path, _support_repack_ppl())
    _out, _messages, report = apply_placements(_support_repack_board(), model)
    floorplan = report["floorplan"]
    assert floorplan["parked_count"] == 0
    moved_refs = {m["ref"] for m in floorplan["moved_components"]}
    assert "TP1" in moved_refs
    assert floorplan["moved_parents"] == []  # support repack preferred over moving parent
    assert any(a["action"] == "repack_support" and a["success"] for a in floorplan["reflow_attempts"])


def test_reflow_ladder_prefers_support_repack_before_moving_parent(tmp_path):
    """Level 2-4 (support) is tried before Level 5 (parent move)."""
    model = _load(tmp_path, _support_repack_ppl())
    _out, _messages, report = apply_placements(_support_repack_board(), model)
    actions = [a["action"] for a in report["floorplan"]["reflow_attempts"] if a.get("success")]
    assert actions and actions[0] == "repack_support"


# ---------------------------------------------------------------------------
# Mechanical authority
# ---------------------------------------------------------------------------


def test_mounting_holes_and_edge_connectors_are_not_movable(tmp_path):
    ppl = (
        "Board(width=40, height=40)\n"
        "Corner(\"H1\", corner=\"top_left\", inset=3)\n"
        "Edge(\"J1\", edge=\"left\", y=20, edge_required=True, access_side=\"left\", rot=\"auto\")\n"
        "Anchor(\"U1\", x=20, y=20, rot=0)\n"
    )
    pcb = _board(
        _passive("H1", 0, 0, size=3.0),
        _passive("J1", 0, 0, size=3.0),
        _ic("U1", 0, 0),
    )
    model = _load(tmp_path, ppl)
    _out, _messages, report = apply_placements(pcb, model)
    # Build an engine view through the report: mechanical + edge-required listed.
    assert "H1" in report["part_classes"]
    assert report["part_classes"]["H1"] == "mechanical"
    assert "J1" in report["edge_required_refs"]


def test_engine_movability_rules():
    """_is_movable: mechanical anchors, edge-required, and locked parts are fixed."""
    import pcb_place

    pcb = _board(
        _passive("H1", 0, 0, size=3.0),
        _ic("U1", 10, 10),
        _ic("U2", 20, 20),
    )
    ppl = (
        "Board(width=40, height=40)\n"
        "Corner(\"H1\", corner=\"top_left\", inset=3)\n"
        "Anchor(\"U1\", x=10, y=10, rot=0)\n"
        "Anchor(\"U2\", x=20, y=20, rot=0)\n"
        "Lock(\"U2\", reason=\"placed by hand\")\n"
    )
    footprints = pcb_place.parse_footprints(pcb)
    model = pcb_place.load_ppl_text(ppl) if hasattr(pcb_place, "load_ppl_text") else None
    if model is None:
        import tempfile
        d = Path(tempfile.mkdtemp())
        (d / "f.ppl").write_text(ppl)
        model = pcb_place.load_ppl(d / "f.ppl")
    geom = pcb_place.resolve_board_geometry(pcb, model, footprints, allow_footprint_fallback=True)
    engine = pcb_place.PlacementEngine(footprints, model, board_geometry=geom)
    engine.apply()
    assert engine._is_movable("U1") is True
    assert engine._is_movable("H1") is False  # mechanical mounting hole
    assert engine._is_movable("U2") is False  # locked


# ---------------------------------------------------------------------------
# Global scoring & iteration
# ---------------------------------------------------------------------------


def test_placement_quality_score_present_and_clean_board_scores_zero(tmp_path):
    ppl = (
        "Board(width=40, height=40)\n"
        "Anchor(\"U1\", x=20, y=20, rot=0)\n"
        "Satellite(\"C1\", parent=\"U1\", side=\"top\", distance=2)\n"
    )
    pcb = _board(_ic("U1", 20, 20), _passive("C1", 0, 0))
    model = _load(tmp_path, ppl)
    _out, _messages, report = apply_placements(pcb, model)
    floorplan = report["floorplan"]
    assert "placement_quality_score" in report
    assert floorplan["placement_quality_score"] == 0.0
    assert floorplan["parked_count"] == 0


def test_parking_dominates_quality_score(tmp_path):
    model = _load(tmp_path, _impossible_decoupling_ppl())
    _out, _messages, report = apply_placements(_impossible_decoupling_board(), model)
    # Parked parts dominate the score (1000 each).
    assert report["floorplan"]["placement_quality_score"] >= 2000.0


def test_floorplan_iteration_convergence(tmp_path):
    model = _load(tmp_path, _impossible_decoupling_ppl())
    _out, _messages, report = apply_placements(
        _impossible_decoupling_board(), model, max_floorplan_iterations=5
    )
    floorplan = report["floorplan"]
    scores = [s["total"] for s in floorplan["iteration_scores"]]
    assert scores, "iteration scores must be recorded"
    assert floorplan["iterations_run"] <= floorplan["max_iterations"] == 5
    # The optimizer keeps the best floorplan: scores never get worse.
    assert scores == sorted(scores, reverse=True) or len(scores) == 1
    assert all(later <= earlier for earlier, later in zip(scores, scores[1:]))


def test_clean_board_converges_in_single_iteration(tmp_path):
    ppl = (
        "Board(width=40, height=40)\n"
        "Anchor(\"U1\", x=20, y=20, rot=0)\n"
    )
    pcb = _board(_ic("U1", 20, 20))
    model = _load(tmp_path, ppl)
    _out, _messages, report = apply_placements(pcb, model)
    assert report["floorplan"]["iterations_run"] == 1


def test_report_contains_floorplan_audit_fields(tmp_path):
    model = _load(tmp_path, _movable_parent_ppl())
    _out, _messages, report = apply_placements(_movable_parent_board(), model)
    floorplan = report["floorplan"]
    for key in ("reflow_attempts", "moved_components", "moved_parents", "parked",
                "score_deltas", "congestion_map", "power_island_score",
                "high_speed_path_score", "placement_quality_score", "iteration_scores",
                "utilization"):
        assert key in floorplan
    # Congestion map is an 8x8 occupancy grid for a board with geometry.
    assert len(floorplan["congestion_map"]) == 8
    assert all(len(row) == 8 for row in floorplan["congestion_map"])


def test_ai_edit_hints_surface_parked_and_moved_parents(tmp_path):
    import pcb_place

    model = _load(tmp_path, _movable_parent_ppl())
    _out, _messages, report = apply_placements(_movable_parent_board(), model)
    hints = pcb_place.ai_edit_hints_md(report)
    assert "Floorplan health" in hints
    assert "movable parent U1" in hints
    assert "[review-required]" in hints
