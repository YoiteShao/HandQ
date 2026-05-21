// HandQ Electron main process.
//
// Responsibilities:
//   * Acquire a single-instance lock.
//   * Create one BrowserWindow with the contextIsolated preload.
//   * Spawn the Python bridge as a long-lived child process, with UTF-8 forced
//     on both ends (PYTHONUTF8=1, PYTHONIOENCODING=utf-8).
//   * Forward each line-delimited JSON line emitted on the bridge's stdout to
//     the renderer via webContents.send('handq:event', evt).
//   * Forward renderer ipcMain.handle requests onto the bridge's stdin as
//     single JSON lines.
//   * Drive an orderly shutdown on app 'before-quit' (send {"type":"shutdown"}
//     on stdin, then child.kill() after a 2-second timeout).

'use strict';

const { app, BrowserWindow, ipcMain } = require('electron');
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');
const readline = require('readline');

// --- configuration ---------------------------------------------------------

// Repository root in dev = one level above this file (electron/ lives at the
// repo root). Used as the default location for logs/ when packaged builds
// don't override it.
const REPO_ROOT = path.resolve(__dirname, '..');

// Bridge launch resolution. Two modes:
//
//   * Dev (app.isPackaged === false):
//       bridgeCmd  = python interpreter (overridable via HANDQ_PYTHON)
//       bridgeArgs = [<repo>/bridge_main.py]
//
//   * Prod (app.isPackaged === true):
//       The Nuitka standalone build produces bridge_main.exe (renamed to
//       handq-bridge.exe in our pack step) plus a sibling _internal/ folder.
//       electron-builder ships those alongside HandQ.exe under the install
//       root, so we resolve the exe relative to app.getPath('exe').
//
// The bridge self-locates handq_config.yaml from its own install dir
// (see bridge_main.py: INSTALL_DIR + resolve_config_path), so we do NOT
// pin a cwd — that was the cwd-coupled bug we just removed.
const BRIDGE_EXE_NAME =
    process.platform === 'win32' ? 'handq-bridge.exe' : 'handq-bridge';
const DEV_PYTHON =
    process.env.HANDQ_PYTHON ||
    (process.platform === 'win32' ? 'python' : 'python3');
const DEV_BRIDGE_SCRIPT = path.join(REPO_ROOT, 'bridge_main.py');

function resolveBridgeLaunch() {
    if (app.isPackaged) {
        const installDir = path.dirname(app.getPath('exe'));
        const exePath = path.join(installDir, BRIDGE_EXE_NAME);
        return { cmd: exePath, args: [], mode: 'packaged' };
    }
    return { cmd: DEV_PYTHON, args: [DEV_BRIDGE_SCRIPT], mode: 'dev' };
}

// Hard timeout (ms) granted to the bridge between the shutdown JSON line and
// the SIGTERM/TerminateProcess signal we send afterwards.
const SHUTDOWN_GRACE_MS = 2000;

// --- per-launch log directory ---------------------------------------------
//
// Every Electron launch gets its own <repo>/logs/<YYYYMMDD-HHMMSS>/ directory,
// shared by both frontend (handq-frontend.log) and the Python bridge
// (handq-bridge.log, written by bridge_main.py which inherits HANDQ_LOG_DIR
// via the spawn env). This is computed at module load — well before the
// BrowserWindow is created — so logLine() can use it from the very first
// call without a lazy app-ready gate.

function computeLaunchTimestamp() {
    // ISO: 2024-05-20T18:04:49.123Z → strip [-:.TZ] → 20240520180449123 → take 14
    // → 20240520180449 → reformat as YYYYMMDD-HHMMSS.
    const compact = new Date().toISOString().replace(/[-:.TZ]/g, '').slice(0, 14);
    return compact.slice(0, 8) + '-' + compact.slice(8, 14);
}

const LAUNCH_TS = computeLaunchTimestamp();
// In dev, logs sit beside the repo for easy inspection. In a packaged build,
// the resources/ tree is read-only (and may live inside app.asar), so we use
// the per-user writable location app.getPath('userData') instead.
const LOG_BASE = app.isPackaged
    ? path.join(app.getPath('userData'), 'logs')
    : path.join(REPO_ROOT, 'logs');
const LOG_DIR = path.join(LOG_BASE, LAUNCH_TS);
try {
    fs.mkdirSync(LOG_DIR, { recursive: true });
} catch (_) { /* best-effort; logLine still falls back gracefully */ }

// --- logging ---------------------------------------------------------------
//
// Every log line is timestamped (ISO 8601), prefixed with a component label,
// echoed to console, and appended to <LOG_DIR>/handq-frontend.log.
// A logging failure must never crash the app — the file write is wrapped in
// try/catch and silently swallowed.

const LOG_FILE = path.join(LOG_DIR, 'handq-frontend.log');

function logLine(component, msg, extra) {
    const ts = new Date().toISOString();
    let line = '[' + ts + '] [' + component + '] ' + msg;
    if (extra !== undefined) {
        try { line += ' ' + JSON.stringify(extra); } catch (_) { line += ' [unserialisable]'; }
    }
    // Console mirror — visible to the terminal that launched `electron .`.
    try { console.log(line); } catch (_) { /* ignore */ }
    // File mirror — best-effort; never throw.
    if (LOG_FILE) {
        try { fs.appendFileSync(LOG_FILE, line + '\n'); } catch (_) { /* swallow */ }
    }
}

// Strip API_KEY (and the legacy api_key) out of any payload we log
// (porting_design.md §(2.8) lets the renderer write the key directly into
// YAML; we must not echo it to disk).
function redactApiKey(payload) {
    if (!payload || typeof payload !== 'object') return payload;
    if (Array.isArray(payload)) return payload.map(redactApiKey);
    const out = {};
    for (const k of Object.keys(payload)) {
        const v = payload[k];
        if (k === 'API_KEY' || k === 'api_key' || k === 'api_key_env') {
            out[k] = (v === undefined || v === null || v === '') ? v : '<redacted>';
        } else if (v && typeof v === 'object') {
            out[k] = redactApiKey(v);
        } else {
            out[k] = v;
        }
    }
    return out;
}

// --- module-level state ----------------------------------------------------

let mainWindow = null;
let pythonChild = null;
let stdoutReader = null;
let isShuttingDown = false;

// --- helpers ---------------------------------------------------------------

function sendToRenderer(evt) {
    if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send('handq:event', evt);
    } else {
        logLine('MAIN', 'sendToRenderer dropped (window destroyed)',
                { type: evt && evt.type });
    }
}

function writeToBridge(obj) {
    if (!pythonChild || !pythonChild.stdin || pythonChild.stdin.destroyed) {
        logLine('IPC-OUT', 'bridge stdin unavailable',
                { type: obj && obj.type, id: obj && obj.id });
        return false;
    }
    try {
        pythonChild.stdin.write(JSON.stringify(obj) + '\n');
        logLine('IPC-OUT', 'bridge stdin write',
                { type: obj && obj.type, id: obj && obj.id });
        return true;
    } catch (err) {
        logLine('IPC-OUT', 'bridge stdin write failed',
                { type: obj && obj.type, id: obj && obj.id,
                  err: err && err.message });
        sendToRenderer({
            type: 'error',
            where: 'bridge',
            message: 'failed to write to bridge stdin: ' + (err && err.message),
            fatal: false,
        });
        return false;
    }
}

function spawnBridge() {
    const env = Object.assign({}, process.env, {
        PYTHONUTF8: '1',
        PYTHONIOENCODING: 'utf-8',
        HANDQ_LOG_DIR: LOG_DIR,
    });

    const launch = resolveBridgeLaunch();

    // Log the resolved spawn parameters and the env *keys only* — never the
    // values, which may contain API keys, tokens, and other secrets.
    logLine('MAIN', 'spawning bridge', {
        mode: launch.mode,
        cmd: launch.cmd,
        args: launch.args,
        env_keys: Object.keys(env),
    });

    const child = spawn(
        launch.cmd,
        launch.args,
        {
            stdio: ['pipe', 'pipe', 'pipe'],
            env: env,
            // No cwd is set on purpose: the bridge self-locates handq_config
            // .yaml from its own install dir (bridge_main.py: INSTALL_DIR),
            // so the child's cwd is irrelevant.
        }
    );

    child.stdout.setEncoding('utf8');
    child.stderr.setEncoding('utf8');

    // Line-delimited JSON: one envelope per line. readline handles partial
    // chunks, CRLF on Windows, and trailing-newline edge cases for us.
    stdoutReader = readline.createInterface({
        input: child.stdout,
        crlfDelay: Infinity,
    });

    stdoutReader.on('line', (line) => {
        const trimmed = line.trim();
        if (!trimmed) return;

        // Truncate the raw payload for the log; parse the type out separately
        // so the structured field is queryable independent of payload size.
        const truncated = trimmed.length > 500
            ? trimmed.slice(0, 500) + '…(' + trimmed.length + ')'
            : trimmed;
        let evtType = null;
        let evt;
        try {
            evt = JSON.parse(trimmed);
            evtType = evt && evt.type;
        } catch (err) {
            logLine('BRIDGE-OUT', 'malformed JSON from bridge',
                    { err: err && err.message, raw: truncated });
            sendToRenderer({
                type: 'error',
                where: 'bridge',
                message: 'malformed JSON on stdout: ' + err.message,
                raw: trimmed.slice(0, 500),
                fatal: false,
            });
            return;
        }
        logLine('BRIDGE-OUT', 'line', {
            evt_type: evtType,
            id: evt && evt.id,
            raw: truncated,
        });
        sendToRenderer(evt);
    });

    // stderr is reserved for backend logging (see porting_design.md §(2)).
    // We surface it to the main-process console so a developer running with
    // `electron .` from a terminal can see Python tracebacks. Full chunk is
    // also appended to the frontend log file.
    child.stderr.on('data', (chunk) => {
        process.stderr.write('[bridge] ' + chunk);
        const text = String(chunk);
        // Strip a single trailing newline so the log line isn't double-broken.
        const stripped = text.endsWith('\n') ? text.slice(0, -1) : text;
        logLine('BRIDGE-ERR', stripped);
    });

    child.on('error', (err) => {
        logLine('MAIN', 'bridge spawn error', { err: err && err.message });
        sendToRenderer({
            type: 'error',
            where: 'bridge',
            message: 'failed to spawn bridge (' + launch.cmd + '): ' + err.message,
            fatal: true,
        });
    });

    child.on('exit', (code, signal) => {
        logLine('MAIN', 'bridge exit', { code: code, signal: signal });
        sendToRenderer({
            type: 'status',
            kind: 'bridge_exit',
            code: code,
            signal: signal,
        });
    });

    return child;
}

function createWindow() {
    logLine('MAIN', 'creating BrowserWindow');
    mainWindow = new BrowserWindow({
        width: 1200,
        height: 800,
        title: 'HandQ',
        webPreferences: {
            preload: path.join(__dirname, 'preload.js'),
            contextIsolation: true,
            nodeIntegration: false,
            sandbox: true,
        },
    });

    mainWindow.loadFile(path.join(__dirname, 'renderer', 'index.html'));

    mainWindow.on('closed', () => {
        logLine('MAIN', 'main window closed');
        mainWindow = null;
    });
}

// --- single-instance lock --------------------------------------------------

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
    app.quit();
} else {
    app.on('second-instance', () => {
        logLine('MAIN', 'second-instance attempted; focusing existing window');
        if (mainWindow) {
            if (mainWindow.isMinimized()) mainWindow.restore();
            mainWindow.focus();
        }
    });

    app.whenReady().then(() => {
        logLine('MAIN', 'app ready', {
            userData: app.getPath('userData'),
            log_dir: LOG_DIR,
            log_file: LOG_FILE,
        });
        pythonChild = spawnBridge();
        createWindow();

        app.on('activate', () => {
            if (BrowserWindow.getAllWindows().length === 0) {
                createWindow();
            }
        });
    });
}

// --- IPC handlers ----------------------------------------------------------

ipcMain.handle('handq:sendRequest', (_event, msg) => {
    logLine('IPC-IN', 'handq:sendRequest', {
        type: msg && msg.type,
        id: msg && msg.id,
    });
    const ok = writeToBridge(msg);
    logLine('IPC-IN', 'handq:sendRequest done', { id: msg && msg.id, ok: ok });
    return ok;
});

ipcMain.handle('handq:getConfig', (_event, id) => {
    logLine('IPC-IN', 'handq:getConfig', { id: id });
    const ok = writeToBridge({ type: 'config_get', id: id });
    logLine('IPC-IN', 'handq:getConfig done', { id: id, ok: ok });
    return ok;
});

ipcMain.handle('handq:setConfig', (_event, payload) => {
    // payload = { id, config } — config may carry an api_key field which we
    // must redact before logging.
    const safe = redactApiKey(payload);
    logLine('IPC-IN', 'handq:setConfig', {
        id: safe && safe.id,
        config: safe && safe.config,
    });
    const ok = writeToBridge({
        type: 'config_set',
        id: payload && payload.id,
        config: payload && payload.config,
    });
    logLine('IPC-IN', 'handq:setConfig done', { id: payload && payload.id, ok: ok });
    return ok;
});

// Renderer/preload-originated log forwarding. Preload runs in an isolated
// world and cannot write to fs directly; it forwards every log line to us so
// all frontend logs land in a single file.
ipcMain.on('handq:log', (_event, payload) => {
    if (!payload || typeof payload !== 'object') return;
    const component = payload.component || 'PRELOAD';
    const msg = payload.msg || '';
    logLine(component, msg, payload.extra);
});

// --- graceful shutdown -----------------------------------------------------

app.on('before-quit', (event) => {
    if (isShuttingDown) {
        return; // second click — let the default quit flow proceed.
    }
    if (!pythonChild || pythonChild.exitCode !== null) {
        logLine('SHUTDOWN', 'before-quit: bridge already gone, no action');
        return;
    }

    isShuttingDown = true;
    event.preventDefault();
    const t0 = Date.now();
    logLine('SHUTDOWN', 'shutdown initiated', {
        grace_ms: SHUTDOWN_GRACE_MS,
        bridge_pid: pythonChild.pid,
    });

    // 1. Send the shutdown envelope so the bridge can run its 4-step teardown
    //    (flow._interrupt_event.set() → cancel_all_tasks() → svc.close() →
    //    InteractionManager.reset_instance()).
    writeToBridge({ type: 'shutdown', id: 'app-quit' });
    logLine('SHUTDOWN', 'shutdown envelope sent',
            { elapsed_ms: Date.now() - t0 });

    // 2. Hard-kill after the grace budget regardless of whether the child
    //    has exited cleanly. On Windows child.kill() maps to TerminateProcess;
    //    on POSIX it sends SIGTERM.
    const killTimer = setTimeout(() => {
        logLine('SHUTDOWN', 'grace budget elapsed; killing bridge',
                { elapsed_ms: Date.now() - t0 });
        if (pythonChild && pythonChild.exitCode === null) {
            try { pythonChild.kill(); } catch (_) { /* ignore */ }
        }
        app.exit(0);
    }, SHUTDOWN_GRACE_MS);

    pythonChild.once('exit', (code, signal) => {
        clearTimeout(killTimer);
        logLine('SHUTDOWN', 'bridge clean exit during shutdown',
                { code: code, signal: signal, elapsed_ms: Date.now() - t0 });
        app.exit(0);
    });
});

app.on('window-all-closed', () => {
    // On macOS apps usually stay alive until Cmd-Q; on Windows/Linux quit
    // immediately so the bridge child is reaped.
    logLine('MAIN', 'window-all-closed', { platform: process.platform });
    if (process.platform !== 'darwin') {
        app.quit();
    }
});
