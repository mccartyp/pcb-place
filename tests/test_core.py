from pathlib import Path
import json
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pcb_place import apply_placements, import_netlist_aliases, load_ppl, parse_footprints, parse_netlist_aliases


def test_parse_footprints():
    text = (ROOT / "tests/fixtures/simple.kicad_pcb").read_text()
    fps = parse_footprints(text)
    assert set(fps) >= {"U1", "C1", "J1", "H1"}
    assert fps["U1"].x == 0


def test_apply_basic_rules():
    text = (ROOT / "tests/fixtures/simple.kicad_pcb").read_text()
    model = load_ppl(ROOT / "tests/fixtures/simple.ppl")
    out, messages, report = apply_placements(text, model, strict=True)
    assert report["placements_applied"] == 8
    assert '(property "Reference" "U1"' in out
    assert '(at 25 15 0)' in out
    assert '(at 25 13 0)' in out  # C1 satellite above U1


def test_cli_list_refs_json():
    result = subprocess.run(
        [sys.executable, str(ROOT / "pcb_place.py"), str(ROOT / "tests/fixtures/simple.kicad_pcb"), "--list-refs", "--format", "json"],
        check=True,
        capture_output=True,
        text=True,
    )
    refs = [item["ref"] for item in json.loads(result.stdout)]
    assert "U1" in refs


def test_cli_check_fails_when_stale(tmp_path):
    pcb = tmp_path / "simple.kicad_pcb"
    ppl = ROOT / "tests/fixtures/simple.ppl"
    pcb.write_text((ROOT / "tests/fixtures/simple.kicad_pcb").read_text())
    result = subprocess.run([sys.executable, str(ROOT / "pcb_place.py"), str(pcb), str(ppl), "--check"], capture_output=True, text=True)
    assert result.returncode == 1

from pcb_place import PlacementError


def test_region_relative_component_and_lock(tmp_path):
    ppl = tmp_path / "region.ppl"
    ppl.write_text('''
Board(width=50, height=30)
Region("CONTROL", x=20, y=10, w=10, h=10)
Component("U1").anchor(region="CONTROL").lock(reason="primary anchor")
Anchor("C1", relative_to="U1", dx=2, dy=-2, rot=0)
''')
    text = (ROOT / "tests/fixtures/simple.kicad_pcb").read_text()
    out, messages, report = apply_placements(text, load_ppl(ppl), strict=True, validate=True)
    assert report["regions"]["CONTROL"]["x"] == 20.0
    assert "U1" in report["locked_refs"]
    assert '(at 25 15 0)' in out
    assert '(at 27 13 0)' in out


def test_locked_component_violation_raises(tmp_path):
    ppl = tmp_path / "locked.ppl"
    ppl.write_text('''
Board(width=50, height=30)
Anchor("U1", x=10, y=10, lock=True)
Anchor("U1", x=20, y=20)
''')
    text = (ROOT / "tests/fixtures/simple.kicad_pcb").read_text()
    with pytest.raises(PlacementError):
        apply_placements(text, load_ppl(ppl), strict=True)


def test_cli_validate(tmp_path):
    pcb = tmp_path / "simple.kicad_pcb"
    ppl = ROOT / "tests/fixtures/simple.ppl"
    pcb.write_text((ROOT / "tests/fixtures/simple.kicad_pcb").read_text())
    result = subprocess.run([sys.executable, str(ROOT / "pcb_place.py"), str(pcb), str(ppl), "--validate"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "validate:" in result.stdout


def test_parse_json_netlist_aliases():
    aliases, diagnostics = parse_netlist_aliases(ROOT / "tests/fixtures/zener.json")
    assert diagnostics.parser == "json"
    assert aliases["MCU.U_MCU"] == "U1"
    assert aliases["MCU.C_VDD_1.C"] == "C1"


def test_parse_xml_and_sexp_netlist_aliases():
    xml_aliases, xml_diagnostics = parse_netlist_aliases(ROOT / "tests/fixtures/zener.xml")
    sexp_aliases, sexp_diagnostics = parse_netlist_aliases(ROOT / "tests/fixtures/zener.sexp")
    assert xml_diagnostics.parser == "xml"
    assert sexp_diagnostics.parser == "sexp"
    assert xml_aliases["MCU.U_MCU"] == "U1"
    assert sexp_aliases["MCU.C_VDD_1.C"] == "C1"


def test_imported_aliases_place_semantic_refs(tmp_path):
    ppl = tmp_path / "semantic.ppl"
    ppl.write_text('''
Board(width=50, height=30)
Anchor("MCU.U_MCU", x=25, y=15, rot=0)
Satellite("MCU.C_VDD_1.C", parent="MCU.U_MCU", side="top", distance=2, rot=0)
''')
    text = (ROOT / "tests/fixtures/simple.kicad_pcb").read_text()
    model = load_ppl(ppl)
    import_netlist_aliases(model, ROOT / "tests/fixtures/zener.json")
    out, messages, report = apply_placements(text, model, strict=True)
    assert '(property "Reference" "U1"' in out
    assert '(at 25 15 0)' in out
    assert '(at 25 13 0)' in out
    assert report["imported_aliases"]["MCU.U_MCU"] == "U1"
    assert report["effective_aliases"]["MCU.C_VDD_1.C"] == "C1"


def test_explicit_alias_overrides_imported_alias(tmp_path):
    ppl = tmp_path / "override.ppl"
    ppl.write_text('''
Board(width=50, height=30)
Alias("MCU.U_MCU", "J1")
Anchor("MCU.U_MCU", x=11, y=12, rot=90)
''')
    text = (ROOT / "tests/fixtures/simple.kicad_pcb").read_text()
    model = load_ppl(ppl)
    import_netlist_aliases(model, ROOT / "tests/fixtures/zener.json")
    out, messages, report = apply_placements(text, model, strict=True)
    assert '(property "Reference" "J1"' in out
    assert '(at 11 12 90)' in out
    assert report["effective_aliases"]["MCU.U_MCU"] == "J1"


def test_ambiguous_suffixes_fail(tmp_path):
    netlist = tmp_path / "ambiguous.json"
    netlist.write_text(json.dumps({"components": [
        {"path": "MCU.U_SHARED", "ref": "U1"},
        {"path": "PWR.U_SHARED", "ref": "J1"},
    ]}))
    ppl = tmp_path / "ambiguous.ppl"
    ppl.write_text('''
Board(width=50, height=30)
Anchor("U_SHARED", x=1, y=2)
''')
    model = load_ppl(ppl)
    import_netlist_aliases(model, netlist)
    with pytest.raises(PlacementError, match="ambiguous"):
        apply_placements((ROOT / "tests/fixtures/simple.kicad_pcb").read_text(), model, strict=True)


def test_missing_semantic_alias_fails_cleanly(tmp_path):
    ppl = tmp_path / "missing.ppl"
    ppl.write_text('''
Board(width=50, height=30)
Anchor("MCU.U_DOES_NOT_EXIST", x=1, y=2)
''')
    with pytest.raises(PlacementError, match="missing footprint"):
        apply_placements((ROOT / "tests/fixtures/simple.kicad_pcb").read_text(), load_ppl(ppl), strict=True)


def test_cli_list_aliases_json():
    result = subprocess.run(
        [sys.executable, str(ROOT / "pcb_place.py"), "--netlist", str(ROOT / "tests/fixtures/zener.json"), "--list-aliases", "--format", "json"],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    assert payload["aliases"]["MCU.U_MCU"] == "U1"


def test_cli_report_json_includes_alias_data(tmp_path):
    pcb = tmp_path / "simple.kicad_pcb"
    report = tmp_path / "report.json"
    ppl = tmp_path / "semantic.ppl"
    pcb.write_text((ROOT / "tests/fixtures/simple.kicad_pcb").read_text())
    ppl.write_text('''
Board(width=50, height=30)
Anchor("MCU.U_MCU", x=25, y=15, rot=0)
''')
    subprocess.run(
        [sys.executable, str(ROOT / "pcb_place.py"), str(pcb), str(ppl), "--netlist", str(ROOT / "tests/fixtures/zener.json"), "--report-json", str(report), "--dry-run"],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(report.read_text())
    assert payload["imported_aliases"]["MCU.U_MCU"] == "U1"
    assert payload["alias_diagnostics"]["parser"] == "json"
