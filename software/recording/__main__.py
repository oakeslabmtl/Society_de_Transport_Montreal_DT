import sys
from pathlib import Path

# Works as `python -m recording` (from software/) and as `python software/recording` (from the repo root).
PACKAGE_DIR = Path(__file__).resolve().parent
SOFTWARE_DIR = str(PACKAGE_DIR.parent)

if sys.path and sys.path[0] and Path(sys.path[0]).resolve() == PACKAGE_DIR:
    sys.path.pop(0)  # run as a folder: don't let our module names shadow other modules
if SOFTWARE_DIR not in sys.path:
    sys.path.insert(0, SOFTWARE_DIR)

from recording.cli import main

if __name__ == '__main__':
    sys.exit(main())
