/* Board bindings are applied separately from the station settings inbox. */
(() => {
  const modes = ['dstar', 'dmr', 'ysf', 'nxdn'];
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
    result.textContent = 'Board changes not applied. Use Apply USB bindings below.';
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
    const used = [...new Set(Object.values(selects).map(select => select.value).filter(Boolean))];
    used.forEach((device, index) => {
      if (!edited[device]) edited[device] = {...loaded.defaults};
      const section = document.createElement('div');
      section.className = 'radio-profile';
      const heading = document.createElement('h3');
      heading.textContent = device + (loaded.devices.find(item => item.device === device)?.present ? '' : ' (not connected)');
      section.append(heading);
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
    edited = JSON.parse(JSON.stringify(data.profiles));
    modes.forEach(mode => {
      const select = selects[mode];
      select.replaceChildren(new Option('Current hotspot (' + data.legacy_device + ')', ''));
      data.devices.forEach(device => select.add(new Option(device.label + (device.present ? '' : ' (not connected)'), device.device)));
      select.value = data.bindings[mode];
    });
    renderProfiles();
    window.__radioDirty = false;
  }
  async function run(save) {
    if (busy || saving || modeBusy || window.__bmBusy) return;
    const options = {cache: 'no-store'};
    if (save) {
      const password = document.getElementById('ConfigPassword').value;
      if (!password) { result.textContent = 'Enter Config Password below first.'; return; }
      if ([...profiles.querySelectorAll('input')].some(input => !input.reportValidity())) return;
      const bindings = Object.fromEntries(modes.map(mode => [mode, selects[mode].value]));
      const selected = Object.fromEntries([...new Set(Object.values(bindings).filter(Boolean))].map(device => [device, edited[device]]));
      Object.assign(options, {method: 'POST', body: new URLSearchParams({
        action: 'configure-radios', ConfigPassword: password,
        radios: JSON.stringify({bindings, profiles: selected, revision: loaded.revision}),
      })});
    }
    busy = true;
    modeBusy = true;
    updateControls();
    result.textContent = save ? 'Applying USB board bindings…' : 'Scanning USB serial devices…';
    try {
      const response = await fetch('mode_control.php?q=radios', options);
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.error || 'USB board operation failed');
      render(data);
      result.textContent = save ? 'Bindings saved. Check each mode’s status above as its board starts.'
        : data.devices.length ? 'USB boards loaded. Select a board for each mode above.'
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
