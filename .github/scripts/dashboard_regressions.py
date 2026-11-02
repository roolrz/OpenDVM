"""End-to-end regressions for reflector classification and overlapping DMR slots."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import subprocess


def check_dashboard_regressions(api, sql, wait_for, logfile, restart_monitor):
    timestamp = datetime.now(timezone.utc) + timedelta(minutes=1)

    def append(*messages):
        nonlocal timestamp
        with logfile.open("a") as output:
            for message in messages:
                output.write(f"M: {timestamp.strftime('%Y-%m-%d %H:%M:%S.000')} {message}\n")
                timestamp += timedelta(seconds=1)
        size = logfile.stat().st_size

        def consumed():
            for line in Path("/tmp/logparse.offsets").read_text().splitlines():
                parts = line.split("\t")
                if len(parts) == 3 and parts[0] == str(logfile) and int(parts[2]) >= size:
                    return True
            return False

        wait_for("fixture logs consumed", consumed)

    # Reproduce the user's exact English slow-data message, including padding.
    append('D-Star, network slow data text = "Linked to XRF055 C  "')
    if api("reflector")["dstar"] != "XRF055 C" or api("reflector")["fusion"] != "-----":
        raise RuntimeError("English D-Star announcement was misclassified as Fusion")
    append('D-Star, network slow data text = "Verlinkt zu DCS001 R"')
    if api("reflector")["dstar"] != "DCS001 R":
        raise RuntimeError("German D-Star announcement is no longer supported")
    append('D-Star, network slow data text = "One life, live it!  "',
           'D-Star, RF slow data text = "Linked to XRF999 A  "')
    if api("reflector")["dstar"] != "DCS001 R" or api("reflector")["fusion"] != "-----":
        raise RuntimeError("User slow data changed a reflector")
    append('D-Star link status set to "Not linked          "')
    if api("reflector")["dstar"] not in ("", "-----"):
        raise RuntimeError("D-Star disconnect did not clear the reflector")
    append('D-Star link status set to "Linked to XRF055 C  "')

    # Simulate the stale quoted Fusion value in an already-installed database.
    sql("UPDATE mmdvmdb.reflector SET dstar='DCS001 R', fusion='XRF055 C  \"' WHERE id=1")
    restart_monitor()
    wait_for("D-Star restored from existing logs", lambda: api("reflector")["dstar"] == "XRF055 C")
    if api("reflector")["fusion"] != "-----":
        raise RuntimeError("Upgrade retained the old misclassified Fusion reflector")
    append("Linked to CI-Fusion")
    if api("reflector")["fusion"] != "CI-Fusion" or api("reflector")["dstar"] != "XRF055 C":
        raise RuntimeError("Genuine Fusion link overwrote D-Star")
    print("OK: English/German D-Star, disconnect, log replay and Fusion isolation", flush=True)

    def slots():
        return {int(slot["slot"]): slot for slot in api("status")["slots"]}

    before = int(sql("SELECT COUNT(*) FROM mmdvmdb.lastheard"))
    append("DMR Slot 1, received network voice header from DL6CI to TG 46020",
           "DMR Slot 2, received network voice header from DL7CI to TG 46001")
    current = slots()
    if not all(current[number]["active"] for number in (1, 2)):
        raise RuntimeError("Concurrent DMR slots are not both active")
    if int(sql("SELECT COUNT(*) FROM mmdvmdb.lastheard")) != before:
        raise RuntimeError("Starting TS2 prematurely ended TS1")

    # Render the actual dashboard in Chromium, rather than testing a copy of its JS.
    # Prevent the initial BrandMeister widget from calling an external network.
    sql("UPDATE mmdvmdb.config_inbox SET bm_api_key='' WHERE id=1")
    browser_log = Path("/tmp/opendvm-ci/chromium.log")
    browser = [
        "chromium", "--headless", "--no-sandbox", "--disable-gpu",
        "--disable-dev-shm-usage", "--disable-background-networking", "--no-proxy-server",
        "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
        "--virtual-time-budget=5000",
    ]
    with browser_log.open("w") as errors:
        html = subprocess.check_output([
            *browser, "--dump-dom", "http://127.0.0.1:8080/",
        ], text=True, stderr=errors, timeout=45)
        subprocess.run([
            *browser, "--screenshot=/tmp/opendvm-ci/dashboard.png", "--window-size=1440,1000",
            "http://127.0.0.1:8080/",
        ], stderr=errors, check=True, timeout=45)
    Path("/tmp/opendvm-ci/dashboard.html").write_text(html)
    for number, callsign, tg in ((1, "DL6CI", "46020"), (2, "DL7CI", "46001")):
        card = re.search(rf'<article id="dmr-slot-{number}"([^>]*)>(.*?)</article>', html, re.S)
        if not card or "active" not in card[1] or callsign not in card[2] or f"TG{tg}" not in card[2]:
            raise RuntimeError(f"Dashboard did not display TS{number}, {callsign} and TG{tg}")
    print("OK: Chromium displays both DMR calls and talkgroups", flush=True)

    append("DMR Slot 1, received network end of voice transmission from DL6CI to TG 46020, 3.0 seconds, BER: 0.1%")
    current = slots()
    if current[1]["active"] or not current[2]["active"] or api("status")["callsign"] != "DL7CI":
        raise RuntimeError("Ending TS1 hid the active TS2 call")
    append("DMR Slot 2, received network end of voice transmission from DL8CI to TG 999, 1.0 seconds, BER: 0.0%")
    if not slots()[2]["active"]:
        raise RuntimeError("An unrelated end event closed the active call")
    append("DMR Slot 2, received network end of voice transmission from DL7CI to TG 46001, 5.0 seconds, BER: 0.2%")
    if any(slot["active"] for slot in slots().values()):
        raise RuntimeError("DMR slots did not become inactive after both calls ended")
    if sql("SELECT COUNT(*) FROM mmdvmdb.lastheard WHERE "
           "(callsign='DL6CI' AND slot=1 AND dgid=46020 AND duration=3) OR "
           "(callsign='DL7CI' AND slot=2 AND dgid=46001 AND duration=5)") != "2":
        raise RuntimeError("Interleaved DMR call history lost its slot, talkgroup or duration")
    if int(sql("SELECT COUNT(*) FROM mmdvmdb.lastheard")) != before + 2:
        raise RuntimeError("Interleaved DMR calls created spurious history entries")

    append("DMR Slot 1, received RF voice header from DL8CI to TG 91",
           "DMR Slot 2, received RF voice header from DL9CI to TG 92",
           "Mode set to Idle")
    if any(slot["active"] for slot in slots().values()) or api("status")["mode"] != "Idle":
        raise RuntimeError("Idle did not close both DMR slots")
    print("OK: DMR slot independence, late ends, history and Idle", flush=True)
