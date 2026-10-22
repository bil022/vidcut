<?php
/**
 * vidcut 网页编辑器（PHP 版）：功能与 web.py 相同，页面同为 web/index.html。
 *
 * 用法一（PHP 自带服务器，推荐）：
 *   cd showcase
 *   PHP_CLI_SERVER_WORKERS=4 php -S 127.0.0.1:8765 index.php
 *   VIDCUT_WORK=work_real PHP_CLI_SERVER_WORKERS=4 php -S 127.0.0.1:8765 index.php   # 其他工作目录
 * 用法二（Apache）：把 showcase 放到网站目录下，.htaccess 会把所有请求交给 index.php。
 *
 * 环境变量：
 *   VIDCUT_WORK       工作目录（默认 ./work，相对路径以本文件所在目录为准）
 *   VIDCUT_ASR        加入新素材时的语音识别：auto（默认，Apple Silicon 用 mlx-whisper）/ mlx-whisper / faster-whisper / none
 *   VIDCUT_ASR_MODEL  Whisper 模型（默认用 models/ 下已下载的模型）
 *   VIDCUT_UV         uv 的路径（找不到时用）
 *
 * PHP 是按请求运行的，没有常驻线程：后台任务（渲染、加入新素材）作为独立进程启动，
 * 状态和日志写在 <工作目录>/.job/ 下，前端轮询 api/job 时读取。
 */

declare(strict_types=1);

const SCHEMA_VERSION = 2;
const TRANSITIONS = ['cut', 'fade', 'fadeblack', 'fadewhite', 'dissolve', 'wipeleft', 'wiperight', 'slideleft',
                     'slideright', 'smoothleft', 'smoothright', 'circleopen', 'circleclose', 'radial', 'zoomin', 'distance'];
const CONTENT_TYPES = ['mp4' => 'video/mp4', 'jpg' => 'image/jpeg', 'srt' => 'text/plain; charset=utf-8'];

$HERE = __DIR__;
$work = getenv('VIDCUT_WORK') ?: 'work';
$WORK = str_starts_with($work, '/') ? $work : "$HERE/$work";

// ───────── 工具函数 ─────────

function load_json(string $path, $default = null)
{
    return is_file($path) ? json_decode(file_get_contents($path), true) : $default;
}

function send_json($data, int $status = 200): void
{
    http_response_code($status);
    header('Content-Type: application/json; charset=utf-8');
    header('Cache-Control: no-store');
    echo json_encode($data, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
}

function read_body(): string
{
    $n = (int)($_SERVER['CONTENT_LENGTH'] ?? 0);
    if ($n > 5 << 20) {
        throw new RuntimeException('请求太大');
    }
    return (string)file_get_contents('php://input');
}

function media(): array
{
    global $WORK;
    return (load_json("$WORK/media.json") ?? [])['media'] ?? [];
}

function send_file(string $path, bool $cache, bool $download = false): void
{
    if (!is_file($path)) {
        send_json(['error' => 'not found'], 404);
        return;
    }
    $size = filesize($path);
    [$start, $end, $status] = [0, $size - 1, 200];
    $rng = trim($_SERVER['HTTP_RANGE'] ?? '');
    if (preg_match('/^bytes=(\d*)-(\d*)$/', $rng, $m)) {
        if ($m[1] !== '') {
            $start = (int)$m[1];
            $end = $m[2] !== '' ? min((int)$m[2], $size - 1) : $size - 1;
        } elseif ($m[2] !== '') {
            $start = max(0, $size - (int)$m[2]);
        }
        if ($start > $end) {
            http_response_code(416);
            header("Content-Range: bytes */$size");
            return;
        }
        $status = 206;
    }
    http_response_code($status);
    header('Content-Type: ' . (CONTENT_TYPES[pathinfo($path, PATHINFO_EXTENSION)] ?? 'application/octet-stream'));
    header('Accept-Ranges: bytes');
    header('Content-Length: ' . ($end - $start + 1));
    header('Cache-Control: ' . ($cache ? 'max-age=3600' : 'no-cache'));
    if ($status === 206) {
        header("Content-Range: bytes $start-$end/$size");
    }
    if ($download) {
        header("Content-Disposition: attachment; filename*=UTF-8''" . rawurlencode(basename($path)));
    }
    while (ob_get_level()) {
        ob_end_clean();
    }
    $f = fopen($path, 'rb');
    fseek($f, $start);
    $left = $end - $start + 1;
    while ($left > 0 && !feof($f) && !connection_aborted()) {  // 浏览器拖动进度条时会主动断开旧请求
        $chunk = fread($f, min(1 << 16, $left));
        echo $chunk;
        flush();
        $left -= strlen($chunk);
    }
    fclose($f);
}

// ───────── 状态与校验 ─────────

function state(): array
{
    global $WORK;
    $media = [];
    foreach (media() as $mid => $m) {
        $d = "$WORK/derived/$mid";
        $row = [];
        foreach (['name', 'duration_ms', 'width', 'height', 'portrait', 'created', 'gps', 'has_audio', 'fps'] as $k) {
            $row[$k] = $m[$k] ?? null;
        }
        $row['summary'] = load_json("$d/summary.json");
        $tr = load_json("$d/transcript.json", []);
        $row['transcript'] = $tr['segments'] ?? [];
        $row['language'] = $tr['language'] ?? null;
        $media[$mid] = $row;
    }
    $data = load_json("$WORK/media.json") ?? [];
    return ['work' => $WORK, 'project' => load_json("$WORK/project.json"),
            'media' => (object)$media, 'sources' => $data['sources'] ?? [], 'job' => job_state(),
            'default_folder' => getenv('VIDCUT_DEFAULT_FOLDER') ?: ''];
}

function outputs(): array
{
    global $WORK;
    $files = array_merge(glob("$WORK/out/*.mp4") ?: [], glob("$WORK/out/*.srt") ?: []);
    usort($files, fn($a, $b) => filemtime($b) <=> filemtime($a));
    return array_map(fn($f) => ['name' => basename($f), 'size' => filesize($f), 'mtime' => filemtime($f),
                                'url' => 'out/' . rawurlencode(basename($f))], $files);
}

function backup(string $path, string $prefix): void
{
    global $WORK;
    if (is_file($path)) {
        @mkdir("$WORK/history", 0777, true);
        $t = microtime(true);
        copy($path, "$WORK/history/$prefix-" . date('Ymd-His', (int)$t) . sprintf('-%06d', ($t - floor($t)) * 1e6) . '.json');
    }
}

/** 同步运行 vidcut.py（读取/校验设置用，很快）；返回 [退出码, 输出] */
function run_vidcut(array $args): array
{
    global $HERE;
    $cmd = 'cd ' . escapeshellarg($HERE) . ' && export PATH=' . escapeshellarg(job_path()) . '; '
        . implode(' ', array_map('escapeshellarg', array_merge(vidcut_cmd(), $args))) . ' 2>&1';
    exec($cmd, $out, $code);
    return [$code, implode("\n", $out)];
}

function settings_get(): array
{
    global $WORK;
    @mkdir($WORK, 0777, true);
    [$code, $out] = run_vidcut(['settings']);
    return [load_json("$WORK/settings.json"), $code === 0 ? '' : $out];
}

/** 写入后用 vidcut.py settings 校验；不通过就恢复原文件 */
function settings_put(string $raw): array
{
    global $WORK;
    $file = "$WORK/settings.json";
    $old = is_file($file) ? file_get_contents($file) : null;
    backup($file, 'settings');
    file_put_contents($file, json_encode(json_decode($raw), JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES));
    [$code, $out] = run_vidcut(['settings']);
    if ($code === 0) {
        return [];
    }
    $old === null ? unlink($file) : file_put_contents($file, $old);
    $lines = array_filter(array_map('trim', explode("\n", $out)),
        fn($l) => $l !== '' && !str_contains($l, '有问题') && !str_contains($l, 'punycode') && !str_contains($l, 'trace-deprecation'));
    return array_values($lines) ?: ['设置无效'];
}


function validate($p): array
{
    $errs = [];
    $media = media();
    if (!is_array($p) || ($p['schema_version'] ?? null) !== SCHEMA_VERSION) {
        return ['schema_version 必须是 ' . SCHEMA_VERSION];
    }
    $cfg = $p['project'] ?? [];
    $res = $cfg['resolution'] ?? null;
    $resOk = is_array($res) && array_is_list($res) && count($res) === 2;
    foreach ($resOk ? $res : [] as $x) {
        $resOk = $resOk && is_int($x) && $x > 0 && $x % 2 === 0;
    }
    if (!$resOk) {
        $errs[] = 'resolution 必须是两个正偶数';
    }
    $fps = $cfg['fps'] ?? null;
    if (!(is_int($fps) && $fps >= 1 && $fps <= 120)) {
        $errs[] = 'fps 必须是 1–120 的整数';
    }
    if (!in_array($cfg['portrait_mode'] ?? null, ['blur_fill', 'pad'], true)) {
        $errs[] = 'portrait_mode 只能是 blur_fill 或 pad';
    }
    if (!is_string($cfg['name'] ?? null) || trim($cfg['name']) === '') {
        $errs[] = '项目名不能为空';
    }
    if (array_key_exists('subtitles', $cfg) && $cfg['subtitles'] !== null) {
        $sc = $cfg['subtitles'];
        if (!(is_array($sc) && is_bool($sc['burn_in'] ?? false)
            && in_array($sc['show'] ?? 'both', ['both', 'original', 'translation'], true))) {
            $errs[] = 'subtitles 设置无效：burn_in 为 true/false，show 为 both / original / translation';
        }
    }
    $tracks = $p['tracks'] ?? null;
    if (!(is_array($tracks) && $tracks && ($tracks[0]['kind'] ?? null) === 'video')) {
        return array_merge($errs, ['第一条轨道必须是 video']);
    }
    $seen = [];
    foreach ($tracks[0]['items'] ?? [] as $i => $it) {
        $tag = '第 ' . ($i + 1) . ' 项（' . (($it['name'] ?? '') ?: ($it['id'] ?? '')) . '）';
        $id = $it['id'] ?? null;
        if (isset($seen[(string)$id])) {
            $errs[] = "$tag: id 重复";
        }
        $seen[(string)$id] = true;
        if (array_key_exists('transition', $it) && $it['transition'] !== null) {
            $tr = $it['transition'];
            $td = is_array($tr) ? ($tr['duration_ms'] ?? 600) : null;
            if (!(is_array($tr) && in_array($tr['type'] ?? null, TRANSITIONS, true) && is_int($td) && $td >= 100 && $td <= 3000)) {
                $errs[] = "$tag: 转场无效（类型见 settings.json 说明，时长 0.1–3 秒）";
            }
        }
        $type = $it['type'] ?? null;
        if ($type === 'clip') {
            $m = $media[$it['media_id'] ?? ''] ?? null;
            if ($m === null) {
                $errs[] = "$tag: 找不到媒体 " . ($it['media_id'] ?? '');
                continue;
            }
            $a = $it['in_ms'] ?? null;
            $b = $it['out_ms'] ?? null;
            if (!(is_int($a) && is_int($b) && 0 <= $a && $a < $b && $b <= $m['duration_ms'])) {
                $errs[] = "$tag: 入点/出点必须满足 0 ≤ 入点 < 出点 ≤ {$m['duration_ms']}";
                continue;
            }
            if ($b - $a < 200) {
                $errs[] = "$tag: 片段至少 0.2 秒";
            }
            $v = $it['volume_db'] ?? 0;
            if (!((is_int($v) || is_float($v)) && $v >= -30 && $v <= 30)) {
                $errs[] = "$tag: 音量须在 -30 到 30 dB 之间";
            }
            $fi = $it['fade_in_ms'] ?? 0;
            $fo = $it['fade_out_ms'] ?? 0;
            if (!(is_int($fi) && is_int($fo) && $fi >= 0 && $fo >= 0 && $fi + $fo <= $b - $a)) {
                $errs[] = "$tag: 淡入淡出必须是非负整数，且总和不超过片段长度";
            }
            if (array_key_exists('subtitles', $it) && $it['subtitles'] !== null) {
                $ok = is_array($it['subtitles']) && array_is_list($it['subtitles']);
                foreach ($ok ? $it['subtitles'] : [] as $x) {
                    $ok = $ok && is_array($x) && is_int($x['start_ms'] ?? null) && is_int($x['end_ms'] ?? null)
                        && $x['start_ms'] < $x['end_ms'] && is_string($x['text'] ?? '') && mb_strlen($x['text'] ?? '') <= 200
                        && is_string($x['translation'] ?? '') && mb_strlen($x['translation'] ?? '') <= 300;
                }
                if (!$ok) {
                    $errs[] = "$tag: 字幕格式无效（每句需要 start_ms < end_ms，原文最多 200 字，译文最多 300 字）";
                }
            }
            if (array_key_exists('caption', $it) && $it['caption'] !== null) {
                $cap = $it['caption'];
                if (!(is_array($cap) && is_string($cap['text'] ?? null) && mb_strlen($cap['text']) <= 60)) {
                    $errs[] = "$tag: 画面文字最多 60 个字";
                } elseif (!in_array($cap['position'] ?? null, ['bottom', 'center', 'top'], true)) {
                    $errs[] = "$tag: 文字位置只能是 bottom / center / top";
                } elseif (!(is_int($cap['start_ms'] ?? null) && is_int($cap['end_ms'] ?? null)
                    && 0 <= $cap['start_ms'] && $cap['start_ms'] < $cap['end_ms'] && $cap['end_ms'] <= $b - $a)) {
                    $errs[] = sprintf('%s: 文字的显示时间须在片段范围内（0–%.1fs）', $tag, ($b - $a) / 1000);
                }
            }
        } elseif ($type === 'title') {
            $text = $it['text'] ?? null;
            if (!(is_string($text) && mb_strlen(trim($text)) > 0 && mb_strlen(trim($text)) <= 40)) {
                $errs[] = "$tag: 标题文字须为 1–40 个字";
            }
            $sub = $it['subtitle'] ?? '';
            if (!(is_string($sub) && mb_strlen($sub) <= 60)) {
                $errs[] = "$tag: 副标题最多 60 个字";
            }
            $d = $it['duration_ms'] ?? null;
            if (!(is_int($d) && $d >= 500 && $d <= 15000)) {
                $errs[] = "$tag: 标题卡时长须在 0.5–15 秒之间";
                continue;
            }
            if (!in_array($it['background'] ?? 'black', ['black', 'blur_next'], true)) {
                $errs[] = "$tag: 背景只能是 black 或 blur_next";
            }
            $fi = $it['fade_in_ms'] ?? 0;
            $fo = $it['fade_out_ms'] ?? 0;
            if (!(is_int($fi) && is_int($fo) && $fi >= 0 && $fo >= 0 && $fi + $fo <= $d)) {
                $errs[] = "$tag: 淡入淡出必须是非负整数，且总和不超过时长";
            }
        } elseif ($type === 'gap') {
            $d = $it['duration_ms'] ?? null;
            if (!(is_int($d) && $d >= 100 && $d <= 60000)) {
                $errs[] = "$tag: 间隔须在 0.1–60 秒之间";
            }
            if (!preg_match('/^(?:[a-z]+|0x[0-9a-fA-F]{6})$/', (string)($it['background'] ?? 'black'))) {
                $errs[] = "$tag: 背景色只能是颜色名或 0xRRGGBB";
            }
        } else {
            $errs[] = "$tag: 未知类型 " . json_encode($type, JSON_UNESCAPED_UNICODE);
        }
    }
    return $errs;
}

function save_project(string $raw): string
{
    global $WORK;
    // 按对象解码再写回，空对象 {} 不会变成 []
    $obj = json_decode($raw);
    $file = "$WORK/project.json";
    $lock = fopen("$WORK/.project.lock", 'c');
    flock($lock, LOCK_EX);
    backup($file, 'project');
    file_put_contents("$file.tmp", json_encode($obj, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES));
    rename("$file.tmp", $file);
    flock($lock, LOCK_UN);
    return date('Y-m-d\TH:i:s');
}

// ───────── 后台任务（独立进程 + 状态文件） ─────────

function job_dir(): string
{
    global $WORK;
    $d = "$WORK/.job";
    if (!is_dir($d)) {
        mkdir($d, 0777, true);
    }
    return $d;
}

function pid_alive(int $pid): bool
{
    return $pid > 0 && (function_exists('posix_kill') ? posix_kill($pid, 0) : trim((string)shell_exec("kill -0 $pid 2>/dev/null && echo 1")) === '1');
}

/** 读取任务状态；任务结束时根据日志末尾的退出码定稿 */
function job_state(): array
{
    $dir = job_dir();
    $job = load_json("$dir/job.json") ?? ['kind' => null, 'status' => 'idle', 'draft' => true, 'output' => null];
    $lines = is_file("$dir/job.log") ? file("$dir/job.log", FILE_IGNORE_NEW_LINES) : [];
    $log = [];
    $exit = null;
    foreach ($lines as $line) {
        $line = rtrim($line);
        if (preg_match('/^__VIDCUT_EXIT__ (\d+)$/', $line, $m)) {
            $exit = (int)$m[1];
            continue;
        }
        if ($line !== '' && !str_contains($line, 'punycode') && !str_contains($line, 'trace-deprecation')) {
            $log[] = $line;
        }
        if (preg_match('/完成：(\S+\.mp4)/u', $line, $m)) {
            $job['output'] = 'out/' . basename($m[1]);
        }
    }
    if ($job['status'] === 'running') {
        if ($exit !== null) {
            $job['status'] = ($exit === 0 && ($job['kind'] !== 'render' || $job['output'])) ? 'done' : 'failed';
        } elseif (!pid_alive((int)($job['pid'] ?? 0))) {
            $log[] = '任务进程已退出（可能被中断）';
            $job['status'] = 'failed';
        }
        if ($job['status'] !== 'running') {
            file_put_contents("$dir/job.json", json_encode($job, JSON_UNESCAPED_UNICODE));
        }
    }
    $job['log'] = $log;
    unset($job['pid']);
    return $job;
}

function find_uv(): ?string
{
    $cands = [getenv('VIDCUT_UV') ?: '', trim((string)shell_exec('command -v uv 2>/dev/null')),
              (getenv('HOME') ?: '') . '/.local/bin/uv', '/opt/homebrew/bin/uv', '/usr/local/bin/uv'];
    foreach ($cands as $c) {
        if ($c !== '' && is_executable($c)) {
            return $c;
        }
    }
    return null;
}

function vidcut_cmd(bool $withAsr = false): array
{
    global $HERE, $WORK;
    // 用 uv 运行，自动带上 vidcut.py 声明的依赖（Pillow 用于画标题文字）
    if ($uv = find_uv()) {
        $cmd = [$uv, 'run'];
        if ($withAsr && ($asr = asr_package())) {
            array_push($cmd, '--with', $asr);
        }
        return array_merge($cmd, ["$HERE/vidcut.py", '-w', $WORK]);
    }
    return ['python3', "$HERE/vidcut.py", '-w', $WORK];
}

function asr_package(): ?string
{
    $asr = getenv('VIDCUT_ASR') ?: 'auto';
    if ($asr === 'auto') {
        return (PHP_OS_FAMILY === 'Darwin' && php_uname('m') === 'arm64') ? 'mlx-whisper' : null;
    }
    return $asr === 'none' ? null : $asr;
}

function asr_model(): ?string
{
    global $HERE;
    if ($m = getenv('VIDCUT_ASR_MODEL')) {
        return $m;
    }
    $local = asr_package() === 'mlx-whisper' ? glob("$HERE/models/*whisper*") : [];
    sort($local);
    return $local[0] ?? null;
}

function job_home(): string
{
    return getenv('HOME') ?: (function_exists('posix_getpwuid') ? posix_getpwuid(posix_geteuid())['dir'] : '/tmp');
}

/** Apache 等环境的 PATH 很短，补上常见的 ffmpeg / uv 位置 */
function job_path(): string
{
    return implode(':', array_filter([getenv('PATH'), '/opt/homebrew/bin', '/usr/local/bin', job_home() . '/.local/bin', '/usr/bin', '/bin']));
}

function start_job(string $kind, array $cmd, bool $draft = true): bool
{
    global $HERE;
    $dir = job_dir();
    $lock = fopen("$dir/.lock", 'c');
    flock($lock, LOCK_EX);
    try {
        if (job_state()['status'] === 'running') {
            return false;
        }
        file_put_contents("$dir/job.log", '');
        $home = job_home();
        $path = job_path();
        $inner = 'cd ' . escapeshellarg($HERE) . ' && export PATH=' . escapeshellarg($path) . ' HOME=' . escapeshellarg($home)
            . ' PYTHONUNBUFFERED=1; ' . implode(' ', array_map('escapeshellarg', $cmd))
            . '; echo "__VIDCUT_EXIT__ $?"';
        $pid = (int)trim((string)shell_exec('nohup sh -c ' . escapeshellarg($inner)
            . ' >> ' . escapeshellarg("$dir/job.log") . ' 2>&1 < /dev/null & echo $!'));
        file_put_contents("$dir/job.json", json_encode(['kind' => $kind, 'status' => 'running', 'draft' => $draft,
            'output' => null, 'pid' => $pid, 'started' => date('c')], JSON_UNESCAPED_UNICODE));
        return true;
    } finally {
        flock($lock, LOCK_UN);
    }
}

// ───────── 路由 ─────────

$uri = rawurldecode(parse_url($_SERVER['REQUEST_URI'] ?? '/', PHP_URL_PATH) ?? '/');
$base = rtrim(str_replace('\\', '/', dirname($_SERVER['SCRIPT_NAME'] ?? '/')), '/');
$route = ($base !== '' && str_starts_with($uri, $base)) ? substr($uri, strlen($base)) : $uri;
if ($route === '' || $route === '/index.php') {
    $route = '/';
}
$method = $_SERVER['REQUEST_METHOD'] ?? 'GET';
set_time_limit(0);

if ($method === 'GET' || $method === 'HEAD') {
    if ($route === '/' || $route === '/index.html') {
        header('Content-Type: text/html; charset=utf-8');
        header('Cache-Control: no-cache');
        readfile("$HERE/web/index.html");
    } elseif ($route === '/api/state') {
        send_json(state());
    } elseif ($route === '/api/job') {
        send_json(job_state());
    } elseif ($route === '/api/outputs') {
        send_json(outputs());
    } elseif ($route === '/api/settings') {
        [$st, $err] = settings_get();
        send_json(['settings' => $st, 'error' => $err], $st ? 200 : 500);
    } elseif (preg_match('#^/media/(m_[0-9a-f]{16})/(proxy\.mp4|thumb\.jpg|kf_\d+\.jpg)$#', $route, $m)) {
        if (!array_key_exists($m[1], media())) {
            send_json(['error' => 'unknown media'], 404);
        } else {
            send_file("$WORK/derived/{$m[1]}/{$m[2]}", true);
        }
    } elseif (preg_match('#^/out/([^/\\\\]+\.(?:mp4|srt))$#u', $route, $m)) {
        send_file("$WORK/out/{$m[1]}", false, isset($_GET['download']));
    } else {
        send_json(['error' => 'not found'], 404);
    }
} elseif ($method === 'PUT' && $route === '/api/settings') {
    $raw = read_body();
    if (!is_array(json_decode($raw, true))) {
        send_json(['errors' => ['设置必须是 JSON 对象']], 400);
    } elseif (job_state()['status'] === 'running') {
        send_json(['errors' => ['后台任务进行中，请等它完成再改设置']], 409);
    } elseif ($errs = settings_put($raw)) {
        send_json(['errors' => $errs], 400);
    } else {
        send_json(['saved_at' => date('Y-m-d\TH:i:s')]);
    }
} elseif ($method === 'PUT' && $route === '/api/project') {
    try {
        $raw = read_body();
        $p = json_decode($raw, true, 512, JSON_THROW_ON_ERROR);
    } catch (Throwable $e) {
        send_json(['errors' => ['JSON 无效: ' . $e->getMessage()]], 400);
        return;
    }
    if ($errs = validate($p)) {
        send_json(['errors' => $errs], 400);
        return;
    }
    send_json(['saved_at' => save_project($raw)]);
} elseif ($method === 'POST' && in_array($route, ['/api/render', '/api/update', '/api/auto'], true)) {
    try {
        $body = json_decode(read_body() ?: '{}', true) ?: [];
    } catch (Throwable) {
        $body = [];
    }
    if ($route === '/api/render') {
        $draft = (bool)($body['draft'] ?? true);
        $started = start_job('render', array_merge(vidcut_cmd(), ['render'], $draft ? ['--draft'] : []), $draft);
    } elseif ($route === '/api/auto') {
        $folder = trim((string)($body['folder'] ?? ''));
        if ($folder !== '' && str_starts_with($folder, '~/')) {
            $folder = (getenv('HOME') ?: '') . substr($folder, 1);
        }
        if ($folder !== '' && !is_dir($folder)) {
            send_json(['error' => "目录不存在：$folder"], 400);
            return;
        }
        if ($folder === '' && !is_file("$WORK/media.json")) {
            send_json(['error' => '请填写素材目录'], 400);
            return;
        }
        $cmd = array_merge(vidcut_cmd(true), ['auto'], $folder !== '' ? [$folder] : []);
        $name = trim((string)($body['name'] ?? ''));
        if ($name !== '') {
            array_push($cmd, '--name', $name);
        }
        if (!empty($body['rebuild'])) {
            $cmd[] = '--rebuild';  // vidcut.py 覆盖 project.json 前会自己备份到 history/
        }
        if (!empty($body['final'])) {
            $cmd[] = '--final';
        }
        if ($model = asr_model()) {
            array_push($cmd, '--model', $model);
        }
        $started = start_job('auto', $cmd);
    } else {
        $folder = trim((string)($body['folder'] ?? ''));
        if ($folder !== '' && str_starts_with($folder, '~/')) {
            $folder = (getenv('HOME') ?: '') . substr($folder, 1);
        }
        if ($folder !== '' && !is_dir($folder)) {
            send_json(['error' => "目录不存在：$folder"], 400);
            return;
        }
        $cmd = array_merge(vidcut_cmd(true), ['update'], $folder !== '' ? [$folder] : []);
        if ($model = asr_model()) {
            array_push($cmd, '--model', $model);
        }
        $started = start_job('update', $cmd);
    }
    if (!$started) {
        send_json(['error' => '已有后台任务在进行，请等它完成'], 409);
        return;
    }
    send_json(job_state(), 202);
} else {
    send_json(['error' => 'not found'], 404);
}
