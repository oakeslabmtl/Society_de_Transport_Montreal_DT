import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RECORDINGS_DIR = ROOT / "recordings"
REPLAYS_DIR = ROOT / "visualization" / "replays"
DATASETS_DIR = ROOT / "datasets"
ROUTE_STOPS_FILE = ROOT / "visualization" / "assets" / "route_stops.json"


def repo_relative(path:Path) -> str:
    """'datasets/x.csv' for files inside the repo, else just the file name (no personal folders in shared files)."""
    path = Path(path).resolve()
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.name


def display_path(path:Path) -> str:
    """Short path for messages: relative to the current folder when inside the repo, else absolute."""
    path = Path(path).resolve()
    try:
        path.relative_to(ROOT)
        Path.cwd().resolve().relative_to(ROOT)
        return os.path.relpath(path)
    except ValueError:
        return str(path)
