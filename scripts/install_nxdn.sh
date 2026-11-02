#!/usr/bin/env bash
# Shared installer functions. Build the same fork revision that Debian CI tests.
NXDN_CLIENTS_REPO="https://github.com/roolrz/NXDNClients.git"
NXDN_CLIENTS_REVISION="6943a0ab388914c5b9198ff307bc0c59218a8fcc"

install_nxdngateway() {
  log "Installing NXDNGateway..."
  build_from_git "$NXDN_CLIENTS_REPO" "NXDNClients/NXDNGateway" "" \
    "install -m 755 NXDNClients/NXDNGateway/NXDNGateway /usr/local/bin/" "$NXDN_CLIENTS_REVISION"
  if [[ ! -f /etc/nxdngateway ]]; then
    install_file "configs/nxdngateway.sample" "/etc/nxdngateway" 664 "root:mmdvm"
    # Carry the existing station identity into the newly installed gateway.
    python3 <<'PY'
import configparser
from pathlib import Path
host = configparser.ConfigParser(interpolation=None, strict=False)
host.read('/etc/MMDVMHost.ini')
gateway = Path('/etc/nxdngateway')
call = host.get('General', 'Callsign', fallback='YOUR_CALL')
gateway.write_text(gateway.read_text().replace('Callsign=YOUR_CALL', 'Callsign=' + call))
PY
  fi
  install_file "systemd/nxdngateway.service" "/etc/systemd/system/nxdngateway.service" 644
  if [[ ! -f /usr/local/etc/NXDNHosts.private ]]; then
    printf '# Private NXDN reflectors: TG numeric-IP port\n' > /usr/local/etc/NXDNHosts.private
    chmod 644 /usr/local/etc/NXDNHosts.private
  fi
}

install_nxdn_tables() {
  install_file "update_nxdn_hosts.py" "/usr/local/sbin/opendvm-update-nxdn-hosts" 755 "root:root"
  /usr/local/sbin/opendvm-update-nxdn-hosts --no-restart
}
