"""USB board discovery and deterministic per-board MMDVMHost configuration."""
import configparser
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import tempfile

HOST_CONFIG = Path('/etc/MMDVMHost.ini')
RADIO_CONFIG = Path('/etc/opendvm-radios.json')
RUNTIME = Path('/run/opendvm-host')
DEV = Path('/dev')
SYS_TTY = Path('/sys/class/tty')
GATEWAY_DIR = Path('/etc')
MODES = {'dstar': ('D-Star', 'D-Star Network'), 'dmr': ('DMR', 'DMR Network'),
         'ysf': ('System Fusion', 'System Fusion Network'), 'nxdn': ('NXDN', 'NXDN Network')}
FIELDS = {
    'rx_frequency': ('Info', 'RXFrequency', 1000000, 1300000000),
    'tx_frequency': ('Info', 'TXFrequency', 1000000, 1300000000),
    'duplex': ('General', 'Duplex', 0, 1),
    'baud': ('Modem', 'UARTSpeed', 1200, 3000000),
    'rx_offset': ('Modem', 'RXOffset', -100000, 100000),
    'tx_offset': ('Modem', 'TXOffset', -100000, 100000),
}


def host_config():
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    with HOST_CONFIG.open() as source:
        parser.read_file(source)
    return parser


def defaults():
    config = host_config()
    return {key: int(config.get(section, option, fallback='115200' if key == 'baud' else '0'))
            for key, (section, option, _, _) in FIELDS.items()}


def legacy_device():
    config = host_config()
    return config.get('Modem', 'UARTPort', fallback=config.get('Modem', 'Port', fallback='/dev/mmdvm'))


def empty_config():
    return {'version': 1, 'bindings': {mode: '' for mode in MODES}, 'profiles': {}}


def read_config():
    if not RADIO_CONFIG.exists():
        return empty_config()
    data = json.loads(RADIO_CONFIG.read_text())
    if data.get('version') != 1 or set(data.get('bindings', {})) != set(MODES):
        raise ValueError('Invalid USB board configuration')
    return data


def stable_path(device):
    return isinstance(device, str) and re.fullmatch(r'/dev/serial/by-(?:id|path)/[^/\x00-\x1f\x7f]{1,240}', device) is not None and Path(device).name not in ('.', '..')


def available(device):
    try:
        path = Path(device)
        return stat.S_ISCHR(path.stat().st_mode)
    except OSError:
        return False


def usb_identity(target):
    for parent in (SYS_TTY / target.name / 'device').resolve().parents:
        try:
            serial = (parent / 'serial').read_text().strip()
            if serial:
                return ((parent / 'idVendor').read_text().strip(),
                        (parent / 'idProduct').read_text().strip(), serial)
        except OSError:
            continue
    return None


def discover():
    # A by-id link is preferred. Devices without unique USB serials can use
    # by-path, which stays stable as long as the USB socket/hub is unchanged.
    devices = []
    candidates = {}
    for kind in ('by-id', 'by-path'):
        directory = DEV / 'serial' / kind
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            resolved = path.resolve()
            if not re.fullmatch(r'tty(?:USB|ACM)\d+', resolved.name) or not available(str(path)):
                continue
            candidates.setdefault(resolved, {})[kind] = path
    identities = {target: usb_identity(target) for target in candidates}
    for target, paths in candidates.items():
        unique = identities[target] is not None and list(identities.values()).count(identities[target]) == 1
        kind = 'by-id' if 'by-id' in paths and (unique or 'by-path' not in paths) else 'by-path'
        path = paths[kind]
        devices.append({'device': str(path), 'label': path.name, 'present': True, 'identity': kind})
    return devices


def snapshot():
    data = read_config()
    devices = discover()
    known = {item['device'] for item in devices}
    for device in sorted((set(data['bindings'].values()) | set(data['profiles'])) - {''} - known):
        devices.append({'device': device, 'label': Path(device).name,
                        'present': available(device), 'identity': 'saved'})
    revision = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    legacy = legacy_device()
    legacy_board = next((item['device'] for item in devices
                         if Path(item['device']).resolve() == Path(legacy).resolve()), '')
    return {'ok': True, **data, 'devices': devices, 'defaults': defaults(),
            'legacy_device': legacy, 'legacy_board': legacy_board, 'revision': revision}


def validate(data):
    if not isinstance(data, dict) or set(data) != {'bindings', 'profiles', 'revision'}:
        raise ValueError('Provide board bindings, profiles and revision')
    if data['revision'] != snapshot()['revision']:
        raise ValueError('Board settings changed. Refresh before saving again')
    bindings, profiles = data['bindings'], data['profiles']
    if not isinstance(bindings, dict) or set(bindings) != set(MODES) or not isinstance(profiles, dict):
        raise ValueError('Provide bindings for all four modes')
    saved = read_config()
    allowed = {item['device'] for item in discover()} | (set(saved['bindings'].values()) - {''}) | set(saved['profiles'])
    for device in bindings.values():
        if not isinstance(device, str) or (device and (not stable_path(device) or device not in allowed)):
            raise ValueError('Select a discovered USB board or a previously saved device')
    used = set(bindings.values()) - {''}
    if not used.issubset(profiles):
        raise ValueError('Provide one profile for each selected USB board')
    physical = {}
    for device, profile in profiles.items():
        if not stable_path(device) or device not in allowed:
            raise ValueError('Select a discovered USB board or a previously saved device')
        if not isinstance(profile, dict) or set(profile) != set(FIELDS):
            raise ValueError('Incomplete USB board profile')
        for key, (_, _, minimum, maximum) in FIELDS.items():
            value = profile[key]
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f'Invalid board setting: {key}')
        if profile['duplex'] == 0 and profile['rx_frequency'] != profile['tx_frequency']:
            raise ValueError('Simplex boards require matching RX and TX frequencies')
        resolved = str(Path(device).resolve())
        if resolved in physical:
            raise ValueError('Two device paths refer to the same board; select the same path for shared modes')
        physical[resolved] = device
    return {'version': 1, 'bindings': bindings, 'profiles': profiles}


def managed(data):
    """Saved board profiles also apply when modes use the legacy device alias."""
    return bool(any(data['bindings'].values()) or data['profiles'])


def atomic_write(path, text, mode=0o644):
    metadata = path.stat() if path.exists() else None
    descriptor, temporary = tempfile.mkstemp(prefix='.opendvm-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as destination:
            destination.write(text)
            destination.flush()
            os.fsync(destination.fileno())
            os.fchmod(destination.fileno(), mode)
            if metadata:
                os.fchmod(destination.fileno(), stat.S_IMODE(metadata.st_mode))
                os.fchown(destination.fileno(), metadata.st_uid, metadata.st_gid)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def effective_profile(mode, data=None):
    data = read_config() if data is None else data
    device = data['bindings'][mode] or legacy_device()
    profile = defaults()
    for path, settings in data['profiles'].items():
        if path == device or Path(path).resolve() == Path(device).resolve():
            profile.update(settings)
            break
    return profile


def sync_gateway_info(force=False):
    if not force and not RADIO_CONFIG.exists():
        return
    for mode, filename in (('dstar', 'ircddbgateway'), ('dmr', 'dmrgateway'),
                           ('ysf', 'ysfgateway'), ('nxdn', 'nxdngateway')):
        path = GATEWAY_DIR / filename
        profile = effective_profile(mode)
        if mode == 'dstar':
            changes = {('', 'frequency1'): f"{profile['rx_frequency'] / 1e6:.6f}",
                       ('', 'offset1'): f"{(profile['tx_frequency'] - profile['rx_frequency']) / 1e6:.6f}"}
        else:
            changes = {('Info', 'RXFrequency'): str(profile['rx_frequency']),
                       ('Info', 'TXFrequency'): str(profile['tx_frequency'])}
        text = path.read_text()
        section = ''
        lines = []
        for line in text.splitlines(keepends=True):
            header = re.match(r'^\s*\[([^]]+)\]', line)
            if header:
                section = header[1]
            key = line.split('=', 1)[0].strip()
            lines.append(f'{key}={changes[(section, key)]}\n' if (section, key) in changes else line)
        updated = ''.join(lines)
        if updated != text:
            atomic_write(path, updated)


def plans(data=None):
    """One host per physical board; each protocol/network appears exactly once."""
    data = read_config() if data is None else data
    base = host_config()
    legacy = legacy_device()
    grouped = {}
    # Explicit profiles take precedence when /dev/mmdvm aliases a selected USB board.
    aliases = {str(Path(device).resolve()): device for device in data['profiles']}
    for mode, sections in MODES.items():
        device = data['bindings'][mode] or legacy
        device = aliases.get(str(Path(device).resolve()), device)
        group = grouped.setdefault(device, {'device': device, 'modes': [], 'enabled_modes': []})
        group['modes'].append(mode)
        if all(base.get(section, 'Enable', fallback='0') == '1' for section in sections):
            group['enabled_modes'].append(mode)
    result = []
    for device, group in sorted(grouped.items()):
        config = host_config()
        def put(section, key, value):
            if not config.has_section(section):
                config.add_section(section)
            config.set(section, key, str(value))
        for mode, sections in MODES.items():
            for section in sections:
                put(section, 'Enable', int(mode in group['enabled_modes']))
        for section in ('P25', 'P25 Network', 'POCSAG', 'POCSAG Network', 'FM', 'FM Network',
                        'AX.25', 'AX.25 Network', 'Transparent Data', 'UMP', 'Remote Control', 'Lock File'):
            put(section, 'Enable', 0)
        put('General', 'Daemon', 0)
        put('General', 'Display', 'None')
        put('Modem', 'Protocol', 'uart')
        put('Modem', 'Port', device)
        put('Modem', 'UARTPort', device)
        for key, value in data['profiles'].get(device, {}).items():
            section, option, _, _ = FIELDS[key]
            put(section, option, value)
        identity = hashlib.sha256(device.encode()).hexdigest()[:12]
        put('Log', 'FilePath', '/var/log/mmdvm')
        put('Log', 'FileRoot', 'MMDVM-board-' + identity)
        output = io.StringIO()
        config.write(output, space_around_delimiters=False)
        result.append({**group, 'id': identity, 'config': output.getvalue()})
    return result
