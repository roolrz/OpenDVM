"""Board identity, config isolation and validation without USB hardware."""
import configparser
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import radio_config as radio
import radio_host
import mode_control


class RadioTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.host = root / 'MMDVMHost.ini'
        self.host.write_text((ROOT / 'configs/MMDVMHost.ini.sample').read_text())
        for name, value in [('HOST_CONFIG', self.host), ('RADIO_CONFIG', root / 'radios.json'),
                            ('DEV', root / 'dev'), ('GATEWAY_DIR', root)]:
            item = patch.object(radio, name, value)
            item.start()
            self.addCleanup(item.stop)
        for sample, target in [('ysfgateway.sample', 'ysfgateway'), ('dmrgateway.sample', 'dmrgateway'),
                               ('nxdngateway.sample', 'nxdngateway'), ('ircddbgateway.sample', 'ircddbgateway')]:
            (root / target).write_text((ROOT / 'configs' / sample).read_text())
        self.a = '/dev/serial/by-id/usb-board-A'
        self.b = '/dev/serial/by-id/usb-board-B'
        self.profile = dict(rx_frequency=431750000, tx_frequency=431750000, duplex=0,
                            baud=115200, rx_offset=100, tx_offset=-100)

    def payload(self):
        return {'bindings': dict(dstar=self.a, dmr=self.b, ysf=self.a, nxdn=self.b),
                'profiles': {self.a: dict(self.profile), self.b: {**self.profile, 'rx_frequency': 433000000, 'tx_frequency': 433000000}},
                'revision': radio.snapshot()['revision']}

    def devices(self):
        return patch.object(radio, 'discover', return_value=[{'device': self.a}, {'device': self.b}])

    def parse(self, plan):
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(plan['config'])
        return parser

    def test_legacy_no_bindings_preserved(self):
        self.assertEqual(radio.read_config(), radio.empty_config())
        plans = radio.plans()
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]['device'], '/dev/mmdvm')
        self.assertEqual(plans[0]['enabled_modes'], ['dstar', 'dmr', 'ysf'])

    def test_two_hosts_group_shared_modes_and_isolate_ports(self):
        with self.devices():
            data = radio.validate(self.payload())
        plans = radio.plans(data)
        self.assertEqual(len(plans), 2)
        a, b = map(self.parse, plans)
        self.assertEqual(a['Modem']['UARTPort'], self.a)
        self.assertEqual(b['Modem']['Port'], self.b)
        self.assertEqual(a['System Fusion Network']['Enable'], '1')
        self.assertEqual(a['DMR Network']['Enable'], '0')
        self.assertEqual(b['DMR Network']['Enable'], '1')
        self.assertEqual(b['D-Star Network']['Enable'], '0')
        self.assertEqual(b['NXDN Network']['Enable'], '0')  # bound but disabled
        self.assertEqual(a['Info']['RXFrequency'], '431750000')
        self.assertEqual(b['Info']['RXFrequency'], '433000000')
        self.assertNotEqual(a['Log']['FileRoot'], b['Log']['FileRoot'])
        self.assertEqual(a['Remote Control']['Enable'], '0')
        self.assertEqual(b['Transparent Data']['Enable'], '0')
        self.assertIn('[DMR]\nEnable=1', self.host.read_text())

    def test_default_alias_and_explicit_selection_share_one_host(self):
        real = self.host.parent / 'ttyUSB0'
        real.touch()
        alias = self.host.parent / 'alias'
        alias.symlink_to(real)
        self.host.write_text(self.host.read_text().replace('/dev/mmdvm', str(alias)))
        data = radio.empty_config()
        data['bindings']['ysf'] = str(real)
        data['profiles'][str(real)] = self.profile
        plans = radio.plans(data)
        self.assertEqual(len(plans), 1)
        self.assertEqual(set(plans[0]['modes']), set(radio.MODES))
        self.assertEqual(self.parse(plans[0])['Info']['TXFrequency'], '431750000')

    def test_board_profiles_saved_without_bindings_survive_unplug_and_reassignment(self):
        with self.devices():
            payload = self.payload()
            payload['bindings'] = {mode: '' for mode in radio.MODES}
            data = radio.validate(payload)
        radio.atomic_write(radio.RADIO_CONFIG, json.dumps(data))
        with patch.object(radio, 'discover', return_value=[]):
            snapshot = radio.snapshot()
            self.assertEqual({item['device'] for item in snapshot['devices']}, {self.a, self.b})
            payload = {key: snapshot[key] for key in ('bindings', 'profiles', 'revision')}
            self.assertEqual(radio.validate(payload), data)
            payload['bindings']['ysf'] = self.b
            reassigned = radio.validate(payload)
        self.assertEqual(reassigned['profiles'], data['profiles'])
        plans = radio.plans(reassigned)
        self.assertNotIn(self.a, [plan['device'] for plan in plans])
        board = next(plan for plan in plans if plan['device'] == self.b)
        self.assertEqual(self.parse(board)['Info']['RXFrequency'], '433000000')

    def test_profile_applies_to_legacy_alias_without_explicit_bindings(self):
        real = self.host.parent / 'ttyUSB0'
        real.touch()
        alias = self.host.parent / 'alias'
        alias.symlink_to(real)
        self.host.write_text(self.host.read_text().replace('/dev/mmdvm', str(alias)))
        data = radio.empty_config()
        self.assertFalse(radio.managed(data))
        data['profiles'][str(real)] = self.profile
        self.assertTrue(radio.managed(data))
        radio.atomic_write(radio.RADIO_CONFIG, json.dumps(data))
        with patch.object(radio, 'discover', return_value=[]):
            snapshot = radio.snapshot()
        self.assertEqual(snapshot['legacy_board'], str(real))
        self.assertTrue(snapshot['legacy_is_usb'])
        plans = radio.plans(data)
        self.assertEqual(len(plans), 1)
        self.assertEqual(self.parse(plans[0])['Info']['RXFrequency'], '431750000')
        self.assertEqual(radio.effective_profile('ysf', data), self.profile)

    def test_reject_arbitrary_paths_unknown_modes_stale_revision_and_bad_profiles(self):
        with self.devices():
            for change in [lambda d: d['bindings'].update(ysf='/etc/passwd'),
                           lambda d: d['bindings'].update(shell=self.a),
                           lambda d: d.update(revision='stale'),
                           lambda d: d['profiles'][self.a].update(baud=0),
                           lambda d: d['profiles'][self.a].update(duplex=True),
                           lambda d: d['profiles'].update({'/etc/passwd': self.profile}),
                           lambda d: d['profiles'].update({'/dev/serial/by-id/unknown': self.profile}),
                           lambda d: d['profiles'][self.a].update(tx_frequency=440000000)]:
                data = self.payload()
                change(data)
                with self.assertRaises(ValueError):
                    radio.validate(data)
        self.assertFalse(radio.RADIO_CONFIG.exists())

    def test_saved_missing_board_is_retained_after_reboot_and_can_be_unbound(self):
        with self.devices():
            data = radio.validate(self.payload())
        radio.atomic_write(radio.RADIO_CONFIG, json.dumps(data))
        with patch.object(radio, 'discover', return_value=[]):
            snapshot = radio.snapshot()
            self.assertEqual(len(snapshot['devices']), 2)
            self.assertTrue(all(not device['present'] for device in snapshot['devices']))
            payload = {key: snapshot[key] for key in ('bindings', 'profiles', 'revision')}
            self.assertEqual(radio.validate(payload), data)
            payload['bindings'] = {mode: '' for mode in radio.MODES}
            payload['profiles'] = {}
            self.assertEqual(radio.validate(payload), radio.empty_config())

    def test_stable_paths_survive_tty_renumbering(self):
        device_dir = radio.DEV / 'serial/by-id'
        device_dir.mkdir(parents=True)
        target = radio.DEV / 'ttyUSB0'
        target.touch()
        link = device_dir / 'board-A'
        link.symlink_to(target)
        with patch.object(radio, 'available', return_value=True):
            first = radio.discover()
            link.unlink()
            link.symlink_to(radio.DEV / 'ttyUSB7')
            second = radio.discover()
        self.assertEqual(first, second)

    def test_discovery_deduplicates_aliases_and_keeps_by_path_fallback(self):
        for kind in ('by-id', 'by-path'):
            (radio.DEV / 'serial' / kind).mkdir(parents=True)
        (radio.DEV / 'serial/by-id/board-A').symlink_to(radio.DEV / 'ttyUSB0')
        (radio.DEV / 'serial/by-path/port-A').symlink_to(radio.DEV / 'ttyUSB0')
        (radio.DEV / 'serial/by-path/port-B').symlink_to(radio.DEV / 'ttyACM1')
        with patch.object(radio, 'available', return_value=True), patch.object(radio, 'usb_identity', side_effect=lambda path: path.name):
            self.assertEqual([item['identity'] for item in radio.discover()], ['by-id', 'by-path'])

    def test_gateway_frequencies_follow_mode_board_and_file_permissions_survive(self):
        with self.devices():
            data = radio.validate(self.payload())
        radio.atomic_write(radio.RADIO_CONFIG, json.dumps(data))
        gateway = radio.GATEWAY_DIR / 'ysfgateway'
        gateway.chmod(0o664)
        radio.sync_gateway_info()
        self.assertIn('RXFrequency=431750000', gateway.read_text())
        self.assertIn('RXFrequency=433000000', (radio.GATEWAY_DIR / 'dmrgateway').read_text())
        self.assertIn('frequency1=431.750000', (radio.GATEWAY_DIR / 'ircddbgateway').read_text())
        self.assertEqual(gateway.stat().st_mode & 0o777, 0o664)
        # A general settings save must not erase a board-specific frequency.
        gateway.write_text(gateway.read_text().replace('431750000', '430000000'))
        radio.sync_gateway_info()
        self.assertIn('RXFrequency=431750000', gateway.read_text())


    def test_supervisor_unplug_and_child_failure_do_not_restart_other_board(self):
        with self.devices():
            data = radio.validate(self.payload())
        radio.atomic_write(radio.RADIO_CONFIG, json.dumps(data))
        runtime = self.host.parent / 'runtime'
        tick = [0]
        processes = []
        handlers = {}
        reports = []

        class Process:
            def __init__(self, args):
                self.args = args
                self.dead = False
                self.terminated = False
                processes.append(self)
            def poll(self):
                return 1 if self.dead else None
            def terminate(self):
                self.dead = self.terminated = True
            def wait(self, timeout=None):
                return 0
            def kill(self):
                self.dead = True

        def sleep(_seconds):
            reports.append(json.loads((runtime / 'status.json').read_text()))
            tick[0] += 1
            if tick[0] == 3:
                processes[-1].dead = True  # Reconnected A exits; B must stay alive.
            if tick[0] == 9:
                handlers[radio_host.signal.SIGTERM](None, None)

        with patch.object(radio, 'RUNTIME', runtime), \
             patch.object(radio, 'available', side_effect=lambda device: device != self.a or tick[0] != 1), \
             patch.object(radio_host.subprocess, 'Popen', Process), \
             patch.object(radio_host.signal, 'signal', side_effect=lambda key, handler: handlers.update({key: handler})), \
             patch.object(radio_host.time, 'sleep', sleep), \
             patch.object(radio_host.time, 'monotonic', side_effect=lambda: tick[0]):
            radio_host.main()
        self.assertEqual(reports[1]['modes']['ysf']['state'], 'missing')
        self.assertEqual(reports[1]['modes']['dmr']['state'], 'active')
        self.assertEqual(reports[3]['modes']['ysf']['state'], 'retrying')
        self.assertEqual(reports[8]['modes']['ysf']['state'], 'active')
        by_config = {}
        for process in processes:
            by_config.setdefault(process.args[1], []).append(process)
        self.assertEqual(sorted(map(len, by_config.values())), [1, 3])
        self.assertTrue(all(process.dead for process in processes))
        self.assertFalse((runtime / 'status.json').exists())



    def test_binding_apply_failure_restores_previous_configuration(self):
        with self.devices():
            payload = self.payload()
        with self.devices(), patch.object(mode_control, 'HOST_CONFIG', self.host), \
             patch.object(mode_control, 'units', return_value={}), \
             patch.object(mode_control.sys, 'stdin', io.StringIO(json.dumps(payload))), \
             patch.object(mode_control, 'apply_runtime', side_effect=RuntimeError('Service failed')):
            with self.assertRaisesRegex(RuntimeError, 'Service failed'):
                mode_control.dispatch(['configure-radios'])
        self.assertFalse(radio.RADIO_CONFIG.exists())

    def test_missing_or_stale_board_report_never_shows_running(self):
        import time
        with self.devices():
            radio.atomic_write(radio.RADIO_CONFIG, json.dumps(radio.validate(self.payload())))
        runtime = self.host.parent / 'runtime'
        runtime.mkdir()
        records = {unit: {'ActiveState': 'active', 'UnitFileState': 'enabled'}
                   for unit in ['mmdvmhost.service', *(value[0] for value in mode_control.MODES.values())]}
        report = {'updated_at': time.time(), 'modes': {
            'dmr': {'state': 'active', 'device': self.b},
            'ysf': {'state': 'missing', 'device': self.a}}}
        (runtime / 'status.json').write_text(json.dumps(report))
        with patch.object(radio, 'RUNTIME', runtime), patch.object(mode_control, 'HOST_CONFIG', self.host), \
             patch.object(mode_control, 'units', return_value=records):
            status = mode_control.status()
            self.assertTrue(status['dmr']['running'])
            self.assertFalse(status['ysf']['running'])
            self.assertEqual(status['ysf']['modem_state'], 'missing')
            report['updated_at'] -= 20
            (runtime / 'status.json').write_text(json.dumps(report))
            self.assertFalse(mode_control.status()['dmr']['running'])



    def test_duplicate_serial_numbers_use_usb_socket_paths(self):
        for kind in ('by-id', 'by-path'):
            (radio.DEV / 'serial' / kind).mkdir(parents=True)
        (radio.DEV / 'serial/by-id/duplicate-serial').symlink_to(radio.DEV / 'ttyUSB1')
        (radio.DEV / 'serial/by-path/socket-A').symlink_to(radio.DEV / 'ttyUSB0')
        (radio.DEV / 'serial/by-path/socket-B').symlink_to(radio.DEV / 'ttyUSB1')
        with patch.object(radio, 'available', return_value=True), \
             patch.object(radio, 'usb_identity', return_value=('1234', '5678', 'duplicate')):
            devices = radio.discover()
        self.assertEqual(len(devices), 2)
        self.assertTrue(all(device['identity'] == 'by-path' for device in devices))


if __name__ == '__main__':
    unittest.main()
