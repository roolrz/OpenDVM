<?php
declare(strict_types=1);

function opendvm_config_password(): string {
    $path = '/etc/opendvm/config-password';
    if (is_file($path)) {
        $password = file_get_contents($path);
        if ($password === false) {
            throw new RuntimeException('Configuration password is unreadable');
        }
        return rtrim($password, "\r\n");
    }
    return 'setuppassword';
}
