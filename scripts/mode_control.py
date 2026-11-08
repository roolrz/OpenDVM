#!/usr/bin/python3
"""Privileged, allowlisted mode controls. No caller-supplied commands or paths."""

import configparser
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import radio_config as radio


SYSTEMCTL = "/usr/bin/systemctl"
HOST_CONFIG = Path("/etc/MMDVMHost.ini")
LOCK = Path("/run/opendvm-mode-control.lock")
INITIALIZED = Path("/etc/opendvm/mode-control.initialized")
MODES = {
    "dstar": ("ircddbgateway.service", "D-Star", "D-Star Network"),
    "dmr": ("dmrgateway.service", "DMR", "DMR Network"),
    "ysf": ("ysfgateway.service", "System Fusion", "System Fusion Network"),
    "nxdn": ("nxdngateway.service", "NXDN", "NXDN Network"),
}


def systemctl(*arguments):
    result = subprocess.run([SYSTEMCTL, *arguments], capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Service operation failed")
    return result.stdout


def units():
    text = systemctl("show", "--no-pager", "--property=Id,LoadState,ActiveState,SubState,UnitFileState,MainPID",
                     "mmdvmhost.service", *(item[0] for item in MODES.values()))
    records = {}
    for block in text.strip().split("\n\n"):
        record = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if "Id" in record:
            records[record["Id"]] = record
    for name in ["mmdvmhost.service", *(item[0] for item in MODES.values())]:
        if records.get(name, {}).get("LoadState") != "loaded":
            raise RuntimeError(f"Service is not installed: {name}")
    return records


def radio_flags():
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    with HOST_CONFIG.open() as source:
        parser.read_file(source)
    return {mode: all(parser.get(section, "Enable", fallback="0").strip() == "1"
                      for section in sections[1:]) for mode, sections in MODES.items()}


def write_radio_flags(flags):
    text = HOST_CONFIG.read_text()
    sections = {section: enabled for mode, enabled in flags.items() for section in MODES[mode][1:]}
    found = set()
    enabled_keys = set()
    current = None
    output = []
    for line in text.splitlines(keepends=True):
        header = re.match(r"^\s*\[([^]]+)\]\s*$", line)
        if header:
            if current in sections and current not in enabled_keys:
                output.append(f"Enable={int(sections[current])}\n")
                enabled_keys.add(current)
            current = header[1]
            found.add(current)
        if current in sections and re.match(r"^\s*Enable\s*=", line):
            output.append(f"Enable={int(sections[current])}\n")
            enabled_keys.add(current)
        else:
            output.append(line)
    if current in sections and current not in enabled_keys:
        output.append(f"\nEnable={int(sections[current])}\n")
    for section in sections.keys() - found:
        output.append(f"\n[{section}]\nEnable={int(sections[section])}\n")
    updated = "".join(output)
    if updated == text:
        return
    metadata = HOST_CONFIG.stat()
    descriptor, temporary = tempfile.mkstemp(prefix=".opendvm-modes-", dir=HOST_CONFIG.parent)
    try:
        with os.fdopen(descriptor, "w") as destination:
            destination.write(updated)
            destination.flush()
            os.fsync(destination.fileno())
            os.fchmod(destination.fileno(), stat.S_IMODE(metadata.st_mode))
            os.fchown(destination.fileno(), metadata.st_uid, metadata.st_gid)
        shutil.copy2(HOST_CONFIG, str(HOST_CONFIG) + ".mode-control.bak")
        os.replace(temporary, HOST_CONFIG)
    finally:
        Path(temporary).unlink(missing_ok=True)


def boot_flags(records):
    return {mode: records[item[0]].get("UnitFileState") == "enabled" for mode, item in MODES.items()}


def status():
    records = units()
    flags = radio_flags()
    modem = records["mmdvmhost.service"]["ActiveState"]
    result = {}
    data = radio.read_config()
    bindings = data['bindings']
    managed = radio.managed(data)
    radio_status = {}
    if managed:
        try:
            report = json.loads((radio.RUNTIME / 'status.json').read_text())
            if time.time() - report['updated_at'] < 10:
                radio_status = report['modes']
        except (OSError, ValueError, KeyError):
            pass
    for mode, item in MODES.items():
        record = records[item[0]]
        board = radio_status.get(mode, {})
        board_state = board.get('state', 'starting') if managed and modem == 'active' else modem
        result[mode] = {
            "start_on_boot": record.get("UnitFileState") == "enabled",
            "rf_enabled": flags[mode], "gateway_state": record["ActiveState"],
            "modem_state": board_state,
            "device": board.get('device', bindings[mode] or radio.legacy_device()),
            "modem_pid": board.get('pid'), "modem_last_exit_code": board.get('last_exit_code'),
            "running": flags[mode] and board_state == "active" and record["ActiveState"] == "active",
        }
    return result


def supports_board_reload():
    if not radio.managed(radio.read_config()):
        return False
    host = units()['mmdvmhost.service']
    try:
        report = json.loads((radio.RUNTIME / 'status.json').read_text())
        # A legacy MMDVMHost or an older supervisor must never receive SIGHUP.
        return (host['ActiveState'] == 'active' and report.get('reload_supported') is True
                and int(host.get('MainPID', 0)) > 0
                and report.get('supervisor_pid') == int(host['MainPID']))
    except (OSError, ValueError, KeyError):
        return False


def apply_runtime(flags, isolate=False):
    validate_flags(flags)
    radio.sync_gateway_info()
    write_radio_flags(flags)
    stopped = [MODES[mode][0] for mode, enabled in flags.items() if not enabled]
    if stopped:
        systemctl("--no-block", "stop", *stopped)
    started = [MODES[mode][0] for mode, enabled in flags.items() if enabled]
    if isolate and supports_board_reload():
        systemctl("--no-block", "reload", "mmdvmhost.service")
        if started:
            systemctl("--no-block", "restart", *started)
    else:
        # Full settings apply, initial activation and legacy configurations.
        systemctl("--no-block", "restart", "mmdvmhost.service", *started)


def validate_flags(flags):
    if flags.get("nxdn"):
        config = configparser.ConfigParser(interpolation=None, strict=False)
        config.read(HOST_CONFIG)
        identity = config.get("NXDN", "Id", fallback="0").strip()
        if not identity.isdecimal() or not 1 <= int(identity) <= 65535:
            raise ValueError("Set an NXDN ID (1–65535) and Save & apply before starting NXDN")


def dispatch(arguments):
    if arguments == ['radios']:
        return radio.snapshot()
    if arguments in (['configure-radios'], ['save-radios']):
        data = radio.validate(json.loads(sys.stdin.read(65537)))
        units()
        previous = radio.RADIO_CONFIG.read_text() if radio.RADIO_CONFIG.exists() else None
        radio.atomic_write(radio.RADIO_CONFIG, json.dumps(data, indent=2) + '\n')
        try:
            # The settings form saves boards before the station configuration.
            # Its final `configure` applies both together: restarting here too
            # would interrupt the first set of hosts during modem initialization.
            if arguments == ['configure-radios']:
                apply_runtime(radio_flags())
        except Exception:
            if previous is None:
                radio.RADIO_CONFIG.unlink(missing_ok=True)
            else:
                radio.atomic_write(radio.RADIO_CONFIG, previous)
            radio.sync_gateway_info(force=True)
            raise
        return radio.snapshot()
    if arguments == ["status"]:
        return {"ok": True, "modes": status()}
    if arguments == ["boot-status"]:
        return boot_flags(units())
    if arguments == ["initialize"]:
        if not INITIALIZED.exists():
            records = units()
            flags = radio_flags()
            for mode, enabled in boot_flags(records).items():
                if enabled and not flags[mode]:
                    systemctl("disable", MODES[mode][0])
            INITIALIZED.parent.mkdir(mode=0o750, exist_ok=True)
            INITIALIZED.write_text("initialized\n")
            INITIALIZED.chmod(0o600)
        return {"ok": True}
    if arguments == ["apply-boot"]:
        flags = boot_flags(units())
        # An unconfigured NXDN ID must not prevent the other modes from booting.
        try:
            validate_flags(flags)
        except ValueError:
            flags["nxdn"] = False
        write_radio_flags(flags)
        radio.sync_gateway_info()
        return {"ok": True}
    if len(arguments) == 2 and arguments[0] in ("start", "stop") and arguments[1] in MODES:
        units()
        apply_runtime({arguments[1]: arguments[0] == "start"}, isolate=True)
        return {"ok": True, "modes": status()}
    if len(arguments) == len(MODES) + 1 and arguments[0] == "configure" and all(value in ("0", "1") for value in arguments[1:]):
        flags = dict(zip(MODES, (value == "1" for value in arguments[1:])))
        # Confirm services are available before changing boot policy or RF flags.
        units()
        validate_flags(flags)
        for mode, enabled in flags.items():
            systemctl("enable" if enabled else "disable", MODES[mode][0])
        apply_runtime(flags)
        return {"ok": True, "modes": status()}
    raise ValueError("Invalid mode-control action")


def main():
    try:
        if os.geteuid() != 0:
            raise PermissionError("Mode control must run through the installed sudo rule")
        with LOCK.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = dispatch(sys.argv[1:])
        print(json.dumps(result))
    except Exception as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
