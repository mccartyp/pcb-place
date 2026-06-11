from pathlib import Path


SIMPLE_KICAD_PCB = '''(kicad_pcb (version 20240108) (generator "pcb-place-test")
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
'''


def pytest_configure(config):
    """Restore the KiCad PCB fixture when source packaging omits it."""
    fixture = Path(__file__).resolve().parent / "fixtures" / "simple.kicad_pcb"
    if not fixture.exists():
        fixture.parent.mkdir(parents=True, exist_ok=True)
        fixture.write_text(SIMPLE_KICAD_PCB)
