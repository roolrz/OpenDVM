#!/usr/bin/python3
"""Supervise independent MMDVMHost children under the existing systemd unit."""
import configparser
import json
import os
from pathlib import Path
import signal
import subprocess
import time

import radio_config as radio

BINARY = '/usr/local/bin/MMDVMHost'
# MMDVMHost can spend roughly 15 seconds reading the firmware version before
# it reaches its shutdown check. Let it close the serial port itself.
STOP_TIMEOUT = 20


def event(message):
    print(time.strftime('%Y-%m-%dT%H:%M:%S%z') + ' radio-host: ' + message, flush=True)


def stop_processes(processes):
    processes = list(processes)
    for process in processes:
        if process.poll() is None:
            process.terminate()
    deadline = time.monotonic() + STOP_TIMEOUT
    for process in processes:
        try:
            process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            event(f'pid={getattr(process, "pid", "unknown")} did not stop within {STOP_TIMEOUT}s; sending SIGKILL')
            process.kill()
            process.wait()


def main():
    data = radio.read_config()
    (radio.RUNTIME / 'status.json').unlink(missing_ok=True)
    if not radio.managed(data):
        os.execv(BINARY, [BINARY, str(radio.HOST_CONFIG)])
    radio.RUNTIME.mkdir(parents=True, exist_ok=True)
    children = {}
    retry = {}
    exits = {}
    availability = {}
    stopping = False
    reload_requested = False

    def stop(_signal, _frame):
        nonlocal stopping
        stopping = True
    def reload_config(_signal, _frame):
        nonlocal reload_requested
        reload_requested = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGHUP, reload_config)
    groups = []
    references = sorted(set(data['bindings'].values()) - {''} | set(data['profiles']) | {radio.legacy_device()})
    aliases = None
    try:
        while not stopping:
            if reload_requested:
                reload_requested = False
                try:
                    updated = radio.read_config()
                    # Validate/render before disturbing any running children.
                    radio.plans(updated)
                    references = sorted(set(updated['bindings'].values()) - {''} | set(updated['profiles']) | {radio.legacy_device()})
                    data = updated
                    aliases = None
                    event('reloading configuration; unchanged boards will keep running')
                except (OSError, ValueError, KeyError, configparser.Error) as error:
                    event(f'configuration reload rejected: {error}')
            # Re-resolve aliases after hotplug. In particular /dev/mmdvm and
            # by-id can be absent at boot and later point at the same board.
            current_aliases = tuple(str(Path(device).resolve()) for device in references)
            desired = radio.plans(data) if current_aliases != aliases else groups
            aliases = current_aliases
            old = {group['id']: group['config'] for group in groups}
            new = {group['id']: group['config'] for group in desired}
            devices = {group['id']: group['device'] for group in groups}
            retiring = []
            for key in list(children):
                if old.get(key) != new.get(key) or not radio.available(devices[key]):
                    reason = 'configuration changed' if old.get(key) != new.get(key) else 'USB device missing'
                    event(f'board={devices[key]} stopping: {reason}')
                    retiring.append(children.pop(key))
                    retry.pop(key, None)
            # Signal all retiring hosts first, so their cleanup runs in parallel.
            stop_processes(retiring)
            if stopping:
                break
            for group in desired:
                if old.get(group['id']) != group['config']:
                    radio.atomic_write(radio.RUNTIME / (group['id'] + '.ini'), group['config'], 0o640)
            groups = desired
            modes = {}
            for group in groups:
                key = group['id']
                process = children.get(key)
                present = radio.available(group['device'])
                if availability.get(key) != present:
                    event(f'board={group["device"]} present={present} target={Path(group["device"]).resolve()}')
                    availability[key] = present
                if process and process.poll() is not None:
                    exits[key] = process.poll()
                    event(f'board={group["device"]} pid={getattr(process, "pid", "unknown")} exited code={exits[key]}; retry in 5s')
                    children.pop(key)
                    retry[key] = time.monotonic() + 5
                    process = None
                state = 'stopped' if not group['enabled_modes'] else 'missing' if not present else 'retrying'
                if group['enabled_modes'] and present and process is None and time.monotonic() >= retry.get(key, 0):
                    process = subprocess.Popen([BINARY, str(radio.RUNTIME / (key + '.ini'))])
                    children[key] = process
                    event(f'board={group["device"]} started pid={getattr(process, "pid", "unknown")} modes={",".join(group["enabled_modes"])}')
                if process and present:
                    state = 'active'
                for mode in group['modes']:
                    modes[mode] = {'device': group['device'], 'state': state if mode in group['enabled_modes'] else 'stopped',
                                   'pid': getattr(process, 'pid', None) if mode in group['enabled_modes'] else None,
                                   'last_exit_code': exits.get(key)}
            radio.atomic_write(radio.RUNTIME / 'status.json', json.dumps({
                'updated_at': time.time(), 'supervisor_pid': os.getpid(), 'reload_supported': True, 'modes': modes}))
            time.sleep(1)
    finally:
        stop_processes(children.values())
        (radio.RUNTIME / 'status.json').unlink(missing_ok=True)


if __name__ == '__main__':
    main()
