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

const { app, BrowserWindow, ipcMain, Tray, Menu, nativeImage } = require('electron');
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
// Log layout (see ARCHITECTURE.md §3):
//   * Dev mode  -> <repo>/logs/<TS>/   (kept under repo for easy inspection)
//   * Packaged  -> %LOCALAPPDATA%\HandQ\logs\<TS>\
//
// Logs deliberately live in LocalAppData, NOT in app.getPath('userData')
// (Roaming) — they are large, machine-specific, and shouldn't follow the user
// across machines. User-owned data (config, session History) lives in
// %USERPROFILE%\HandQ\ instead, which is the bridge's per-user root.
function packagedLogBase() {
    const localAppData =
        process.env.LOCALAPPDATA ||
        path.join(app.getPath('home'), 'AppData', 'Local');
    return path.join(localAppData, 'HandQ', 'logs');
}
const LOG_BASE = app.isPackaged
    ? packagedLogBase()
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
let tray = null;
let isQuitting = false;
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

// Procedurally-built 16x16 tray icon — a white "H" on a tinted-blue square,
// with the corner pixels chipped to suggest a rounded shape. We build the PNG
// in-memory (zlib + manual CRC32) so the renderer doesn't need a bundled file
// asset; if a user-supplied electron/tray-icon.png is dropped in alongside,
// it takes precedence.
const TRAY_ICON_FILE = path.join(__dirname, 'tray-icon.png');

const _crcTable = (() => {
    const t = new Uint32Array(256);
    for (let i = 0; i < 256; i++) {
        let c = i;
        for (let k = 0; k < 8; k++) {
            c = (c & 1) ? (0xedb88320 ^ (c >>> 1)) : (c >>> 1);
        }
        t[i] = c >>> 0;
    }
    return t;
})();

function _pngCrc32(buf) {
    let c = 0xffffffff;
    for (let i = 0; i < buf.length; i++) {
        c = _crcTable[(c ^ buf[i]) & 0xff] ^ (c >>> 8);
    }
    return (c ^ 0xffffffff) >>> 0;
}

function _pngChunk(type, data) {
    const len = Buffer.alloc(4);
    len.writeUInt32BE(data.length, 0);
    const typeBuf = Buffer.from(type, 'ascii');
    const crcInput = Buffer.concat([typeBuf, data]);
    const crc = Buffer.alloc(4);
    crc.writeUInt32BE(_pngCrc32(crcInput), 0);
    return Buffer.concat([len, typeBuf, data, crc]);
}

function buildHandqIconPng() {
    const zlib = require('zlib');
    const SIZE = 16;
    // Pixel pattern: 'B' = tint background, 'F' = white foreground,
    // '.' = transparent (corner chip).
    const PATTERN = [
        '..BBBBBBBBBBBB..',
        '.BBBBBBBBBBBBBB.',
        'BBFFBBBBBBBBFFBB',
        'BBFFBBBBBBBBFFBB',
        'BBFFBBBBBBBBFFBB',
        'BBFFBBBBBBBBFFBB',
        'BBFFBBBBBBBBFFBB',
        'BBFFFFFFFFFFFFBB',
        'BBFFFFFFFFFFFFBB',
        'BBFFBBBBBBBBFFBB',
        'BBFFBBBBBBBBFFBB',
        'BBFFBBBBBBBBFFBB',
        'BBFFBBBBBBBBFFBB',
        'BBFFBBBBBBBBFFBB',
        '.BBBBBBBBBBBBBB.',
        '..BBBBBBBBBBBB..',
    ];
    const TINT = [60, 124, 224, 255];   // Apple-ish blue
    const FORE = [255, 255, 255, 255];  // white H
    const TRANS = [0, 0, 0, 0];

    const rowBytes = SIZE * 4 + 1; // 1 filter byte per row + RGBA
    const raw = Buffer.alloc(rowBytes * SIZE);
    for (let y = 0; y < SIZE; y++) {
        raw[y * rowBytes] = 0; // filter: None
        for (let x = 0; x < SIZE; x++) {
            const ch = PATTERN[y][x];
            const c = ch === 'F' ? FORE : ch === 'B' ? TINT : TRANS;
            const idx = y * rowBytes + 1 + x * 4;
            raw[idx]     = c[0];
            raw[idx + 1] = c[1];
            raw[idx + 2] = c[2];
            raw[idx + 3] = c[3];
        }
    }

    // IHDR
    const ihdr = Buffer.alloc(13);
    ihdr.writeUInt32BE(SIZE, 0);
    ihdr.writeUInt32BE(SIZE, 4);
    ihdr[8]  = 8; // bit depth
    ihdr[9]  = 6; // color type RGBA
    ihdr[10] = 0; // compression
    ihdr[11] = 0; // filter
    ihdr[12] = 0; // interlace

    const idat = zlib.deflateSync(raw);
    const sig  = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
    return Buffer.concat([
        sig,
        _pngChunk('IHDR', ihdr),
        _pngChunk('IDAT', idat),
        _pngChunk('IEND', Buffer.alloc(0)),
    ]);
}

function buildTrayIcon() {
    // 1) User override: electron/tray-icon.png if a real artist drops one in.
    try {
        if (fs.existsSync(TRAY_ICON_FILE)) {
            const img = nativeImage.createFromPath(TRAY_ICON_FILE);
            if (img && !img.isEmpty()) return img;
        }
    } catch (_) { /* fall through */ }
    // 2) Procedurally-built 16x16 H-on-blue.
    try {
        const png = buildHandqIconPng();
        const img = nativeImage.createFromBuffer(png);
        if (img && !img.isEmpty()) return img;
    } catch (err) {
        logLine('TRAY', 'PNG synthesis failed', { err: err && err.message });
    }
    return nativeImage.createEmpty();
}

function ensureTray() {
    if (tray) return;
    try {
        tray = new Tray(buildTrayIcon());
    } catch (err) {
        logLine('TRAY', 'tray create failed', { err: err && err.message });
        return;
    }
    tray.setToolTip('HandQ');
    tray.setContextMenu(Menu.buildFromTemplate([
        {
            label: 'Show HandQ',
            click: () => {
                if (!mainWindow) return;
                if (mainWindow.isMinimized()) mainWindow.restore();
                mainWindow.show();
                mainWindow.focus();
            },
        },
        { type: 'separator' },
        {
            label: 'Quit',
            click: () => {
                isQuitting = true;
                app.quit();
            },
        },
    ]));
    tray.on('click', () => {
        if (!mainWindow) return;
        if (mainWindow.isVisible()) {
            mainWindow.focus();
        } else {
            mainWindow.show();
            mainWindow.focus();
        }
    });
    logLine('TRAY', 'tray installed');
}

function createWindow() {
    logLine('MAIN', 'creating BrowserWindow');
    mainWindow = new BrowserWindow({
        width: 1200,
        height: 800,
        minWidth: 720,
        minHeight: 480,
        title: 'HandQ',
        frame: false,
        titleBarStyle: 'hidden',
        // Match the page's gradient base so resizing never reveals a strip
        // of OS desktop colour around the edge. The "liquid glass" look is
        // produced by CSS gradients + backdrop-filter, not by OS-level
        // transparency (which causes flicker on Win resize).
        backgroundColor: '#f4f6fb',
        autoHideMenuBar: true,
        webPreferences: {
            preload: path.join(__dirname, 'preload.js'),
            contextIsolation: true,
            nodeIntegration: false,
            sandbox: true,
        },
    });
    // No native menu bar (Alt won't reveal it either).
    mainWindow.setMenuBarVisibility(false);

    mainWindow.loadFile(path.join(__dirname, 'renderer', 'index.html'));

    // Close behavior: hide to tray instead of quitting, unless the user picked
    // "Quit" from the tray menu (sets isQuitting). Minimize is left alone so
    // it still goes to the taskbar like a normal window.
    mainWindow.on('close', (e) => {
        if (!isQuitting) {
            e.preventDefault();
            mainWindow.hide();
            ensureTray();
            logLine('MAIN', 'main window close intercepted; hidden to tray');
        }
    });

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
        ensureTray();
        createWindow();

        app.on('activate', () => {
            if (BrowserWindow.getAllWindows().length === 0) {
                createWindow();
            } else if (mainWindow) {
                mainWindow.show();
                mainWindow.focus();
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

// Custom titlebar -> main control IPC. The window is frameless, so the
// renderer ships its own min/max/close buttons and asks main to drive them.
ipcMain.on('window:minimize', () => {
    if (mainWindow && !mainWindow.isDestroyed()) mainWindow.minimize();
});
ipcMain.on('window:toggle-maximize', () => {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    if (mainWindow.isMaximized()) {
        mainWindow.unmaximize();
    } else {
        mainWindow.maximize();
    }
});
ipcMain.on('window:hide', () => {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    mainWindow.hide();
    ensureTray();
});

// --- graceful shutdown -----------------------------------------------------

app.on('before-quit', (event) => {
    isQuitting = true;
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
    // We hide-to-tray on the user's "close" click rather than destroy the
    // window, so this fires only when the window is actually destroyed (e.g.
    // tray "Quit"). Reap the bridge unconditionally on Windows/Linux; on
    // macOS apps stay alive until Cmd-Q.
    logLine('MAIN', 'window-all-closed', { platform: process.platform });
    if (process.platform !== 'darwin') {
        app.quit();
    }
});
