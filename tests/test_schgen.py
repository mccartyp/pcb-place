"""Tests for pcb-schgen: deterministic KiCad schematic generation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import pcb_schgen as schgen

FIX = Path(__file__).parent / "fixtures" / "schgen"
FIXTURES = ["resistor_led", "connector_esd_ic", "regulator_feedback", "zener_board"]


def _gen(name: str, **kwargs):
    base = FIX / name
    pln = base / "board.pln"
    return schgen.generate(
        base / "default.net",
        None,
        pln if pln.exists() else None,
        project=name,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_netlist_parse_recovers_refs_values_footprints_nets():
    nl = schgen.parse_netlist(FIX / "zener_board" / "default.net")
    assert "U2" in nl.components
    mcu = nl.components["U2"]
    assert mcu.prefix == "U"
    assert mcu.footprint.endswith("LQFP-48")
    assert any(p.name == "NRST" for p in mcu.pins)
    names = {n.name for n in nl.nets}
    assert {"+3V3", "GND", "BUCK_FB", "TMDS_IN_D0_P"} <= names


def test_pln_parse_extracts_intent():
    pln = schgen.parse_pln(FIX / "zener_board" / "board.pln")
    assert pln["roles"]["U1"] == "regulator"
    assert pln["functional_paths"]["HDMI_IN"]["sequence"] == ["J1", "U3", "U4"]
    assert "BUCK_3V3" in pln["power_islands"]


# ---------------------------------------------------------------------------
# Structure / KiCad validity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", FIXTURES)
def test_generated_sch_parses_structurally(name):
    res = _gen(name)
    text = res.sch_text
    assert text.count("(") == text.count(")"), "unbalanced parentheses"
    tree = schgen._parse_sexp(schgen._sexp_tokens(text))
    assert tree[0] == "kicad_sch"
    keys = [c[0] for c in tree if isinstance(c, list)]
    assert "lib_symbols" in keys
    assert "sheet_instances" in keys
    assert keys.count("uuid") == 1


@pytest.mark.parametrize("name", FIXTURES)
def test_all_refs_emitted(name):
    res = _gen(name)
    emitted = {s.ref for s in res.model.symbols}
    assert emitted == set(res.netlist.components)
    assert res.report["refs_emitted"] == res.report["refs_total"]
    assert res.report["missing_refs"] == []
    for ref in res.netlist.components:
        assert f'(reference "{ref}")' in res.sch_text


# ---------------------------------------------------------------------------
# Connectivity correctness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", FIXTURES)
def test_connectivity_matches_netlist(name):
    res = _gen(name)
    assert res.report["connectivity_ok"], res.report["connectivity_mismatches"]
    assert res.report["missing_nets"] == []
    assert res.report["nets_matched"] == res.report["nets_total"]
    # Independent re-extraction must agree, too.
    val = schgen.validate(res.model, res.netlist)
    assert val["ok"], val["mismatches"]


# ---------------------------------------------------------------------------
# Wire vs label policy
# ---------------------------------------------------------------------------


def test_global_power_and_ground_use_labels_or_power_symbols():
    res = _gen("zener_board")
    rep = res.report
    for rail in ("GND", "+3V3", "+5V"):
        assert rail in rep["global_label_nets"]
        assert rail not in rep["wired_nets"]
    # GND and +3V3 are realised as power symbols, not bare global labels.
    power_nets = {p.net for p in res.model.powers}
    assert {"GND", "+3V3", "+5V"} <= power_nets


def test_local_nets_drawn_as_wires():
    res = _gen("resistor_led")
    assert "LED_ANODE" in res.report["wired_nets"]
    # A wire actually exists between the resistor and the LED.
    assert any(w.seed.startswith(("link:LED_ANODE", "chain:LED_ANODE")) for w in res.model.wires)


def test_regulator_feedback_drawn_locally():
    res = _gen("regulator_feedback")
    assert "FB_DIV" in res.report["wired_nets"]
    assert "FB_DIV" not in res.report["global_label_nets"]


# ---------------------------------------------------------------------------
# Functional grouping / layout
# ---------------------------------------------------------------------------


def test_esd_placed_between_connector_and_ic():
    res = _gen("connector_esd_ic")
    x = {s.ref: s.x for s in res.model.symbols}
    assert x["J1"] < x["U1"] < x["U2"], x  # connector -> ESD -> IC


def test_esd_between_connector_and_retimer_zener():
    res = _gen("zener_board")
    x = {s.ref: s.x for s in res.model.symbols}
    assert x["J1"] < x["U3"] < x["U4"], x


def test_diff_pair_members_labeled_together():
    res = _gen("zener_board")
    rep = res.report
    assert "TMDS_IN_D0_P" in rep["global_label_nets"]
    assert "TMDS_IN_D0_N" in rep["global_label_nets"]


# ---------------------------------------------------------------------------
# Symbol mapping + fallback
# ---------------------------------------------------------------------------


def test_known_passives_map_to_builtin_symbols():
    res = _gen("zener_board")
    m = res.mappings
    assert m["R1"].lib_id == "schgen:R" and not m["R1"].fallback
    assert m["C1"].lib_id == "schgen:C" and not m["C1"].fallback
    assert m["L1"].lib_id == "schgen:L" and not m["L1"].fallback
    assert m["D1"].lib_id == "schgen:LED" and not m["D1"].fallback
    assert m["TP1"].lib_id == "schgen:TestPoint"
    assert m["SW1"].lib_id == "schgen:SW_Push"


def test_unknown_ic_uses_generic_fallback_with_review():
    res = _gen("connector_esd_ic")
    for ref in ("J1", "U1", "U2"):
        mp = res.mappings[ref]
        assert mp.fallback and mp.requires_review
        assert ref in res.report["symbol_fallbacks"]
        assert ref in res.report["requires_review"]
    # Generic symbols preserve the netlist pin numbers.
    retimer = res.mappings["U2"].symbol
    nums = {p.number for p in retimer.pins}
    assert {"1", "2", "39", "40"} <= nums
    assert "schgen_review" in res.sch_text


def test_generation_never_fails_on_unknown_symbols():
    # The whole point: an all-unknown design still produces a valid schematic.
    res = _gen("connector_esd_ic")
    assert res.report["refs_emitted"] == res.report["refs_total"]
    assert res.report["connectivity_ok"]


# ---------------------------------------------------------------------------
# Reports + determinism
# ---------------------------------------------------------------------------


def test_report_json_fields_accurate(tmp_path):
    res = _gen("zener_board")
    rep = res.report
    required = {
        "refs_total", "refs_emitted", "nets_total", "nets_matched", "missing_refs",
        "missing_nets", "global_label_nets", "wired_nets", "symbol_fallbacks",
        "requires_review",
    }
    assert required <= set(rep)
    netnames = {n.name for n in res.netlist.nets}
    assert set(rep["wired_nets"]) <= netnames
    assert set(rep["global_label_nets"]) <= netnames
    # wired and global classifications are disjoint.
    assert not (set(rep["wired_nets"]) & set(rep["global_label_nets"]))
    assert json.dumps(rep)  # serialisable


def test_symbol_map_written(tmp_path):
    res = _gen("zener_board")
    out = tmp_path / "symbol-map.yaml"
    schgen.write_symbol_map(out, res.netlist, res.mappings)
    text = out.read_text()
    assert "R1:" in text and "lib_id: schgen:R" in text
    assert "requires_review: true" in text  # the generic ICs


def test_deterministic_output(tmp_path):
    a = _gen("zener_board").sch_text
    b = _gen("zener_board").sch_text
    assert a == b
    # UUIDs are stable across runs.
    assert a.count("uuid") == b.count("uuid")


def test_random_uuids_differ_but_stay_valid():
    a = _gen("zener_board", random_uuids=True).sch_text
    b = _gen("zener_board", random_uuids=True).sch_text
    assert a != b  # random uuids change run to run
    tree = schgen._parse_sexp(schgen._sexp_tokens(a))
    assert tree[0] == "kicad_sch"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_end_to_end(tmp_path):
    base = FIX / "zener_board"
    out = tmp_path / "out.kicad_sch"
    report = tmp_path / "report.json"
    smap = tmp_path / "symbol-map.yaml"
    rc = schgen.main([
        "--netlist", str(base / "default.net"),
        "--pln", str(base / "board.pln"),
        "-o", str(out),
        "--report-json", str(report),
        "--symbol-map", str(smap),
        "--quiet",
    ])
    assert rc == 0
    assert out.exists() and report.exists() and smap.exists()
    rep = json.loads(report.read_text())
    assert rep["connectivity_ok"]
    assert rep["nets_matched"] == rep["nets_total"]


def test_cli_strict_passes_on_clean_design(tmp_path):
    base = FIX / "resistor_led"
    out = tmp_path / "o.kicad_sch"
    rc = schgen.main([
        "--netlist", str(base / "default.net"),
        "-o", str(out),
        "--strict", "--quiet",
    ])
    assert rc == 0
