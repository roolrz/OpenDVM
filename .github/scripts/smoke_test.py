"""Exercise real services in the disposable Debian CI container (no RF hardware)."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from dashboard_regressions import check_dashboard_regressions
from mode_control_regressions import PASSWORD, prepare_mode_controls, check_mode_controls
from nxdn_regressions import check_nxdn_regressions
from bm_regressions import check_bm_regressions


ROOT = Path(__file__).resolve().parents[2]
LOGS = Path("/tmp/opendvm-ci")
PROCESSES = []


def start(name, command, user=None):
    output = (LOGS / f"{name}.log").open("w")
    try:
        process = subprocess.Popen(
            command, cwd="/tmp", stdout=output, stderr=subprocess.STDOUT,
            user=user, group=user,
        )
    finally:
        output.close()
    PROCESSES.append((name, process))
    return process


def stop(name):
    entry = next(entry for entry in PROCESSES if entry[0] == name)
    entry[1].terminate()
    entry[1].wait(timeout=10)
    PROCESSES.remove(entry)


def wait_for(description, check, timeout=20):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        for name, process in PROCESSES:
            if process.poll() is not None:
                raise RuntimeError(f"{name} exited with status {process.returncode}")
        try:
            if check():
                print(f"OK: {description}", flush=True)
                return
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            last_error = error
        time.sleep(0.2)
    raise RuntimeError(f"Timed out waiting for {description}; last error: {last_error}")


def sql(statement):
    return subprocess.check_output([
        "mariadb", "--protocol=socket", "--user=root", "--batch",
        "--skip-column-names", "--execute", statement,
    ], text=True, stderr=subprocess.PIPE, timeout=5).strip()


def request(path, data=None, expected_status=200):
    encoded = None if data is None else urllib.parse.urlencode(data).encode()
    try:
        response = urllib.request.urlopen(
            f"http://127.0.0.1:8080/{path}", data=encoded, timeout=5
        )
    except urllib.error.HTTPError as error:
        response = error
    with response:
        body = response.read().decode()
        if response.status != expected_status:
            raise RuntimeError(f"{path}: HTTP {response.status}: {body}")
        return body


def api(query):
    payload = json.loads(request(f"api.php?q={query}"))
    if isinstance(payload, dict) and "error" in payload:
        raise RuntimeError(f"API {query}: {payload}")
    return payload


def broker_ready():
    with socket.create_connection(("127.0.0.1", 1883), timeout=1):
        return True


def main():
    if os.geteuid() != 0 or not Path("/.dockerenv").exists():
        raise RuntimeError("Run this test as root inside a disposable Docker container.")
    LOGS.mkdir(exist_ok=True)
    # The HTTPS BM fixture is local even when the runner provides proxy variables.
    os.environ['NO_PROXY'] = '127.0.0.1,localhost,api.brandmeister.network'
    os.environ['no_proxy'] = os.environ['NO_PROXY']
    try:
        subprocess.run(["install", "-d", "-o", "mysql", "-g", "mysql", "/run/mysqld"], check=True)
        start("mariadb", ["mariadbd", "--user=mysql", "--skip-networking"])
        wait_for("MariaDB socket", lambda: sql("SELECT 1") == "1")

        # Use the production users, directories, socket authentication and grants.
        subprocess.run([
            "bash", "-c",
            "source ./install.sh; ensure_user_mmdvm; ensure_dirs; setup_mariadb",
        ], cwd=ROOT, check=True)
        for sample, target in (
            ("MMDVMHost.ini.sample", "MMDVMHost.ini"),
            ("ircddbgateway.sample", "ircddbgateway"),
            ("dmrgateway.sample", "dmrgateway"),
            ("ysfgateway.sample", "ysfgateway"),
            ("nxdngateway.sample", "nxdngateway"),
        ):
            destination = Path("/etc") / target
            shutil.copyfile(ROOT / "configs" / sample, destination)
            shutil.chown(destination, group="mmdvm")
            destination.chmod(0o664)
        if "language=0" not in Path("/etc/ircddbgateway").read_text().splitlines():
            raise RuntimeError("D-Star announcements must default to English")
        prepare_mode_controls(ROOT)

        # Keep MQTT traffic local even though the application uses a fixed hostname.
        with Path("/etc/hosts").open("a") as hosts:
            hosts.write("\n127.0.0.1 mqtt.fm-funknetz.de\n")
        start("mqtt", ["mosquitto", "-p", "1883"])
        wait_for("local MQTT broker", broker_ready)
        start("DVconfig", ["/usr/local/bin/DVconfig"], user="mmdvm")
        wait_for("configuration imported", lambda: sql(
            "SELECT COUNT(*) FROM mmdvmdb.config_inbox WHERE id=1 AND callsign <> ''"
        ) == "1")

        logfile = LOGS / "MMDVM-test.log"
        logfile.touch()
        gateway_log = LOGS / "NXDNGateway.log"
        gateway_log.touch()
        start("mmdvm-status", ["/usr/local/bin/mmdvm-status", str(logfile), str(gateway_log)], user="mmdvm")
        offsets = Path("/tmp/logparse.offsets")
        wait_for("log monitor ready", lambda: offsets.exists() and str(logfile) in offsets.read_text())
        messages = [
            "D-Star, received RF header from DL1CI",
            "D-Star, received RF end of transmission from DL1CI, 2.0 seconds, BER: 0.1%",
            "YSF, received RF header from DL2CI to DG-ID 10",
            "YSF, received RF end of transmission from DL2CI to DG-ID 10, 2.0 seconds, BER: 0.2%",
            "DMR Slot 2, received RF voice header from DL3CI to TG 91",
            "DMR Slot 2, received RF end of voice transmission from DL3CI to TG 91, 2.0 seconds, BER: 0.3%",
        ]
        now = datetime.now(timezone.utc)
        with logfile.open("a") as output:
            for index, message in enumerate(messages):
                timestamp = (now + timedelta(seconds=index)).strftime("%Y-%m-%d %H:%M:%S.000")
                output.write(f"M: {timestamp} {message}\n")
        wait_for("D-Star, YSF and DMR logs persisted", lambda: sql(
            "SELECT COUNT(DISTINCT mode) FROM mmdvmdb.lastheard "
            "WHERE callsign IN ('DL1CI', 'DL2CI', 'DL3CI') AND source='RF' AND duration=2"
        ) == "3")

        # Retained delivery also covers the initial subscription without a timing race.
        subprocess.run([
            "mosquitto_pub", "-h", "127.0.0.1", "-t", "/server/statethr/1/ci", "-r",
            "-m", json.dumps({"time": now.strftime("%H:%M:%S"), "talk": "start",
                              "call": "DL4CI", "tg": "91", "server": "CI"}),
        ], check=True, timeout=5)
        wait_for("MQTT message persisted", lambda: sql(
            "SELECT COUNT(*) FROM mmdvmdb.fmlastheard WHERE callsign='DL4CI' AND tg=91"
        ) == "1")

        start("php", ["php", "-S", "127.0.0.1:8080", "-t", str(ROOT / "gui/html")], user="www-data")
        wait_for("PHP HTTP server", lambda: "OpenDVM" in request("index.html"))
        for query in (
            "status", "lastheard", "reflector", "localconfig", "config_inbox",
            "activity48h", "activityByMode48h", "activityByMode48hSplit",
            "heatmap30d", "avgDurationByMode", "callsignTop10Count",
            "callsignTop10Duration", "hallOfFame",
        ):
            api(query)
            print(f"OK: API {query}", flush=True)
        if len(api("lastheard")) != 3 or api("status")["callsign"] != "DL3CI":
            raise RuntimeError("HTTP API did not return the parsed transmissions")
        if "OpenDVM" not in request("setup.html"):
            raise RuntimeError("Setup page is unavailable")
        for endpoint in ("save_config.php", "ysf_command.php"):
            payload = json.loads(request(endpoint, {"ConfigPassword": "invalid"}, 401))
            if payload.get("ok") is not False:
                raise RuntimeError(f"{endpoint} did not reject invalid credentials")

        def restart_monitor():
            stop("mmdvm-status")
            start("mmdvm-status", ["/usr/local/bin/mmdvm-status", str(logfile), str(gateway_log)], user="mmdvm")

        check_dashboard_regressions(api, sql, wait_for, logfile, restart_monitor)
        check_mode_controls(api, request, wait_for, LOGS)
        check_nxdn_regressions(api, wait_for, logfile, start, stop, LOGS)
        check_bm_regressions(api, request, sql, wait_for, start, stop, LOGS)

        # Exercise a valid form submission and its real database schema after stopping
        # the consumer; a queued request must survive restarting the daemon.
        stop("DVconfig")
        config = api("config_inbox")
        config["ConfigPassword"] = PASSWORD
        config["Callsign"] = "DL5CI"
        result = json.loads(request("save_config.php", config))
        if result.get("ok") is not True or sql(
            "SELECT callsign FROM mmdvmdb.config_inbox WHERE id=1 AND is_new='GUI'"
        ) != "DL5CI":
            raise RuntimeError(f"Configuration submission failed: {result}")
        request("save_config.php", config, 409)
        request("mode_control.php", {"action": "stop", "mode": "dmr", "ConfigPassword": PASSWORD}, 409)
        start("DVconfig", ["/usr/local/bin/DVconfig"], user="mmdvm")
        wait_for("queued settings survive daemon restart and apply", lambda: sql(
            "SELECT callsign FROM mmdvmdb.config_inbox WHERE id=1 AND is_new='IDLE'"
        ) == "DL5CI")
        print("All smoke tests passed.", flush=True)
    finally:
        for _, process in reversed(PROCESSES):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        for path in sorted(LOGS.glob("*.log")):
            print(f"\n--- {path.name} ---\n{path.read_text(errors='replace')}", flush=True)


if __name__ == "__main__":
    main()
