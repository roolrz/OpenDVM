<?php
declare(strict_types=1);

// This endpoint is fixed; credentials and destination URLs never come from a browser URL.
const BM_API_BASE = 'https://api.brandmeister.network/v2/device/';

class BmError extends RuntimeException {
    public function __construct(string $message, public int $httpStatus = 502, public string $errorCode = 'BM_ERROR') {
        parent::__construct($message);
    }
}

function bm_has_key(?string $key): bool {
    return $key !== null && trim($key) !== '' && trim($key) !== '12345';
}

function bm_context(PDO $pdo, bool $lock = false): array {
    $row = $pdo->query('SELECT bm_api_key, is_new FROM config_inbox WHERE id=1' . ($lock ? ' FOR UPDATE' : ''))->fetch(PDO::FETCH_ASSOC);
    if (!$row) throw new BmError('Configuration is not initialized', 503);
    if (in_array($row['is_new'], ['GUI', 'APPLYING'], true)) {
        throw new BmError('Wait for the saved configuration to finish applying', 409, 'CONFIG_PENDING');
    }
    $gateway = @parse_ini_file('/etc/dmrgateway', true, INI_SCANNER_RAW);
    $host = @parse_ini_file('/etc/MMDVMHost.ini', true, INI_SCANNER_RAW);
    // Use the ID actually presented to BM, including a configured hotspot ESSID.
    $identity = trim((string)($gateway['DMR Network 1']['Id'] ?? ''));
    $duplex = (string)($host['General']['Duplex'] ?? '');
    if (is_file('/etc/opendvm-radios.json')) {
        $radios = json_decode((string)file_get_contents('/etc/opendvm-radios.json'), true);
        $device = $radios['bindings']['dmr'] ?? '';
        if ($device === '') $device = $host['Modem']['UARTPort'] ?? $host['Modem']['Port'] ?? '/dev/mmdvm';
        foreach (($radios['profiles'] ?? []) as $path => $profile) {
            if ($path === $device || (realpath($path) !== false && realpath($path) === realpath($device))) {
                $duplex = (string)$profile['duplex'];
                break;
            }
        }
    }
    if (!preg_match('/^[0-9]{6,9}$/D', $identity) || (int)$identity === 0 || !in_array($duplex, ['0', '1'], true)) {
        throw new BmError('Saved DMRGateway ID or duplex setting is invalid; save station settings first', 400, 'BAD_DEVICE');
    }
    return ['id' => $identity, 'duplex' => $duplex === '1', 'key' => (string)($row['bm_api_key'] ?? '')];
}

function bm_integer(mixed $value, int $minimum, int $maximum): int {
    if ((!is_int($value) && !is_string($value)) || !preg_match('/^[0-9]+$/D', (string)$value)
        || strlen((string)$value) > 10 || (int)$value < $minimum || (int)$value > $maximum) {
        throw new BmError('Invalid BM timeslot or talkgroup number', 400, 'BAD_GROUP');
    }
    return (int)$value;
}

function bm_pairs(mixed $data): array {
    if (is_array($data) && isset($data['data'])) $data = $data['data'];
    if (is_array($data) && isset($data['talkgroups'])) $data = $data['talkgroups'];
    if (!is_array($data) || !array_is_list($data)) throw new BmError('BM returned an invalid talkgroup list');
    $pairs = [];
    foreach ($data as $record) {
        if (!is_array($record)) throw new BmError('BM returned an invalid talkgroup record');
        try {
            $slot = bm_integer($record['slot'] ?? $record['timeslot'] ?? null, 0, 2);
            $tg = bm_integer($record['group'] ?? $record['talkgroup'] ?? $record['tg'] ?? $record['id'] ?? null, 1, 16777215);
        } catch (BmError) {
            throw new BmError('BM returned an invalid talkgroup record');
        }
        $pairs["$slot:$tg"] = ['slot' => $slot, 'tg' => $tg];
    }
    ksort($pairs, SORT_NATURAL);
    return $pairs;
}

function bm_groups(array $pairs): array {
    $groups = ['TS0' => [], 'TS1' => [], 'TS2' => []];
    foreach ($pairs as $pair) $groups['TS' . $pair['slot']][] = $pair['tg'];
    return $groups;
}

function bm_desired(mixed $groups): array {
    if (!is_array($groups) || count($groups) !== 3 || array_diff(['TS0', 'TS1', 'TS2'], array_keys($groups))) {
        throw new BmError('Provide the complete static TG lists for TS0, TS1 and TS2', 400);
    }
    $pairs = [];
    foreach ([0, 1, 2] as $slot) {
        if (!is_array($groups["TS$slot"]) || !array_is_list($groups["TS$slot"]) || count($groups["TS$slot"]) > 128) {
            throw new BmError('Invalid static TG list (maximum 128 per slot)', 400);
        }
        foreach ($groups["TS$slot"] as $value) {
            $tg = bm_integer($value, 1, 16777215);
            $pairs["$slot:$tg"] = ['slot' => $slot, 'tg' => $tg];
        }
    }
    ksort($pairs, SORT_NATURAL);
    return $pairs;
}

class BmClient {
    public function __construct(private array $context) {
        if (!bm_has_key($context['key'])) throw new BmError('Save a BrandMeister API v2 key first', 400, 'NO_API_KEY');
        if (preg_match('/[\r\n]/', $context['key'])) throw new BmError('Invalid BrandMeister API key', 401, 'BAD_API_KEY');
    }

    private function request(string $method, string $suffix = '', ?array $body = null, int $timeout = 6): mixed {
        $ch = curl_init(BM_API_BASE . $this->context['id'] . '/talkgroup' . $suffix);
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true, CURLOPT_CUSTOMREQUEST => $method,
            CURLOPT_HTTPHEADER => ['Authorization: Bearer ' . $this->context['key'],
                'Accept: application/json', 'Content-Type: application/json'],
            CURLOPT_CONNECTTIMEOUT => min(3, $timeout), CURLOPT_TIMEOUT => $timeout,
            CURLOPT_SSL_VERIFYPEER => true, CURLOPT_SSL_VERIFYHOST => 2,
            CURLOPT_PROTOCOLS => CURLPROTO_HTTPS, CURLOPT_FOLLOWLOCATION => false,
            CURLOPT_USERAGENT => 'OpenDVM BrandMeister manager',
        ]);
        if ($body !== null) curl_setopt($ch, CURLOPT_POSTFIELDS, json_encode($body, JSON_THROW_ON_ERROR));
        $output = curl_exec($ch);
        $status = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
        $curlError = curl_errno($ch);
        curl_close($ch);
        if ($output === false) {
            error_log("BM transport failed (cURL error $curlError)");
            throw new BmError('Cannot reach BrandMeister; check the connection and try refreshing');
        }
        if (in_array($status, [401, 403], true)) throw new BmError('BM rejected the API key or permission for this device', 401, 'BAD_API_KEY');
        if ($status < 200 || $status >= 300) throw new BmError("BrandMeister request failed (HTTP $status)");
        if ($method !== 'GET' && trim($output) === '') return null;
        try { $data = json_decode($output, true, 32, JSON_THROW_ON_ERROR); }
        catch (JsonException) { throw new BmError('BM returned invalid JSON; refresh before retrying'); }
        if (is_array($data) && (($data['success'] ?? null) === false || ($data['ok'] ?? null) === false
            || !empty($data['error']) || ($data['status'] ?? '') === 'error')) {
            throw new BmError('BM rejected the operation; refresh to check the current configuration');
        }
        if ($method === 'GET' && str_starts_with(ltrim($output), '{')
            && !isset($data['data']) && !isset($data['talkgroups'])) {
            throw new BmError('BM returned an unexpected talkgroup response');
        }
        return $data;
    }

    public function staticPairs(int $timeout = 6): array { return bm_pairs($this->request('GET', '', null, $timeout)); }
    public function dynamicPairs(): array { return bm_pairs($this->request('GET', '/dynamic', null, 3)); }

    public function snapshot(?array $pairs = null): array {
        $pairs ??= $this->staticPairs();
        return ['device' => ['id' => $this->context['id'], 'duplex' => $this->context['duplex']],
            'static' => bm_groups($pairs),
            'revision' => hash('sha256', json_encode([$this->context['id'], $this->context['duplex'], $pairs]))];
    }

    public function apply(array $desired, string $revision): array {
        $current = $this->staticPairs();
        if (!hash_equals($this->snapshot($current)['revision'], $revision)) {
            throw new BmError('BM configuration or saved device settings changed; load from BM again before applying', 409, 'STALE_STATE');
        }
        $add = array_diff_key($desired, $current);
        $remove = array_diff_key($current, $desired);
        foreach ($add as $pair) {
            if (!in_array($pair['slot'], $this->context['duplex'] ? [1, 2] : [0], true)) {
                throw new BmError('Duplex devices use TS1/TS2; simplex hotspots use BM slot 0', 400, 'BAD_SLOT');
            }
        }
        $deadline = microtime(true) + 25;
        // Add first: an unsuccessful addition must not remove existing bindings.
        foreach (['POST' => $add, 'DELETE' => $remove] as $method => $changes) {
            foreach ($changes as $pair) {
                $timeout = min(6, (int)($deadline - microtime(true)));
                if ($timeout < 1) throw new BmError('BM changes took too long; some may have applied. Load from BM before retrying');
                $suffix = $method === 'DELETE' ? '/' . $pair['slot'] . '/' . $pair['tg'] : '';
                $this->request($method, $suffix,
                    $method === 'POST' ? ['slot' => $pair['slot'], 'group' => $pair['tg']] : null, $timeout);
            }
        }
        $actual = $this->staticPairs(3);
        for ($attempt = 0; $actual !== $desired && $attempt < 2; $attempt++) {
            usleep(250000);
            $actual = $this->staticPairs(3);
        }
        if ($actual !== $desired) throw new BmError('BM has not confirmed all requested bindings; load from BM before retrying', 502, 'NOT_CONFIRMED');
        return $this->snapshot($actual);
    }
}
