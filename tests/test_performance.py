"""Performance, runtime-budget, and spatial-index tests for pcb-place.

These cover the bounded/staged search work: the spatial index that keeps
collision checks local, the optimization-level knobs (fast/normal/deep), the
wall-clock time budget with best-effort degradation, and the profiling report.

The synthetic boards mirror the builders in test_floorplan.py: a handful of
parent ICs each carrying a decoupling array of passives, which exercises the
expensive grouped-array search that previously dominated runtime.
"""

from pathlib import Path
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pcb_place import apply_placements, load_ppl
from pcb_place.pcb_place import (
    PlacementError,
    SpatialIndex,
    BBox,
    RuntimeBudget,
    OPTIMIZATION_LEVELS,
)


# ---------------------------------------------------------------------------
# Board builders
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


def _hundred_component_board(n_parents: int = 4, caps_per_parent: int = 24):
    """A ~100-component board: several parent ICs each with a decoupling array."""

    fps: list[str] = []
    lines = ["Board(width=160, height=100)", "Spacing(default=0.25)"]
    for i in range(n_parents):
        px = 20 + i * 35
        fps.append(_fp(f"U{i + 1}", "U", px, 40, pad=4))
        lines.append(f'Anchor("U{i + 1}", x={px}, y=40, rot=0)')
        refs = []
        for j in range(caps_per_parent):
            ref = f"C{i}_{j}"
            fps.append(_fp(ref, "C", 1, 1))
            refs.append(f'"{ref}"')
        lines.append(
            f'DecouplingArray([{", ".join(refs)}], parent="U{i + 1}", side="auto", '
            f"distance=2.0, spacing=1.5, stagger=True, max_per_row=3)")
    return _pcb(fps), "\n".join(lines) + "\n"


def _run(tmp_path, pcb_text, ppl_text, **kwargs):
    ppl = tmp_path / "board.ppl"
    ppl.write_text(ppl_text)
    return apply_placements(pcb_text, load_ppl(ppl), **kwargs)


# ---------------------------------------------------------------------------
# SpatialIndex unit tests
# ---------------------------------------------------------------------------

def test_spatial_index_query_returns_only_nearby():
    index = SpatialIndex(cell_size=5.0)
    index.insert("A", BBox(0, 0, 2, 2))
    index.insert("B", BBox(3, 3, 5, 5))
    index.insert("FAR", BBox(80, 80, 82, 82))
    near = index.query(BBox(0, 0, 5, 5))
    assert "A" in near and "B" in near
    assert "FAR" not in near


def test_spatial_index_incremental_update():
    index = SpatialIndex(cell_size=5.0)
    index.insert("A", BBox(0, 0, 1, 1))
    assert "A" in index.query(BBox(0, 0, 1, 1))
    # Move A far away; it must no longer be reported near the origin.
    index.insert("A", BBox(90, 90, 91, 91))
    assert "A" not in index.query(BBox(0, 0, 1, 1))
    assert "A" in index.query(BBox(89, 89, 92, 92))
    index.remove("A")
    assert index.query(BBox(89, 89, 92, 92)) == set()


def test_spatial_index_tracks_query_stats():
    index = SpatialIndex(cell_size=5.0)
    index.insert("A", BBox(0, 0, 1, 1))
    index.query(BBox(0, 0, 1, 1))
    index.query(BBox(0, 0, 1, 1))
    report = index.report()
    assert report["enabled"] is True
    assert report["queries"] == 2
    assert report["average_candidates_checked"] >= 0.0


# ---------------------------------------------------------------------------
# Runtime / report wiring
# ---------------------------------------------------------------------------

def test_report_exposes_runtime_spatial_and_profile_blocks(tmp_path):
    pcb, ppl = _hundred_component_board(n_parents=2, caps_per_parent=6)
    _out, _msgs, report = _run(tmp_path, pcb, ppl, optimization_level="fast")
    runtime = report["runtime"]
    assert set(runtime) >= {"elapsed_seconds", "budget_seconds", "timed_out",
                            "candidates_evaluated", "cache_hits"}
    assert runtime["optimization_level"] == "fast"
    spatial = report["spatial_index"]
    assert spatial["enabled"] is True
    assert spatial["queries"] > 0
    assert "average_candidates_checked" in spatial
    profile = report["profile"]
    assert profile["counters"]["collision_checks"] > 0
    assert "slowest_rules" in profile


def test_spatial_index_keeps_collision_checks_local(tmp_path):
    """The index must keep average neighbours per query well below the part count."""

    pcb, ppl = _hundred_component_board()
    _out, _msgs, report = _run(tmp_path, pcb, ppl, optimization_level="fast")
    spatial = report["spatial_index"]
    total_parts = report["footprints_total"]
    assert total_parts >= 90
    # A naive all-pairs scan would check ~total_parts per query; the grid index
    # must return only a small local neighbourhood.
    assert spatial["average_candidates_checked"] < total_parts / 4.0


# ---------------------------------------------------------------------------
# Optimization levels and candidate budget
# ---------------------------------------------------------------------------

def test_fast_evaluates_fewer_candidates_than_deep(tmp_path):
    pcb, ppl = _hundred_component_board(n_parents=2, caps_per_parent=10)
    _o1, _m1, fast = _run(tmp_path, pcb, ppl, optimization_level="fast")
    _o2, _m2, deep = _run(tmp_path, pcb, ppl, optimization_level="deep")
    assert fast["runtime"]["candidates_evaluated"] < deep["runtime"]["candidates_evaluated"]


def test_normal_between_fast_and_deep(tmp_path):
    pcb, ppl = _hundred_component_board(n_parents=2, caps_per_parent=10)
    _o1, _m1, fast = _run(tmp_path, pcb, ppl, optimization_level="fast")
    _o2, _m2, normal = _run(tmp_path, pcb, ppl, optimization_level="normal")
    _o3, _m3, deep = _run(tmp_path, pcb, ppl, optimization_level="deep")
    fast_n = fast["runtime"]["candidates_evaluated"]
    normal_n = normal["runtime"]["candidates_evaluated"]
    deep_n = deep["runtime"]["candidates_evaluated"]
    assert fast_n <= normal_n <= deep_n


def test_max_candidates_per_rule_is_respected(tmp_path):
    pcb, ppl = _hundred_component_board(n_parents=2, caps_per_parent=10)
    _out, _msgs, report = _run(tmp_path, pcb, ppl, optimization_level="deep",
                               max_candidates_per_rule=40)
    assert report["runtime"]["max_candidates_per_rule"] == 40
    # All parts still placed despite the tighter per-rule cap.
    assert report["placements_applied"] == report["footprints_total"]


def test_hundred_component_board_under_target_normal_mode(tmp_path):
    """Acceptance: a ~100-component board places well under 60s in normal mode."""

    pcb, ppl = _hundred_component_board()
    start = time.perf_counter()
    _out, _msgs, report = _run(tmp_path, pcb, ppl, optimization_level="normal")
    elapsed = time.perf_counter() - start
    assert report["placements_applied"] == report["footprints_total"]
    assert report["footprints_total"] >= 100
    # The acceptance target is 60s; assert well under it with headroom for slow CI.
    assert elapsed < 30.0, f"normal placement took {elapsed:.1f}s"


# ---------------------------------------------------------------------------
# Time budget / best-effort degradation
# ---------------------------------------------------------------------------

def test_timeout_produces_degraded_placement_not_abort(tmp_path):
    pcb, ppl = _hundred_component_board()
    _out, _msgs, report = _run(tmp_path, pcb, ppl, optimization_level="deep",
                               time_budget_seconds=0.4)
    assert report["runtime"]["timed_out"] is True
    # Best-effort: every footprint still receives a placement.
    assert report["placements_applied"] == report["footprints_total"]
    # Degraded placements are flagged for review rather than aborting.
    assert len(report["requires_review"]) > 0


def test_strict_timeout_aborts(tmp_path):
    pcb, ppl = _hundred_component_board()
    with pytest.raises(PlacementError):
        _run(tmp_path, pcb, ppl, strict=True, time_budget_seconds=0.01)


def test_zero_budget_disables_timeout(tmp_path):
    pcb, ppl = _hundred_component_board(n_parents=2, caps_per_parent=6)
    _out, _msgs, report = _run(tmp_path, pcb, ppl, optimization_level="fast",
                               time_budget_seconds=0.0)
    assert report["runtime"]["timed_out"] is False


# ---------------------------------------------------------------------------
# RuntimeBudget knobs
# ---------------------------------------------------------------------------

def test_runtime_budget_presets():
    fast = RuntimeBudget(level="fast")
    deep = RuntimeBudget(level="deep")
    assert fast.max_candidates_per_rule < deep.max_candidates_per_rule
    assert fast.reflow_max_level <= deep.reflow_max_level
    # Unknown levels fall back to normal.
    assert RuntimeBudget(level="bogus").level == "normal"


def test_runtime_budget_explicit_overrides_preset():
    budget = RuntimeBudget(level="normal", max_candidates_per_rule=7,
                           max_floorplan_iterations=3)
    assert budget.max_candidates_per_rule == 7
    assert budget.max_floorplan_iterations == 3


def test_all_levels_present():
    assert set(OPTIMIZATION_LEVELS) == {"fast", "normal", "deep"}
