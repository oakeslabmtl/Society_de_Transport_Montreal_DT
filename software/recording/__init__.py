"""Record STM bus positions to SQLite and export them as replays for the Godot viewer.

Run from software/:   python -m recording --help
Run from repo root:   python software/recording --help

Keep this file free of imports from the project: `python software/recording` executes
__main__.py before the software/ folder is on sys.path.
"""


class RecordingError(Exception):
    """A problem the user can fix (bad path, missing key, ...): printed without a traceback."""


class NothingToForecast(RecordingError):
    """No bus in the replay reports its occupancy: installing Timestral or running `forecast` later won't help."""
