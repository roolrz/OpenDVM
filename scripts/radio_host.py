#!/usr/bin/python3
"""Supervise independent MMDVMHost children under the existing systemd unit."""
import json
import os
from pathlib import Path
import signal
import subprocess
import time

import radio_config as radio

BINARY = '/usr/local/bin/MMDVMHost'


def main():
    data = radio.read_config()
    (radio.RUNTIME / 'status.json').unlink(missing_ok=True)
    if not any(data['bindings'].values()):
        os.execv(BINARY, [BINARY, str(radio.HOST_CONFIG)])
    radio.RUNTIME.mkdir(parents=True, exist_ok=True)
    children = {}
    retry = {}
    stopping = False

    def stop(_signal, _frame):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    groups = []
    references = sorted(set(data['bindings'].values()) - {''} | {radio.legacy_device()})
    aliases = None
    try:
        while not stopping:
            # Re-resolve aliases after hotplug. In particular /dev/mmdvm and
            # by-id can be absent at boot and later point at the same board.
            current_aliases = tuple(str(Path(device).resolve()) for device in references)
            desired = radio.plans(data) if current_aliases != aliases else groups
            aliases = current_aliases
            old = {group['id']: group['config'] for group in groups}
            new = {group['id']: group['config'] for group in desired}
            for key in list(children):
                if old.get(key) != new.get(key):
                    process = children.pop(key)
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
            for group in desired:
                if old.get(group['id']) != group['config']:
                    radio.atomic_write(radio.RUNTIME / (group['id'] + '.ini'), group['config'], 0o640)
            groups = desired
            modes = {}
            for group in groups:
                key = group['id']
                process = children.get(key)
                present = radio.available(group['device'])
                if process and process.poll() is not None:
                    children.pop(key)
                    retry[key] = time.monotonic() + 5
                    process = None
                if process and not present:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    children.pop(key)
                    process = None
                state = 'stopped' if not group['enabled_modes'] else 'missing' if not present else 'retrying'
                if group['enabled_modes'] and present and process is None and time.monotonic() >= retry.get(key, 0):
                    process = subprocess.Popen([BINARY, str(radio.RUNTIME / (key + '.ini'))])
                    children[key] = process
                if process:
                    state = 'active'
                for mode in group['modes']:
                    modes[mode] = {'device': group['device'], 'state': state if mode in group['enabled_modes'] else 'stopped'}
            radio.atomic_write(radio.RUNTIME / 'status.json', json.dumps({'updated_at': time.time(), 'modes': modes}))
            time.sleep(1)
    finally:
        for process in children.values():
            if process.poll() is None:
                process.terminate()
        deadline = time.monotonic() + 5
        for process in children.values():
            try:
                process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        (radio.RUNTIME / 'status.json').unlink(missing_ok=True)


if __name__ == '__main__':
    main()
