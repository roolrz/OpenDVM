<?php
declare(strict_types=1);
header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store, must-revalidate');

function response($ok, $payload = []) {
  echo json_encode($ok ? array_merge(['ok'=>true], $payload)
                       : array_merge(['ok'=>false], $payload));
  exit;
}

require_once __DIR__ . '/config_auth.php';
$CONFIG_PASSWORD = opendvm_config_password();

// Nur POST akzeptieren
if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') {
  http_response_code(405);
  response(false, ['error' => 'method not allowed']);
}

/* ======= Passwort prüfen ======= */
$givenPw = $_POST['ConfigPassword'] ?? '';
if ($CONFIG_PASSWORD === '' || !hash_equals($CONFIG_PASSWORD, (string)$givenPw)) {
  http_response_code(401);
  response(false, ['error' => 'auth required']);
}
// nie weiterverarbeiten/mitzählen
unset($_POST['ConfigPassword']);

$expected = [
  'Callsign','Module','Id','Duplex','RXFrequency','TXFrequency',
  'Latitude','Longitude','Height','Location','Description','URL',
  'reflector1','reflector_module','Suffix','Startup','Options',
  'Address','Password','Name', 'DstarStartOnBoot', 'DmrStartOnBoot', 'YsfStartOnBoot',
  'NxdnStartOnBoot', 'NxdnId', 'NxdnRAN', 'NxdnStatic'
];

// collect
$data = [];
foreach ($expected as $k) { $data[$k] = isset($_POST[$k]) ? trim((string)$_POST[$k]) : null; }
// normalize
foreach (['Latitude','Longitude'] as $k) { if ($data[$k] !== null) $data[$k] = str_replace(',', '.', $data[$k]); }

// validate
$errors = [];
foreach (['DstarStartOnBoot','DmrStartOnBoot','YsfStartOnBoot','NxdnStartOnBoot'] as $key) {
  if ($data[$key] !== null && !in_array($data[$key], ['0','1'], true)) $errors[] = "$key must be 0 or 1";
}
if ($data['NxdnId'] !== null && (!ctype_digit($data['NxdnId']) || (int)$data['NxdnId'] > 65535)) $errors[] = 'NXDN ID must be 0–65535';
if ($data['NxdnRAN'] !== null && (!ctype_digit($data['NxdnRAN']) || (int)$data['NxdnRAN'] > 63)) $errors[] = 'NXDN RAN must be 0–63';
if ($data['NxdnStatic'] !== null && $data['NxdnStatic'] !== '') {
  $groups = array_map('trim', explode(',', $data['NxdnStatic']));
  foreach ($groups as $group) {
    if (!ctype_digit($group) || (int)$group < 1 || (int)$group > 65535) $errors[] = 'NXDN talkgroups must be 1–65535';
  }
  $data['NxdnStatic'] = implode(',', array_unique($groups));
  if (strlen($data['NxdnStatic']) > 255) $errors[] = 'Too many NXDN static talkgroups';
}
if (empty($data['Callsign']) || !preg_match('/^[A-Za-z0-9\/]{3,}$/', $data['Callsign'])) $errors[] = 'Invalid callsign';
foreach (['RXFrequency','TXFrequency'] as $hz) {
  if ($data[$hz] === null || $data[$hz] === '' || !preg_match('/^\d+$/', $data[$hz])) $errors[] = "$hz must be an integer number (Hz)";
}
if (empty($data['Id']) || !preg_match('/^\d+$/', $data['Id'])) $errors[] = 'Id (DMR) missing/invalid';
if ($data['Latitude'] === null || $data['Longitude'] === null || !is_numeric($data['Latitude']) || !is_numeric($data['Longitude'])) $errors[] = 'Latitude/Longitude invalid';
//if (!empty($data['URL']) && !preg_match('#^https?://#i', $data['URL'])) $errors[] = 'URL must start with http(s)';
if ($errors) response(false, ['error' => implode('; ', $errors)]);

// types
$duplex = (int)($data['Duplex'] ?? 0);
$rxfreq = (int)$data['RXFrequency'];
$txfreq = (int)$data['TXFrequency'];
$lat    = (float)$data['Latitude'];
$lon    = (float)$data['Longitude'];
$height = ($data['Height'] !== null && $data['Height'] !== '') ? (int)$data['Height'] : null;
$dmrId  = (int)$data['Id'];
$reflBase   = strtoupper(trim((string)($data['reflector1'] ?? '')));
$reflModule = strtoupper(substr((string)($data['reflector_module'] ?? ''), 0, 1));
$reflector1Combined = trim($reflBase . ' ' . ($reflModule ?: ''));

try {
  $pdo = new PDO(
    'mysql:unix_socket=/run/mysqld/mysqld.sock;dbname=mmdvmdb;charset=utf8mb4',
    'www-data',
    '',
    [
      PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
      PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
      PDO::ATTR_EMULATE_PREPARES => false,
    ]
  );

  $pdo->beginTransaction();
  $current = $pdo->query('SELECT is_new, dstar_boot, dmr_boot, ysf_boot, nxdn_boot, nxdn_id, nxdn_ran, nxdn_static FROM config_inbox WHERE id=1 FOR UPDATE')->fetch();
  if ($current && in_array($current['is_new'], ['GUI', 'APPLYING'], true)) {
    $pdo->rollBack();
    http_response_code(409);
    response(false, ['error' => 'A configuration change is already pending']);
  }
  $nxdnBoot = (int)($data['NxdnStartOnBoot'] ?? $current['nxdn_boot'] ?? 0);
  $nxdnId = (int)($data['NxdnId'] ?? $current['nxdn_id'] ?? 0);
  if ($nxdnBoot && $nxdnId < 1) {
    $pdo->rollBack();
    http_response_code(400);
    response(false, ['error' => 'Set your NXDN ID (1–65535) before enabling NXDN']);
  }

  // kein CREATE TABLE, keine Initialzeile – das erledigt das Backend
  // Single-row Upsert (id=1) + mark as new
  $sql = "
    INSERT INTO config_inbox (
      id, callsign, module, dmr_id, duplex, rxfreq, txfreq, latitude, longitude, height,
      location, description, url, reflector1, 
      ysf_suffix, ysf_startup, ysf_options,
      dmr_address, dmr_password, dmr_name,
      dstar_boot, dmr_boot, ysf_boot, nxdn_boot, nxdn_id, nxdn_ran, nxdn_static, apply_error, is_new
    ) VALUES (
      1, :callsign, :module, :dmr_id, :duplex, :rxfreq, :txfreq, :latitude, :longitude, :height,
      :location, :description, :url, :reflector1, 
      :ysf_suffix, :ysf_startup, :ysf_options,
      :dmr_address, :dmr_password, :dmr_name,
      :dstar_boot, :dmr_boot, :ysf_boot, :nxdn_boot, :nxdn_id, :nxdn_ran, :nxdn_static, '', 'GUI'
    )
    ON DUPLICATE KEY UPDATE
      callsign         = VALUES(callsign),
      module           = VALUES(module),
      dmr_id           = VALUES(dmr_id),
      duplex           = VALUES(duplex),
      rxfreq           = VALUES(rxfreq),
      txfreq           = VALUES(txfreq),
      latitude         = VALUES(latitude),
      longitude        = VALUES(longitude),
      height           = VALUES(height),
      location         = VALUES(location),
      description      = VALUES(description),
      url              = VALUES(url),
      reflector1       = VALUES(reflector1),
      ysf_suffix       = VALUES(ysf_suffix),
      ysf_startup      = VALUES(ysf_startup),
      ysf_options      = VALUES(ysf_options), 
      dmr_address      = VALUES(dmr_address),
      dmr_password     = VALUES(dmr_password),
      dmr_name         = VALUES(dmr_name),
      dstar_boot       = VALUES(dstar_boot),
      dmr_boot         = VALUES(dmr_boot),
      ysf_boot         = VALUES(ysf_boot),
      nxdn_boot        = VALUES(nxdn_boot),
      nxdn_id          = VALUES(nxdn_id),
      nxdn_ran         = VALUES(nxdn_ran),
      nxdn_static      = VALUES(nxdn_static),
      apply_error      = '',
      is_new           = 'GUI',
      updated_at       = CURRENT_TIMESTAMP
  ";

  $stmt = $pdo->prepare($sql);
  $stmt->execute([
    ':callsign'         => strtoupper($data['Callsign']),
    ':module'           => strtoupper(substr($data['Module'] ?? 'B', 0, 1)),
    ':dmr_id'           => $dmrId,
    ':duplex'           => $duplex,
    ':rxfreq'           => $rxfreq,
    ':txfreq'           => $txfreq,
    ':latitude'         => $lat,
    ':longitude'        => $lon,
    ':height'           => $height,
    ':location'         => $data['Location'] ?: null,
    ':description'      => $data['Description'] ?: null,
    ':url'              => $data['URL'] ?: null,
    ':reflector1'       => $reflector1Combined,
    ':ysf_suffix'       => $data['Suffix'] ?: null,
    ':ysf_startup'      => $data['Startup'] ?: null,
    ':ysf_options'      => $data['Options'] ?: null,
    ':dmr_address'      => $data['Address'] ?: null,
    ':dmr_password'     => $data['Password'] ?: null,
    ':dmr_name'         => $data['Name'] ?: null,
    ':dstar_boot'       => (int)($data['DstarStartOnBoot'] ?? $current['dstar_boot'] ?? 1),
    ':dmr_boot'         => (int)($data['DmrStartOnBoot'] ?? $current['dmr_boot'] ?? 1),
    ':ysf_boot'         => (int)($data['YsfStartOnBoot'] ?? $current['ysf_boot'] ?? 1),
    ':nxdn_boot'        => $nxdnBoot,
    ':nxdn_id'          => $nxdnId,
    ':nxdn_ran'         => (int)($data['NxdnRAN'] ?? $current['nxdn_ran'] ?? 1),
    ':nxdn_static'      => $data['NxdnStatic'] ?? $current['nxdn_static'] ?? '',
  ]);
  $pdo->commit();

  $count = 0;
  foreach ($expected as $k) {
    if ($data[$k] !== null && $data[$k] !== '') $count++;
  }
  response(true, [
    'stored_table' => 'config_inbox',
    'is_new' => 'GUI',
    'count' => $count,
  ]);

} catch (Throwable $e) {
  http_response_code(500);
  response(false, ['error' => $e->getMessage()]);
}
