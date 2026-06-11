from pathlib import Path


FIXTURES = {
    "simple.kicad_pcb": '''(kicad_pcb (version 20240108) (generator "pcb-place-test")
  (footprint "Test:U" (layer "F.Cu")
    (at 0 0 0)
    (property "Reference" "U1" (at 0 0 0) (layer "F.SilkS"))
  )
  (footprint "Test:C" (layer "F.Cu")
    (at 0 0 0)
    (property "Reference" "C1" (at 0 0 0) (layer "F.SilkS"))
  )
  (footprint "Test:C" (layer "F.Cu")
    (at 0 0 0)
    (property "Reference" "C2" (at 0 0 0) (layer "F.SilkS"))
  )
  (footprint "Test:J" (layer "F.Cu")
    (at 0 0 0)
    (property "Reference" "J1" (at 0 0 0) (layer "F.SilkS"))
  )
  (footprint "Test:J" (layer "F.Cu")
    (at 0 0 0)
    (property "Reference" "J2" (at 0 0 0) (layer "F.SilkS"))
  )
  (footprint "Test:H" (layer "F.Cu")
    (at 0 0 0)
    (property "Reference" "H1" (at 0 0 0) (layer "F.SilkS"))
  )
  (footprint "Test:H" (layer "F.Cu")
    (at 0 0 0)
    (property "Reference" "H2" (at 0 0 0) (layer "F.SilkS"))
  )
  (footprint "Test:D" (layer "F.Cu")
    (at 0 0 0)
    (property "Reference" "D1" (at 0 0 0) (layer "F.SilkS"))
  )
)
''',
    "simple.ppl": '''Board(width=50, height=30)
Corner("H1", corner="top_left", inset=3)
Corner("H2", corner="top_right", inset=3)
Edge("J1", edge="left", y=15)
Edge("J2", edge="right", y=15)
Anchor("U1", x=25, y=15, rot=0)
Satellite("C1", parent="U1", side="top", distance=2)
Between("D1", a="J1", b="U1", t=0.5, rot="path")
Orbit(refs=["C2"], parent="U1", radius=4, start_angle=0)
''',
    "zener.json": '''{
  "components": [
    {"path": "/MCU/U_MCU", "ref": "U1"},
    {"hierarchical_path": "MCU.C_VDD_1.C", "reference": "C1"},
    {"instance": "HDMI_IN.J1", "designator": "J1"}
  ]
}
''',
    "zener.xml": '''<export>
  <components>
    <comp ref="U1"><property name="path" value="/MCU/U_MCU" /></comp>
    <comp ref="C1"><property name="hierarchical_path" value="/MCU/C_VDD_1/C" /></comp>
  </components>
</export>
''',
    "zener.sexp": '''(netlist
  (component (path "/MCU/U_MCU") (ref "U1"))
  (component (instance "MCU.C_VDD_1.C") (reference "C1")))
''',
}


def pytest_configure(config):
    """Restore minimal test fixtures when source packaging omits fixture files."""
    fixtures_dir = Path(__file__).resolve().parent / "fixtures"
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    for name, content in FIXTURES.items():
        fixture = fixtures_dir / name
        if not fixture.exists():
            fixture.write_text(content)
