"""Run the real NXDN gateway against local UDP peers, then verify dashboard logs."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import socket
import subprocess


def check_nxdn_regressions(api, wait_for, logfile, start, stop, logs):
    # No radio or public reflector is contacted. These peers speak the actual
    # NXDN reflector poll/data protocol and MMDVMHost's Icom UDP interface.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as first, \
            socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as second, \
            socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as modem:
        for peer in (first, second):
            peer.bind(("127.0.0.1", 0))
            peer.setblocking(False)
        modem.bind(("127.0.0.1", 14021))
        modem.settimeout(5)
        Path("/usr/local/etc/NXDNHosts.json").write_text(json.dumps({"reflectors": [
            {"designator": tg, "port": peer.getsockname()[1], "ipv4": "127.0.0.1", "ipv6": None}
            for tg, peer in ((10200, first), (65000, second))]}))
        Path("/usr/local/etc/NXDN.csv").write_text("RADIO_ID,CALLSIGN\n12345,DL9CI\n")
        start("NXDNGateway", ["/usr/local/bin/NXDNGateway", "/etc/nxdngateway"], user="mmdvm")
        seen = set()

        def polls():
            for peer in (first, second):
                try:
                    packet, _ = peer.recvfrom(2048)
                    if len(packet) == 17 and packet[:5] == b"NXDNP":
                        seen.add(int.from_bytes(packet[15:17], "big"))
                except BlockingIOError:
                    pass
            return seen == {10200, 65000}

        wait_for("NXDNGateway starts and polls both static TGs", polls)
        before = api("reflector")
        packet = bytearray(43)
        packet[:5] = b"NXDND"
        packet[7:9] = (10200).to_bytes(2, "big")
        packet[9] = 1
        first.sendto(packet, ("127.0.0.1", 14050))
        forwarded, _ = modem.recvfrom(2048)
        assert len(forwarded) == 102 and forwarded[:4] == b"ICOM"
        wait_for("real NXDN gateway link log reaches API", lambda: api("reflector")["nxdn"] == "TG 10200")
        after = api("reflector")
        assert after["dstar"] == before["dstar"] and after["fusion"] == before["fusion"]
        assert after["nxdn_static"] == "10200,65000"
        stop("NXDNGateway")
        wait_for("stopping NXDN gateway clears its selected reflector", lambda: api("reflector")["nxdn"] in ("", "-----"))

    timestamp = datetime.now(timezone.utc) + timedelta(minutes=2)

    def append(*messages):
        nonlocal timestamp
        with logfile.open("a") as output:
            for message in messages:
                output.write(f"M: {timestamp.strftime('%Y-%m-%d %H:%M:%S.000')} {message}\n")
                timestamp += timedelta(seconds=1)
        size = logfile.stat().st_size
        wait_for("NXDN fixture logs consumed", lambda: any(
            len(parts) == 3 and parts[0] == str(logfile) and int(parts[2]) >= size
            for parts in (line.split("\t") for line in Path("/tmp/logparse.offsets").read_text().splitlines())))

    append("NXDN, received RF header from DL9CI to TG 10200",
           "NXDN, received RF end of transmission from DL9CI to TG 10200, 2.4 seconds, BER: 0.2%",
           "NXDN, received network transmission from DL0CI to TG 65000",
           "NXDN, received network end of transmission from DL0CI to TG 65000, 3.2 seconds")
    entries = [row for row in api("lastheard") if row["mode"] == "NXDN"]
    assert any(row["callsign"] == "DL9CI" and row["source"] == "RF" and float(row["duration"]) == 2.4 for row in entries)
    assert any(row["callsign"] == "DL0CI" and row["source"] == "NET" and row["ber"] is None for row in entries)
    append("NXDN, received RF late entry from 12346 to TG 10200",
           "NXDN, transmission lost from 12346 to TG 10200, 1.6 seconds, BER: 0.4%",
           "NXDN, received network transmission from DL0CI to TG 65000",
           "NXDN, network watchdog has expired, 1.8 seconds")
    assert any(row["callsign"] == "12346" for row in api("lastheard"))
    assert not api("status")["active"]
    activity = api("activityByMode48hSplit")["nxdn"]
    assert activity["RF"] >= 2 and activity["NET"] >= 2
    before = api("reflector")
    append("Switched to reflector 999 due to network activity", "Unlinking from 999 due to inactivity")
    assert api("reflector")["nxdn"] == before["nxdn"]  # Other logs cannot change the NXDN link.
    append("NXDN, received RF header from DL9CI to TG 10200")
    current = api("status")
    assert current["mode"] == "NXDN" and current["active"] and int(current["dgid"]) == 10200
    command = ["chromium", "--headless", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
               "--disable-background-networking", "--no-proxy-server",
               "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
               "--virtual-time-budget=5000", "--window-size=1280,1200", "--dump-dom",
               "http://127.0.0.1:8080/index.html"]
    with (logs / "nxdn-dashboard.html").open("w") as output, (logs / "nxdn-chromium.log").open("w") as errors:
        subprocess.run(command, stdout=output, stderr=errors, check=True, timeout=30)
    html = (logs / "nxdn-dashboard.html").read_text()
    assert 'id="dgid"' in html and '>TG10200<' in html and '>DL9CI<' in html
    assert 'Static TGs: 10200,65000' in html
    append("NXDN, received RF end of transmission from DL9CI to TG 10200, 2.0 seconds, BER: 0.0%")
    print("OK: real NXDN UDP gateway, separate reflector status, RF/NET/lost/watchdog logs and dashboard", flush=True)
