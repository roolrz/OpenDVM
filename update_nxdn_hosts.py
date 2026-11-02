#!/usr/bin/python3
"""Refresh NXDN reflectors and IDs without putting DNS on the gateway boot path."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
import fcntl
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


HOSTS_URL = "https://www.pistar.uk/downloads/NXDN_Hosts.txt"
IDS_URL = "https://database.radioid.net/static/nxdn.csv"
MIN_HOSTS = 50
MIN_IDS = 100


def download(url):
    result = subprocess.run(["curl", "--fail", "--silent", "--show-error", "--location",
                             "--connect-timeout", "10", "--max-time", "30", "--max-filesize", "10485760",
                             url], capture_output=True, timeout=35)
    if result.returncode:
        raise ValueError(f"Download failed: {url}: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout.decode("utf-8-sig")


def address(value):
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def parse_hosts(text):
    records = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        fields = line.split()
        if len(fields) != 3:
            raise ValueError("Invalid NXDN host record")
        tg, host, port = fields
        if not tg.isdecimal() or not 1 <= int(tg) <= 65535:
            raise ValueError("Invalid NXDN talkgroup")
        if not port.isdecimal() or not 1 <= int(port) <= 65535:
            raise ValueError("Invalid NXDN port")
        if not address(host) and (len(host) > 253 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host)):
            raise ValueError("Invalid NXDN hostname")
        if int(tg) in records:
            raise ValueError(f"Duplicate NXDN talkgroup: {tg}")
        records[int(tg)] = (host, int(port))
    if len(records) < MIN_HOSTS:
        raise ValueError(f"Too few NXDN reflectors: {len(records)} (need {MIN_HOSTS})")
    return records


def resolve(host):
    numeric = address(host)
    if numeric:
        return str(numeric)
    # Bound each DNS lookup in its own process; gateway startup only sees IPs.
    try:
        result = subprocess.run(["getent", "ahostsv4", host], capture_output=True, text=True, timeout=3)
        for line in result.stdout.splitlines():
            numeric = address(line.split()[0])
            if numeric and numeric.version == 4:
                return str(numeric)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def old_hosts(path):
    try:
        records = json.loads(path.read_text())["reflectors"]
        return {record["designator"]: record for record in records
                if type(record["designator"]) is int and 1 <= record["designator"] <= 65535
                and type(record["port"]) is int and 1 <= record["port"] <= 65535
                and all(record.get(key) is None or (address(record[key]) is not None
                        and address(record[key]).version == version)
                        for key, version in (("ipv4", 4), ("ipv6", 6)))
                and (record.get("ipv4") or record.get("ipv6"))}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def hosts_json(records, previous, resolver=resolve):
    names = sorted({host for host, _ in records.values()})
    with ThreadPoolExecutor(max_workers=16) as pool:
        resolved = dict(zip(names, pool.map(resolver, names)))
    output = []
    failed = []
    reused = 0
    for tg, (host, port) in sorted(records.items()):
        ip = address(resolved[host]) if resolved[host] else None
        if ip:
            output.append({"designator": tg, "port": port,
                           "ipv4": str(ip) if ip.version == 4 else None,
                           "ipv6": str(ip) if ip.version == 6 else None})
        elif tg in previous:
            output.append(previous[tg])
            reused += 1
        else:
            failed.append(tg)
    if len(output) < MIN_HOSTS or len(failed) > len(records) // 5:
        raise ValueError("Too many NXDN DNS failures; keeping the existing lists")
    print(f"NXDN reflectors: {len(output)}; retained previous addresses: {reused}; unresolved: {len(failed)}")
    return json.dumps({"reflectors": output}, sort_keys=True, indent=2) + "\n"


def ids_csv(text):
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or not {"RADIO_ID", "CALLSIGN"}.issubset(reader.fieldnames):
        raise ValueError("Invalid NXDN ID CSV header")
    records = {}
    for row in reader:
        identity = (row.get("RADIO_ID") or "").strip()
        callsign = (row.get("CALLSIGN") or "").strip().upper()
        if not identity.isdecimal() or not 1 <= int(identity) <= 65535:
            raise ValueError("Invalid NXDN radio ID")
        if not re.fullmatch(r"[A-Z0-9/-]{1,20}", callsign):
            raise ValueError("Invalid NXDN ID callsign")
        if int(identity) in records:
            raise ValueError("Duplicate NXDN radio ID")
        records[int(identity)] = callsign
    if len(records) < MIN_IDS:
        raise ValueError(f"Too few NXDN IDs: {len(records)}")
    # Only the first two columns are read by MMDVMHost/NXDNGateway. Normalize
    # away names and addresses so unrelated metadata changes do not restart it.
    return "RADIO_ID,CALLSIGN\n" + "".join(f"{key},{value}\n" for key, value in sorted(records.items()))


def replace_file(path, content):
    if path.exists() and path.read_text() == content:
        return False
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
            os.fchmod(output.fileno(), 0o644)
        if path.exists():
            # Stage the backup too, so an interrupted copy cannot destroy it.
            backup = Path(name + ".bak")
            try:
                shutil.copy2(path, backup)
                os.replace(backup, str(path) + ".bak")
            finally:
                backup.unlink(missing_ok=True)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
    return True


def update(dest, hosts_url, ids_url, no_restart):
    dest.mkdir(parents=True, exist_ok=True)
    with (dest / ".nxdn-update.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("Another NXDN update is running") from error
        # Validate both downloads before replacing either active file.
        records = parse_hosts(download(hosts_url))
        identities = ids_csv(download(ids_url))
        hosts = hosts_json(records, old_hosts(dest / "NXDNHosts.json"))
        changed_hosts = replace_file(dest / "NXDNHosts.json", hosts)
        changed_ids = replace_file(dest / "NXDN.csv", identities)
        private = dest / "NXDNHosts.private"
        if private.exists():
            for line in private.read_text().splitlines():
                fields = line.split("#", 1)[0].split()
                if len(fields) >= 2 and not address(fields[1]):
                    print("[WARN] A private NXDN entry contains a hostname; it may delay gateway startup", file=sys.stderr)
                    break
        if (changed_hosts or changed_ids) and not no_restart:
            subprocess.run(["systemctl", "try-restart", "nxdngateway.service"], check=True, timeout=30)
        print("NXDN host/ID lists updated" if changed_hosts or changed_ids else "NXDN lists unchanged; restart skipped")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hosts-url", default=HOSTS_URL)
    parser.add_argument("--ids-url", default=IDS_URL)
    parser.add_argument("--dest-dir", type=Path, default=Path("/usr/local/etc"))
    parser.add_argument("--no-restart", action="store_true")
    args = parser.parse_args()
    try:
        update(args.dest_dir, args.hosts_url, args.ids_url, args.no_restart)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"[ERROR] {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
