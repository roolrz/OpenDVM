"""Exercise real curl downloads and host file replacement using a local HTTP server."""

import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[2]
VALID = ("# YSF host fixture\n" + "".join(
    f"{number:05d};CI-{number};Fixture;127.0.0.1;42000;000;\n" for number in range(1, 502)
)).encode()
WITH_CHINA = VALID + b"46004;CN-C4FM;China C4FM;203.0.113.4;42002;000;\n"
RESPONSES = {
    "/valid": WITH_CHINA,
    "/no-china": VALID,
    "/crlf": WITH_CHINA.replace(b"\n", b"\r\n"),
    "/html": b"<html>upstream unavailable</html>\n" * 600,
    "/short": b"# padding\n" * 600 + b"46004;CN-C4FM;China;203.0.113.4;42002;000;\n",
    "/bad-port": WITH_CHINA + b"55555;Invalid;Fixture;127.0.0.1;65536;000;\n",
}


class HostHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/truncated":
            self.send_response(200)
            self.send_header("Content-Length", str(len(WITH_CHINA) + 100))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(WITH_CHINA)
            return
        body = RESPONSES.get(self.path)
        self.send_response(200 if body is not None else 503)
        self.end_headers()
        self.wfile.write(body if body is not None else b"Unavailable")

    def log_message(self, *_):
        pass


class HostUpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), HostHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join(timeout=5)
        cls.server.server_close()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="opendvm-ysf-test-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.dest = self.path / "YSFHosts.txt"
        self.backup = self.path / "YSFHosts.txt.bak"
        self.restarts = self.path / "restarts.log"
        self.bin = self.path / "bin"
        self.bin.mkdir()
        systemctl = self.bin / "systemctl"
        systemctl.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$*" >> "$RESTART_LOG"\nexit "${RESTART_STATUS:-0}"\n'
        )
        systemctl.chmod(0o755)
        self.env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}",
                        RESTART_LOG=str(self.restarts), NO_PROXY="127.0.0.1", no_proxy="127.0.0.1")

    def update(self, endpoint="/valid", *arguments):
        return subprocess.run([
            str(ROOT / "update_ysf_hosts.sh"), "--url", self.url + endpoint,
            "--dest", str(self.dest), *arguments,
        ], env=self.env, text=True, capture_output=True, timeout=10)

    def assert_no_temporary_files(self):
        self.assertEqual(list(self.path.glob("YSFHosts.txt*.tmp.*")), [])

    def test_first_install_without_existing_file(self):
        result = self.update("/valid", "--no-restart")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.dest.read_bytes(), WITH_CHINA)
        self.assertEqual(self.dest.stat().st_mode & 0o777, 0o644)
        self.assertFalse(self.backup.exists())
        self.assertFalse(self.restarts.exists())
        self.assertIn("46004;CN-C4FM", result.stdout)
        self.assert_no_temporary_files()

    def test_replacement_preserves_backup_and_restarts_once(self):
        self.dest.write_bytes(VALID)
        result = self.update()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.backup.read_bytes(), VALID)
        self.assertEqual(self.dest.read_bytes(), WITH_CHINA)
        self.assertEqual(self.restarts.read_text().splitlines(), ["restart ysfgateway.service"])
        self.assert_no_temporary_files()

    def test_unchanged_preserves_backup_and_skips_restart(self):
        self.dest.write_bytes(WITH_CHINA)
        self.backup.write_bytes(b"earlier backup")
        result = self.update()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.backup.read_bytes(), b"earlier backup")
        self.assertFalse(self.restarts.exists())
        self.assert_no_temporary_files()

    def test_invalid_downloads_preserve_files_and_do_not_restart(self):
        for endpoint in ("/html", "/short", "/bad-port", "/unavailable", "/truncated"):
            with self.subTest(endpoint=endpoint):
                self.dest.write_bytes(VALID)
                self.backup.write_bytes(b"earlier backup")
                result = self.update(endpoint)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.dest.read_bytes(), VALID)
                self.assertEqual(self.backup.read_bytes(), b"earlier backup")
                self.assertFalse(self.restarts.exists())
                self.assert_no_temporary_files()

    def test_missing_reflector_46004_is_not_a_download_failure(self):
        result = self.update("/no-china", "--no-restart")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.dest.read_bytes(), VALID)

    def test_crlf_file(self):
        result = self.update("/crlf", "--no-restart")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.dest.read_bytes(), RESPONSES["/crlf"])

    def test_concurrent_update_is_rejected(self):
        self.dest.write_bytes(VALID)
        with Path(f"{self.dest}.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.update()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.dest.read_bytes(), VALID)
        self.assertFalse(self.restarts.exists())

    def test_restart_failure_is_reported(self):
        self.dest.write_bytes(VALID)
        self.env["RESTART_STATUS"] = "1"
        result = self.update()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.backup.read_bytes(), VALID)
        self.assertIn("restarting ysfgateway failed", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
