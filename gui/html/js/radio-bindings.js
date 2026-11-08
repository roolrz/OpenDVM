/* One editor per physical board; the main form saves all configuration changes. */
(() => {
  const modes = ['dstar', 'dmr', 'ysf', 'nxdn'];
  const modeNames = {dstar: 'D-Star', dmr: 'DMR', ysf: 'YSF', nxdn: 'NXDN'};
  const selects = {};
  const summaries = {};
  const result = document.getElementById('radioResult');
  const profiles = document.getElementById('radioProfiles');
  const reload = document.getElementById('radioReload');
  const legacy = document.getElementById('legacyBoard');
  let loaded = null;
  let edited = {};
  let busy = false;
  window.__radioReady = false;
  const fields = [
    ['rx_frequency', 'RX frequency (Hz)', 1000000, 1300000000],
    ['tx_frequency', 'TX frequency (Hz)', 1000000, 1300000000],
    ['duplex', 'Operating mode', 0, 1],
    ['baud', 'Serial baud rate', 1200, 3000000],
    ['rx_offset', 'RX offset (Hz)', -100000, 100000],
    ['tx_offset', 'TX offset (Hz)', -100000, 100000],
  ];
  const boardName = board => {
    const port = board.label.match(/-usb(?:v\d+)?-\d+:([\d.]+):[\d.]+-port(\d+)$/);
    if (!port) return board.label;
    const name = 'USB port ' + port[1] + (port[2] === '0' ? '' : ' / serial ' + port[2]);
    const duplicate = loaded?.devices.some(other => other.device !== board.device &&
      other.label.match(/-usb(?:v\d+)?-\d+:([\d.]+):[\d.]+-port(\d+)$/)?.[1] === port[1]);
    return duplicate ? name + ' · ' + board.label.split('-usb')[0] : name;
  };
  const dirty = () => {
    window.__radioDirty = true;
    result.textContent = 'Unsaved board changes. Use Save & apply at the bottom of this page.';
    updateControls();
  };
  const mhz = value => Number.isFinite(Number(value)) && value !== null ? (Number(value) / 1e6).toFixed(6) : '—';
  function updateSummaries() {
    if (!loaded) return;
    modes.forEach(mode => {
      const device = selects[mode].value;
      const profile = device ? edited[device] : !legacy.hidden ? {
        rx_frequency: document.getElementById('RXFrequency').value,
        tx_frequency: document.getElementById('TXFrequency').value,
      } : null;
      const board = loaded.devices.find(item => item.device === device);
      summaries[mode].textContent = profile
        ? `RX ${mhz(profile.rx_frequency)} / TX ${mhz(profile.tx_frequency)} MHz${board && !board.present ? ' · Board not connected' : ''}`
        : 'Choose a board before enabling this mode.';
    });
    profiles.querySelectorAll('.radio-profile').forEach(card => {
      const names = modes.filter(mode => selects[mode].value === card.dataset.device).map(mode => modeNames[mode]);
      card.querySelector('.board-assignment').textContent = names.length
        ? 'Assigned modes: ' + names.join(', ') : 'No modes assigned. This board will not run until a mode uses it.';
    });
  }
  modes.forEach(mode => {
    const select = document.getElementById(mode + 'Board');
    const summary = document.createElement('div');
    summary.className = 'hint';
    summary.id = mode + 'BoardSummary';
    select.after(summary);
    summaries[mode] = summary;
    selects[mode] = select;
    select.addEventListener('change', () => { dirty(); updateSummaries(); });
  });
  ['Duplex', 'RXFrequency', 'TXFrequency'].forEach(id => {
    document.getElementById(id).addEventListener('input', () => { dirty(); updateSummaries(); });
  });
  window.__refreshBoardSummaries = updateSummaries;
  window.__updateRadioControls = otherBusy => {
    const disabled = busy || otherBusy;
    reload.disabled = disabled;
    Object.values(selects).forEach(select => { select.disabled = disabled || !loaded; });
    profiles.querySelectorAll('input, select').forEach(input => { input.disabled = disabled; });
  };
  function renderProfiles() {
    profiles.replaceChildren();
    if (!loaded.devices.length && legacy.hidden) {
      const empty = document.createElement('p');
      empty.className = 'hint';
      empty.textContent = 'No USB boards detected. Connect a board and select Rescan boards.';
      profiles.append(empty);
    }
    loaded.devices.forEach((board, index) => {
      const device = board.device;
      if (!edited[device]) edited[device] = {...loaded.defaults};
      const section = document.createElement('div');
      section.className = 'radio-profile';
      section.id = 'radio-profile-' + index;
      section.dataset.device = device;
      const header = document.createElement('div');
      header.className = 'board-heading';
      const heading = document.createElement('h3');
      heading.textContent = boardName(board);
      const status = document.createElement('span');
      status.className = 'board-status';
      status.textContent = board.present ? 'Connected' : 'Not connected';
      header.append(heading, status);
      const assigned = document.createElement('p');
      assigned.className = 'hint board-assignment';
      section.append(header, assigned);
      const advanced = document.createElement('details');
      const summary = document.createElement('summary');
      summary.textContent = 'Device details & calibration';
      const identity = document.createElement('p');
      identity.className = 'hint mono board-path';
      identity.textContent = device;
      advanced.append(summary, identity);
      let row;
      fields.forEach(([key, title, min, max], fieldIndex) => {
        const parent = fieldIndex < 3 ? section : advanced;
        if (fieldIndex === 0 || fieldIndex === 2 || fieldIndex === 3 || fieldIndex === 4) {
          row = document.createElement('div');
          row.className = 'row';
          parent.append(row);
        }
        const field = document.createElement('div');
        field.className = 'field';
        const label = document.createElement('label');
        label.htmlFor = `board-${index}-${key}`;
        label.textContent = title;
        const input = document.createElement(key === 'duplex' ? 'select' : 'input');
        input.id = label.htmlFor;
        if (key === 'duplex') {
          input.add(new Option('Simplex — same RX / TX', '0'));
          input.add(new Option('Duplex — separate RX / TX', '1'));
        } else {
          Object.assign(input, {type: 'number', min: String(min), max: String(max), step: '1'});
        }
        input.value = edited[device][key] ?? '';
        input.required = true;
        input.addEventListener('input', () => {
          edited[device][key] = input.value === '' ? null : Number(input.value);
          dirty(); updateSummaries();
        });
        field.append(label, input);
        row.append(field);
      });
      const hint = document.createElement('p');
      hint.className = 'hint';
      hint.textContent = 'Enter Hz, e.g. 431750000 = 431.750 MHz. Simplex requires matching RX and TX.';
      section.append(hint, advanced);
      profiles.append(section);
    });
    updateSummaries();
  }
  function render(data, preserveDirty = false) {
    loaded = data;
    // Older endpoints returned [] for an empty map; always edit a plain object.
    edited = Object.fromEntries(Object.entries(data.profiles || {}).map(([device, profile]) => [device, {...profile}]));
    legacy.hidden = Boolean(data.legacy_board || data.legacy_is_usb);
    document.getElementById('legacyDevice').textContent = data.legacy_device + (data.legacy_present === false ? ' · Not connected' : '');
    let pinned = false;
    modes.forEach(mode => {
      const select = selects[mode];
      select.replaceChildren(new Option(legacy.hidden ? 'Choose a board' : 'Serial board (' + data.legacy_device + ')', ''));
      select.options[0].disabled = Boolean(data.legacy_board);
      data.devices.forEach(board => select.add(new Option(boardName(board) + (board.present ? '' : ' (not connected)'), board.device)));
      // Make an old alias assignment visible as the concrete board it currently
      // resolves to. Saving pins that path instead of following /dev/mmdvm.
      const previous = data.bindings[mode];
      select.value = previous || data.legacy_board || '';
      if (!previous && select.value) pinned = true;
    });
    renderProfiles();
    const connected = data.devices.filter(board => board.present).length;
    document.getElementById('radioCount').textContent = `${connected} USB connected · ${data.devices.length - connected} disconnected`;
    window.__radioReady = true;
    window.__radioDirty = preserveDirty || pinned;
    return pinned;
  }
  function validate() {
    if (!loaded) { result.textContent = 'Wait for board settings to load before saving.'; return false; }
    profiles.querySelectorAll('input, select').forEach(input => input.setCustomValidity(''));
    const legacyTx = document.getElementById('TXFrequency');
    legacyTx.setCustomValidity('');
    if (!legacy.hidden && document.getElementById('Duplex').value === '0' &&
        document.getElementById('RXFrequency').value !== legacyTx.value) {
      legacyTx.setCustomValidity('Simplex requires matching RX and TX frequencies.');
      legacyTx.scrollIntoView({block: 'center'}); legacyTx.reportValidity();
      return false;
    }
    for (const card of profiles.querySelectorAll('.radio-profile')) {
      const profile = edited[card.dataset.device];
      if (profile.duplex === 0 && profile.rx_frequency !== profile.tx_frequency) {
        card.querySelector('[id$="-tx_frequency"]').setCustomValidity('Simplex requires matching RX and TX frequencies.');
      }
    }
    for (const input of profiles.querySelectorAll('input, select')) {
      if (!input.validity.valid) {
        const details = input.closest('details');
        if (details) details.open = true;
        input.scrollIntoView({block: 'center'});
        input.reportValidity();
        result.textContent = 'Check the highlighted board setting.';
        return false;
      }
    }
    if (legacy.hidden) {
      for (const mode of modes) {
        if (!selects[mode].value && document.querySelector(`.mode-controls[data-mode="${mode}"] input`).checked) {
          result.textContent = 'Choose a board for ' + modeNames[mode] + ' before saving.';
          selects[mode].scrollIntoView({block: 'center'}); selects[mode].focus();
          return false;
        }
      }
    }
    return true;
  }
  window.__validateRadioSettings = validate;
  async function run(save, fromSettings = false) {
    if (busy || (saving && !fromSettings) || modeBusy || window.__bmBusy) return false;
    const options = {cache: 'no-store'};
    if (save) {
      if (!loaded) return false;
      const password = document.getElementById('ConfigPassword').value;
      if (!password) { result.textContent = 'Enter Config Password below first.'; return false; }
      const bindings = Object.fromEntries(modes.map(mode => [mode, selects[mode].value]));
      Object.assign(options, {method: 'POST', body: new URLSearchParams({
        action: 'configure-radios', ConfigPassword: password,
        radios: JSON.stringify({bindings, profiles: edited, revision: loaded.revision}),
      })});
    }
    busy = true; modeBusy = true; updateControls();
    result.textContent = save ? 'Saving boards…' : 'Scanning boards…';
    try {
      const response = await fetch('mode_control.php?q=radios', options);
      let data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.error || 'Board operation failed');
      const preserve = !save && loaded && window.__radioDirty;
      if (preserve) {
        const fresh = new Set(data.devices.map(board => board.device));
        data = {...data, revision: loaded.revision, profiles: edited,
          bindings: Object.fromEntries(modes.map(mode => [mode, selects[mode].value])),
          devices: [...data.devices, ...loaded.devices.filter(board => !fresh.has(board.device)).map(board => ({...board, present: false}))]};
      }
      const pinned = render(data, preserve);
      result.textContent = save ? 'Board settings saved.' : preserve ? 'Boards rescanned. Your unsaved changes are kept.'
        : pinned ? 'Review the board assignments below. Save & apply will pin them to these devices.' : 'Board settings loaded.';
      if (save && window.__invalidateBm) window.__invalidateBm();
      return true;
    } catch (error) {
      result.textContent = error.message;
      return false;
    } finally {
      busy = false; modeBusy = false; await refreshModes();
    }
  }
  reload.addEventListener('click', () => run(false));
  window.__saveRadioSettings = () => run(true, true);
  async function initialize() {
    for (let attempt = 0; attempt < 3; attempt++) {
      while (!loaded && (busy || saving || modeBusy || window.__bmBusy)) {
        await new Promise(resolve => setTimeout(resolve, 250));
      }
      if (loaded || await run(false)) return;
      if (attempt < 2) {
        result.textContent = 'Board scan failed; retrying automatically…';
        await new Promise(resolve => setTimeout(resolve, 1000));
      }
    }
  }
  initialize();
})();
