"""Integration checks for the installed root helper, UI and configuration daemon."""

import configparser
import json
from pathlib import Path
import shutil
import subprocess


PASSWORD = "ci-private-config-password"
STATE = Path("/run/opendvm-ci-units.json")
HOST = Path("/etc/MMDVMHost.ini")


def prepare_mode_controls(root):
    # Exercise migration of an existing, customized web password before copying
    # the new web scripts. A second update must preserve the migrated secret.
    legacy = Path("/var/www/html/save_config.php")
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(f"<?php\n$CONFIG_PASSWORD = '{PASSWORD}';\n")
    subprocess.run(["bash", "-c", "source ./install.sh; install_mode_controls"], cwd=root, check=True)
    password = Path("/etc/opendvm/config-password")
    assert password.read_text().rstrip() == PASSWORD
    assert password.stat().st_mode & 0o777 == 0o640
    shutil.copyfile(root / "gui/html/save_config.php", legacy)
    subprocess.run(["bash", "-c", "source ./install.sh; install_mode_controls"], cwd=root, check=True)
    assert password.read_text().rstrip() == PASSWORD
    subprocess.run(["visudo", "-cf", "/etc/sudoers.d/mmdvm-opendvm"], check=True)

    units = ["mmdvmhost.service", "ircddbgateway.service", "dmrgateway.service", "ysfgateway.service", "nxdngateway.service"]
    STATE.write_text(json.dumps({"commands": [], "units": {
        unit: {"Id": unit, "LoadState": "loaded", "ActiveState": "active",
               "SubState": "running", "UnitFileState": "enabled"} for unit in units}}))
    state = json.loads(STATE.read_text())
    state["units"]["nxdngateway.service"].update(ActiveState="inactive", UnitFileState="disabled")
    STATE.write_text(json.dumps(state))
    fixture = Path("/usr/local/libexec/opendvm-ci-systemctl")
    fixture.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(root / ".github/scripts/ci_systemctl.py", fixture)
    fixture.chmod(0o755)
    installed = Path("/usr/local/sbin/opendvm-mode-control")
    installed.write_text(installed.read_text().replace('SYSTEMCTL = "/usr/bin/systemctl"',
                                                     f'SYSTEMCTL = "{fixture}"'))
    subprocess.run([str(installed), "initialize"], check=True)
    subprocess.run([str(installed), "apply-boot"], check=True)


def flags():
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.read(HOST)
    return {mode: all(parser.get(section, "Enable") == "1" for section in sections)
            for mode, sections in {"dstar": ["D-Star", "D-Star Network"],
                                   "dmr": ["DMR", "DMR Network"],
                                   "ysf": ["System Fusion", "System Fusion Network"],
                                   "nxdn": ["NXDN", "NXDN Network"]}.items()}


def check_mode_controls(api, request, wait_for, logs):
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait

    status = json.loads(request("mode_control.php"))
    assert status["ok"] and set(status["modes"]) == {"dstar", "dmr", "ysf", "nxdn"}
    assert json.loads(request("mode_control.php", {"action": "stop", "mode": "dmr",
                                                  "ConfigPassword": "invalid"}, 401))["ok"] is False
    assert json.loads(request("mode_control.php", {"action": "start", "mode": "dmr; reboot",
                                                  "ConfigPassword": PASSWORD}, 400))["ok"] is False
    options = Options()
    for argument in ("--headless", "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                     "--no-proxy-server", "--disable-background-networking", "--window-size=1280,1600",
                     "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1"):
        options.add_argument(argument)
    browser = webdriver.Chrome(service=Service("/usr/bin/chromedriver"), options=options)
    wait = WebDriverWait(browser, 20)
    try:
        browser.get("http://127.0.0.1:8080/setup.html")
        wait.until(lambda driver: driver.find_element(By.ID, "saveBtn").is_enabled())
        for field, value in (("ConfigPassword", PASSWORD), ("Callsign", "DL8CI"),
                             ("NxdnId", "12345"), ("NxdnRAN", "7"), ("NxdnStatic", "10200,65000")):
            element = browser.find_element(By.ID, field)
            element.clear()
            element.send_keys(value)
        for field in ("DstarStartOnBoot", "DmrStartOnBoot", "YsfStartOnBoot", "NxdnStartOnBoot"):
            checkbox = browser.find_element(By.ID, field)
            if checkbox.is_selected():
                checkbox.click()
        browser.find_element(By.ID, "saveBtn").click()
        wait.until(lambda driver: driver.find_element(By.ID, "formState").text == "applied")
        assert flags() == {"dstar": False, "dmr": False, "ysf": False, "nxdn": False}
        assert api("config_inbox")["Callsign"] == "DL8CI"
        assert api("config_inbox")["NxdnStatic"] == "10200,65000"
        host_config = configparser.ConfigParser(interpolation=None, strict=False)
        host_config.read(HOST)
        assert host_config.get("NXDN", "Id") == "12345"
        assert host_config.get("NXDN", "RAN") == "7"
        records = json.loads(STATE.read_text())
        assert all(record["UnitFileState"] == "disabled" for unit, record in records["units"].items()
                   if unit != "mmdvmhost.service")

        # Every mode has both runtime controls; neither changes its boot checkbox.
        for mode in ("dstar", "dmr", "ysf", "nxdn"):
            def click_action(action):
                selector = f'[data-mode="{mode}"] button[data-action="{action}"]'
                wait.until(lambda driver: driver.find_element(By.CSS_SELECTOR, selector).is_enabled())
                browser.find_element(By.CSS_SELECTOR, selector).click()
            click_action("start")
            wait.until(lambda driver: driver.find_element(By.ID, mode + "State").text == "Running")
            assert flags()[mode]
            current = json.loads(request("mode_control.php"))["modes"][mode]
            assert current["running"] and not current["start_on_boot"]
            click_action("stop")
            wait.until(lambda driver: driver.find_element(By.ID, mode + "State").text == "Stopped")
            assert not flags()[mode]
        assert all(not browser.find_element(By.ID, field).is_selected()
                   for field in ("DstarStartOnBoot", "DmrStartOnBoot", "YsfStartOnBoot", "NxdnStartOnBoot"))

        # A restart of the device reapplies boot policy even after a temporary Start.
        request("mode_control.php", {"action": "start", "mode": "dmr", "ConfigPassword": PASSWORD})
        assert flags()["dmr"]
        subprocess.run(["/usr/local/sbin/opendvm-mode-control", "apply-boot"], check=True)
        assert not flags()["dmr"]

        # Failed service changes must reach the actual UI, rather than showing saved.
        records = json.loads(STATE.read_text())
        records["fail"] = True
        STATE.write_text(json.dumps(records))
        browser.find_element(By.ID, "saveBtn").click()
        wait.until(lambda driver: driver.find_element(By.ID, "formState").text == "error")
        wait.until(lambda driver: driver.find_element(By.ID, "saveBtn").is_enabled())
        assert "Simulated service operation failure" in browser.find_element(By.ID, "msg").text
        assert api("config_inbox")["is_new"] == "ERROR"
        records["fail"] = False
        STATE.write_text(json.dumps(records))
        browser.find_element(By.ID, "DmrStartOnBoot").click()
        browser.find_element(By.ID, "NxdnStartOnBoot").click()
        browser.find_element(By.ID, "saveBtn").click()
        wait.until(lambda driver: driver.find_element(By.ID, "formState").text == "applied")
        assert flags() == {"dstar": False, "dmr": True, "ysf": False, "nxdn": True}
        wait.until(lambda driver: driver.find_element(By.ID, "dmrState").text == "Running")
        browser.save_screenshot(str(logs / "mode-controls.png"))
        (logs / "mode-controls.html").write_text(browser.page_source)
        browser.refresh()
        wait.until(lambda driver: driver.find_element(By.ID, "saveBtn").is_enabled())
        assert browser.find_element(By.ID, "DmrStartOnBoot").is_selected()
        assert browser.find_element(By.ID, "NxdnStartOnBoot").is_selected()
        assert not browser.find_element(By.ID, "DstarStartOnBoot").is_selected()
        assert not browser.find_element(By.ID, "YsfStartOnBoot").is_selected()
        print("OK: settings UI applies boot policy, RF/network flags, runtime controls and errors", flush=True)
    finally:
        browser.quit()
