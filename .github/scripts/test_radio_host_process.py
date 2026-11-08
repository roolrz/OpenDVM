"""Exercise supervisor reloads with real child processes and temporary devices."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import mode_control
import radio_config as radio


class SupervisorProcessTests(unittest.TestCase):
    def test_mode_reload_and_child_failure_preserve_other_board_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            host = root / 'MMDVMHost.ini'
            host.write_text((ROOT / 'configs/MMDVMHost.ini.sample').read_text())
            config = root / 'radios.json'
            runtime = root / 'runtime'
            devices = []
            for name in ('dstar', 'dmr', 'ysf'):
                device = root / name
                device.symlink_to(os.devnull)
                devices.append(str(device))
            data = radio.empty_config()
            data['bindings'] = dict(zip(('dstar', 'dmr', 'ysf', 'nxdn'), [*devices, devices[2]]))
            config.write_text(json.dumps(data))
            fake_host = root / 'fake-host'
            fake_host.write_text(f'#!{sys.executable}\nimport time\nwhile True: time.sleep(1)\n')
            fake_host.chmod(0o755)
            runner = root / 'supervisor.py'
            runner.write_text(
                f'import sys\nsys.path.insert(0, {str(ROOT / "scripts")!r})\n'
                'from pathlib import Path\nimport radio_config as radio\nimport radio_host\n'
                f'radio.HOST_CONFIG=Path({str(host)!r})\n'
                f'radio.RADIO_CONFIG=Path({str(config)!r})\n'
                f'radio.RUNTIME=Path({str(runtime)!r})\n'
                f'radio_host.BINARY={str(fake_host)!r}\nradio_host.main()\n')
            with patch.object(mode_control, 'HOST_CONFIG', host), (root / 'events.log').open('w+') as log:
                mode_control.write_radio_flags({'dstar': True, 'dmr': True, 'ysf': False, 'nxdn': False})
                supervisor = subprocess.Popen([sys.executable, str(runner)], stdout=log, stderr=log)

                def wait_for(predicate):
                    deadline = time.monotonic() + 12
                    while time.monotonic() < deadline:
                        try:
                            report = json.loads((runtime / 'status.json').read_text())
                            if predicate(report['modes']):
                                return report
                        except (OSError, ValueError):
                            pass
                        if supervisor.poll() is not None:
                            break
                        time.sleep(0.05)
                    log.flush()
                    self.fail('Supervisor did not reach expected state:\n' + (root / 'events.log').read_text())

                def reload_flags(enabled):
                    mode_control.write_radio_flags({'ysf': enabled})
                    supervisor.send_signal(signal.SIGHUP)

                try:
                    initial = wait_for(lambda modes: modes['dstar']['pid'] and modes['dmr']['pid'])
                    self.assertEqual(initial['supervisor_pid'], supervisor.pid)
                    self.assertTrue(initial['reload_supported'])
                    original = {mode: initial['modes'][mode]['pid'] for mode in ('dstar', 'dmr')}

                    def assert_others(report):
                        self.assertEqual({mode: report['modes'][mode]['pid'] for mode in original}, original)
                        for pid in original.values():
                            os.kill(pid, 0)

                    reload_flags(True)
                    started = wait_for(lambda modes: modes['ysf']['pid'])
                    assert_others(started)
                    ysf_pid = started['modes']['ysf']['pid']
                    reload_flags(False)
                    stopped = wait_for(lambda modes: modes['ysf']['state'] == 'stopped' and modes['ysf']['pid'] is None)
                    assert_others(stopped)
                    with self.assertRaises(ProcessLookupError):
                        os.kill(ysf_pid, 0)

                    reload_flags(True)
                    restarted = wait_for(lambda modes: modes['ysf']['pid'])
                    assert_others(restarted)
                    ysf_pid = restarted['modes']['ysf']['pid']
                    os.kill(ysf_pid, signal.SIGKILL)
                    failed = wait_for(lambda modes: modes['ysf']['state'] == 'retrying')
                    self.assertEqual(failed['modes']['ysf']['last_exit_code'], -signal.SIGKILL)
                    assert_others(failed)
                    recovered = wait_for(lambda modes: modes['ysf']['pid'] and modes['ysf']['pid'] != ysf_pid)
                    assert_others(recovered)

                    # Moving YSF onto D-Star's board must restart that shared
                    # board, while the independent DMR process stays untouched.
                    data['bindings']['ysf'] = devices[0]
                    config.write_text(json.dumps(data))
                    supervisor.send_signal(signal.SIGHUP)
                    shared = wait_for(lambda modes: modes['ysf']['pid'] == modes['dstar']['pid']
                                      and modes['dstar']['pid'] != original['dstar'])
                    self.assertEqual(shared['modes']['dmr']['pid'], original['dmr'])
                    self.assertIn('exited code=', (root / 'events.log').read_text())
                finally:
                    supervisor.terminate()
                    try:
                        supervisor.wait(timeout=25)
                    except subprocess.TimeoutExpired:
                        supervisor.kill()
                        supervisor.wait()


if __name__ == '__main__':
    unittest.main()
