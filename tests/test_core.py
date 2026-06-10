from pathlib import Path
import json
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pcb_place import apply_placements, load_ppl, parse_footprints


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
