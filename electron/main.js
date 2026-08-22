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

const { app, BrowserWindow, ipcMain, Tray, Menu, nativeImage, nativeTheme, globalShortcut, Notification, dialog, shell, screen, desktopCapturer, clipboard } = require('electron');
const path = require('path');
const fs = require('fs');
const os = require('os');
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

// Size-based rotation for handq-frontend.log. logLine() appends with no
// built-in bound, so a single long-running launch would grow frontend.log
// without limit. Mirror the Python bridge's bounded policy: when the file
// crosses FRONTEND_LOG_MAX_BYTES, shuffle .log -> .1 -> .2 -> .3 (keep 3
// backups) and reset. The byte counter is kept in memory (seeded from
// statSync at startup) so the hot path avoids a stat() on every line.
const FRONTEND_LOG_MAX_BYTES = 5 * 1024 * 1024;
const FRONTEND_LOG_BACKUPS = 3;
let frontendLogBytes = 0;
try { frontendLogBytes = fs.statSync(LOG_FILE).size; } catch (_) { frontendLogBytes = 0; }

function rotateFrontendLog() {
    // Best-effort: a locked file (Windows log viewer open) must not crash the
    // app — if a rename fails we just keep appending to the current file.
    try { fs.rmSync(LOG_FILE + '.' + FRONTEND_LOG_BACKUPS, { force: true }); } catch (_) { /* ignore */ }
    for (let i = FRONTEND_LOG_BACKUPS - 1; i >= 1; i--) {
        try { fs.renameSync(LOG_FILE + '.' + i, LOG_FILE + '.' + (i + 1)); } catch (_) { /* ignore */ }
    }
    try { fs.renameSync(LOG_FILE, LOG_FILE + '.1'); frontendLogBytes = 0; } catch (_) { /* keep current */ }
}

// Set HANDQ_FRONTEND_DEBUG=1 to capture the high-volume diagnostic firehoses
// (every bridge envelope incl. per-token streaming deltas, and the full bridge
// stderr mirror). Off by default — those duplicate handq-bridge.log and would
// otherwise dominate frontend.log. Lifecycle / IPC / error lines always log.
const FRONTEND_DEBUG = !!process.env.HANDQ_FRONTEND_DEBUG;

function formatLogLine(component, msg, extra) {
    const ts = new Date().toISOString();
    let line = '[' + ts + '] [' + component + '] ' + msg;
    if (extra !== undefined) {
        try { line += ' ' + JSON.stringify(extra); } catch (_) { line += ' [unserialisable]'; }
    }
    return line;
}

function writeFrontendLine(line) {
    // Console mirror — visible to the terminal that launched `electron .`.
    try { console.log(line); } catch (_) { /* ignore */ }
    // File mirror — best-effort; never throw.
    if (LOG_FILE) {
        try {
            const buf = line + '\n';
            fs.appendFileSync(LOG_FILE, buf);
            frontendLogBytes += Buffer.byteLength(buf);
            if (frontendLogBytes >= FRONTEND_LOG_MAX_BYTES) rotateFrontendLog();
        } catch (_) { /* swallow */ }
    }
}

function logLine(component, msg, extra) {
    writeFrontendLine(formatLogLine(component, msg, extra));
}

// Debug-gated variant for the high-volume firehoses (see FRONTEND_DEBUG).
// No-op (not even a console echo) unless HANDQ_FRONTEND_DEBUG is set.
function logLineDebug(component, msg, extra) {
    if (!FRONTEND_DEBUG) return;
    writeFrontendLine(formatLogLine(component, msg, extra));
}

// Strip API_KEY (and the legacy api_key) out of any payload we log
// (porting_design.md §(2.8) lets the renderer write the key directly into
// YAML; we must not echo it to disk).
//
// The same applies to the remote-control bearer token and the
// `handq://host:port/token` pairing string that embeds it: those flow through
// the remote_pair / remote_probe / remote_control_status envelopes, which are
// logged like any other. Anyone holding a machine's token can open agent
// sessions on it, so it must never reach handq-main.log. `capability` is a
// per-session authorization value with the same property.
const REDACT_KEYS = new Set([
    'API_KEY', 'api_key', 'api_key_env',
    'token', 'pairing', 'capability', 'remote_control_token',
]);

function redactApiKey(payload) {
    if (!payload || typeof payload !== 'object') return payload;
    if (Array.isArray(payload)) return payload.map(redactApiKey);
    const out = {};
    for (const k of Object.keys(payload)) {
        const v = payload[k];
        if (REDACT_KEYS.has(k)) {
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

// Content-protection (WDA_EXCLUDEFROMCAPTURE) state. Hoisted to module scope so
// both the Ctrl+Shift+P manual toggle AND the renderer-driven glass-mode switch
// (glass:setContentProtection IPC) share one source of truth. See the win32
// block in createWindow() for the full rationale. win32-only in effect —
// setContentProtection is a no-op elsewhere, but we still track the flag so the
// two callers agree. Starts ON (needed by the WebGL glass boot; veil turns it
// off once the renderer reports its mode).
let contentProtected = true;

// Apply the current contentProtected flag to the live window. Safe to call
// before mainWindow exists (no-op) and wrapped since setContentProtection can
// throw on unsupported platforms/builds.
function applyContentProtection() {
    if (!mainWindow) return;
    try { mainWindow.setContentProtection(contentProtected); } catch (_) { /* ignore */ }
}
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

// The overlay controller is extracted into takeover-overlay.js so its
// session-id plumbing can be unit-tested without booting Electron. We inject
// the Electron surfaces and the bridge writer / logger here. See that module's
// header for the full IPC contract.
const { createTakeoverOverlay } = require('./takeover-overlay');

const takeoverOverlayController = createTakeoverOverlay({
    BrowserWindow,
    globalShortcut,
    writeToBridge: (obj) => writeToBridge(obj),
    logLine,
    overlayHtmlPath: path.join(__dirname, 'overlay', 'overlay.html'),
});

function showTakeoverOverlay(sessionId) {
    takeoverOverlayController.show(sessionId);
}

function hideTakeoverOverlay(sessionId) {
    // sessionId omitted (bridge exit / app quit) forces teardown regardless
    // of which session is currently bound. sessionId present is checked
    // against the controller's live binding — a stale/superseded session's
    // `ended` must not tear down a newer session's still-active overlay.
    takeoverOverlayController.hide(sessionId);
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
        body  = String((evt && (evt.question || evt.prompt)) || 'The agent has a question for you.');
    } else if (kind === 'risk_confirmation') {
        // evt.title may be an override sent by the bridge for a specific
        // sub-flow (e.g. browser login). Fall back to the generic title
        // otherwise so unrelated risk_confirmation callers keep their
        // current behavior.
        title = (evt && evt.title)
            ? '⚠️ HandQ — ' + String(evt.title)
            : '⚠️ HandQ — high-risk operation';
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

// Show a system toast + flash the taskbar when a task actually finishes
// (bridge emits {type:"status", kind:"task_completed", summary}) and the
// window is not in focus. Distinct from notifyConfirmationNeeded: this fires
// once per real completion, not per confirmation request. Removed by
// accident in the 1.3.0 controller v1->v2 migration (main.js dropped the
// whole function while nothing else in that diff replaced its trigger) —
// re-added so "task finished while the window is hidden/minimized" is
// visible again.
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
        logLineDebug('BRIDGE-OUT', 'line', {
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
                try { showTakeoverOverlay(evt.session_id); }
                catch (err) {
                    logLine('OVERLAY', 'showTakeoverOverlay threw',
                            { err: err && err.message });
                }
            } else if (evt.kind === 'desktop_takeover_ended') {
                try { hideTakeoverOverlay(evt.session_id); }
                catch (err) {
                    logLine('OVERLAY', 'hideTakeoverOverlay threw',
                            { err: err && err.message });
                }
            } else if (evt.kind === 'served_desktop_takeover_started') {
                // A REMOTE operator's agent is driving this machine's desktop.
                // Same overlay + same Ctrl+Shift+C revoke as a local takeover:
                // the person sitting here must be able to see it and stop it.
                // The id arrives as served_session_id, never session_id — the
                // renderer mounts a tab for any unrecognised session_id, so a
                // served rc- session must not travel under that key (see
                // stdio_bridge._ServedDesktopNotifier). The overlay treats the
                // id as opaque and stamps it onto the revoke envelope, which
                // reaches the served session's real DesktopState.
                try { showTakeoverOverlay(evt.served_session_id); }
                catch (err) {
                    logLine('OVERLAY', 'showTakeoverOverlay (served) threw',
                            { err: err && err.message });
                }
            } else if (evt.kind === 'served_desktop_takeover_ended') {
                try { hideTakeoverOverlay(evt.served_session_id); }
                catch (err) {
                    logLine('OVERLAY', 'hideTakeoverOverlay (served) threw',
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
        // Notify the user when a task actually completes (not just a chat
        // reply) and the window is not in focus.
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
        logLineDebug('BRIDGE-LOG', stripped);
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

function seedUserConfigForVersion() {
    try {
        const userCfgDir = path.join(os.homedir(), 'HandQ');
        const userCfgPath = path.join(userCfgDir, 'handq_config.yaml');
        const stampPath = path.join(userCfgDir, '.handq_config_version');
        const currentVersion = app.getVersion();

        let previousVersion = null;
        try {
            if (fs.existsSync(stampPath)) {
                previousVersion = fs.readFileSync(stampPath, 'utf8').trim() || null;
            }
        } catch (e) {
            logLine('MAIN', 'seedUserConfigForVersion: stamp read failed',
                    { err: e && e.message });
        }

        const prodBundled = path.join(
            process.resourcesPath || '', 'bridge_main.dist', 'handq_config.yaml');
        const devBundled = path.join(
            __dirname, '..', 'dist', '.nuitka_cache', 'bridge_main.dist',
            'handq_config.yaml');
        let bundled = null;
        if (fs.existsSync(prodBundled)) {
            bundled = prodBundled;
        } else if (fs.existsSync(devBundled)) {
            bundled = devBundled;
        }

        if (!bundled) {
            logLine('MAIN', 'seedUserConfigForVersion: no bundled yaml; skip',
                    { prodBundled, devBundled });
            return;
        }

        const userCfgExists = fs.existsSync(userCfgPath);
        const versionChanged = previousVersion !== currentVersion;
        if (userCfgExists && !versionChanged) {
            return;
        }

        fs.mkdirSync(userCfgDir, { recursive: true });

        if (userCfgExists) {
            // Upgrade, not first run — leave the file alone. Copying the shipped
            // template over it is how 1.5.5→1.6.0 wiped llm.API_KEY out of the
            // user's config: every value they own that the template ships blank
            // was replaced by the blank.
            //
            // Migration belongs to bridge_main._merge_user_config_with_seed(),
            // which walks _PRESERVE_PATHS (llm.API_KEY, session,
            // interaction_switches, …), drops retired keys, writes atomically and
            // keeps a .bak. A copy here didn't just duplicate that job, it
            // DISARMED it: that merge only runs while the user yaml's `version:`
            // is older than the shipped one, and a freshly-copied template
            // already reports the shipped version.
            //
            // Only the stamp moves, so this branch is a no-op on the next boot.
            fs.writeFileSync(stampPath, currentVersion, 'utf8');
            logLine('MAIN',
                    'seedUserConfigForVersion: upgrade — leaving the user yaml to the bridge merge',
                    { userCfgPath, prevVersion: previousVersion,
                      version: currentVersion });
            return;
        }

        fs.copyFileSync(bundled, userCfgPath);
        fs.writeFileSync(stampPath, currentVersion, 'utf8');
        logLine('MAIN', 'seedUserConfigForVersion: seeded user yaml',
                { from: bundled, to: userCfgPath, version: currentVersion,
                  prevVersion: previousVersion });
    } catch (err) {
        logLine('MAIN', 'seedUserConfigForVersion failed',
                { err: err && err.message });
    }
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
// Removed: tray flash now blinks the normal logo on/off rather than
// alternating with a separate alert icon.

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

// Lazily-built alert icon (orange "!"). Removed — tray flash now blinks
// the normal logo on/off, no separate alert image needed.

// Flash the tray icon by alternating between the normal logo and an
// empty image at ~600 ms intervals — a clean blink with no separate
// alert badge. Stops automatically when the user focuses the window.
function startTrayFlash() {
    if (_trayFlashTimer) return; // already flashing
    if (!tray) return;
    let visible = true;
    const normalIcon = buildTrayIcon();
    const blankIcon  = nativeImage.createEmpty();
    _trayFlashTimer = setInterval(() => {
        if (!tray) { stopTrayFlash(); return; }
        visible = !visible;
        try {
            tray.setImage(visible ? normalIcon : blankIcon);
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

// --- Windows rounded window shape -----------------------------------------
//
// Tried and abandoned: DwmSetWindowAttribute (DWMWA_WINDOW_CORNER_PREFERENCE,
// DWMWA_BORDER_COLOR=NONE, DWMWA_NCRENDERING_POLICY=DISABLED,
// DWMWA_SYSTEMBACKDROP_TYPE=NONE) and SetWindowRgn + CreateRoundRectRgn via
// koffi FFI. All four DWM attributes returned S_OK but had no visible effect
// on `transparent: true` + WS_EX_LAYERED windows in this app on Win11 24H2
// (build 26100). SetWindowRgn did clip hit-testing (clicks passed through
// the corners) but didn't remove the residual 1px composite artifact visible
// on white backgrounds. That artifact appears to be Chromium's
// UpdateLayeredWindow compositor edge halo at the physical window rect
// boundary, below the DWM level. Accepted as a limitation — barely visible
// in practice unless the app sits over a pure-white page.

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
        width: 672,
        height: 576,
        // Default launch size, in DIPs — the Stage-Manager rail and detail
        // sidebar are expected to appear/disappear on demand, and the window
        // grows on top of this baseline when either arrives (see the layout-
        // driven auto-resize handler further down).
        //
        // Kept deliberately compact so the window doesn't dominate the screen
        // on high-DPI displays: 672×576 DIP renders at 840×720 physical on a
        // 125%-scaled display (the previous 840×720 DIP default rendered at
        // 1050×900 there, which read as oversized). The main session card is
        // flex and reflows to whatever width the window offers, so a smaller
        // default just means a snugger chat column, not a broken layout — the
        // effect is identical to the user dragging the window smaller.
        //
        // Min sizing keeps the main card usable (down to its own 340px
        // floor) even when the user shrinks the window aggressively. Rail
        // and sidebar shrink/hide when the window is too narrow to host
        // them together.
        minWidth: 640,
        minHeight: 520,
        title: 'HandQ',
        frame: false,
        hasShadow: false,
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

    // Markdown-rendered chat links use target="_blank" (renderer.js
    // renderMarkdownInline). Electron's default window-open behavior for
    // that is a bare chromeless BrowserWindow with none of HandQ's custom
    // titlebar/chrome — visually broken and confusing. Deny the in-app
    // popup and hand http(s) off to the OS default browser instead.
    mainWindow.webContents.setWindowOpenHandler(({ url }) => {
        if (/^https?:\/\//i.test(url)) {
            shell.openExternal(url);
        }
        return { action: 'deny' };
    });

    // System-native Cut/Copy/Paste/Select All context menu — matches what
    // VSCode/Slack/Discord do (a real OS-rendered menu, not an HTML
    // popup), so right-click in the composer or any input feels native
    // rather than a leftover browser affordance.
    mainWindow.webContents.on('context-menu', (_event, params) => {
        const { editFlags, isEditable, selectionText } = params;
        const template = [];
        if (isEditable) {
            template.push(
                { label: 'Cut', role: 'cut', enabled: editFlags.canCut },
                { label: 'Copy', role: 'copy', enabled: editFlags.canCopy },
                { label: 'Paste', role: 'paste', enabled: editFlags.canPaste },
                { type: 'separator' },
                { label: 'Select All', role: 'selectAll', enabled: editFlags.canSelectAll },
            );
        } else if (selectionText) {
            template.push({ label: 'Copy', role: 'copy' });
        }
        if (template.length === 0) return;
        Menu.buildFromTemplate(template).popup({ window: mainWindow });
    });

    if (process.platform === 'win32') {
        // Content protection state — starts ON to keep the glass effect
        // working. When ON, SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)
        // hides the window from ALL screen captures at the OS-display-
        // subsystem level (BitBlt, Windows.Graphics.Capture, getUserMedia
        // with chromeMediaSource:'desktop', etc.), including HandQ's own
        // desktopCapturer used by glass-effect.js. Without it the glass
        // shader would sample its OWN rendered pixels back into itself
        // via desktopCapturer, producing a recursive/washed-out result.
        //
        // For screenshots use Ctrl+Shift+S — that goes through
        // webContents.capturePage() which pulls pixels from Chromium's
        // internal compositor BEFORE they hit the OS display subsystem,
        // so WDA doesn't apply. Glass stays on, screenshot works.
        //
        // Fallback: Ctrl+Shift+P toggles WDA off for cases where the
        // internal capturePage() path fails (older Electron builds, some
        // driver issues) or when using external screen-recording tools.
        // State resets on app restart to ON.
        //
        // NOTE: `contentProtected` is now module-level (see top of file) and is
        // also driven by the renderer's glass-mode switch: veil mode needs no
        // self-capture guard (it's a pure-CSS veil, not a desktopCapturer
        // shader) so it releases protection, letting the window show up in
        // ordinary OS screenshots; webgl mode re-asserts it. The manual toggle
        // below and that IPC path share this one flag.
        applyContentProtection();

        // Window-scoped key handlers — `before-input-event` fires only
        // when this window has focus, so shortcuts don't clash with the
        // OS or other apps. preventDefault stops Chromium's default
        // action (Ctrl+Shift+S = "Save Page As" in a normal browser).
        mainWindow.webContents.on('before-input-event', (event, input) => {
            if (!(input.control && input.shift && !input.alt && !input.meta)) return;
            if (typeof input.key !== 'string') return;
            const key = input.key.toLowerCase();

            if (key === 'p') {
                // Toggle content protection — escape hatch for external
                // capture tools (OBS, XSplit) that also can't see the
                // window while WDA is on. Logs each flip so it's easy
                // to reason about whether glass is "safe" right now.
                contentProtected = !contentProtected;
                applyContentProtection();
                logLine('MAIN', 'content-protection toggled', { on: contentProtected });
                event.preventDefault();
                return;
            }

            if (key === 's') {
                // In-app screenshot — pulls the current window contents
                // via Chromium's internal compositor capture (bypasses
                // OS display capture APIs, so it works even with WDA on).
                // Result goes to the system clipboard as an image; paste
                // with Ctrl+V into any image-aware app (chat, Photoshop,
                // markdown editor, etc.). No file is written — pure
                // clipboard so it doesn't leave leftover artifacts on
                // disk that need pruning.
                event.preventDefault();
                mainWindow.webContents.capturePage().then((img) => {
                    if (!img || img.isEmpty()) {
                        logLine('MAIN', 'screenshot: capturePage returned empty');
                        return;
                    }
                    clipboard.writeImage(img);
                    const sz = img.getSize();
                    logLine('MAIN', 'screenshot: copied to clipboard',
                            { width: sz.width, height: sz.height });
                    // Optional toast so the user knows it worked. Silent
                    // (no ping) so it's non-intrusive during focused work.
                    try {
                        if (Notification.isSupported()) {
                            const n = new Notification({
                                title: 'HandQ',
                                body: 'Screenshot copied to clipboard',
                                silent: true,
                            });
                            n.show();
                            setTimeout(() => { try { n.close(); } catch (_) {} }, 1600);
                        }
                    } catch (_) { /* ignore notification failures */ }
                }).catch((err) => {
                    logLine('MAIN', 'screenshot failed', { err: err && err.message });
                });
                return;
            }
        });
    }

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
        mainWindow.webContents.send('window:activeState', { active: true });
    });
    // Push focus/blur to the renderer so titlebar chrome can dim like a
    // real OS window when it's not the active one — frameless windows get
    // no native active/inactive chrome swap for free.
    mainWindow.on('blur', () => {
        mainWindow.webContents.send('window:activeState', { active: false });
    });

    // Push maximize/unmaximize state to the renderer so the custom titlebar
    // can swap its max-button icon (single square ↔ two overlapping squares).
    // Frameless windows don't get the OS-provided glyph swap for free.
    const pushMaxState = () => {
        if (!mainWindow || mainWindow.isDestroyed()) return;
        try {
            mainWindow.webContents.send('window:maxState', {
                isMaximized: mainWindow.isMaximized(),
            });
        } catch (_) { /* ignore */ }
    };
    mainWindow.on('maximize', pushMaxState);
    mainWindow.on('unmaximize', pushMaxState);
    // Seed the renderer with the initial state as soon as the page finishes
    // loading — otherwise the icon starts stale if the window opens maximized.
    mainWindow.webContents.once('did-finish-load', pushMaxState);

    // Push window bounds to renderer on move/resize. Glass canvas subscribes
    // to this instead of polling glass:getWindowBounds every 2 frames — the
    // IPC round-trip was the dominant per-frame cost during window drag.
    //
    // Trailing-edge 60Hz coalesce: Windows fires 'move' 100+ times/sec during
    // drag. We only need bounds at monitor refresh rate; the extra events
    // would just be IPC noise the renderer drops anyway.
    let pushBoundsTimer = null;
    const pushBounds = () => {
        if (pushBoundsTimer) return;
        pushBoundsTimer = setTimeout(() => {
            pushBoundsTimer = null;
            if (!mainWindow || mainWindow.isDestroyed()) return;
            try {
                const b = mainWindow.getBounds();
                const display = screen.getDisplayMatching(b);
                mainWindow.webContents.send('glass:boundsChanged', {
                    x: b.x - display.bounds.x,
                    y: b.y - display.bounds.y,
                    width: b.width,
                    height: b.height,
                    displayWidth: display.bounds.width,
                    displayHeight: display.bounds.height,
                    scaleFactor: display.scaleFactor,
                    displayId: display.id,
                });
            } catch (_) { /* ignore */ }
        }, 16);
    };
    mainWindow.on('move', pushBounds);
    mainWindow.on('resize', pushBounds);

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

        seedUserConfigForVersion();

        pythonChild = spawnBridge();
        spawnMentionSearch();
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

// --- mention search worker (Windows SystemIndex via powershell.exe) --------
//
// A resident PowerShell child that answers renderer @-mention path queries by
// running SQL over the Windows SystemIndex (ADODB, Provider=Search.CollatorDSO).
// Same line-delimited JSON idiom as the Python bridge, but the response comes
// back to `ipcMain.handle` directly (no reverse event bus) — this is a
// self-contained side-channel that Python knows nothing about.
//
// If Search is disabled or the worker crashes past the restart budget the
// feature goes dark for the rest of this app lifetime and the renderer just
// stops showing the dropdown.

// In dev, __dirname is the electron/ source dir. In the packaged app, main.js
// runs from inside resources/app.asar, so __dirname resolves to a virtual asar
// path that powershell.exe (an external process) cannot open. The script is
// listed under `asarUnpack` in package.json so it also lands on real disk at
// resources/app.asar.unpacked/mention_search.ps1 — point spawn there instead.
const MENTION_SEARCH_SCRIPT = app.isPackaged
    ? path.join(process.resourcesPath, 'app.asar.unpacked', 'mention_search.ps1')
    : path.join(__dirname, 'mention_search.ps1');
const MENTION_QUERY_TIMEOUT_MS = 1500;
const MENTION_MAX_RESTARTS = 1;

let mentionChild = null;
let mentionReady = false;
let mentionDisabled = false;
let mentionRestarts = 0;
const mentionPending = new Map();   // id → { resolve, timer }
let mentionSeq = 0;

function spawnMentionSearch() {
    if (mentionDisabled) return;
    logLine('MENTION', 'spawning powershell worker', {
        script: MENTION_SEARCH_SCRIPT,
    });
    let child;
    try {
        child = spawn('powershell.exe', [
            '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass',
            '-File', MENTION_SEARCH_SCRIPT,
        ], {
            stdio: ['pipe', 'pipe', 'pipe'],
            windowsHide: true,
        });
    } catch (err) {
        logLine('MENTION', 'spawn failed; disabling feature', {
            err: err && err.message,
        });
        mentionDisabled = true;
        return;
    }
    mentionChild = child;
    mentionReady = false;

    child.stdout.setEncoding('utf8');
    child.stderr.setEncoding('utf8');
    const rl = readline.createInterface({ input: child.stdout, crlfDelay: Infinity });

    rl.on('line', (line) => {
        const trimmed = line.trim();
        if (!trimmed) return;
        let evt;
        try {
            evt = JSON.parse(trimmed);
        } catch (err) {
            logLine('MENTION', 'malformed JSON from worker', {
                err: err && err.message,
                raw: trimmed.slice(0, 200),
            });
            return;
        }
        // Startup handshake — first line is {"ready":true|false}.
        if (typeof evt.ready === 'boolean') {
            mentionReady = evt.ready;
            if (!evt.ready) {
                logLine('MENTION', 'worker signalled unavailable', {
                    error: evt.error,
                });
                mentionDisabled = true;
            } else {
                logLine('MENTION', 'worker ready');
            }
            return;
        }
        // Per-request response.
        if (evt.id && mentionPending.has(evt.id)) {
            const pending = mentionPending.get(evt.id);
            mentionPending.delete(evt.id);
            clearTimeout(pending.timer);
            pending.resolve({
                results: Array.isArray(evt.results) ? evt.results : [],
                error: evt.error || null,
            });
        }
    });

    child.stderr.on('data', (chunk) => {
        logLineDebug('MENTION-STDERR', String(chunk).trim());
    });

    child.on('error', (err) => {
        logLine('MENTION', 'worker process error', { err: err && err.message });
    });

    child.on('exit', (code, signal) => {
        logLine('MENTION', 'worker exited', {
            code, signal, restarts: mentionRestarts, ready: mentionReady,
        });
        mentionReady = false;
        // Drain pending resolvers so callers move on.
        for (const [, pending] of mentionPending) {
            clearTimeout(pending.timer);
            pending.resolve({ results: [], error: 'worker_exit' });
        }
        mentionPending.clear();
        mentionChild = null;

        if (isShuttingDown || isQuitting) return;
        if (mentionDisabled) return;
        if (mentionRestarts >= MENTION_MAX_RESTARTS) {
            logLine('MENTION', 'restart budget exhausted; disabling feature');
            mentionDisabled = true;
            return;
        }
        mentionRestarts += 1;
        setTimeout(spawnMentionSearch, 250);
    });
}

async function mentionSearch(query) {
    if (mentionDisabled) return { results: [], disabled: true };
    if (!mentionChild || !mentionReady) {
        return { results: [], notReady: true };
    }
    if (!query || typeof query !== 'string' || query.length > 50) {
        return { results: [] };
    }
    const id = 'm' + (++mentionSeq);
    return await new Promise((resolve) => {
        const timer = setTimeout(() => {
            if (mentionPending.has(id)) {
                mentionPending.delete(id);
                resolve({ results: [], timedOut: true });
            }
        }, MENTION_QUERY_TIMEOUT_MS);
        mentionPending.set(id, { resolve, timer });
        try {
            mentionChild.stdin.write(JSON.stringify({ id, query }) + '\n');
        } catch (err) {
            mentionPending.delete(id);
            clearTimeout(timer);
            resolve({ results: [], error: err && err.message });
        }
    });
}

// --- UNC / arbitrary-directory listing -------------------------------------
//
// Backs the mention dropdown when the query looks like a UNC path
// (\\host\share\...). SystemIndex does not index network locations by
// default, so we fall back to fs.readdir over the parent path and fuzzy-
// filter by the trailing suffix. Wrapped in a Promise.race timeout so an
// unreachable host doesn't hang the dropdown — SMB's own timeout is 60s+.

const LIST_DIR_TIMEOUT_MS = 1500;
const LIST_DIR_MAX_RESULTS = 20;

function _fuzzyMatchSubsequence(query, name) {
    // Case-insensitive sub-sequence: true if every char of `query` appears
    // in `name` in order. Mirrors the "%q1%q2%..." SQL fuzzy pattern used
    // by mention_search.ps1 so both branches feel identical to the user.
    if (!query) return true;
    const q = query.toLowerCase();
    const n = name.toLowerCase();
    let qi = 0;
    for (let ni = 0; ni < n.length && qi < q.length; ni++) {
        if (n[ni] === q[qi]) qi++;
    }
    return qi === q.length;
}

async function listDirectory(dir, filter) {
    if (!dir || typeof dir !== 'string') return { results: [] };
    return await new Promise((resolve) => {
        let done = false;
        const timer = setTimeout(() => {
            if (done) return;
            done = true;
            resolve({ results: [], timedOut: true });
        }, LIST_DIR_TIMEOUT_MS);
        fs.promises.readdir(dir, { withFileTypes: true })
            .then((entries) => {
                if (done) return;
                done = true;
                clearTimeout(timer);
                const results = [];
                for (const e of entries) {
                    if (!_fuzzyMatchSubsequence(filter || '', e.name)) continue;
                    results.push({
                        path: path.join(dir, e.name),
                        name: e.name,
                        parent: dir,
                        isDir: e.isDirectory(),
                    });
                    if (results.length >= LIST_DIR_MAX_RESULTS) break;
                }
                resolve({ results });
            })
            .catch((err) => {
                if (done) return;
                done = true;
                clearTimeout(timer);
                resolve({ results: [], error: err && err.message });
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

ipcMain.handle('handq:searchPaths', async (_event, payload) => {
    const query = payload && payload.query;
    return await mentionSearch(query);
});

ipcMain.handle('handq:listDirectory', async (_event, payload) => {
    const dir = payload && payload.path;
    const filter = payload && payload.filter;
    return await listDirectory(dir, filter);
});

// Reveal a file path in the OS file explorer (Explorer / Finder). Bounces
// on shell.showItemInFolder — Chromium's built-in cross-platform bridge.
// Fires from the FILES tree in the session sidebar (double-click on a row);
// non-fatal by design — bad paths and permission errors return { ok:false }
// rather than throwing back into the renderer.
ipcMain.handle('handq:revealFile', (_event, payload) => {
    const p = payload && payload.path;
    if (!p || typeof p !== 'string') return { ok: false, reason: 'invalid path' };
    try {
        shell.showItemInFolder(p);
        return { ok: true };
    } catch (err) {
        logLine('IPC-IN', 'handq:revealFile failed', { path: p, err: String(err && err.message || err) });
        return { ok: false, reason: String(err && err.message || err) };
    }
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

// Debug-level variant — gated behind HANDQ_FRONTEND_DEBUG via logLineDebug so
// the renderer's high-volume per-event firehose (onStatus per envelope,
// reply_delta streaming) doesn't flood handq-frontend.log by default. The
// renderer's window.__handqLog routes DEBUG here; INFO/WARN/ERROR go to
// handq:log above and always persist.
ipcMain.on('handq:logDebug', (_event, payload) => {
    if (!payload || typeof payload !== 'object') return;
    const component = payload.component || 'RENDER-DEBUG';
    const msg = payload.msg || '';
    logLineDebug(component, msg, payload.extra);
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

// Layout-driven auto-resize. The renderer sends a compact descriptor for
// the current visible layout (total open sessions, rail visibility,
// sidebar visibility) each time _updateLayout fires; main computes the
// desired window size from a formula rather than a hard-coded table so
// each column's contribution is explicit and additive on top of a
// "main card only" baseline:
//
//   baseline (main card only) — DIP, matches the BrowserWindow default
//     width  = 672
//     height = 576
//   + rail visible                → width += RAIL_DELTA    (160 + margin + gap 0)
//   + sidebar visible             → width += (sidebarWidth + 10 margin)
//                                    Dynamic per-call. Renderer measures
//                                    the actual sidebar (auto 4:2.5 ratio
//                                    of card OR user's drag pin — see
//                                    session-sidebar.js) and sends it in
//                                    the payload. SIDEBAR_DELTA below is
//                                    a legacy fallback only.
//
// The new Stage-Manager state machine keeps at most 1 card in the main
// stage — any 2nd+ session drops into the rail — so the "rail visible"
// signal already captures "more than one session exists". No extra
// per-extra-session delta is needed on top of RAIL_DELTA.
//
// Semantics:
//   * Delta-application: _autoResizeApply tracks the previously-applied
//     "auto extras" (width above baseline, tracked separately per side)
//     and applies only the DIFFERENCE on each call. Panel opens → window
//     grows by that panel's delta; panel closes → window shrinks by the
//     same delta; manual user resizes in between ride on top and are
//     preserved. Without this, the chat region would keep the widened
//     space after a panel closed (flex:1 auto swallowing the freed
//     pixels), which reads as "the window won't shrink back".
//   * Edge anchoring: each panel's delta is applied to the window edge
//     that panel actually lives on — rail grows the left edge, sidebar
//     grows the right edge. So a sidebar toggle never moves x, and the
//     centre chat card (whose width is unchanged by that toggle) never
//     translates across the screen. See _autoResizeApply for the full
//     rationale and what the previous re-centring rule got wrong.
//   * Never touches a manually-maximized window (the user picked that
//     state on purpose; we won't fight them).
//   * No more auto-maximize threshold. The Stage-Manager rail absorbs
//     any 2nd+ session; more sessions don't need more screen space, they
//     just add rows to the rail.
//   * Sizes are clamped to the current display's work area so the window
//     doesn't spill onto an adjacent monitor, and floored at baseline so
//     a shrink from a panel-close can't drag the window below baseline
//     even if the user had manually made it smaller.
//   * Never smaller than BrowserWindow's own minWidth/minHeight floor.
//
// AUTO_RESIZE_TABLE is retained ONLY for the legacy `window:auto-resize`
// numeric payload (backwards compat with an older renderer build that
// sent a bare session count instead of a descriptor).
const AUTO_RESIZE_BASELINE = { w: 672, h: 576 };
const AUTO_RESIZE_RAIL_DELTA    = 170;   // rail width 160 + margin 10 + gap 0
// SIDEBAR_DELTA is a LEGACY fallback used only when a payload arrives
// without a sidebarWidth (very old renderer builds, boot-race pre-first-
// _refreshSidebarWidth). Real width is dynamic — see session-sidebar.js's
// SIDEBAR_TO_CARD_RATIO — and the renderer sends it explicitly on every
// _updateLayout so this constant almost never gets used. Value here is
// the sensible-default sidebar width (264) + its 10px margin-right,
// matching the old fixed-width behavior.
const AUTO_RESIZE_SIDEBAR_DELTA = 274;   // fallback: sidebar 264 + margin 10 (dynamic in normal path)
const AUTO_RESIZE_TABLE = [
    { w: 672, h: 576 },   // 1 session (legacy payload only)
    { w: 842, h: 576 },   // ≥2 sessions (legacy payload only — baseline + rail 170)
];

function _computeDesiredSize(layout) {
    // layout: { sessions:int, sidebarOpen:bool, sidebarWidth:number, railOpen:bool }
    const sidebarOpen = !!(layout && layout.sidebarOpen);
    const sidebarWidth = Math.max(0, Number(layout && layout.sidebarWidth) || 0);
    const railOpen = !!(layout && layout.railOpen);
    // Extras are tracked PER SIDE, not as one lump width. The rail lives on
    // the layout's left edge and the sidebar on its right, so _autoResizeApply
    // can grow the matching window edge instead of re-centring — see the
    // anchoring comment there for why re-centring was wrong.
    let left = 0;
    let right = 0;
    const h = AUTO_RESIZE_BASELINE.h;
    if (railOpen) left += AUTO_RESIZE_RAIL_DELTA;
    if (sidebarOpen) {
        // Sidebar extras = actual measured width + its 10px right margin
        // against the window edge. Renderer sends the actual width because
        // the sidebar is dynamic (4:2.5 ratio auto, or the user's drag
        // pin — see session-sidebar.js). If renderer didn't send a width
        // (very early boot / legacy payload) fall back to the fixed
        // AUTO_RESIZE_SIDEBAR_DELTA so old shapes still resize sanely
        // instead of getting a 10px near-zero window grow.
        right += sidebarWidth > 0
            ? Math.round(sidebarWidth + 10)
            : AUTO_RESIZE_SIDEBAR_DELTA;
    }
    return {
        w: AUTO_RESIZE_BASELINE.w + left + right,
        h: h,
        left: left,
        right: right,
    };
}

// Delta-application state: how much this session has auto-grown the
// window ABOVE the baseline (from panels opening), tracked SEPARATELY for
// the left edge (rail) and the right edge (sidebar). Each _autoResizeApply
// call computes the newly-requested "extras" per side and applies the
// DIFFERENCE from what we grew by last time. Closing a panel therefore
// SHRINKS the window by that panel's delta on that panel's own side, while
// any manual resize the user did in between rides on top and is preserved.
// Reset to zeroes at module load; the first call after launch computes its
// true baseline delta from whatever panels the renderer reports as open.
let _lastAutoExtras = { left: 0, right: 0, h: 0 };

function _autoResizeApply(target) {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    if (mainWindow.isMaximized()) return;
    const cur = mainWindow.getBounds();

    // Extras above the fixed baseline this call is asking for, per side.
    // Clamped to ≥0 defensively — every current caller emits target ≥
    // baseline, but a stray legacy payload with a below-baseline entry
    // would otherwise underflow the deltas into a spurious extra shrink.
    // Legacy numeric payloads (AUTO_RESIZE_TABLE) carry no side breakdown;
    // attribute their whole width delta to the left edge, since the table's
    // only non-baseline entry is the rail — which is a left-side panel.
    const hasSides = Number.isFinite(target.left) && Number.isFinite(target.right);
    const wantLeft = hasSides
        ? Math.max(0, target.left)
        : Math.max(0, target.w - AUTO_RESIZE_BASELINE.w);
    const wantRight = hasSides ? Math.max(0, target.right) : 0;
    const wantExtraH = Math.max(0, target.h - AUTO_RESIZE_BASELINE.h);
    const deltaLeft = wantLeft - _lastAutoExtras.left;
    const deltaRight = wantRight - _lastAutoExtras.right;
    const deltaH = wantExtraH - _lastAutoExtras.h;
    _lastAutoExtras = { left: wantLeft, right: wantRight, h: wantExtraH };
    if (deltaLeft === 0 && deltaRight === 0 && deltaH === 0) return;
    const deltaW = deltaLeft + deltaRight;

    const display = screen.getDisplayMatching(cur);
    const wa = display.workArea;
    // Floor at baseline: if the user shrank the window BELOW baseline and
    // then a panel closed, we don't want that -delta to drag them further
    // down. Ceiling at the current display's work area so a +delta near
    // the right edge doesn't push the window off-screen.
    const nextW = Math.max(AUTO_RESIZE_BASELINE.w, Math.min(wa.width,  cur.width  + deltaW));
    const nextH = Math.max(AUTO_RESIZE_BASELINE.h, Math.min(wa.height, cur.height + deltaH));
    if (nextW === cur.width && nextH === cur.height) return;

    // Anchor each panel's growth to ITS OWN side. The rail is a left-edge
    // panel and the sidebar a right-edge one, so a sidebar toggle moves the
    // window's RIGHT edge only and leaves x alone — the content between the
    // two panels never translates across the screen.
    //
    // This replaces a re-centring rule (nextX = centre - nextW/2) whose
    // stated intent was visual continuity but which achieved the opposite:
    // opening the sidebar grew the window by ~520px and therefore slid x
    // ~260px LEFT, so the entire UI lurched sideways "to make room" before
    // the panel appeared. Worse, the centre chat card — whose width a
    // sidebar toggle does NOT change, since the window grows by exactly the
    // sidebar's width — visibly drifted along with it, which read as the
    // card janking for no reason.
    //
    // The work-area ceiling above can clamp nextW below what was asked for;
    // scale the left share by the width actually applied so we never move x
    // further than the window really grew. Mixed-sign deltas (rail closing
    // while the sidebar opens) fall out of this correctly: each edge lands
    // where its own panel's delta puts it.
    const appliedW = nextW - cur.width;
    const leftShare = deltaW === 0 ? 0 : Math.round(appliedW * (deltaLeft / deltaW));
    let nextX = cur.x - leftShare;
    // Nothing grows the window vertically today (_computeDesiredSize always
    // returns the baseline height), so vertical stays centred — a symmetric
    // split is the right default for a delta with no side attribution.
    let nextY = Math.round(cur.y + cur.height / 2 - nextH / 2);
    nextX = Math.max(wa.x, Math.min(nextX, wa.x + wa.width  - nextW));
    nextY = Math.max(wa.y, Math.min(nextY, wa.y + wa.height - nextH));
    try {
        mainWindow.setBounds({ x: nextX, y: nextY, width: nextW, height: nextH }, true);
    } catch (err) {
        logLine('MAIN', 'auto-resize setBounds failed', { err: err && err.message });
    }
}

ipcMain.on('window:auto-resize', (_event, payload) => {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    // Two payload shapes accepted:
    //   { sessions, sidebarOpen, railOpen }  ← current renderer
    //   <number>                             ← legacy: bare session count
    let sessions;
    let target;
    if (payload && typeof payload === 'object') {
        sessions = Math.max(1, Number(payload.sessions) || 1);
        target = _computeDesiredSize({
            sessions: sessions,
            sidebarOpen: !!payload.sidebarOpen,
            sidebarWidth: Number(payload.sidebarWidth) || 0,
            railOpen: !!payload.railOpen,
        });
    } else {
        sessions = Math.max(1, Number(payload) || 1);
        target = AUTO_RESIZE_TABLE[Math.min(sessions, AUTO_RESIZE_TABLE.length) - 1];
    }
    // No more auto-maximize-at-N-sessions. The old threshold assumed the
    // tiled-card layout where 6+ visible cards genuinely needed the whole
    // screen; with the Stage-Manager rail (≤1 card in main, everything
    // else scrolls in the 160px rail), extra sessions don't want more
    // screen space at all — they just add rows to the rail. If the user
    // wants full-screen they can maximize themselves.
    _autoResizeApply(target);
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

ipcMain.handle('app:getVersion', () => {
    return { version: app.getVersion() };
});

// --- Liquid Glass: desktopCapturer IPC ------------------------------------
// The renderer needs a live screen capture sourceId and window bounds to
// crop/refract the portion of the desktop behind the window.

ipcMain.handle('glass:getScreenSource', async () => {
    if (!mainWindow || mainWindow.isDestroyed()) return null;
    try {
        const bounds = mainWindow.getBounds();
        const display = screen.getDisplayMatching(bounds);
        const sources = await desktopCapturer.getSources({
            types: ['screen'],
            thumbnailSize: { width: 1, height: 1 },
        });
        // Match the source to the display the window is on
        let source = sources.find((s) => String(s.display_id) === String(display.id));
        if (!source) source = sources[0];
        return {
            sourceId: source?.id || null,
            displayId: display.id,
            displayWidth: display.bounds.width,
            displayHeight: display.bounds.height,
            scaleFactor: display.scaleFactor,
        };
    } catch (err) {
        logLine('GLASS', 'desktopCapturer.getSources failed', { error: err.message });
        return null;
    }
});

ipcMain.handle('glass:getWindowBounds', () => {
    if (!mainWindow || mainWindow.isDestroyed()) return null;
    const bounds = mainWindow.getBounds();
    const display = screen.getDisplayMatching(bounds);
    return {
        x: bounds.x - display.bounds.x,
        y: bounds.y - display.bounds.y,
        width: bounds.width,
        height: bounds.height,
        displayWidth: display.bounds.width,
        displayHeight: display.bounds.height,
        scaleFactor: display.scaleFactor,
        displayId: display.id,
    };
});

// Renderer-driven content protection. The glass layer (glass-effect.js) calls
// this on every mode switch: webgl needs WDA on (its desktopCapturer shader
// would otherwise sample its own output — recursive wash-out), veil is a
// pure-CSS surface with no self-capture concern, so it releases protection and
// the window becomes visible in ordinary OS screenshots/recordings. Shares the
// module-level `contentProtected` flag with the Ctrl+Shift+P manual toggle —
// whichever fires last wins, which is the intended behavior (an explicit manual
// toggle after a mode switch, or vice-versa, both take effect). No-op off win32
// where setContentProtection does nothing anyway.
ipcMain.handle('glass:setContentProtection', (_event, on) => {
    contentProtected = !!on;
    applyContentProtection();
    logLine('GLASS', 'content-protection set by renderer', { on: contentProtected });
    return contentProtected;
});

// Live toggle for Win11's system-level frosted glass (acrylic). Session-
// only — nothing persists, and the window is re-created with default
// (transparent-only) BrowserWindow options on next launch. The team tried
// shipping `backgroundMaterial: 'acrylic'` as a launch-time default and
// reverted because some Win11 builds rendered it fully opaque; this
// runtime toggle lets an operator A/B the effect on the current build
// without committing the whole install to it.
//
// `mainWindow.setBackgroundMaterial` was added in Electron 24; we're on
// 31. macOS silently ignores it (the equivalent knob is `vibrancy` at
// window-creation time, which we already set to 'sidebar'). Linux has no
// equivalent — the call is a no-op there.
//
// Values passed through unchanged: 'none' / 'auto' / 'mica' / 'acrylic'
// / 'tabbed'. Unknown values raise a TypeError inside Electron, so we
// whitelist here rather than letting a bad renderer input propagate.
const BACKGROUND_MATERIALS = new Set(['none', 'auto', 'mica', 'acrylic', 'tabbed']);
ipcMain.handle('window:setBackgroundMaterial', (_event, material) => {
    if (!mainWindow || mainWindow.isDestroyed()) return false;
    if (process.platform !== 'win32') return false;
    if (!BACKGROUND_MATERIALS.has(material)) return false;
    try {
        mainWindow.setBackgroundMaterial(material);
        // Windows tints acrylic (and mica) from the SYSTEM theme — dark
        // mode gets a grey wash, light mode a near-white one. HandQ's own
        // palette is already light-mode-authored (dark text on light
        // surfaces), so a grey system tint mismatches everything above.
        // Force this Electron window's themeSource to 'light' while a
        // material is active, back to 'system' when it's turned off.
        // themeSource is a per-window override; other Electron apps and
        // native Windows chrome are untouched.
        if (material === 'none') {
            nativeTheme.themeSource = 'system';
        } else {
            nativeTheme.themeSource = 'light';
        }
        logLine('GLASS', 'window backgroundMaterial set', { material, themeSource: nativeTheme.themeSource });
        return true;
    } catch (err) {
        logLine('GLASS', 'setBackgroundMaterial failed', { material, err: err && err.message });
        return false;
    }
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

ipcMain.handle('dialog:pickSkillFile', async () => {
    const owner = BrowserWindow.getFocusedWindow() || mainWindow || null;
    // A skill is a folder (SKILL.md + scripts/ + reference/), so import picks
    // the directory and Python mirrors the whole tree. Picking a loose
    // SKILL.md would lose every sibling file.
    const result = await dialog.showOpenDialog(owner, {
        title: 'Select skill folder to import',
        properties: ['openDirectory'],
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
    // Reap the mention search worker unconditionally — no state to flush,
    // no grace budget needed.
    if (mentionChild && mentionChild.exitCode === null) {
        try { mentionChild.stdin.end(); } catch (_) { /* ignore */ }
        try { mentionChild.kill(); } catch (_) { /* ignore */ }
    }
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
