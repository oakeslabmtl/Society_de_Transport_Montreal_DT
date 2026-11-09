"""Checks a Godot export of the viewer before it is shared or published: no STM API key, nothing that looks like a
secret.

- The key: when STM_API_KEY is set on this computer, no file of the export, and no file of the project it was made
  from (--project), may contain its value (as UTF-8 or UTF-16, inside zip files too). The value is never printed.
  Without the variable (GitHub Actions has none) this part is skipped, and the report says so.
- The packs (a web export's .pck, the one at the end of a Windows exe): no file whose name looks like a secret (.env,
  *.key, *.pem, credentials...) and none of the words an API key travels with (apikey, api_key, api-key). Only the
  packs: the engine and the .NET runtime have such words of their own.
- The project's files (--project, without its builds in Build/ and the editor's cache in .godot/): none of those
  words either. A pack holds the scripts compressed (and some resources), where no search can see them: their
  sources are what is checked.

Prints file names only, and exits with 1 when something is found (or when there is no pack to check). From the
repository root (build_demo.ps1 and the GitHub Pages workflow run it after their export):
    .venv/Scripts/python -I software/tools/check_export.py visualization/Build/Web --project=visualization
"""
import fnmatch
import os
import re
import struct
import sys
import zipfile
from pathlib import Path

KEY_VARIABLE = "STM_API_KEY"
CHUNK = 16 * 1024 * 1024
# Names the presets' exclude_filter keeps out of every pack (visualization/export_presets.cfg)
SECRET_NAMES = ["*.env", ".env", ".env.*", "*.key", "*.pem", "*.pfx", "*.p12", "*.sqlite", "*.db", "*secret*",
                "*credential*"]
SECRET_WORDS = re.compile(rb"api[_-]?key|-----begin [a-z ]*private key", re.IGNORECASE)
PCK_MAGIC = b"GDPC"
PROJECT_SKIPPED = {"Build", ".godot"}  # A project's exports and the editor's cache: not sources


def key_needles() -> list[bytes]:
    """The key's value as it could be stored (UTF-8, UTF-16), empty when the variable is not set."""
    key = os.environ.get(KEY_VARIABLE, "")
    values = {value for value in (key, key.strip()) if value}
    return [value.encode(encoding) for value in values for encoding in ("utf-8", "utf-16-le")]


def stream_contains(stream, needles: list[bytes]) -> bool:
    """True when one of the needles is in the stream (read by chunks, overlapping by a needle's length)."""
    overlap = max(len(needle) for needle in needles) - 1
    tail = b""
    while chunk := stream.read(CHUNK):
        data = tail + chunk
        if any(needle in data for needle in needles):
            return True
        tail = data[-overlap:] if overlap else b""
    return False


def file_contains(path: Path, needles: list[bytes]) -> bool:
    """True when the file has one of the needles, or a zip file one of its entries."""
    with open(path, "rb") as stream:
        if stream_contains(stream, needles):
            return True
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for entry in archive.infolist():
                with archive.open(entry) as stream:
                    if stream_contains(stream, needles):
                        return True
    return False


def pack_region(path: Path) -> tuple[int, int] | None:
    """(start, end) of the Godot pack in the file: a whole .pck, or one embedded at the end of an exe; None without"""
    size = path.stat().st_size
    if size < 12:
        return None
    with open(path, "rb") as stream:
        if stream.read(4) == PCK_MAGIC:
            return 0, size
        stream.seek(size - 4)
        if stream.read(4) != PCK_MAGIC:
            return None
        stream.seek(size - 12)
        pack_size = struct.unpack("<Q", stream.read(8))[0]
        start = size - 12 - pack_size
        stream.seek(start)
        return (start, size - 12) if start >= 0 and stream.read(4) == PCK_MAGIC else None


def pack_names(path: Path, start: int) -> list[str]:
    """The paths of the files in a pack (format 2 and later: Godot 4)"""
    with open(path, "rb") as stream:
        stream.seek(start + 4)
        version, _major, _minor, _patch, flags = struct.unpack("<5I", stream.read(20))
        if flags & 1:
            raise ValueError(f"{path}: the pack's file list is encrypted")
        stream.read(8)  # file_base
        dir_offset = struct.unpack("<Q", stream.read(8))[0] if version >= 3 else None
        stream.read(16 * 4)
        if dir_offset is not None:
            stream.seek(start + dir_offset)
        names = []
        for _ in range(struct.unpack("<I", stream.read(4))[0]):
            length = struct.unpack("<I", stream.read(4))[0]
            names.append(stream.read(length).rstrip(b"\0").decode("utf-8"))
            stream.read(8 + 8 + 16 + (4 if version >= 2 else 0))
        return names


def region_has_words(path: Path, start: int, end: int) -> bool:
    """True when the bytes between start and end have a SECRET_WORDS match (read by overlapping chunks)"""
    overlap = 64
    tail = b""
    with open(path, "rb") as stream:
        stream.seek(start)
        remaining = end - start
        while remaining > 0:
            chunk = stream.read(min(CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            data = tail + chunk
            if SECRET_WORDS.search(data):
                return True
            tail = data[-overlap:]
    return False


def files_under(roots: list[Path]) -> list[Path]:
    return sorted(path for root in roots for path in ([root] if root.is_file() else root.rglob("*")) if path.is_file())


def project_files(project: Path) -> list[Path]:
    """The Godot project's own files, what its exports are made from (not its builds, nor the editor's cache)"""
    return [path for path in files_under([project]) if path.relative_to(project).parts[0] not in PROJECT_SKIPPED]


def check(roots: list[Path], project: Path | None = None) -> list[str]:
    """Problems found in the export under roots and in the project's files (file names only, never the key's value);
    prints a report"""
    files = files_under(roots)
    sources = project_files(project) if project is not None else []
    problems = []
    needles = key_needles()
    if needles:
        holding = [str(path) for path in files + sources if file_contains(path, needles)]
        print(f"  {KEY_VARIABLE}: {'in ' + ', '.join(holding) if holding else 'not in any file'}")
        if holding:
            problems.append(f"The value of {KEY_VARIABLE} is in: {', '.join(holding)}")
    else:
        print(f"  {KEY_VARIABLE}: not set on this computer, its value was not looked for")
    packs = 0
    for path in files:
        region = pack_region(path)
        if region is None:
            continue
        packs += 1
        names = pack_names(path, region[0])
        secret = [name for name in names
                  if any(fnmatch.fnmatch(part.lower(), pattern) for part in name.split("/") for pattern in SECRET_NAMES)]
        words = region_has_words(path, *region)
        print(f"  pack {path}: {len(names)} files, secret-like names: {', '.join(secret) or 'none'}, "
              f"key words: {'found' if words else 'none'}")
        if secret:
            problems.append(f"{path} packs files that look like secrets: {', '.join(secret)}")
        if words:
            problems.append(f"{path} has a word an API key travels with (apikey, api_key, api-key or a private key)")
    if packs == 0:
        problems.append(f"No Godot pack (.pck, or one at the end of an exe) in {', '.join(map(str, roots))}")
    if project is not None:
        worded = [str(path) for path in sources if region_has_words(path, 0, path.stat().st_size)]
        print(f"  project {project}: {len(sources)} files, key words: {', '.join(worded) or 'none'}")
        if worded:
            problems.append(f"Project files with a word an API key travels with (apikey, api_key, api-key or a private "
                            f"key): {', '.join(worded)}")
    total = sum(path.stat().st_size for path in files)
    print(f"  {len(files)} files, {total / 1e6:.1f} MB, {packs} pack(s)")
    return problems


def main(argv: list[str]) -> int:
    project_args = [arg for arg in argv if arg.startswith("--project=")]
    paths = [arg for arg in argv if not arg.startswith("--project=")]
    if not paths or len(project_args) > 1:
        print(__doc__)
        return 2
    roots = [Path(arg) for arg in paths]
    project = Path(project_args[0].removeprefix("--project=")) if project_args else None
    missing = [str(path) for path in roots + ([project] if project is not None else []) if not path.exists()]
    if missing:
        print(f"check_export: not found: {', '.join(missing)}")
        return 2
    print(f"check_export: {', '.join(map(str, roots))}")
    problems = check(roots, project)
    for problem in problems:
        print(f"FAILED: {problem}")
    print("OK" if not problems else "The export must not be shared as it is.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
