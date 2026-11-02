"""Exercise the real PHP/cURL client against a local HTTPS BM API, never BM itself."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import ssl
import subprocess
import threading

from mode_control_regressions import PASSWORD, STATE


KEY = "bmCiHeader.bmCiPayload.bmCiSignature"


class BMHandler(BaseHTTPRequestHandler):
    pairs = set()
    calls = []
    fail_group = None
    bad_get = None
    ignore_writes = False
    get_field = "talkgroup"

    def reply(self, status, data):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, *_):
        pass

    def do_GET(self):
        type(self).calls.append(("GET", self.path))
        if self.headers.get("Authorization") != "Bearer " + KEY:
            self.reply(403, {"error": "unauthorized"})
        elif not re.fullmatch(r"/v2/device/[0-9]+/talkgroup(?:/dynamic)?", self.path):
            self.reply(404, {"error": "not found"})
        elif self.path.endswith("/dynamic"):
            self.reply(200, [{"slot": 2, "group": 91}])
        elif self.bad_get is not None:
            self.reply(200, self.bad_get)
        else:
            self.reply(200, [{"slot": slot, self.get_field: tg} for slot, tg in sorted(self.pairs)])

    def do_POST(self):
        if self.headers.get("Authorization") != "Bearer " + KEY:
            self.reply(403, {"error": "unauthorized"})
            return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        assert set(body) == {"slot", "group"}
        slot, tg = body["slot"], body["group"]
        assert type(slot) is int and type(tg) is int
        type(self).calls.append(("POST", self.path, slot, tg))
        if tg == self.fail_group:
            self.reply(500, {"error": "failure"})
        else:
            if not self.ignore_writes:
                type(self).pairs.add((slot, tg))
            self.reply(200, {"success": True})

    def do_DELETE(self):
        if self.headers.get("Authorization") != "Bearer " + KEY:
            self.reply(403, {"error": "unauthorized"})
            return
        slot, tg = map(int, self.path.rsplit("/", 2)[1:])
        type(self).calls.append(("DELETE", self.path, slot, tg))
        if not self.ignore_writes:
            type(self).pairs.discard((slot, tg))
        self.reply(204, None)


def check_bm_regressions(api, request, sql, wait_for, start, stop, logs):
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait

    # Trust a disposable certificate for the fixed production hostname. Keep TLS
    # verification and the unmodified production URL/client enabled throughout.
    certificate = Path("/usr/local/share/ca-certificates/opendvm-ci-bm.crt")
    private_key = logs / "bm-ci.key"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=api.brandmeister.network", "-addext", "subjectAltName=DNS:api.brandmeister.network",
                    "-keyout", str(private_key), "-out", str(certificate)], check=True, capture_output=True)
    subprocess.run(["update-ca-certificates"], check=True, capture_output=True)
    with Path("/etc/hosts").open("a") as hosts:
        hosts.write("\n127.0.0.1 api.brandmeister.network\n")
    server = ThreadingHTTPServer(("127.0.0.1", 443), BMHandler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(certificate, private_key)
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Verify DNS, proxy bypass and CA trust as the actual web-service user.
    probe = subprocess.run(['curl', '--silent', '--show-error', '--max-time', '5',
                            '--write-out', '%{http_code}',
                            'https://api.brandmeister.network/v2/device/123456701/talkgroup'],
                           capture_output=True, text=True, user='www-data', group='www-data')
    if probe.returncode or not probe.stdout.endswith('403'):
        raise RuntimeError(f'Local BM HTTPS preflight failed: {probe.stderr}; response: {probe.stdout}')
    host = Path("/etc/MMDVMHost.ini")
    gateway = Path("/etc/dmrgateway")
    host_original, gateway_original = host.read_text(), gateway.read_text()
    host.write_text(host_original.replace("Duplex=0", "Duplex=1", 1))
    gateway.write_text(re.sub(r"(\[DMR Network 1\][\s\S]*?\nId=)[0-9]+", r"\g<1>123456701", gateway_original, count=1))
    commands_before = json.loads(STATE.read_text())["commands"]
    BMHandler.pairs = {(1, 46020), (2, 46001)}
    BMHandler.calls = []

    def control(action, expected=200, **fields):
        raw = request("bm_control.php", {"action": action, "ConfigPassword": PASSWORD, **fields}, expected)
        assert KEY not in raw
        return json.loads(raw)

    def apply(snapshot, groups, expected=200):
        return control("apply", expected, revision=snapshot["revision"], static=json.dumps(groups))

    browser = None
    try:
        request("bm_control.php", expected_status=405)
        request("bm_control.php", {"action": "load", "ConfigPassword": "invalid"}, 401)
        assert BMHandler.calls == []
        assert control("load", 400)["error_code"] == "NO_API_KEY"
        control("save_key", 400, api_key="bad\r\nheader")
        snapshot = control("save_key", api_key=KEY)
        assert snapshot["device"] == {"id": "123456701", "duplex": True}
        assert snapshot["static"] == {"TS0": [], "TS1": [46020], "TS2": [46001]}
        assert KEY not in request("api.php?q=config_inbox")
        assert "BmApiKey" not in api("config_inbox")
        assert int(api("config_inbox")["BmApiKeyConfigured"]) == 1
        assert control("save_key", 401, api_key="wrong.payload.signature")["error_code"] == "BAD_API_KEY"
        assert sql("SELECT bm_api_key FROM mmdvmdb.config_inbox WHERE id=1") == KEY

        # Support both current and legacy group-field spelling, preserving slot 0.
        for field in ("group", "talkgroup", "tg"):
            BMHandler.get_field = field
            assert control("load")["static"]["TS1"] == [46020]
        BMHandler.get_field = "talkgroup"
        assert api("bm_tgs")["dynamic"]["TS2"] == [91]
        assert all("/123456701/" in call[1] for call in BMHandler.calls)

        target = {"TS0": [], "TS1": [91], "TS2": [46001, 46020]}
        snapshot = apply(control("load"), target)
        assert BMHandler.pairs == {(1, 91), (2, 46001), (2, 46020)}
        changes = [call for call in BMHandler.calls if call[0] != "GET"]
        assert [call[0] for call in changes] == ["POST", "POST", "DELETE"]
        assert snapshot["static"] == target
        before = len(changes)
        apply(snapshot, target)
        assert len([call for call in BMHandler.calls if call[0] != "GET"]) == before

        stale = control("load")
        BMHandler.pairs.add((2, 9))
        assert apply(stale, target, 409)["error_code"] == "STALE_STATE"
        for bad in (True, 0, -1, 16777216, "1;reboot", 1.5):
            apply(control("load"), {"TS0": [], "TS1": [bad], "TS2": []}, 400)

        BMHandler.fail_group = 9999
        old = set(BMHandler.pairs)
        apply(control("load"), {"TS0": [], "TS1": [9999], "TS2": []}, 502)
        assert BMHandler.pairs == old  # A rejected add never deletes old bindings.
        BMHandler.fail_group = None
        BMHandler.ignore_writes = True
        assert apply(control("load"), {"TS0": [], "TS1": [92], "TS2": []}, 502)["error_code"] == "NOT_CONFIRMED"
        BMHandler.ignore_writes = False
        for bad in ({}, {"error": "bad response"}, [{"slot": 9, "group": 91}], [{"group": 91}]):
            BMHandler.bad_get = bad
            control("load", 502)
        BMHandler.bad_get = None

        # Single-frequency hotspots use BM slot 0, which must never become TS2.
        host.write_text(host_original.replace("Duplex=1", "Duplex=0", 1))
        BMHandler.pairs = {(0, 91)}
        simplex = control("load")
        assert simplex["static"] == {"TS0": [91], "TS1": [], "TS2": []}
        assert api("bm_tgs")["static"]["TS0"] == [91]
        apply(simplex, {"TS0": [46001], "TS1": [], "TS2": []})
        assert apply(control("load"), {"TS0": [], "TS1": [91], "TS2": []}, 400)["error_code"] == "BAD_SLOT"
        host.write_text(host_original.replace("Duplex=0", "Duplex=1", 1))
        BMHandler.pairs = {(1, 46020), (2, 46001)}

        options = Options()
        for arg in ("--headless", "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--no-proxy-server",
                    "--disable-background-networking", "--window-size=1280,1600",
                    "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1"):
            options.add_argument(arg)
        browser = webdriver.Chrome(service=Service("/usr/bin/chromedriver"), options=options)
        wait = WebDriverWait(browser, 20)
        browser.get("http://127.0.0.1:8080/setup.html")
        wait.until(lambda driver: driver.find_element(By.ID, "saveBtn").is_enabled())
        browser.find_element(By.ID, "ConfigPassword").send_keys(PASSWORD)
        browser.find_element(By.ID, "bmLoad").click()
        wait.until(lambda driver: driver.find_element(By.ID, "bmApply").is_enabled())
        assert "123456701" in browser.find_element(By.ID, "bmDevice").text
        assert browser.find_element(By.ID, "BmApiKey").get_attribute("value") == ""
        for slot, text in ((1, "91"), (2, "46001,46020")):
            element = browser.find_element(By.ID, "BmStatic" + str(slot))
            element.clear()
            element.send_keys(text)
        browser.find_element(By.ID, "bmApply").click()
        wait.until(lambda driver: "BM confirmed" in driver.find_element(By.ID, "bmResult").text)
        assert BMHandler.pairs == {(1, 91), (2, 46001), (2, 46020)}
        browser.find_element(By.ID, "bmManager").screenshot(str(logs / "bm-manager.png"))
        (logs / "bm-manager.html").write_text(browser.page_source)
        BMHandler.fail_group = 9999
        element = browser.find_element(By.ID, "BmStatic1")
        wait.until(lambda driver: element.is_enabled())
        element.clear()
        element.send_keys("9999")
        browser.find_element(By.ID, "bmApply").click()
        wait.until(lambda driver: "failed" in driver.find_element(By.ID, "bmResult").text)
        assert not browser.find_element(By.ID, "bmApply").is_enabled()
        assert (1, 91) in BMHandler.pairs
        BMHandler.fail_group = None
        browser.quit()
        browser = None
        assert json.loads(STATE.read_text())["commands"] == commands_before

        # Ordinary settings saves and a real backend restart preserve the key.
        config = api("config_inbox")
        config["ConfigPassword"] = PASSWORD
        request("save_config.php", config)
        wait_for("station save preserves BM key", lambda: api("config_inbox")["is_new"] == "IDLE")
        assert control("load")["device"]["id"] == "123456701"
        assert sql("SELECT bm_api_key FROM mmdvmdb.config_inbox WHERE id=1") == KEY
        stop("DVconfig")
        start("DVconfig", ["/usr/local/bin/DVconfig"], user="mmdvm")
        wait_for("backend restart preserves BM key", lambda: api("config_inbox")["is_new"] == "BACKEND")
        assert sql("SELECT bm_api_key FROM mmdvmdb.config_inbox WHERE id=1") == KEY
        control("clear_key")
        assert control("load", 400)["error_code"] == "NO_API_KEY"
        print("OK: BM HTTPS API, ESSID, TS1/TS2/slot0, verified changes, stale/failure cases, key privacy and browser controls", flush=True)
    finally:
        if browser: browser.quit()
        sql("UPDATE mmdvmdb.config_inbox SET bm_api_key=NULL WHERE id=1")
        host.write_text(host_original)
        gateway.write_text(gateway_original)
        server.shutdown()
        server.server_close()
        thread.join()
        private_key.unlink(missing_ok=True)
