#!/usr/bin/env python3
"""Start the optional TLS world-generation assistance service (not BeamMP)."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

if __name__ == "__main__":
    from assistservice import main
    raise SystemExit(main())
