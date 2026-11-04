#!/usr/bin/env bash
# Shared by the fresh installer and updater; callers provide install_file().

install_mode_controls() {
  install_file "scripts/mode_control.py" "/usr/local/sbin/opendvm-mode-control" 755 "root:root"
  install_file "scripts/radio_config.py" "/usr/local/sbin/radio_config.py" 644 "root:root"
  install_file "scripts/radio_host.py" "/usr/local/sbin/opendvm-radio-host" 755 "root:root"
  install_file "systemd/mmdvm-radio-host.conf" "/etc/systemd/system/mmdvmhost.service.d/opendvm-radio-host.conf" 644
  install_file "systemd/opendvm-mode-boot.service" "/etc/systemd/system/opendvm-mode-boot.service" 644
  install_file "systemd/mmdvm-mode-boot.conf" "/etc/systemd/system/mmdvmhost.service.d/opendvm-mode-boot.conf" 644
  install -d -m 750 -o root -g www-data /etc/opendvm
  if [[ ! -f /etc/opendvm/config-password ]]; then
    # Read a legacy literal password without executing the installed web script.
    php <<'PHP'
<?php
$password = 'setuppassword';
$legacy = '/var/www/html/save_config.php';
if (is_file($legacy)) {
    $tokens = array_values(array_filter(token_get_all(file_get_contents($legacy)),
        fn($token) => !is_array($token) || !in_array($token[0], [T_WHITESPACE, T_COMMENT, T_DOC_COMMENT], true)));
    $found = false;
    foreach ($tokens as $index => $token) {
        if (is_array($token) && $token[0] === T_VARIABLE && $token[1] === '$CONFIG_PASSWORD') {
            $literal = $tokens[$index + 2] ?? null;
            if (($tokens[$index + 1] ?? null) !== '=' || !is_array($literal) || $literal[0] !== T_CONSTANT_ENCAPSED_STRING) {
                file_put_contents('php://stderr', "Cannot migrate configuration password; create /etc/opendvm/config-password first.\n");
                exit(1);
            }
            $raw = substr($literal[1], 1, -1);
            $password = $literal[1][0] === "'" ? strtr($raw, ["\\\\" => "\\", "\\'" => "'"]) : stripcslashes($raw);
            $found = true;
            break;
        }
    }
    if (!$found) {
        file_put_contents('php://stderr', "Cannot find legacy configuration password; create /etc/opendvm/config-password first.\n");
        exit(1);
    }
}
file_put_contents('/etc/opendvm/config-password', $password . "\n");
PHP
  fi
  chown root:www-data /etc/opendvm/config-password
  chmod 640 /etc/opendvm/config-password
}
