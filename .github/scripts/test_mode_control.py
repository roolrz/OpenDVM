"""Exercise real INI edits and boot/runtime policy with a simulated unit manager."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("mode_control", ROOT / "scripts/mode_control.py")
controller = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(controller)


class ModeControlTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = Path(self.directory.name) / "MMDVMHost.ini"
        self.original = (ROOT / "configs/MMDVMHost.ini.sample").read_text()
        self.config.write_text(self.original)
        self.config.chmod(0o664)
        for name, value in (("HOST_CONFIG", self.config),
                            ("RADIO_CONFIG", self.config.parent / 'radios.json'),
                            ("RUNTIME", self.config.parent / 'runtime'),
                            ("GATEWAY_DIR", self.config.parent)):
            item = patch.object(controller.radio, name, value)
            item.start()
            self.addCleanup(item.stop)
        self.calls = []
        self.records = {unit: {"Id": unit, "LoadState": "loaded", "ActiveState": "active",
                               "SubState": "running", "UnitFileState": "enabled"}
                        for unit in ["mmdvmhost.service", *(entry[0] for entry in controller.MODES.values())]}
        for name, value in (("HOST_CONFIG", self.config),
                            ("INITIALIZED", self.config.parent / "initialized"),
                            ("systemctl", self.systemctl)):
            item = patch.object(controller, name, value)
            item.start()
            self.addCleanup(item.stop)

    def systemctl(self, *arguments):
        self.calls.append(arguments)
        if arguments[0] == "show":
            return "\n\n".join("\n".join(f"{k}={v}" for k, v in entry.items())
                                 for entry in self.records.values())
        args = [arg for arg in arguments if not arg.startswith("--")]
        action, *units = args
        for unit in units:
            if action in ("enable", "disable"):
                self.records[unit]["UnitFileState"] = "enabled" if action == "enable" else "disabled"
            elif action == 'reload':
                pass
            elif action in ("restart", "stop"):
                self.records[unit]["ActiveState"] = "active" if action == "restart" else "inactive"
            else:
                self.fail(f"Unexpected service action: {arguments}")
        return ""

    def enable_supervisor_reload(self):
        for name in ('ircddbgateway', 'ysfgateway', 'dmrgateway', 'nxdngateway'):
            (self.config.parent / name).write_text((ROOT / 'configs' / (name + '.sample')).read_text())
        data = controller.radio.empty_config()
        data['bindings']['ysf'] = '/dev/serial/by-path/board-ysf'
        controller.radio.RADIO_CONFIG.write_text(json.dumps(data))
        controller.radio.RUNTIME.mkdir()
        self.records['mmdvmhost.service']['MainPID'] = '1234'
        (controller.radio.RUNTIME / 'status.json').write_text(json.dumps({
            'supervisor_pid': 1234, 'reload_supported': True, 'updated_at': 0, 'modes': {}}))

    def test_managed_start_reloads_hosts_and_only_restarts_target_gateway(self):
        self.enable_supervisor_reload()
        controller.dispatch(['start', 'ysf'])
        changes = [call for call in self.calls if call[0] != 'show']
        self.assertEqual(changes, [('--no-block', 'reload', 'mmdvmhost.service'),
                                   ('--no-block', 'restart', 'ysfgateway.service')])

    def test_managed_stop_reloads_hosts_without_restarting_other_gateways(self):
        self.enable_supervisor_reload()
        controller.dispatch(['stop', 'ysf'])
        changes = [call for call in self.calls if call[0] != 'show']
        self.assertEqual(changes, [('--no-block', 'stop', 'ysfgateway.service'),
                                   ('--no-block', 'reload', 'mmdvmhost.service')])

    def test_old_supervisor_report_cannot_signal_new_main_process(self):
        self.enable_supervisor_reload()
        self.records['mmdvmhost.service']['MainPID'] = '5678'
        controller.dispatch(['start', 'ysf'])
        self.assertFalse(any('reload' in call for call in self.calls))
        self.assertIn(('--no-block', 'restart', 'mmdvmhost.service', 'ysfgateway.service'), self.calls)

    def test_older_supervisor_without_capability_is_restarted(self):
        self.enable_supervisor_reload()
        (controller.radio.RUNTIME / 'status.json').write_text('{"supervisor_pid":1234}')
        controller.dispatch(['start', 'ysf'])
        self.assertFalse(any('reload' in call for call in self.calls))

    def test_runtime_stop_disables_radio_and_network_without_changing_boot(self):
        controller.dispatch(["stop", "dmr"])
        self.assertFalse(controller.radio_flags()["dmr"])
        result = controller.status()["dmr"]
        self.assertTrue(result["start_on_boot"])
        self.assertFalse(result["running"])
        self.assertEqual(result["gateway_state"], "inactive")
        self.assertNotIn(("disable", "dmrgateway.service"), self.calls)
        controller.dispatch(["apply-boot"])
        self.assertTrue(controller.radio_flags()["dmr"])

    def test_temporary_start_does_not_enable_at_boot(self):
        controller.dispatch(["configure", "1", "0", "1", "0"])
        controller.dispatch(["start", "dmr"])
        self.assertTrue(controller.status()["dmr"]["running"])
        self.assertFalse(controller.status()["dmr"]["start_on_boot"])
        controller.dispatch(["apply-boot"])
        self.assertFalse(controller.radio_flags()["dmr"])

    def test_save_applies_all_modes_in_one_modem_restart(self):
        controller.dispatch(["configure", "0", "1", "0", "0"])
        self.assertEqual(controller.radio_flags(), {"dstar": False, "dmr": True, "ysf": False, "nxdn": False})
        restarts = [call for call in self.calls if "restart" in call]
        self.assertEqual(restarts, [("--no-block", "restart", "mmdvmhost.service", "dmrgateway.service")])
        self.assertEqual(controller.dispatch(["boot-status"]), {"dstar": False, "dmr": True, "ysf": False, "nxdn": False})

    def test_one_mode_preserves_other_config_and_file_permissions(self):
        # A non-target section and comments survive the root helper's atomic edit.
        self.config.write_text(self.original + "\n# custom setting\n[Custom]\nKeep=yes\n")
        before = controller.radio_flags()
        controller.dispatch(["stop", "ysf"])
        after = controller.radio_flags()
        self.assertEqual((before["dmr"], before["dstar"]), (after["dmr"], after["dstar"]))
        self.assertIn("# custom setting\n[Custom]\nKeep=yes\n", self.config.read_text())
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o664)
        self.assertEqual(Path(str(self.config) + ".mode-control.bak").read_text(),
                         self.original + "\n# custom setting\n[Custom]\nKeep=yes\n")

    def test_initialize_preserves_previously_disabled_radio(self):
        controller.write_radio_flags({"dstar": False})
        controller.dispatch(["initialize"])
        self.assertFalse(controller.status()["dstar"]["start_on_boot"])
        # Future updates must not reinterpret a temporary Stop as a new boot policy.
        controller.dispatch(["stop", "dmr"])
        controller.dispatch(["initialize"])
        self.assertTrue(controller.status()["dmr"]["start_on_boot"])

    def test_boot_hook_does_not_start_or_restart_services(self):
        self.records["ysfgateway.service"]["UnitFileState"] = "disabled"
        controller.dispatch(["apply-boot"])
        self.assertFalse(controller.radio_flags()["ysf"])
        self.assertTrue(all(call[0] == "show" for call in self.calls))

    def test_rejects_unknown_modes_commands_and_extra_arguments(self):
        for args in (["start", "dmr; reboot"], ["restart", "dmr"], ["configure", "1", "2", "1"],
                     ["start", "dmr", "--now"], ["configure", "1", "1"], ["/bin/sh"]):
            with self.subTest(args=args), self.assertRaises(ValueError):
                controller.dispatch(args)
        self.assertEqual(self.config.read_text(), self.original)
        self.assertEqual(self.calls, [])

    def test_missing_unit_fails_before_mutation(self):
        self.records["ircddbgateway.service"]["LoadState"] = "not-found"
        with self.assertRaisesRegex(RuntimeError, "not installed"):
            controller.dispatch(["configure", "0", "0", "0", "0"])
        self.assertEqual(self.config.read_text(), self.original)
        self.assertTrue(all(call[0] == "show" for call in self.calls))

    def test_partial_state_is_not_reported_running(self):
        controller.write_radio_flags({"dmr": True})
        self.records["mmdvmhost.service"]["ActiveState"] = "failed"
        self.assertFalse(controller.status()["dmr"]["running"])

    def test_nxdn_requires_own_id_before_start_or_boot_enable(self):
        for arguments in (["start", "nxdn"], ["configure", "1", "1", "1", "1"]):
            with self.subTest(arguments=arguments), self.assertRaisesRegex(ValueError, "NXDN ID"):
                controller.dispatch(arguments)
        self.assertEqual(self.config.read_text(), self.original)
        self.assertTrue(all(call[0] == "show" for call in self.calls))

    def test_nxdn_runtime_and_boot_policy_control_both_flags(self):
        self.config.write_text(self.original.replace("[NXDN]\nEnable=0\nId=0", "[NXDN]\nEnable=0\nId=12345"))
        controller.dispatch(["configure", "1", "1", "1", "0"])
        controller.dispatch(["start", "nxdn"])
        self.assertTrue(controller.status()["nxdn"]["running"])
        self.assertFalse(controller.status()["nxdn"]["start_on_boot"])
        controller.dispatch(["apply-boot"])
        self.assertFalse(controller.radio_flags()["nxdn"])
        controller.dispatch(["configure", "1", "1", "1", "1"])
        controller.dispatch(["stop", "nxdn"])
        self.assertFalse(controller.status()["nxdn"]["running"])
        self.assertTrue(controller.status()["nxdn"]["start_on_boot"])


if __name__ == "__main__":
    unittest.main()
