"""Use real HTTP downloads and atomic writes; never contact public reflectors."""

import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("nxdn_hosts", ROOT / "update_nxdn_hosts.py")
updater = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(updater)
HOSTS = "# Pi-Star NXDN format\n" + "".join(f"{tg} 192.0.2.1 41400\n" for tg in range(100, 160))
IDS = "RADIO_ID,CALLSIGN,FIRST_NAME\n" + "".join(f"{identity},DL{identity}CI,Test\n" for identity in range(1, 151))


class Handler(BaseHTTPRequestHandler):
    responses = {}

    def do_GET(self):
        body = self.responses.get(self.path)
        self.send_response(200 if body is not None else 404)
        self.end_headers()
        self.wfile.write((body or "Not found").encode())

    def log_message(self, *_):
        pass


class NXDNUpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        Handler.responses = {"/hosts": HOSTS, "/ids": IDS}
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.dest = Path(self.directory.name)
        self.commands = self.dest / "commands"
        binary = self.dest / "bin"
        binary.mkdir()
        fake = binary / "systemctl"
        fake.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$NXDN_TEST_COMMANDS"\nexit 0\n')
        fake.chmod(0o755)
        self.environment = {**os.environ, "PATH": str(binary) + os.pathsep + os.environ["PATH"],
                            "NXDN_TEST_COMMANDS": str(self.commands)}

    def run_update(self, *extra):
        return subprocess.run([sys.executable, str(ROOT / "update_nxdn_hosts.py"),
                               "--hosts-url", self.base + "/hosts", "--ids-url", self.base + "/ids",
                               "--dest-dir", str(self.dest), *extra], env=self.environment,
                              capture_output=True, text=True, timeout=15)

    def files(self):
        return {p.name: p.read_bytes() for p in self.dest.iterdir() if p.suffix in (".csv", ".json", ".bak")}

    def test_first_install_has_numeric_addresses_and_only_try_restarts(self):
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stderr)
        hosts = json.loads((self.dest / "NXDNHosts.json").read_text())["reflectors"]
        self.assertEqual(len(hosts), 60)
        for record in hosts:
            self.assertEqual(ipaddress.ip_address(record["ipv4"]).version, 4)
            self.assertIsNone(record["ipv6"])
        self.assertEqual(self.commands.read_text().strip(), "try-restart nxdngateway.service")
        self.assertEqual((self.dest / "NXDN.csv").stat().st_mode & 0o777, 0o644)

    def test_unchanged_preserves_backup_and_skips_restart(self):
        self.assertEqual(self.run_update().returncode, 0)
        before = self.files()
        commands = self.commands.read_text()
        self.assertEqual(self.run_update().returncode, 0)
        self.assertEqual(self.files(), before)
        self.assertEqual(self.commands.read_text(), commands)

    def test_changed_files_are_backed_up_and_restart_only_once(self):
        self.assertEqual(self.run_update().returncode, 0)
        before = self.files()
        Handler.responses["/hosts"] = HOSTS.replace("192.0.2.1", "192.0.2.2")
        Handler.responses["/ids"] = IDS.replace("DL1CI", "DL1NEW")
        self.assertEqual(self.run_update().returncode, 0)
        self.assertEqual((self.dest / "NXDNHosts.json.bak").read_bytes(), before["NXDNHosts.json"])
        self.assertEqual((self.dest / "NXDN.csv.bak").read_bytes(), before["NXDN.csv"])
        self.assertEqual(len(self.commands.read_text().splitlines()), 2)

    def test_bad_downloads_preserve_every_file_and_backup(self):
        self.assertEqual(self.run_update().returncode, 0)
        before = self.files()
        for endpoint, body in (("/hosts", None), ("/ids", None), ("/hosts", "<html>error</html>"),
                               ("/ids", "<html>error</html>"), ("/hosts", "1 192.0.2.1 41400\n"),
                               ("/ids", "RADIO_ID,CALLSIGN\n1,DL1CI\n")):
            with self.subTest(endpoint=endpoint, body=body):
                Handler.responses = {"/hosts": HOSTS, "/ids": IDS, endpoint: body}
                result = self.run_update()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.files(), before)
        self.assertEqual(len(self.commands.read_text().splitlines()), 1)

    def test_duplicate_and_out_of_range_records_rejected(self):
        for body in (HOSTS + "100 192.0.2.3 41400\n", HOSTS.replace("41400", "65536"),
                     HOSTS.replace("100 ", "65536 ", 1)):
            with self.subTest(body=body[-60:]), self.assertRaises(ValueError):
                updater.parse_hosts(body)
        for body in (IDS + "1,DL1DUPE,Test\n", IDS.replace("1,DL1CI", "65536,DL1CI", 1)):
            with self.assertRaises(ValueError):
                updater.ids_csv(body)

    def test_dns_failures_reuse_existing_numeric_records(self):
        records = updater.parse_hosts(HOSTS.replace("192.0.2.1", "old.invalid"))
        previous = {tg: {"designator": tg, "port": 41400, "ipv4": "192.0.2.9", "ipv6": None}
                    for tg in records}
        result = json.loads(updater.hosts_json(records, previous, resolver=lambda _: None))
        self.assertEqual(len(result["reflectors"]), 60)
        self.assertTrue(all(record["ipv4"] == "192.0.2.9" for record in result["reflectors"]))
        with self.assertRaisesRegex(ValueError, "DNS failures"):
            updater.hosts_json(records, {}, resolver=lambda _: None)

    def test_dns_lookup_has_bounded_timeout(self):
        with patch.object(updater.subprocess, "run", side_effect=subprocess.TimeoutExpired("getent", 3)) as run:
            self.assertIsNone(updater.resolve("old.invalid"))
            self.assertEqual(run.call_args.kwargs["timeout"], 3)

    def test_ipv6_and_private_lists_are_preserved(self):
        private = self.dest / "NXDNHosts.private"
        private.write_text("# my private reflector\n65000 personal.invalid 41400\n")
        Handler.responses["/hosts"] = HOSTS.replace("192.0.2.1", "2001:db8::1")
        result = self.run_update("--no-restart")
        self.assertEqual(result.returncode, 0, result.stderr)
        records = json.loads((self.dest / "NXDNHosts.json").read_text())["reflectors"]
        self.assertTrue(all(record["ipv6"] == "2001:db8::1" and record["ipv4"] is None for record in records))
        self.assertIn("private NXDN", result.stderr)
        self.assertIn("personal.invalid", private.read_text())
        self.assertFalse(self.commands.exists())

    def test_concurrent_update_rejected(self):
        with (self.dest / ".nxdn-update.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertIn("Another NXDN update", self.run_update().stderr)
        self.assertEqual(self.files(), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
