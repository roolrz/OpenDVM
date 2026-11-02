<?php
declare(strict_types=1);
require_once __DIR__ . '/config_auth.php';
require_once __DIR__ . '/bm_client.php';
header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');

function bm_reply(int $status, array $data): never {
    http_response_code($status);
    echo json_encode($data, JSON_UNESCAPED_UNICODE);
    exit;
}
if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') bm_reply(405, ['ok' => false, 'error' => 'POST required']);
$password = opendvm_config_password();
$given = $_POST['ConfigPassword'] ?? '';
if (!is_string($given) || $password === '' || !hash_equals($password, $given)) {
    bm_reply(401, ['ok' => false, 'error' => 'Configuration password required']);
}
$pdo = null;
$locked = false;
$status = 200;
try {
    $action = $_POST['action'] ?? '';
    if (!in_array($action, ['load', 'save_key', 'clear_key', 'apply'], true)) throw new BmError('Invalid BM action', 400);
    $desired = null;
    $revision = $_POST['revision'] ?? '';
    if ($action === 'apply') {
        $desired = bm_desired(json_decode((string)($_POST['static'] ?? ''), true, 16, JSON_THROW_ON_ERROR));
        if (!is_string($revision) || !preg_match('/^[a-f0-9]{64}$/D', $revision)) throw new BmError('Load from BM before applying', 400);
    }
    $pdo = new PDO('mysql:unix_socket=/run/mysqld/mysqld.sock;dbname=mmdvmdb;charset=utf8mb4',
        'www-data', '', [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION]);
    $locked = (int)$pdo->query("SELECT GET_LOCK('opendvm-bm-static', 0)")->fetchColumn() === 1;
    if (!$locked) throw new BmError('Another BM operation is in progress; try again shortly', 409);
    $pdo->beginTransaction();
    // Serialize against station saves and runtime controls while using the saved device.
    $context = bm_context($pdo, true);
    if ($action === 'clear_key') {
        $pdo->exec('UPDATE config_inbox SET bm_api_key=NULL WHERE id=1');
        $result = ['ok' => true, 'key_configured' => false];
    } else {
        if ($action === 'save_key') {
            $key = $_POST['api_key'] ?? '';
            if (!is_string($key) || strlen($key) > 16384 || !preg_match('/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/D', trim($key))) {
                throw new BmError('Enter a BrandMeister API v2 key from Profile Settings → API Keys', 400, 'BAD_API_KEY');
            }
            $context['key'] = trim($key);
        }
        $client = new BmClient($context);
        $data = $action === 'apply' ? $client->apply($desired, $revision) : $client->snapshot();
        if ($action === 'save_key') {
            $stmt = $pdo->prepare('UPDATE config_inbox SET bm_api_key=? WHERE id=1');
            $stmt->execute([$context['key']]);
        }
        $result = ['ok' => true, 'key_configured' => true, ...$data];
    }
    $pdo->commit();
} catch (BmError $error) {
    $status = $error->httpStatus;
    $result = ['ok' => false, 'error' => $error->getMessage(), 'error_code' => $error->errorCode];
} catch (JsonException) {
    $status = 400;
    $result = ['ok' => false, 'error' => 'Invalid static talkgroup JSON'];
} catch (Throwable) {
    $status = 500;
    $result = ['ok' => false, 'error' => 'BM settings could not be processed; check the service logs'];
} finally {
    if ($pdo && $pdo->inTransaction()) $pdo->rollBack();
    if ($pdo && $locked) $pdo->query("SELECT RELEASE_LOCK('opendvm-bm-static')");
}
bm_reply($status, $result);
