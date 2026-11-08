<?php
declare(strict_types=1);
require_once __DIR__ . '/config_auth.php';
header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');

function mode_response(int $status, array|object $body): never {
    http_response_code($status);
    echo json_encode($body, JSON_UNESCAPED_UNICODE);
    exit;
}

$method = $_SERVER['REQUEST_METHOD'] ?? '';
$arguments = ['status'];
$input = '';
if ($method === 'POST') {
    $password = opendvm_config_password();
    $given = $_POST['ConfigPassword'] ?? '';
    if (!is_string($given) || $password === '' || !hash_equals($password, $given)) {
        mode_response(401, ['ok' => false, 'error' => 'auth required']);
    }
    $action = $_POST['action'] ?? '';
    $mode = $_POST['mode'] ?? '';
    if ($action === 'configure-radios') {
        $input = $_POST['radios'] ?? '';
        if (!is_string($input) || strlen($input) > 65536 || !is_array(json_decode($input, true))) {
            mode_response(400, ['ok' => false, 'error' => 'Invalid USB board settings']);
        }
        $arguments = [$action];
    } elseif (!in_array($action, ['start', 'stop'], true) || !in_array($mode, ['dstar', 'dmr', 'ysf', 'nxdn'], true)) {
        mode_response(400, ['ok' => false, 'error' => 'Invalid mode or action']);
    } else {
        $arguments = [$action, $mode];
    }
} elseif ($method === 'GET' && ($_GET['q'] ?? '') === 'radios') {
    $arguments = ['radios'];
} elseif ($method !== 'GET') {
    mode_response(405, ['ok' => false, 'error' => 'method not allowed']);
}

try {
    // Hold the inbox row while a runtime operation runs, so it cannot race a
    // settings save or the configuration daemon writing the same INI file.
    $pdo = null;
    if ($method === 'POST') {
        $pdo = new PDO('mysql:unix_socket=/run/mysqld/mysqld.sock;dbname=mmdvmdb;charset=utf8mb4',
            'www-data', '', [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION]);
        $pdo->beginTransaction();
        $pending = $pdo->query('SELECT is_new FROM config_inbox WHERE id=1 FOR UPDATE')->fetchColumn();
        if (in_array($pending, ['GUI', 'APPLYING'], true)) {
            $pdo->rollBack();
            mode_response(409, ['ok' => false, 'error' => 'Wait for the configuration change to finish']);
        }
    }
    $command = array_merge(['/usr/bin/sudo', '-n', '/usr/local/sbin/opendvm-mode-control'], $arguments);
    $process = proc_open($command, [0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']], $pipes);
    if (!is_resource($process)) throw new RuntimeException('Mode controller could not start');
    if ($input !== '') fwrite($pipes[0], $input);
    fclose($pipes[0]);
    $output = stream_get_contents($pipes[1]);
    $error = stream_get_contents($pipes[2]);
    fclose($pipes[1]);
    fclose($pipes[2]);
    $code = proc_close($process);
    if ($pdo) $pdo->commit();
    // Preserve JSON objects, including an empty profiles map. Decoding into
    // associative arrays would turn {} into [] when the response is encoded.
    $result = json_decode($output);
    if (!is_object($result)) throw new RuntimeException($error ?: 'Mode controller returned invalid data');
    mode_response($code === 0 ? 200 : 503, $result);
} catch (Throwable $error) {
    mode_response(503, ['ok' => false, 'error' => $error->getMessage()]);
}
