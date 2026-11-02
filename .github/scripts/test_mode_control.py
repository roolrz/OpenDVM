"""Exercise real INI edits and boot/runtime policy with a simulated unit manager."""

import importlib.util
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
            elif action in ("restart", "stop"):
                self.records[unit]["ActiveState"] = "active" if action == "restart" else "inactive"
            else:
                self.fail(f"Unexpected service action: {arguments}")
        return ""

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
        controller.dispatch(["configure", "1", "0", "1"])
        controller.dispatch(["start", "dmr"])
        self.assertTrue(controller.status()["dmr"]["running"])
        self.assertFalse(controller.status()["dmr"]["start_on_boot"])
        controller.dispatch(["apply-boot"])
        self.assertFalse(controller.radio_flags()["dmr"])

    def test_save_applies_all_modes_in_one_modem_restart(self):
        controller.dispatch(["configure", "0", "1", "0"])
        self.assertEqual(controller.radio_flags(), {"dstar": False, "dmr": True, "ysf": False})
        restarts = [call for call in self.calls if "restart" in call]
        self.assertEqual(restarts, [("--no-block", "restart", "mmdvmhost.service", "dmrgateway.service")])
        self.assertEqual(controller.dispatch(["boot-status"]), {"dstar": False, "dmr": True, "ysf": False})

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
            controller.dispatch(["configure", "0", "0", "0"])
        self.assertEqual(self.config.read_text(), self.original)
        self.assertTrue(all(call[0] == "show" for call in self.calls))

    def test_partial_state_is_not_reported_running(self):
        controller.write_radio_flags({"dmr": True})
        self.records["mmdvmhost.service"]["ActiveState"] = "failed"
        self.assertFalse(controller.status()["dmr"]["running"])


if __name__ == "__main__":
    unittest.main()
