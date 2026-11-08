/* Board bindings are applied separately from the station settings inbox. */
(() => {
  const modes = ['dstar', 'dmr', 'ysf', 'nxdn'];
  const modeNames = {dstar: 'D-Star', dmr: 'DMR', ysf: 'YSF', nxdn: 'NXDN'};
  const selects = {};
  const result = document.getElementById('radioResult');
  const profiles = document.getElementById('radioProfiles');
  const reload = document.getElementById('radioReload');
  const apply = document.getElementById('radioApply');
  let loaded = null;
  let edited = {};
  let busy = false;
  const fields = [
    ['rx_frequency', 'RX frequency (Hz)', 1000000, 1300000000],
    ['tx_frequency', 'TX frequency (Hz)', 1000000, 1300000000],
    ['baud', 'Serial baud rate', 1200, 3000000],
    ['duplex', 'Duplex: 0 = simplex, 1 = duplex', 0, 1],
    ['rx_offset', 'RX offset (Hz)', -100000, 100000],
    ['tx_offset', 'TX offset (Hz)', -100000, 100000],
  ];
  const dirty = () => {
    window.__radioDirty = true;
    result.textContent = 'Board changes not applied. Use Save & apply, or Apply board settings to save only boards.';
  };
  modes.forEach(mode => {
    const select = document.getElementById(mode + 'Board');
    select.addEventListener('change', () => { dirty(); renderProfiles(); });
    selects[mode] = select;
  });
  window.__updateRadioControls = otherBusy => {
    const disabled = busy || otherBusy;
    reload.disabled = disabled;
    apply.disabled = disabled || !loaded;
    Object.values(selects).forEach(select => { select.disabled = disabled || !loaded; });
    profiles.querySelectorAll('input').forEach(input => { input.disabled = disabled; });
  };
  function renderProfiles() {
    profiles.replaceChildren();
    if (!loaded.devices.length) {
      const hint = document.createElement('p');
      hint.className = 'hint';
      hint.textContent = 'No USB boards detected or saved. Connect a USB board and use Reload boards. Current hotspot uses the Station frequency settings until a board profile is saved.';
      profiles.append(hint);
    }
    loaded.devices.forEach((board, index) => {
      const device = board.device;
      if (!edited[device]) edited[device] = {...loaded.defaults};
      const section = document.createElement('div');
      section.className = 'radio-profile';
      section.id = 'radio-profile-' + index;
      section.dataset.device = device;
      const heading = document.createElement('h3');
      heading.textContent = board.label + (board.present ? '' : ' (not connected)');
      section.append(heading);
      const identity = document.createElement('p');
      identity.className = 'hint';
      identity.style.overflowWrap = 'anywhere';
      identity.textContent = device;
      section.append(identity);
      const assigned = document.createElement('p');
      assigned.className = 'hint';
      const names = modes.filter(mode => (selects[mode].value || loaded.legacy_board) === device).map(mode => modeNames[mode]);
      assigned.textContent = names.length ? 'Used by: ' + names.join(', ') : 'No modes assigned. Settings can be saved before assigning a mode.';
      section.append(assigned);
      let row;
      fields.forEach(([key, title, min, max], fieldIndex) => {
        if (fieldIndex % 2 === 0) {
          row = document.createElement('div');
          row.className = 'row';
          section.append(row);
        }
        const field = document.createElement('div');
        field.className = 'field';
        const label = document.createElement('label');
        label.htmlFor = `board-${index}-${key}`;
        label.textContent = title;
        const input = document.createElement('input');
        Object.assign(input, {id: label.htmlFor, type: 'number', min: String(min), max: String(max), step: '1', value: edited[device][key]});
        input.required = true;
        input.addEventListener('input', () => { edited[device][key] = input.value === '' ? null : Number(input.value); dirty(); });
        field.append(label, input);
        row.append(field);
      });
      profiles.append(section);
    });
  }
  function render(data) {
    loaded = data;
    // Older API versions returned [] for an empty map. Always edit an object:
    // JSON.stringify(array) silently drops properties keyed by device path.
    edited = Object.fromEntries(Object.entries(data.profiles || {}).map(([device, profile]) => [device, {...profile}]));
    modes.forEach(mode => {
      const select = selects[mode];
      select.replaceChildren(new Option('Current hotspot (' + data.legacy_device + ')', ''));
      data.devices.forEach(device => select.add(new Option(device.label + (device.present ? '' : ' (not connected)'), device.device)));
      select.value = data.bindings[mode];
    });
    renderProfiles();
    window.__radioDirty = false;
  }
  async function run(save, fromSettings = false) {
    if (busy || (saving && !fromSettings) || modeBusy || window.__bmBusy) return false;
    if (save && !loaded) { result.textContent = 'Wait for USB boards to load before saving.'; return false; }
    const options = {cache: 'no-store'};
    if (save) {
      const password = document.getElementById('ConfigPassword').value;
      if (!password) { result.textContent = 'Enter Config Password below first.'; return false; }
      if ([...profiles.querySelectorAll('input')].some(input => !input.reportValidity())) {
        result.textContent = 'Check the highlighted board setting before saving.';
        return false;
      }
      const bindings = Object.fromEntries(modes.map(mode => [mode, selects[mode].value]));
      Object.assign(options, {method: 'POST', body: new URLSearchParams({
        action: 'configure-radios', ConfigPassword: password,
        radios: JSON.stringify({bindings, profiles: edited, revision: loaded.revision}),
      })});
    }
    busy = true;
    modeBusy = true;
    updateControls();
    result.textContent = save ? 'Applying board settings…' : 'Scanning USB serial devices…';
    try {
      const response = await fetch('mode_control.php?q=radios', options);
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.error || 'USB board operation failed');
      render(data);
      result.textContent = save ? 'Board settings saved. Check each mode’s status above as its board starts.'
        : data.devices.length ? 'USB boards loaded. Configure each board below; assign modes using the selectors above.'
        : 'No USB boards with stable serial paths detected. Current hotspot settings are still available.';
      if (save && window.__invalidateBm) window.__invalidateBm();
      return true;
    } catch (error) {
      result.textContent = error.message;
      return false;
    } finally {
      busy = false;
      modeBusy = false;
      await refreshModes();
    }
  }
  reload.addEventListener('click', () => run(false));
  apply.addEventListener('click', () => run(true));
  window.__saveRadioSettings = () => run(true, true);
  async function initialize() {
    for (let attempt = 0; attempt < 3; attempt++) {
      // Other settings operations can temporarily hold the shared controls.
      // Do not silently drop the initial load, or overwrite a successful reload.
      while (!loaded && (busy || saving || modeBusy || window.__bmBusy)) {
        await new Promise(resolve => setTimeout(resolve, 250));
      }
      if (loaded || await run(false)) return;
      if (attempt < 2) {
        result.textContent = 'USB board scan failed; retrying automatically…';
        await new Promise(resolve => setTimeout(resolve, 1000));
      }
    }
  }
  initialize();
})();
