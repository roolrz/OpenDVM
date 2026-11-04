"""Installed USB-binding API and concurrent log checks in disposable Debian CI."""
import configparser
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
import subprocess

from mode_control_regressions import PASSWORD


def check_radio_bindings(api, request, wait_for, start, stop, logs):
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import Select, WebDriverWait

    # Fake USB character devices (the null driver), never real radio hardware.
    serial = Path('/dev/serial/by-id')
    serial.mkdir(parents=True, exist_ok=True)
    devices = []
    for index in (900, 901):
        target = Path(f'/dev/ttyUSB{index}')
        os.mknod(target, stat.S_IFCHR | 0o666, os.makedev(1, 3))
        link = serial / f'opendvm-ci-{index}'
        link.symlink_to(target)
        devices.append(str(link))
    data = json.loads(request('mode_control.php?q=radios'))
    assert data['ok'] and {item['device'] for item in data['devices']} >= set(devices)
    request('mode_control.php', {'action': 'configure-radios', 'ConfigPassword': 'wrong', 'radios': '{}'}, 401)
    profiles = {device: dict(rx_frequency=431750000 + index * 1000000,
                            tx_frequency=431750000 + index * 1000000, duplex=0,
                            baud=115200, rx_offset=0, tx_offset=0)
                for index, device in enumerate(devices)}
    bindings = dict(dstar=devices[0], dmr=devices[1], ysf=devices[0], nxdn=devices[1])
    payload = dict(bindings=bindings, profiles=profiles, revision=data['revision'])
    response = json.loads(request('mode_control.php', {'action': 'configure-radios', 'ConfigPassword': PASSWORD,
                                                      'radios': json.dumps(payload)}))
    assert response['bindings'] == bindings
    request('mode_control.php', {'action': 'configure-radios', 'ConfigPassword': PASSWORD,
                                'radios': json.dumps(payload)}, 503)  # Stale editor rejected.
    saved = json.loads(Path('/etc/opendvm-radios.json').read_text())
    assert saved['bindings'] == bindings
    config = configparser.ConfigParser(interpolation=None)
    config.read('/etc/ysfgateway')
    assert config['Info']['RXFrequency'] == '431750000'
    # Saved board identity remains selected when unplugged.
    Path(devices[0]).unlink()
    missing = json.loads(request('mode_control.php?q=radios'))
    assert any(item['device'] == devices[0] and not item['present'] for item in missing['devices'])

    options = Options()
    for argument in ('--headless', '--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu',
                     '--no-proxy-server', '--disable-background-networking', '--window-size=1280,1600',
                     '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1'):
        options.add_argument(argument)
    browser = webdriver.Chrome(service=Service('/usr/bin/chromedriver'), options=options)
    wait = WebDriverWait(browser, 20)
    try:
        browser.get('http://127.0.0.1:8080/setup.html')
        wait.until(lambda driver: driver.find_element(By.ID, 'radioApply').is_enabled())
        assert Select(browser.find_element(By.ID, 'ysfBoard')).first_selected_option.get_attribute('value') == devices[0]
        assert len(browser.find_elements(By.CSS_SELECTOR, '.radio-profile')) == 2
        assert 'not connected' in Select(browser.find_element(By.ID, 'ysfBoard')).first_selected_option.text
        for mode in bindings:
            Select(browser.find_element(By.ID, mode + 'Board')).select_by_value('')
        password = browser.find_element(By.ID, 'ConfigPassword')
        password.clear()
        password.send_keys(PASSWORD)
        browser.find_element(By.ID, 'radioApply').click()
        wait.until(lambda driver: 'Bindings saved' in driver.find_element(By.ID, 'radioResult').text)
        assert not any(json.loads(Path('/etc/opendvm-radios.json').read_text())['bindings'].values())
        browser.save_screenshot(str(logs / 'usb-board-settings.png'))
    finally:
        browser.quit()
    for index, device in zip((900, 901), devices):
        Path(device).unlink(missing_ok=True)
        Path(f'/dev/ttyUSB{index}').unlink()

    # The real monitor must keep sessions on three hosts independent.
    directory = logs / 'boards'
    directory.mkdir()
    stamp = datetime.now(timezone.utc)
    paths = [directory / f'MMDVM-board-{index:012x}-{stamp:%Y-%m-%d}.log' for index in (1, 2, 3)]
    for path in paths:
        path.touch()
    stop('mmdvm-status')
    start('mmdvm-status', ['/usr/local/bin/mmdvm-status', str(directory)], user='mmdvm')
    def consumed(path):
        return any(len(parts) == 3 and parts[0] == str(path) and int(parts[2]) >= path.stat().st_size
                   for parts in (line.split('\t') for line in Path('/tmp/logparse.offsets').read_text().splitlines()))
    wait_for('board log discovery', lambda: all(consumed(path) for path in paths))
    def usb_calls():
        return [row for row in api('lastheard') if row['callsign'] in ('DL1USB', 'DL2USB', 'DL3USB')]
    timestamp = stamp + timedelta(minutes=4)
    def append(path, text):
        nonlocal timestamp
        with path.open('a') as output:
            output.write(f'M: {timestamp:%Y-%m-%d %H:%M:%S}.000 {text}\n')
        timestamp += timedelta(seconds=1)
        wait_for('board log consumed', lambda: consumed(path))
    append(paths[0], 'D-Star, received RF header from DL1USB')
    append(paths[1], 'YSF, received RF header from DL2USB to DG-ID 0')
    append(paths[2], 'DMR Slot 2, received network voice header from DL3USB to TG 46001')
    assert not usb_calls()
    append(paths[0], 'Mode set to Idle')
    assert len(usb_calls()) == 1
    assert any(int(slot['slot']) == 2 and slot['active'] for slot in api('status')['slots'])
    append(paths[1], 'YSF, received RF end of transmission from DL2USB to DG-ID 0, 4.0 seconds, BER: 0.1%')
    append(paths[2], 'DMR Slot 2, received network end of voice transmission from DL3USB to TG 46001, 4.0 seconds, BER: 0.0%')
    calls = {row['callsign']: row for row in api('lastheard')}
    assert all(name in calls for name in ('DL1USB', 'DL2USB', 'DL3USB'))
    assert float(calls['DL2USB']['duration']) == 4.0
    print('OK: installed board API/authentication, missing devices, persisted UI, apply and simultaneous board logs', flush=True)
