"""Test staged D-Star host updates against a real local HTTP server."""

import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[2]
FILES = {"DExtra_Hosts.txt": "XRF", "DPlus_Hosts.txt": "REF", "DCS_Hosts.txt": "DCS"}


def table(prefix, count=120):
    return ("# Resolved host fixture\n" + "".join(
        f"{prefix}{index:03d}\t198.51.100.{index % 250 + 1}\n" for index in range(count)
    )).encode()


VALID = {name: table(prefix) for name, prefix in FILES.items()}
VALID["DExtra_Hosts.txt"] += b"XRF00A\t203.0.113.1\n"
VALID["DPlus_Hosts.txt"] += b"W5FC\t203.0.113.2\tL\n4O0LPG\t203.0.113.3\n"
VALID["DCS_Hosts.txt"] += b"XLX00A\t203.0.113.4\tL\n"


class HostHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        name = self.path.rsplit("/", 1)[-1]
        self.server.calls.append(name)
        body = self.server.payloads.get(name)
        self.send_response(200 if body is not None else 503)
        self.end_headers()
        self.wfile.write(body if body is not None else b"Unavailable")

    def log_message(self, *_):
        pass


class DStarUpdateTests(unittest.TestCase):
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
        self.directory = tempfile.TemporaryDirectory(prefix="opendvm-dstar-test-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.dest = self.path / "hosts"
        self.dest.mkdir()
        self.bin = self.path / "bin"
        self.bin.mkdir()
        self.restarts = self.path / "restarts.log"
        systemctl = self.bin / "systemctl"
        systemctl.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$*" >> "$RESTART_LOG"\nexit "${RESTART_STATUS:-0}"\n'
        )
        systemctl.chmod(0o755)
        self.env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}",
                        RESTART_LOG=str(self.restarts), NO_PROXY="127.0.0.1", no_proxy="127.0.0.1")
        self.server.payloads = dict(VALID)
        self.server.calls = []

    def update(self, *arguments):
        return subprocess.run([
            str(ROOT / "update_dstar_hosts.sh"), "--base-url", self.url,
            "--dest-dir", str(self.dest), *arguments,
        ], env=self.env, text=True, capture_output=True, timeout=10)

    def existing(self):
        for name in FILES:
            (self.dest / name).write_bytes(b"old host list\n")
            (self.dest / f"{name}.bak").write_bytes(b"previous backup\n")

    def assert_preserved(self):
        for name in FILES:
            self.assertEqual((self.dest / name).read_bytes(), b"old host list\n")
            self.assertEqual((self.dest / f"{name}.bak").read_bytes(), b"previous backup\n")
        self.assertFalse(self.restarts.exists())
        self.assert_no_staging()

    def assert_no_staging(self):
        self.assertEqual([path for path in self.dest.glob(".opendvm-dstar.*") if path.is_dir()], [])

    def test_first_install_preserves_aliases_callsigns_and_locks(self):
        result = self.update("--no-restart")
        self.assertEqual(result.returncode, 0, result.stderr)
        for name, body in VALID.items():
            path = self.dest / name
            self.assertEqual(path.read_bytes(), body)
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)
            self.assertFalse((self.dest / f"{name}.bak").exists())
            for line in path.read_text().splitlines():
                if line and not line.startswith("#"):
                    ipaddress.IPv4Address(line.split()[1])
        self.assertFalse(self.restarts.exists())
        self.assertIn("XRF055", result.stdout)
        self.assert_no_staging()

    def test_backups_and_single_restart_after_all_replacements(self):
        self.existing()
        result = self.update()
        self.assertEqual(result.returncode, 0, result.stderr)
        for name, body in VALID.items():
            self.assertEqual((self.dest / name).read_bytes(), body)
            self.assertEqual((self.dest / f"{name}.bak").read_bytes(), b"old host list\n")
        self.assertEqual(self.restarts.read_text().splitlines(), ["try-restart ircddbgateway.service"])
        self.assert_no_staging()

    def test_unchanged_skips_restart_and_preserves_backups(self):
        for name, body in VALID.items():
            (self.dest / name).write_bytes(body)
            (self.dest / f"{name}.bak").write_bytes(b"previous backup\n")
        result = self.update()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.restarts.exists())
        for name in FILES:
            self.assertEqual((self.dest / f"{name}.bak").read_bytes(), b"previous backup\n")
        self.assert_no_staging()

    def test_failed_download_of_each_table_preserves_every_active_file(self):
        self.existing()
        for name in FILES:
            with self.subTest(file=name):
                self.server.payloads = dict(VALID, **{name: None})
                result = self.update()
                self.assertNotEqual(result.returncode, 0)
                self.assert_preserved()

    def test_hostname_records_are_rejected_before_any_replacement(self):
        self.existing()
        for name, prefix in FILES.items():
            with self.subTest(file=name):
                self.server.payloads = dict(VALID)
                self.server.payloads[name] += f"{prefix}XYZ\txrf016.ampr.at\n".encode()
                result = self.update()
                self.assertNotEqual(result.returncode, 0)
                self.assert_preserved()

    def test_html_and_short_lists_are_rejected(self):
        self.existing()
        for body in (b"<html>error</html>\n" * 500, table("DCS", 10) + b"# padding\n" * 500):
            with self.subTest(body=body[:30]):
                self.server.payloads["DCS_Hosts.txt"] = body
                result = self.update()
                self.assertNotEqual(result.returncode, 0)
                self.assert_preserved()

    def test_invalid_or_ambiguous_ipv4_addresses_are_rejected(self):
        self.existing()
        for address in ("999.1.2.3", "01.2.3.4", "0.0.0.0", "255.255.255.255", "2001:db8::1"):
            with self.subTest(address=address):
                self.server.payloads["DCS_Hosts.txt"] = VALID["DCS_Hosts.txt"] + f"DCSXYZ\t{address}\n".encode()
                result = self.update()
                self.assertNotEqual(result.returncode, 0)
                self.assert_preserved()

    def test_swapped_protocol_table_is_rejected(self):
        self.existing()
        self.server.payloads["DExtra_Hosts.txt"] = VALID["DCS_Hosts.txt"]
        result = self.update()
        self.assertNotEqual(result.returncode, 0)
        self.assert_preserved()

    def test_crlf_tables(self):
        self.server.payloads = {name: body.replace(b"\n", b"\r\n") for name, body in VALID.items()}
        result = self.update("--no-restart")
        self.assertEqual(result.returncode, 0, result.stderr)
        for name, body in self.server.payloads.items():
            self.assertEqual((self.dest / name).read_bytes(), body)

    def test_concurrent_updates_are_rejected_without_downloading(self):
        self.existing()
        with (self.dest / ".opendvm-dstar.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.update()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.server.calls, [])
        self.assert_preserved()

    def test_restart_failure_is_reported(self):
        self.existing()
        self.env["RESTART_STATUS"] = "1"
        result = self.update()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("restarting ircddbgateway failed", result.stderr)
        for name in FILES:
            self.assertEqual((self.dest / f"{name}.bak").read_bytes(), b"old host list\n")

    def test_custom_per_user_list_is_preserved_and_reported(self):
        home = self.path / "home"
        home.mkdir()
        custom = home / "DExtra_Hosts.txt"
        custom.write_text("XRF999 private.example\n")
        getent = self.bin / "getent"
        getent.write_text('#!/bin/sh\nprintf "mmdvm:x:999:999::%s:/usr/sbin/nologin\\n" "$DSTAR_TEST_HOME"\n')
        getent.chmod(0o755)
        self.env["DSTAR_TEST_HOME"] = str(home)
        result = self.update("--no-restart")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(custom), result.stderr)
        self.assertEqual(custom.read_text(), "XRF999 private.example\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
