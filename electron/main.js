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

const { app, BrowserWindow, ipcMain, Tray, Menu, nativeImage, globalShortcut, Notification, dialog, shell } = require('electron');
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');
const readline = require('readline');
const { checkForUpdates } = require('./updater');

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
// Log layout (see ARCHITECTURE.md §3 + bridge_main.py:_LOG_BASE):
//   * Windows (any mode)         -> %USERPROFILE%\HandQ\logs\<TS>\
//   * Linux/macOS, packaged      -> <install_dir>/logs/<TS>\
//   * Linux/macOS, dev           -> <repo>/logs/<TS>\
//
// Per ARCHITECTURE.md §1.5, every user-owned HandQ artifact on Windows
// belongs under %USERPROFILE%\HandQ\, alongside config, History, and
// personality. This now applies in dev mode too so the path matches the
// architecture independently of how Electron was launched. Linux/macOS
// have no equivalent "user root" convention; co-locating with the bridge
// install keeps everything self-contained — same dir as the bridge exe.
// The diag tree sits at logs\.dia\ as a hidden subdirectory (bridge_main.py
// applies the NTFS hidden attribute on Windows).
// bridge_main._prune_old_log_dirs keeps only the most recent 30 launch
// directories so this does not grow without bound.
function platformLogBase() {
    if (process.platform === 'win32') {
        const userProfile =
            process.env.USERPROFILE ||
            app.getPath('home');
        return path.join(userProfile, 'HandQ', 'logs');
    }
    // Linux / macOS — install dir for packaged, repo root for dev (they
    // are the same directory in dev: REPO_ROOT).
    if (app.isPackaged) {
        return path.join(path.dirname(app.getPath('exe')), 'logs');
    }
    return path.join(REPO_ROOT, 'logs');
}
const LOG_BASE = platformLogBase();
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
let _trayFlashTimer = null;
let stdoutReader = null;
let isShuttingDown = false;

// --- bridge crash diagnostics ----------------------------------------------
//
// When `handq-bridge.exe` (or `python bridge_main.py`) dies before the
// Electron renderer finishes booting, the user is left staring at a stuck
// "Starting…" screen with no actionable info. We catch that case by:
//   * recording the spawn time + a flag that flips true once any IPC line
//     arrives or `boot_progress phase=stdio_loop_ready` lands;
//   * keeping the last STDERR_RING_SIZE bridge stderr lines in memory;
// on `exit` (when not user-initiated), if the bridge never booted OR died
// inside the startup grace window, we show a dialog with the tail of stderr,
// the log file path, and offer "Open Log Folder", "Reset Config & Relaunch",
// "Quit". One-shot — `_crashDialogShown` makes sure we don't loop.

const STDERR_RING_SIZE = 50;
const STARTUP_GRACE_MS = 10000;
let bridgeStartedAt = 0;
let bridgeBooted = false;
let stderrRing = [];
let _crashDialogShown = false;

// --- desktop takeover overlay ----------------------------------------------
//
// When the agent invokes a desktop input action (click_at / type_text / drag /
// scroll / hotkey / key_press), the Python side emits
//   {type:"status", kind:"desktop_takeover_started", reason:"input_action"}
// We respond by spawning a fullscreen frameless transparent BrowserWindow that
// renders a rainbow border + corner watermark (electron/overlay/overlay.html)
// and registering Ctrl+Shift+C as a process-wide revoke hotkey. On
//   {type:"status", kind:"desktop_takeover_ended", reason:"..."}
// we close the window and unregister the shortcut. See docs/desktop_tool.md
// §11 for the full IPC contract.
//
// We DO NOT use Ctrl+C for revoke even though docs §11.4 mentioned it — that
// would hijack the system-wide copy shortcut for the entire duration of the
// task, which is too aggressive a tradeoff.

let takeoverOverlay = null;
const TAKEOVER_REVOKE_ACCELERATOR = 'Control+Shift+C';

function showTakeoverOverlay() {
    if (takeoverOverlay && !takeoverOverlay.isDestroyed()) {
        // Backend's _start_takeover is idempotent but a duplicate event
        // could still arrive on edge cases. Don't double-create.
        return;
    }
    logLine('OVERLAY', 'show takeover overlay');

    try {
        takeoverOverlay = new BrowserWindow({
            frame: false,
            transparent: true,
            alwaysOnTop: true,
            focusable: false,
            skipTaskbar: true,
            fullscreen: true,
            hasShadow: false,
            resizable: false,
            movable: false,
            minimizable: false,
            maximizable: false,
            closable: false,
            backgroundColor: '#00000000',
            // Overlay is purely presentational — no preload, no IPC.
            webPreferences: {
                contextIsolation: true,
                sandbox: true,
                nodeIntegration: false,
            },
        });
    } catch (err) {
        logLine('OVERLAY', 'create BrowserWindow failed',
                { err: err && err.message });
        takeoverOverlay = null;
        return;
    }

    // Forward mouse events so the agent's clicks reach the underlying app.
    try {
        takeoverOverlay.setIgnoreMouseEvents(true, { forward: true });
    } catch (err) {
        logLine('OVERLAY', 'setIgnoreMouseEvents failed',
                { err: err && err.message });
    }
    // "screen-saver" beats most fullscreen apps; fall back silently if the
    // platform rejects the level.
    try {
        takeoverOverlay.setAlwaysOnTop(true, 'screen-saver');
    } catch (_) { /* ignore */ }

    takeoverOverlay.loadFile(path.join(__dirname, 'overlay', 'overlay.html'))
        .catch((err) => {
            logLine('OVERLAY', 'loadFile failed', { err: err && err.message });
        });

    // Hold off on setting visibleOnAllWorkspaces; default behaviour follows the
    // user's active virtual desktop, which is what we want.

    takeoverOverlay.on('closed', () => {
        takeoverOverlay = null;
    });

    // Register the revoke hotkey. Registration can fail if another app
    // already owns the combo — log it but continue showing the overlay so
    // the user still sees the indicator.
    try {
        const ok = globalShortcut.register(TAKEOVER_REVOKE_ACCELERATOR, () => {
            logLine('OVERLAY', 'revoke hotkey fired');
            writeToBridge({ type: 'user_input', kind: 'desktop_takeover_revoked' });
        });
        if (!ok) {
            logLine('OVERLAY', 'revoke hotkey register returned false',
                    { accelerator: TAKEOVER_REVOKE_ACCELERATOR });
        }
    } catch (err) {
        logLine('OVERLAY', 'revoke hotkey register error',
                { err: err && err.message });
    }
}

function hideTakeoverOverlay() {
    // Always free the shortcut even if the window object is already gone.
    try {
        if (globalShortcut.isRegistered(TAKEOVER_REVOKE_ACCELERATOR)) {
            globalShortcut.unregister(TAKEOVER_REVOKE_ACCELERATOR);
        }
    } catch (err) {
        logLine('OVERLAY', 'revoke hotkey unregister error',
                { err: err && err.message });
    }
    if (!takeoverOverlay || takeoverOverlay.isDestroyed()) {
        takeoverOverlay = null;
        return;
    }
    logLine('OVERLAY', 'hide takeover overlay');
    try {
        takeoverOverlay.destroy();
    } catch (err) {
        logLine('OVERLAY', 'destroy failed', { err: err && err.message });
    }
    takeoverOverlay = null;
}

// --- global hotkey (toggle window visibility) --------------------------------

const HOTKEY_SETTINGS_FILE = path.join(
    app.isPackaged
        ? path.join(process.env.LOCALAPPDATA || path.join(app.getPath('home'), 'AppData', 'Local'), 'HandQ')
        : __dirname,
    'hotkey.json'
);
const DEFAULT_HOTKEY = 'Ctrl+Alt+W';
let currentHotkey = DEFAULT_HOTKEY;

function loadHotkeySetting() {
    try {
        if (fs.existsSync(HOTKEY_SETTINGS_FILE)) {
            const data = JSON.parse(fs.readFileSync(HOTKEY_SETTINGS_FILE, 'utf8'));
            if (data && data.hotkey) return data.hotkey;
        }
    } catch (_) { /* use default */ }
    return DEFAULT_HOTKEY;
}

function saveHotkeySetting(hotkey) {
    try {
        const dir = path.dirname(HOTKEY_SETTINGS_FILE);
        fs.mkdirSync(dir, { recursive: true });
        fs.writeFileSync(HOTKEY_SETTINGS_FILE, JSON.stringify({ hotkey: hotkey }, null, 2), 'utf8');
    } catch (err) {
        logLine('HOTKEY', 'save failed', { err: err && err.message });
    }
}

function toggleWindowVisibility() {
    if (!mainWindow) return;
    if (mainWindow.isVisible() && mainWindow.isFocused()) {
        mainWindow.hide();
        ensureTray();
    } else {
        mainWindow.show();
        if (mainWindow.isMinimized()) mainWindow.restore();
        mainWindow.focus();
    }
}

function registerHotkey(accelerator) {
    globalShortcut.unregisterAll();
    if (!accelerator) return false;
    try {
        const ok = globalShortcut.register(accelerator, toggleWindowVisibility);
        if (ok) {
            logLine('HOTKEY', 'registered', { accelerator: accelerator });
            currentHotkey = accelerator;
            return true;
        }
        logLine('HOTKEY', 'register failed (already in use?)', { accelerator: accelerator });
        return false;
    } catch (err) {
        logLine('HOTKEY', 'register error', { accelerator: accelerator, err: err && err.message });
        return false;
    }
}

// --- helpers ---------------------------------------------------------------

function sendToRenderer(evt) {
    if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send('handq:event', evt);
    } else {
        logLine('MAIN', 'sendToRenderer dropped (window destroyed)',
                { type: evt && evt.type });
    }
}

// Show a system toast + flash the taskbar when a confirmation is needed and
// the window is not in focus (hidden to tray or minimized). Clicking the
// notification brings the window to the front.
function notifyConfirmationNeeded(evt) {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    const windowNeedsAttention =
        !mainWindow.isVisible() ||
        mainWindow.isMinimized() ||
        !mainWindow.isFocused();
    if (!windowNeedsAttention) return;

    const kind = evt && evt.kind;
    let title, body;
    if (kind === 'secret_input') {
        title = '🔑 HandQ — input required';
        body  = String((evt && evt.prompt) || 'Enter a value to continue.');
    } else if (kind === 'ask_human') {
        title = '❓ HandQ — agent question';
        body  = String((evt && evt.prompt) || 'The agent has a question for you.');
    } else if (kind === 'risk_confirmation') {
        title = '⚠️ HandQ — high-risk operation';
        body  = String((evt && evt.description) || 'Agent wants to run a high-risk command.');
    } else {
        const tool = String((evt && evt.tool) || 'tool');
        title = '🛠️ HandQ — approval needed';
        body  = String((evt && evt.description) || ('Agent wants to use the ' + tool + ' tool.'));
    }
    // Truncate body so it fits in the OS notification area.
    if (body.length > 120) body = body.slice(0, 117) + '…';

    if (Notification.isSupported()) {
        const note = new Notification({ title, body, silent: false });
        note.on('click', () => {
            if (!mainWindow || mainWindow.isDestroyed()) return;
            if (mainWindow.isMinimized()) mainWindow.restore();
            mainWindow.show();
            mainWindow.focus();
        });
        note.show();
        logLine('NOTIFY', 'toast shown', { kind, title });
    }

    // Flash the taskbar button / tray icon regardless of toast support.
    try { mainWindow.flashFrame(true); } catch (_) { /* ignore */ }
    startTrayFlash();
}

function notifyTaskCompleted(evt) {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    const windowNeedsAttention =
        !mainWindow.isVisible() ||
        mainWindow.isMinimized() ||
        !mainWindow.isFocused();
    if (!windowNeedsAttention) return;

    const summary = String((evt && evt.summary) || '');
    const title = 'HandQ — 任务完成';
    let body = summary || '任务已完成。';
    if (body.length > 120) body = body.slice(0, 117) + '…';

    if (Notification.isSupported()) {
        const note = new Notification({ title, body, silent: false });
        note.on('click', () => {
            if (!mainWindow || mainWindow.isDestroyed()) return;
            if (mainWindow.isMinimized()) mainWindow.restore();
            mainWindow.show();
            mainWindow.focus();
        });
        note.show();
        logLine('NOTIFY', 'task_completed toast shown');
    }

    try { mainWindow.flashFrame(true); } catch (_) { /* ignore */ }
    startTrayFlash();
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

    // Reset crash-detection state for this spawn.
    bridgeStartedAt = Date.now();
    bridgeBooted = false;
    stderrRing = [];

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
        // Mark bridge as booted once we see any non-error envelope (or the
        // explicit stdio_loop_ready phase). Used by the early-exit detector
        // in child.on('exit') below.
        if (!bridgeBooted) {
            if (evtType === 'status' && evt && evt.kind === 'boot_progress'
                && evt.phase === 'stdio_loop_ready') {
                bridgeBooted = true;
            } else if (evtType && evtType !== 'error') {
                bridgeBooted = true;
            }
        }
        // Desktop takeover overlay control. We act on these BEFORE
        // forwarding to the renderer so the overlay reaction isn't
        // blocked by a slow renderer process. The renderer is also free
        // to react (e.g. show a status pill) — both can happen.
        if (evtType === 'status' && evt && typeof evt.kind === 'string') {
            if (evt.kind === 'desktop_takeover_started') {
                try { showTakeoverOverlay(); }
                catch (err) {
                    logLine('OVERLAY', 'showTakeoverOverlay threw',
                            { err: err && err.message });
                }
            } else if (evt.kind === 'desktop_takeover_ended') {
                try { hideTakeoverOverlay(); }
                catch (err) {
                    logLine('OVERLAY', 'hideTakeoverOverlay threw',
                            { err: err && err.message });
                }
            }
        }
        // Notify the user when a confirmation is needed and the window is
        // hidden / minimized — a toast + taskbar flash so they don't miss it.
        // Bridge emits {type:"status", kind:"risk_confirmation"|"tool_confirmation"|"secret_input"}.
        if (evtType === 'status' && evt && (
            evt.kind === 'risk_confirmation' ||
            evt.kind === 'tool_confirmation' ||
            evt.kind === 'secret_input' ||
            evt.kind === 'ask_human'
        )) {
            try { notifyConfirmationNeeded(evt); }
            catch (err) {
                logLine('NOTIFY', 'notifyConfirmationNeeded threw',
                        { err: err && err.message });
            }
        }
        // Notify the user when a task completes and the window is not in focus.
        if (evtType === 'status' && evt && evt.kind === 'task_completed') {
            try { notifyTaskCompleted(evt); }
            catch (err) {
                logLine('NOTIFY', 'notifyTaskCompleted threw',
                        { err: err && err.message });
            }
        }
        sendToRenderer(evt);
    });

    // stderr is reserved for backend logging (see porting_design.md §(2)).
    // Python's logging.StreamHandler is wired to sys.stderr because stdout
    // is the JSON IPC channel — so EVERY log record (DEBUG/INFO/WARN/ERROR)
    // arrives here, not just errors. Tag the line as BRIDGE-LOG so the
    // frontend log doesn't mislead readers into thinking every record
    // is an error; the actual level is in the line content.
    child.stderr.on('data', (chunk) => {
        process.stderr.write('[bridge] ' + chunk);
        const text = String(chunk);
        // Strip a single trailing newline so the log line isn't double-broken.
        const stripped = text.endsWith('\n') ? text.slice(0, -1) : text;
        logLine('BRIDGE-LOG', stripped);
        // Buffer for the crash dialog. Split on newlines so multi-line
        // tracebacks don't get glued together visually. Cap at
        // STDERR_RING_SIZE total lines.
        for (const ln of stripped.split(/\r?\n/)) {
            if (!ln) continue;
            stderrRing.push(ln);
            if (stderrRing.length > STDERR_RING_SIZE) stderrRing.shift();
        }
    });

    child.on('error', (err) => {
        logLine('MAIN', 'bridge spawn error', { err: err && err.message });
        sendToRenderer({
            type: 'error',
            where: 'bridge',
            message: 'failed to spawn bridge (' + launch.cmd + '): ' + err.message,
            fatal: true,
        });
        // For spawn errors (ENOENT etc.) the 'exit' event may not fire in all
        // Electron/Node.js versions, leaving the boot overlay stuck forever.
        // The IPC message above may also be dropped if the renderer hasn't
        // registered its listeners yet (race on fast-failing spawns).
        // Show the native crash dialog immediately — it works regardless of
        // renderer readiness and gives the user actionable options.
        if (!isQuitting && !isShuttingDown && !_crashDialogShown) {
            _crashDialogShown = true;
            logLine('MAIN', 'bridge spawn error; showing crash dialog',
                    { elapsed_ms: Date.now() - bridgeStartedAt });
            showBridgeCrashDialog({
                code: null, signal: null, elapsed: Date.now() - bridgeStartedAt,
            });
        }
    });

    child.on('exit', (code, signal) => {
        logLine('MAIN', 'bridge exit', { code: code, signal: signal });
        // If the bridge died mid-takeover, the user is left with a
        // rainbow border and no Python to honour Ctrl+Shift+C. Hide it.
        try { hideTakeoverOverlay(); } catch (_) { /* ignore */ }
        sendToRenderer({
            type: 'status',
            kind: 'bridge_exit',
            code: code,
            signal: signal,
        });
        // Crash detector — only when the user did NOT initiate the quit.
        const elapsed = Date.now() - bridgeStartedAt;
        const isStartupFailure = !bridgeBooted || elapsed < STARTUP_GRACE_MS;
        if (!isQuitting && !isShuttingDown && !_crashDialogShown
                && isStartupFailure) {
            _crashDialogShown = true;
            logLine('MAIN', 'bridge early-exit detected; showing crash dialog',
                    { elapsed_ms: elapsed, booted: bridgeBooted });
            showBridgeCrashDialog({ code, signal, elapsed });
        }
    });

    return child;
}

// --- bridge crash dialog ---------------------------------------------------
//
// Called when the bridge child exits before booting (or within the startup
// grace window) and the user did not initiate the quit. We surface what
// happened with the tail of bridge stderr, the log file path, and three
// actions:
//   * "Open Log Folder & Quit" — for the user to send us logs;
//   * "Reset Config & Relaunch" — rename %USERPROFILE%\HandQ\handq_config.yaml
//     to handq_config.yaml.broken-<TS> so first-run path re-seeds from the
//     install default; then app.relaunch() + app.quit();
//   * "Quit" — give up.
// Reset Config is the foot-gun button (it shadows the user's API key and
// any custom switches), so we add a confirmation message and the user can
// recover the broken file after the relaunch.
function showBridgeCrashDialog({ code, signal, elapsed }) {
    const tail = stderrRing.slice(-20).join('\n');
    const exitDesc = signal
        ? `signal=${signal}`
        : `exit code=${code === null ? 'null' : code}`;
    const detail = [
        `Bridge ${exitDesc} after ${elapsed}ms`
            + (bridgeBooted ? '' : ' (never reached stdio_loop_ready)'),
        `Log file: ${LOG_FILE}`,
        '',
        '── Last bridge stderr ────────────────────────────',
        tail || '(no stderr captured)',
    ].join('\n');

    dialog.showMessageBox({
        type: 'error',
        title: 'HandQ 启动失败',
        message: 'HandQ 后端进程未能正常启动。',
        detail,
        buttons: ['打开日志目录并退出', '重置配置并重启', '退出'],
        defaultId: 0,
        cancelId: 2,
        noLink: true,
    }).then(({ response }) => {
        if (response === 0) {
            try { shell.openPath(LOG_DIR); } catch (e) {
                logLine('MAIN', 'openPath(LOG_DIR) failed',
                        { err: e && e.message });
            }
            isQuitting = true;
            app.quit();
        } else if (response === 1) {
            try { resetUserConfig(); } catch (e) {
                logLine('MAIN', 'resetUserConfig failed',
                        { err: e && e.message });
            }
            isQuitting = true;
            app.relaunch();
            app.quit();
        } else {
            isQuitting = true;
            app.quit();
        }
    }).catch((err) => {
        logLine('MAIN', 'showBridgeCrashDialog dialog error',
                { err: err && err.message });
        isQuitting = true;
        app.quit();
    });
}

function resetUserConfig() {
    const home = process.env.USERPROFILE || app.getPath('home');
    const cfg = path.join(home, 'HandQ', 'handq_config.yaml');
    if (!fs.existsSync(cfg)) {
        logLine('MAIN', 'resetUserConfig: no config file to reset', { cfg });
        return;
    }
    const ts = new Date().toISOString().replace(/[-:.TZ]/g, '').slice(0, 14);
    const broken = `${cfg}.broken-${ts}`;
    fs.renameSync(cfg, broken);
    logLine('MAIN', 'resetUserConfig: renamed broken config',
            { from: cfg, to: broken });
}

// Procedurally-built 16x16 tray icon — a white "H" on a tinted-blue square,
// with the corner pixels chipped to suggest a rounded shape. We build the PNG
// in-memory (zlib + manual CRC32) so the renderer doesn't need a bundled file
// asset; if a user-supplied electron/tray-icon.png is dropped in alongside,
// it takes precedence.
const TRAY_ICON_FILE = path.join(__dirname, 'tray-icon.png');

// Bundled logo (copied from <repo>/logo.png into electron/ at build time so
// the same path resolves in dev and packaged builds). Used for the window
// taskbar icon and as the default tray image. nativeImage handles PNG
// decoding; we cache the decoded images per-size to avoid re-reading the
// file every time startTrayFlash() rebuilds the normal icon.
const LOGO_FILE = path.join(__dirname, 'logo.png');
const _logoCache = new Map();   // sizeKey → nativeImage

function loadLogoImage(size) {
    // size === undefined → original-resolution icon, suitable for the window
    // taskbar (Windows scales it down to 16x16 / 32x32 itself). Pass an
    // explicit width/height for the tray, where 16x16 renders cleanly.
    const key = size ? `${size.width}x${size.height}` : 'orig';
    if (_logoCache.has(key)) return _logoCache.get(key);
    try {
        if (!fs.existsSync(LOGO_FILE)) return null;
        let img = nativeImage.createFromPath(LOGO_FILE);
        if (!img || img.isEmpty()) return null;
        if (size) img = img.resize(size);
        _logoCache.set(key, img);
        return img;
    } catch (err) {
        logLine('ICON', 'loadLogoImage failed', { err: err && err.message });
        return null;
    }
}

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
    return _buildIconPng(zlib, SIZE, PATTERN, TINT);
}

// Alert variant: orange background with a white "!" for attention.
function buildHandqAlertIconPng() {
    const zlib = require('zlib');
    const SIZE = 16;
    const PATTERN = [
        '..BBBBBBBBBBBB..',
        '.BBBBBBBBBBBBBB.',
        'BBBBBBBFFBBBBBBB',
        'BBBBBBBFFBBBBBBB',
        'BBBBBBBFFBBBBBBB',
        'BBBBBBBFFBBBBBBB',
        'BBBBBBBFFBBBBBBB',
        'BBBBBBBFFBBBBBBB',
        'BBBBBBBBBBBBBBBB',
        'BBBBBBBBBBBBBBBB',
        'BBBBBBBFFBBBBBBB',
        'BBBBBBBFFBBBBBBB',
        'BBBBBBBBBBBBBBBB',
        'BBBBBBBBBBBBBBBB',
        '.BBBBBBBBBBBBBB.',
        '..BBBBBBBBBBBB..',
    ];
    const TINT = [220, 100, 20, 255];   // amber/orange
    return _buildIconPng(zlib, SIZE, PATTERN, TINT);
}

function _buildIconPng(zlib, SIZE, PATTERN, TINT) {
    const FORE = [255, 255, 255, 255];
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
    // 2) Bundled logo.png, resized to tray dimensions. 32×32 reads better on
    //    HiDPI displays — Windows scales it down to 16/24 for the system
    //    tray itself. This is the primary path in normal installs; the
    //    procedural fallback below only fires if logo.png is missing or
    //    unreadable for some reason.
    const logo = loadLogoImage({ width: 32, height: 32 });
    if (logo) return logo;
    // 3) Last-ditch procedurally-built 16x16 H-on-blue.
    try {
        const png = buildHandqIconPng();
        const img = nativeImage.createFromBuffer(png);
        if (img && !img.isEmpty()) return img;
    } catch (err) {
        logLine('TRAY', 'PNG synthesis failed', { err: err && err.message });
    }
    return nativeImage.createEmpty();
}

// Lazily-built alert icon (orange "!"). Built once, reused for every flash.
let _alertTrayIcon = null;
function getAlertTrayIcon() {
    if (_alertTrayIcon) return _alertTrayIcon;
    try {
        const png = buildHandqAlertIconPng();
        const img = nativeImage.createFromBuffer(png);
        if (img && !img.isEmpty()) { _alertTrayIcon = img; return img; }
    } catch (err) {
        logLine('TRAY', 'alert PNG synthesis failed', { err: err && err.message });
    }
    return nativeImage.createEmpty();
}

// Flash the tray icon between normal and alert states at ~600 ms intervals.
// Stops automatically when the user focuses the window.
function startTrayFlash() {
    if (_trayFlashTimer) return; // already flashing
    if (!tray) return;
    let alertVisible = false;
    const normalIcon = buildTrayIcon();
    const alertIcon  = getAlertTrayIcon();
    _trayFlashTimer = setInterval(() => {
        if (!tray) { stopTrayFlash(); return; }
        alertVisible = !alertVisible;
        try {
            tray.setImage(alertVisible ? alertIcon : normalIcon);
        } catch (_) { /* ignore */ }
    }, 600);
    logLine('TRAY', 'flash started');
}

function stopTrayFlash() {
    if (!_trayFlashTimer) return;
    clearInterval(_trayFlashTimer);
    _trayFlashTimer = null;
    // Restore normal icon.
    if (tray) {
        try { tray.setImage(buildTrayIcon()); } catch (_) { /* ignore */ }
    }
    logLine('TRAY', 'flash stopped');
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

    // Platform-specific transparency. We want the OS desktop to show through
    // a system-blurred surface (Win11 acrylic / macOS vibrancy). Falls back
    // to a solid background on platforms / Windows builds that don't support
    // the requested material.
    const transparencyOpts = {};
    if (process.platform === 'win32') {
        // Pure transparency — no backgroundMaterial (acrylic renders opaque
        // on some Win11 builds). With transparent:true the Chromium compositor
        // is genuinely see-through wherever CSS has no solid background.
        transparencyOpts.transparent = true;
        transparencyOpts.backgroundColor = '#00000000';
    } else if (process.platform === 'darwin') {
        transparencyOpts.transparent = true;
        transparencyOpts.backgroundColor = '#00000000';
        transparencyOpts.vibrancy = 'sidebar';
        transparencyOpts.visualEffectState = 'active';
    } else {
        transparencyOpts.backgroundColor = '#f4f6fb';
    }

    // Window icon — drives the taskbar icon, alt-tab thumbnail, and the
    // alert flash target for flashFrame(). Falls through to Electron's
    // default if logo.png is missing.
    const windowIcon = loadLogoImage();

    mainWindow = new BrowserWindow({
        width: 1200,
        height: 800,
        minWidth: 720,
        minHeight: 480,
        title: 'HandQ',
        frame: false,
        ...(windowIcon ? { icon: windowIcon } : {}),
        ...transparencyOpts,
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

    // Stop taskbar flash as soon as the user focuses the window.
    mainWindow.on('focus', () => {
        try { mainWindow.flashFrame(false); } catch (_) { /* ignore */ }
        stopTrayFlash();
    });

    // Update check — fires once after the renderer has painted, so the
    // dialog never appears over a black window. Errors are logged inside
    // the updater; this catch is the last-resort safety net.
    mainWindow.webContents.once('did-finish-load', () => {
        checkForUpdates({ logLine, mainWindow }).catch((err) => {
            logLine('UPDATER', 'unexpected error',
                    { err: err && err.message });
        });
    });
}

// --- single-instance lock --------------------------------------------------

// Windows: set AppUserModelId so that native Toast notifications are
// attributed to this app. Must be called before app.whenReady().
// Value MUST match electron/package.json :: build.appId so the prod NSIS
// shortcut (which embeds appId as the shortcut's AUMID) and the running
// process register under the same identity — otherwise toasts get
// orphaned with the raw AUMID string as the source.
if (process.platform === 'win32') {
    app.setName('HandQ');
    app.setAppUserModelId('HandQ');
}

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

        // Register global hotkey for toggling window visibility.
        currentHotkey = loadHotkeySetting();
        registerHotkey(currentHotkey);

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

// Global hotkey IPC — renderer can read and update the toggle shortcut.
ipcMain.handle('hotkey:get', () => {
    return { hotkey: currentHotkey };
});

ipcMain.handle('hotkey:set', (_event, accelerator) => {
    const ok = registerHotkey(accelerator);
    if (ok) {
        saveHotkeySetting(accelerator);
        return { success: true, hotkey: accelerator };
    }
    // Restore previous hotkey on failure.
    registerHotkey(currentHotkey);
    return { success: false, hotkey: currentHotkey, error: 'Failed to register shortcut. It may be in use by another application.' };
});

// --- "Load history" file picker for the Templates panel -------------------
// Default path: %USERPROFILE%\HandQ\History\ (Windows) or ~/HandQ/History
// elsewhere — same root the bridge writes session History under, so the
// picker lands the user on the dir they're already familiar with. Filters
// to *.log + *.json so the user sees both raw plan logs and the cleaned
// execution_summary.json that `_trigger_save_session` produces.
function defaultHistoryDir() {
    const home = process.env.USERPROFILE || app.getPath('home');
    return path.join(home, 'HandQ', 'History');
}

ipcMain.handle('dialog:pickHistoryLog', async () => {
    const owner = BrowserWindow.getFocusedWindow() || mainWindow || null;
    const result = await dialog.showOpenDialog(owner, {
        title: 'Select session log to import',
        defaultPath: defaultHistoryDir(),
        properties: ['openFile'],
        filters: [
            { name: 'Session logs', extensions: ['log', 'json'] },
            { name: 'All files',    extensions: ['*'] },
        ],
    });
    if (result.canceled || !result.filePaths || !result.filePaths.length) {
        return { canceled: true, path: null };
    }
    return { canceled: false, path: result.filePaths[0] };
});

// --- graceful shutdown -----------------------------------------------------

app.on('before-quit', (event) => {
    isQuitting = true;
    globalShortcut.unregisterAll();
    // Tear down the takeover overlay if a task is still mid-flight on shutdown.
    try { hideTakeoverOverlay(); } catch (_) { /* ignore */ }
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
