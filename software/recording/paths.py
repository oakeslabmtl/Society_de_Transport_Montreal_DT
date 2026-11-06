import os
from pathlib import Path
from recording import RecordingError

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


def write_text_atomic(text:str, path:Path) -> int:
    """Writes UTF-8 text through a temporary file, so a reader never sees half a file. Returns the size in bytes.
    A failed write (read-only file, full disk...) is a RecordingError and leaves no temporary file behind."""
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(text.encode("utf-8"))
        os.replace(temporary, path)
    except OSError as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise RecordingError(f"Could not write {display_path(path)}: {error.strerror or error}") from error
    return path.stat().st_size


def display_path(path:Path) -> str:
    """Short path for messages: relative to the current folder when inside the repo, else absolute."""
    path = Path(path).resolve()
    try:
        path.relative_to(ROOT)
        Path.cwd().resolve().relative_to(ROOT)
        return os.path.relpath(path)
    except ValueError:
        return str(path)
