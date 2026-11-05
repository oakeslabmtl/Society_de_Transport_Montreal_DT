import contextlib
import io
import os
import struct
import zipfile
from unittest import mock
from fixtures import TempDirTestCase
from tools import check_export

DUMMY_KEY = "dummy-key-0123456789abcdef"  # Never the real key: the checker is tested with this one


def pack(names:list[str], payload:bytes=b"") -> bytes:
    """A Godot 4 pack (format 3) holding these file names, each with the payload."""
    header_size = 4 + 5 * 4 + 8 + 8 + 16 * 4
    data = payload * len(names)
    directory = struct.pack("<I", len(names))
    for i, name in enumerate(names):
        encoded = name.encode("utf-8")
        encoded += b"\0" * (-len(encoded) % 4)
        directory += struct.pack("<I", len(encoded)) + encoded
        directory += struct.pack("<QQ", i * len(payload), len(payload)) + b"\0" * 16 + struct.pack("<I", 0)
    header = b"GDPC" + struct.pack("<5I", 3, 4, 7, 1, 2) + struct.pack("<QQ", header_size, header_size + len(data))
    return header + b"\0" * 64 + data + directory


def embedded(exe:bytes, pck:bytes) -> bytes:
    """An exe with the pack at its end, as Godot embeds it."""
    return exe + pck + struct.pack("<Q", len(pck)) + b"GDPC"


class CheckExportTest(TempDirTestCase):

    def run_check(self, *paths, key:str|None=None) -> tuple[int, str]:
        env = {k: v for k, v in os.environ.items() if k != check_export.KEY_VARIABLE}
        if key is not None:
            env[check_export.KEY_VARIABLE] = key
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(out):
            code = check_export.main([str(p) for p in paths])
        return code, out.getvalue()

    def write(self, name:str, content:bytes):
        path = self.tmp / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_clean_web_export_passes(self):
        self.write("web/index.pck", pack(["project.binary", "scripts/main.gdc", "replays/hour.json"], b"buses"))
        self.write("web/index.html", b"<html></html>")
        code, out = self.run_check(self.tmp / "web", key=DUMMY_KEY)
        self.assertEqual(code, 0, out)
        self.assertIn("3 files", out)
        self.assertIn("not in any file", out)

    def test_key_value_is_found_and_never_printed(self):
        self.write("web/index.pck", pack(["replays/hour.json"], b'{"detail": "' + DUMMY_KEY.encode() + b'"}'))
        code, out = self.run_check(self.tmp / "web", key=DUMMY_KEY)
        self.assertEqual(code, 1)
        self.assertIn("index.pck", out)
        self.assertNotIn(DUMMY_KEY, out)

    def test_key_in_utf16_in_an_exe_and_in_a_zip_is_found(self):
        dll = b"MZ" + DUMMY_KEY.encode("utf-16-le")
        self.write("demo/game.exe", embedded(b"MZ engine", pack(["project.binary"], b"x")))
        self.write("demo/data/listener.dll", dll)
        with zipfile.ZipFile(self.tmp / "demo.zip", "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("data/listener.dll", dll)
        code, out = self.run_check(self.tmp / "demo", self.tmp / "demo.zip", key=DUMMY_KEY)
        self.assertEqual(code, 1)
        self.assertIn("listener.dll", out)
        self.assertIn("demo.zip", out)
        self.assertNotIn("game.exe,", out)
        self.assertNotIn(DUMMY_KEY, out)

    def test_key_with_a_trailing_newline_is_found_stripped(self):
        self.write("web/index.pck", pack(["a.json"], DUMMY_KEY.encode()))
        code, _ = self.run_check(self.tmp / "web", key=DUMMY_KEY + "\r\n")
        self.assertEqual(code, 1)

    def test_without_the_variable_the_value_check_is_skipped(self):
        self.write("web/index.pck", pack(["a.json"], DUMMY_KEY.encode()))
        code, out = self.run_check(self.tmp / "web")
        self.assertEqual(code, 0, out)
        self.assertIn("not set on this computer", out)

    def test_secret_like_names_in_a_pack_fail(self):
        for name in [".env", "config/.env.local", "certs/server.pem", "Credentials.json", "recordings/hour.sqlite"]:
            with self.subTest(name=name):
                self.write("web/index.pck", pack(["project.binary", name]))
                code, out = self.run_check(self.tmp / "web")
                self.assertEqual(code, 1)
                self.assertIn(name, out)

    def test_key_words_in_a_pack_fail(self):
        for word in [b'"apiKey": "x"', b"API_KEY=", b"x-api-key", b"-----BEGIN RSA PRIVATE KEY-----"]:
            with self.subTest(word=word):
                self.write("web/index.pck", pack(["a.json"], word))
                code, out = self.run_check(self.tmp / "web")
                self.assertEqual(code, 1)
                self.assertIn("api_key", out)

    def test_key_words_outside_the_packs_are_ignored(self):
        self.write("demo/game.exe", embedded(b"MZ apikey engine", pack(["project.binary"], b"x")))
        self.write("demo/data/System.Security.Cryptography.dll", b"apikey")
        code, out = self.run_check(self.tmp / "demo")
        self.assertEqual(code, 0, out)

    def test_project_sources_are_checked_too(self):
        # A pack holds the scripts compressed: a key in a script is only visible in its source
        self.write("web/index.pck", pack(["scripts/main.gdc"], b"GDSC zstd frame"))
        self.write("project/scripts/main.gd", b'const HEADERS = {"token": "' + DUMMY_KEY.encode() + b'"}')
        code, out = self.run_check(self.tmp / "web", f"--project={self.tmp / 'project'}", key=DUMMY_KEY)
        self.assertEqual(code, 1)
        self.assertIn("main.gd", out)
        self.assertNotIn(DUMMY_KEY, out)
        self.write("project/scripts/main.gd", b'const HEADERS = {"apiKey": "x"}')
        code, out = self.run_check(self.tmp / "web", f"--project={self.tmp / 'project'}")
        self.assertEqual(code, 1)
        self.assertIn("main.gd", out)

    def test_project_builds_and_editor_cache_are_skipped(self):
        self.write("project/Build/Web/index.pck", pack(["a.json"], b"apikey"))
        self.write("project/.godot/editor/cache.cfg", b"apikey")
        self.write("project/scripts/main.gd", b"extends Node")
        self.write("web/index.pck", pack(["scripts/main.gdc"], b"x"))
        code, out = self.run_check(self.tmp / "web", f"--project={self.tmp / 'project'}")
        self.assertEqual(code, 0, out)
        self.assertIn("1 files, key words: none", out)

    def test_no_pack_fails(self):
        self.write("web/index.html", b"<html></html>")
        code, out = self.run_check(self.tmp / "web")
        self.assertEqual(code, 1)
        self.assertIn("No Godot pack", out)
