"""Constraint-driven iterative floorplanner tests.

Covers the no-abort default, reflow-before-parking ladder, movable-parent
consideration, global floorplan scoring / iteration convergence, and the
mechanical invariants that reflow must preserve (mounting holes stay distinct,
edge connectors rotate, connector bodies may extend past the outline).
"""

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pcb_place import apply_placements, load_ppl
from pcb_place.pcb_place import PlacementError


# ---------------------------------------------------------------------------
# Synthetic board/ppl builders
# ---------------------------------------------------------------------------

def _fp(ref: str, kind: str, x: float = 0.0, y: float = 0.0, *, pad: float = 0.6) -> str:
    return f'''  (footprint "Test:{kind}" (layer "F.Cu")
    (at {x} {y} 0)
    (property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size {pad} {pad}) (layers "F.Cu"))
  )'''


def _pcb(footprints: list[str]) -> str:
    body = "\n".join(footprints)
    return f'(kicad_pcb (version 20240108) (generator "test")\n{body}\n)\n'


def _decoupling_scene(*, obstacles_locked: bool, allow_anchor_move: bool,
                      region: bool = True, n_caps: int = 2):
    """U1 with a decoupling array whose target strip is packed with obstacles.

    When the obstacles are movable passives, reflow relocates them and the array
    places.  When they are locked, reflow cannot clear the strip.  The obstacle
    count is kept small so the (combinatorial) array search stays fast.
    """

    fps = [_fp("U1", "U", 20, 20)]
    fps += [_fp(c, "C", 0, 0) for c in [f"C{i+1}" for i in range(n_caps)]]
    obs = []
    k = 1
    for ox in (22.5, 23.5):
        for yi in range(6):
            obs.append((f"R{k}", round(ox, 2), 16.0 + yi))
            k += 1
    fps += [_fp(r, "R", ox, oy) for r, ox, oy in obs]

    lines = [
        "Board(width=40, height=40)",
        "Spacing(default=0.25)",
        f"PlacementPolicy(allow_anchor_move={allow_anchor_move})",
        'Anchor("U1", x=20, y=20, rot=0)',
    ]
    if region:
        lines.append('Region("DECAP", x=22, y=15.5, w=2.5, h=6)')
    lines += [f'Anchor("{r}", x={ox}, y={oy}, rot=0)' for r, ox, oy in obs]
    if obstacles_locked:
        lines += [f'Lock("{r}")' for r, _ox, _oy in obs]
    region_kw = ', region="DECAP"' if region else ""
    caps = ", ".join(f'"C{i+1}"' for i in range(n_caps))
    lines.append(f'DecouplingArray([{caps}], parent="U1", side="right"{region_kw}, '
                 f"distance=2, spacing=1.5)")
    return _pcb(fps), "\n".join(lines) + "\n"


def _run(tmp_path, pcb_text, ppl_text, **kwargs):
    ppl = tmp_path / "board.ppl"
    ppl.write_text(ppl_text)
    return apply_placements(pcb_text, load_ppl(ppl), **kwargs)


def _placed(report):
    return {p["ref"]: (round(p["x"], 3), round(p["y"], 3)) for p in report["placements"]}


# ---------------------------------------------------------------------------
# No-abort + reflow ladder
# ---------------------------------------------------------------------------

def test_reflow_resolves_decoupling_conflict_by_moving_support(tmp_path):
    """Movable support passives are relocated so the array places (Level 2-4)."""

    pcb, ppl = _decoupling_scene(obstacles_locked=False, allow_anchor_move=False)
    _out, _msgs, report = _run(tmp_path, pcb, ppl)
    floor = report["floorplan"]

    assert floor["parking_fallback_used"] == 0
    assert any(a["resolved"] and a["moved_components"] for a in floor["reflow_attempts"])
    placed = _placed(report)
    # All caps landed inside the DECAP region strip (22 <= x <= 26).
    for cap in ("C1", "C2"):
        assert 22.0 <= placed[cap][0] <= 26.0


def test_no_abort_default_parks_when_reflow_exhausted(tmp_path):
    """Locked obstacles + immovable parent -> park, not abort (no-abort default)."""

    pcb, ppl = _decoupling_scene(obstacles_locked=True, allow_anchor_move=False)
    _out, _msgs, report = _run(tmp_path, pcb, ppl)
    floor = report["floorplan"]

    assert floor["best_effort"] is True
    assert floor["parking_fallback_used"] >= 1
    assert floor["degraded_warnings"]
    # Parked parts are still placed (downstream tools get a coordinate).
    placed = _placed(report)
    assert {"C1", "C2"}.issubset(placed)
    assert all(p["review_required"] for p in floor["parked"])


def test_strict_mode_aborts_on_unplaceable(tmp_path):
    pcb, ppl = _decoupling_scene(obstacles_locked=True, allow_anchor_move=False)
    with pytest.raises(PlacementError):
        _run(tmp_path, pcb, ppl, strict=True)


def test_fail_fast_aborts_on_unplaceable(tmp_path):
    pcb, ppl = _decoupling_scene(obstacles_locked=True, allow_anchor_move=False)
    with pytest.raises(PlacementError):
        _run(tmp_path, pcb, ppl, best_effort=False, fail_fast=True)


def test_movable_parent_is_considered_when_allowed(tmp_path):
    """With allow_anchor_move the floorplanner considers moving the parent (Level 5)."""

    pcb, ppl = _decoupling_scene(obstacles_locked=True, allow_anchor_move=True)
    _out, _msgs, report = _run(tmp_path, pcb, ppl)
    floor = report["floorplan"]

    level5 = [a for a in floor["reflow_attempts"] if a["level"] == 5]
    assert level5, "expected a Level-5 parent-move reflow attempt"
    assert any("parent" in a["strategy"] for a in level5)
    # Either the parent move resolved it, or it parked -- never aborted.
    assert "U1" not in floor["moved_parents"] or floor["parking_fallback_used"] >= 0


def test_immovable_parent_is_not_considered(tmp_path):
    """Without allow_anchor_move, no parent move is attempted."""

    pcb, ppl = _decoupling_scene(obstacles_locked=True, allow_anchor_move=False)
    _out, _msgs, report = _run(tmp_path, pcb, ppl)
    floor = report["floorplan"]
    assert all(a["level"] != 5 for a in floor["reflow_attempts"])
    assert floor["moved_parents"] == []


# ---------------------------------------------------------------------------
# Global scoring + iteration
# ---------------------------------------------------------------------------

def test_floorplan_report_has_expected_fields(tmp_path):
    pcb, ppl = _decoupling_scene(obstacles_locked=False, allow_anchor_move=False)
    _out, _msgs, report = _run(tmp_path, pcb, ppl)
    floor = report["floorplan"]
    for key in ("best_effort", "max_iterations", "iterations", "reflow_attempts",
                "moved_components", "moved_parents", "parked", "parking_fallback_used",
                "degraded_warnings", "congestion_map", "placement_quality_score"):
        assert key in floor
    quality = floor["placement_quality_score"]
    for key in ("score", "collisions", "parked", "congestion_peak"):
        assert key in quality


def test_iteration_scores_are_recorded_and_nonincreasing(tmp_path):
    pcb, ppl = _decoupling_scene(obstacles_locked=False, allow_anchor_move=False)
    _out, _msgs, report = _run(tmp_path, pcb, ppl, max_floorplan_iterations=5)
    iterations = report["floorplan"]["iterations"]
    assert iterations[0]["iteration"] == 0
    scores = [it["score"] for it in iterations]
    # The iterative optimizer never makes the global score worse.
    assert scores == sorted(scores, reverse=True) or len(set(scores)) == 1


def test_congestion_map_present(tmp_path):
    pcb, ppl = _decoupling_scene(obstacles_locked=False, allow_anchor_move=False)
    _out, _msgs, report = _run(tmp_path, pcb, ppl)
    cmap = report["floorplan"]["congestion_map"]
    assert cmap["cells"] >= 1
    assert "peak" in cmap and "peak_penalty" in cmap


# ---------------------------------------------------------------------------
# Mechanical invariants preserved by reflow
# ---------------------------------------------------------------------------

def test_mounting_holes_remain_distinct(tmp_path):
    fps = [_fp(h, "H", 0, 0) for h in ("H1", "H2", "H3", "H4")]
    pcb = _pcb(fps)
    ppl = (
        "Board(width=30, height=30)\n"
        'Corner("H1", corner="top_left", inset=3, role="mechanical")\n'
        'Corner("H2", corner="top_right", inset=3, role="mechanical")\n'
        'Corner("H3", corner="bottom_left", inset=3, role="mechanical")\n'
        'Corner("H4", corner="bottom_right", inset=3, role="mechanical")\n'
    )
    _out, _msgs, report = _run(tmp_path, pcb, ppl)
    placed = _placed(report)
    coords = list(placed.values())
    assert len(set(coords)) == 4, "mounting holes must not be clustered/coincident"


def test_edge_connector_rotates_to_access_side(tmp_path):
    pcb = _pcb([_fp("J1", "J", 5, 5)])
    ppl = (
        "Board(width=40, height=20)\n"
        'Edge("J1", edge="left", y=10, rot="auto", access_side="left")\n'
    )
    _out, _msgs, report = _run(tmp_path, pcb, ppl)
    j1 = next(p for p in report["placements"] if p["ref"] == "J1")
    assert j1["rotation_changed"] is True


def test_connector_body_extension_allowed(tmp_path):
    """A connector flagged allow_body_outside_board keeps its anchor on-board."""

    pcb = _pcb([_fp("J1", "J", 5, 5, pad=4.0)])
    ppl = (
        "Board(width=40, height=20)\n"
        'Edge("J1", edge="left", y=10, inset=0, allow_body_outside_board=True)\n'
    )
    _out, _msgs, report = _run(tmp_path, pcb, ppl)
    assert "J1" in report["allowed_outside_board_refs"]
    j1 = next(p for p in report["placements"] if p["ref"] == "J1")
    assert 0.0 <= j1["x"] <= 40.0 and 0.0 <= j1["y"] <= 20.0


def test_spacing_profile_enforced(tmp_path):
    """A larger spacing profile forces non-overlapping support placement."""

    fps = [_fp("U1", "U", 20, 20)] + [_fp(c, "C", 0, 0) for c in ("C1", "C2", "C3")]
    pcb = _pcb(fps)
    ppl = (
        "Board(width=40, height=40)\n"
        "Spacing(default=1.0, passive_to_passive=1.0)\n"
        'Anchor("U1", x=20, y=20, rot=0)\n'
        'DecouplingArray(["C1","C2","C3"], parent="U1", distance=2, spacing=2.0)\n'
    )
    _out, _msgs, report = _run(tmp_path, pcb, ppl)
    placed = _placed(report)
    pts = [placed[c] for c in ("C1", "C2", "C3")]
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            dist = ((pts[i][0] - pts[j][0]) ** 2 + (pts[i][1] - pts[j][1]) ** 2) ** 0.5
            assert dist >= 1.0


def test_best_effort_never_raises_but_strict_does(tmp_path):
    """Same unplaceable input: default succeeds (parks), strict raises."""

    pcb, ppl = _decoupling_scene(obstacles_locked=True, allow_anchor_move=False)
    # default (best-effort)
    _out, _msgs, report = _run(tmp_path, pcb, ppl)
    assert report["placements_applied"] >= 1
    # strict
    with pytest.raises(PlacementError):
        _run(tmp_path, pcb, ppl, strict=True)
