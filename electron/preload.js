 // HandQ Electron preload.
//
// Exposes a minimal, safe surface to the renderer via contextBridge. No Node
// APIs (require, child_process, fs, path) are leaked — every privileged
// operation routes through ipcMain in main.js.
//
// Event-type dispatch happens here so each renderer-side callback only sees
// the envelopes it cares about. The backend emits `status`, `final`, and
// `error` envelopes; renderer-initiated requests are correlated by `id` and
// answered with a `final` envelope of the matching id.

'use strict';

const { contextBridge, ipcRenderer } = require('electron');

// --- preload-side logging --------------------------------------------------
//
// The preload script runs in an isolated world: it has Node access for the
// `require('electron')` line above but cannot write to disk on its own
// because the contextBridge boundary is the only thing the renderer sees.
// We log to console.debug for in-window devtools visibility and forward the
// same line to main.js via ipcRenderer.send('handq:log', ...) so it lands in
// the unified <userData>/logs/handq-frontend.log file.

function preloadLog(methodName, argSummary) {
    const ts = new Date().toISOString();
    const line = '[' + ts + '] [PRELOAD] ' + methodName +
                 (argSummary !== undefined ? ' ' + safeStringify(argSummary) : '');
    try { console.debug(line); } catch (_) { /* ignore */ }
    try {
        ipcRenderer.send('handq:log', {
            component: 'PRELOAD',
            msg: methodName,
            extra: argSummary,
        });
    } catch (_) { /* swallow — logging must never throw */ }
}

function safeStringify(v) {
    try { return JSON.stringify(v); } catch (_) { return '[unserialisable]'; }
}

// Strip API_KEY (and the legacy api_key / api_key_env) from any nested
// object before we log it. Used to redact the config payload passed to
// setConfig() (porting_design.md §(2.8)).
//
// Also strips the remote-control bearer token and the `handq://host:port/token`
// pairing string. Those travel through remote_pair / remote_probe / the
// remote_control_status response, all of which are logged like any other
// envelope — so without these keys a machine's control token would land in
// handq-frontend.log in cleartext. Anyone with the token can open agent
// sessions on that machine.
const _REDACT_KEYS = new Set([
    'API_KEY', 'api_key', 'api_key_env',
    'token', 'pairing', 'capability', 'remote_control_token',
]);

function redactApiKey(value) {
    if (!value || typeof value !== 'object') return value;
    if (Array.isArray(value)) return value.map(redactApiKey);
    const out = {};
    for (const k of Object.keys(value)) {
        const v = value[k];
        if (_REDACT_KEYS.has(k)) {
            out[k] = (v === undefined || v === null || v === '') ? v : '<redacted>';
        } else if (v && typeof v === 'object') {
            out[k] = redactApiKey(v);
        } else {
            out[k] = v;
        }
    }
    return out;
}

// --- correlation-id generator ---------------------------------------------

let _idCounter = 0;
function nextId(prefix) {
    _idCounter += 1;
    return (prefix || 'req') + '-' + Date.now().toString(36) + '-' + _idCounter;
}

// --- registered listeners --------------------------------------------------

const finalListeners = [];
const errorListeners = [];
const statusListeners = [];

// Bridge emits its boot_progress status burst within ~1.5s of spawn, which
// can land before the renderer has finished parsing renderer.js and called
// onStatus(). Without this backlog the boot overlay stays stuck because the
// `stdio_loop_ready` event is dropped on the floor. Drained exactly once,
// when the first onStatus listener registers.
const statusBacklog = [];
let statusReplayed = false;

// One-shot id-correlated waiters for getConfig / setConfig responses, which
// the bridge delivers as `final` envelopes (porting_design.md §(2.7), §(2.8)).
const pendingByid = new Map();

ipcRenderer.on('handq:event', (_evt, evt) => {
    if (!evt || typeof evt !== 'object') return;
    const t = evt.type;
    // Skip per-token streaming envelopes — they'd otherwise flood the log at
    // one line per token. Every other status kind still logs.
    if (!(t === 'status' && evt.kind === 'reply_delta')) {
        preloadLog('event', { type: t, kind: evt.kind, id: evt.id });
    }

    if (t === 'status') {
        if (!statusReplayed && statusListeners.length === 0) {
            statusBacklog.push(evt);
        } else {
            for (const cb of statusListeners) {
                try { cb(evt); } catch (_) { /* swallow */ }
            }
        }
    } else if (t === 'final') {
        if (evt.id && pendingByid.has(evt.id)) {
            const resolver = pendingByid.get(evt.id);
            pendingByid.delete(evt.id);
            try { resolver(evt.result); } catch (_) { /* swallow */ }
        }
        for (const cb of finalListeners) {
            try { cb(evt); } catch (_) { /* swallow */ }
        }
    } else if (t === 'error') {
        for (const cb of errorListeners) {
            try { cb(evt); } catch (_) { /* swallow */ }
        }
    }
});

preloadLog('preload loaded', {
    listeners: ['final', 'error', 'status'],
});

// --- exposed API -----------------------------------------------------------

contextBridge.exposeInMainWorld('handq', {
    /**
     * sendRequest({ type, ...fields }) — fire-and-forget write to the bridge
     * stdin. Used for `request`, `user_input`, and `shutdown` envelopes.
     * If the caller did not supply an `id`, one is generated so subsequent
     * `final`/`error` envelopes can be correlated.
     */
    sendRequest: (msg) => {
        const out = Object.assign({}, msg || {});
        if (!out.id) out.id = nextId(out.type || 'req');
        preloadLog('sendRequest', { type: out.type, id: out.id });
        ipcRenderer.invoke('handq:sendRequest', out);
        return out.id;
    },

    /**
     * onFinal(cb) — cb receives every {type:"final", id, result} envelope.
     */
    onFinal: (cb) => {
        preloadLog('onFinal', { registered: typeof cb === 'function' });
        if (typeof cb === 'function') finalListeners.push(cb);
    },

    /**
     * onError(cb) — cb receives every {type:"error", where, message, fatal}
     * envelope.
     */
    onError: (cb) => {
        preloadLog('onError', { registered: typeof cb === 'function' });
        if (typeof cb === 'function') errorListeners.push(cb);
    },

    /**
     * onStatus(cb) — cb receives every {type:"status", kind, ...} envelope.
     */
    onStatus: (cb) => {
        preloadLog('onStatus', {
            registered: typeof cb === 'function',
            backlog: statusBacklog.length,
        });
        if (typeof cb !== 'function') return;
        statusListeners.push(cb);
        if (!statusReplayed) {
            statusReplayed = true;
            const drained = statusBacklog.splice(0, statusBacklog.length);
            for (const evt of drained) {
                try { cb(evt); } catch (_) { /* swallow */ }
            }
        }
    },

    /**
     * getConfig() — Promise<result> resolving with the bridge's `config_get`
     * response (porting_design.md §(2.7)). Result shape:
     *   { config_path: string, config: { llm: {...}, session: {...}, ... } }
     */
    getConfig: () => {
        const id = nextId('cfg-get');
        preloadLog('getConfig', { id: id });
        return new Promise((resolve, reject) => {
            const timer = setTimeout(() => {
                if (pendingByid.has(id)) {
                    pendingByid.delete(id);
                    reject(new Error('Config load timed out'));
                }
            }, 10000);
            pendingByid.set(id, (result) => {
                clearTimeout(timer);
                resolve(result);
            });
            ipcRenderer.invoke('handq:getConfig', id)
                .then((ok) => {
                    if (ok === false) {
                        clearTimeout(timer);
                        pendingByid.delete(id);
                        reject(new Error('Bridge unavailable'));
                    }
                })
                .catch((err) => {
                    clearTimeout(timer);
                    pendingByid.delete(id);
                    reject(err);
                });
        });
    },

    /**
     * setConfig(config) — Promise<result> resolving with the bridge's
     * `config_set` response (porting_design.md §(2.8)). Result shape:
     *   { saved: boolean, path: string }
     */
    setConfig: (config) => {
        const id = nextId('cfg-set');
        preloadLog('setConfig', { id: id, config: redactApiKey(config) });
        return new Promise((resolve, reject) => {
            const timer = setTimeout(() => {
                if (pendingByid.has(id)) {
                    pendingByid.delete(id);
                    reject(new Error('Config save timed out'));
                }
            }, 10000);
            pendingByid.set(id, (result) => {
                clearTimeout(timer);
                resolve(result);
            });
            ipcRenderer.invoke('handq:setConfig', { id: id, config: config })
                .then((ok) => {
                    if (ok === false) {
                        clearTimeout(timer);
                        pendingByid.delete(id);
                        reject(new Error('Bridge unavailable'));
                    }
                })
                .catch((err) => {
                    clearTimeout(timer);
                    pendingByid.delete(id);
                    reject(err);
                });
        });
    },

    /**
     * searchPaths(query) — Promise resolving to
     *   { results: [{path, name, parent, isDir}, ...], disabled?, notReady?, timedOut? }
     * Backed by the resident PowerShell worker in main.js that runs SQL over
     * the Windows SystemIndex. Never throws; on any failure the results array
     * is empty so the caller can silently hide the dropdown.
     */
    searchPaths: (query) => {
        return ipcRenderer.invoke('handq:searchPaths', { query });
    },

    /**
     * listDirectory(dirPath, filter) — Promise resolving to
     *   { results: [{path, name, parent, isDir}, ...], timedOut?, error? }
     * Backs the mention dropdown's UNC / raw-directory fallback: renderer
     * detects a path-style @-token, main.js runs fs.readdir + fuzzy filter.
     */
    listDirectory: (dirPath, filter) => {
        return ipcRenderer.invoke('handq:listDirectory', { path: dirPath, filter });
    },

    /**
     * revealFile(path) — pop the OS file explorer to the given file, with
     * that file selected. Resolves to { ok:true } on success or
     * { ok:false, reason } on any failure — the caller can silently no-op.
     * Backed by Electron's shell.showItemInFolder in main.js. Fires from
     * the FILES tree in the session sidebar (double-click on a leaf).
     */
    revealFile: (filePath) => {
        return ipcRenderer.invoke('handq:revealFile', { path: filePath });
    },
});

// Custom-titlebar window controls (frameless window). The renderer ships its
// own min / max / close buttons; main.js drives the actual window state.
contextBridge.exposeInMainWorld('windowControls', {
    minimize: () => {
        preloadLog('window:minimize');
        ipcRenderer.send('window:minimize');
    },
    toggleMaximize: () => {
        preloadLog('window:toggle-maximize');
        ipcRenderer.send('window:toggle-maximize');
    },
    hide: () => {
        preloadLog('window:hide');
        ipcRenderer.send('window:hide');
    },
    // Ask main to grow the window based on the current visible layout.
    // Grow-only + capped at maximize when session count reaches the
    // threshold (6). Payload is a descriptor object; main resolves the
    // desired size via a formula (baseline + per-panel deltas).
    // Renderer calls this every time _updateLayout runs.
    autoResize: (layout) => {
        // Backwards compatible: if a bare number is passed, forward it
        // as a session count; main.js accepts either shape.
        preloadLog('window:auto-resize', typeof layout === 'object' ? layout : { sessions: layout });
        ipcRenderer.send('window:auto-resize', layout);
    },
    // Subscribe to maximize / unmaximize events so the custom titlebar
    // can swap the max-button icon (single square ↔ two overlapping squares).
    onMaxState: (cb) => {
        if (typeof cb !== 'function') return;
        ipcRenderer.on('window:maxState', (_evt, state) => {
            try { cb(state); } catch (_) { /* swallow */ }
        });
    },
    // Subscribe to window focus/blur so the custom titlebar can dim like a
    // real OS window when it loses activation.
    onActiveState: (cb) => {
        if (typeof cb !== 'function') return;
        ipcRenderer.on('window:activeState', (_evt, state) => {
            try { cb(state); } catch (_) { /* swallow */ }
        });
    },
});

// Global hotkey settings — renderer reads/writes the toggle shortcut.
contextBridge.exposeInMainWorld('hotkeySettings', {
    get: () => {
        preloadLog('hotkey:get');
        return ipcRenderer.invoke('hotkey:get');
    },
    set: (accelerator) => {
        preloadLog('hotkey:set', { accelerator });
        return ipcRenderer.invoke('hotkey:set', accelerator);
    },
});

// Native open-file dialog used by the Templates panel's "Load history"
// button. Default path lands on %USERPROFILE%\HandQ\History\ (Windows) or
// the platform-appropriate fallback resolved in main.js. Returns either
// the absolute path the user picked, or null if they cancelled.
contextBridge.exposeInMainWorld('handqDialog', {
    pickHistoryLog: () => {
        preloadLog('dialog:pickHistoryLog');
        return ipcRenderer.invoke('dialog:pickHistoryLog');
    },
    pickSkillFile: () => {
        preloadLog('dialog:pickSkillFile');
        return ipcRenderer.invoke('dialog:pickSkillFile');
    },
});

contextBridge.exposeInMainWorld('appInfo', {
    getVersion: () => {
        preloadLog('app:getVersion');
        return ipcRenderer.invoke('app:getVersion');
    },
});

// Renderer-side log forwarding. The renderer's window.__handqLog writes to the
// console + in-window debug panel; this bridge also ships each line to main so
// it lands in handq-frontend.log alongside the PRELOAD lines. Level is passed
// through as the component-adjacent tag; main.js gates DEBUG behind
// HANDQ_FRONTEND_DEBUG (see logLineDebug) so the high-volume per-event firehose
// doesn't flood the file, while INFO/WARN/ERROR always persist.
contextBridge.exposeInMainWorld('handqLog', {
    write: (level, msg) => {
        try {
            const lvl = String(level || 'INFO').toUpperCase();
            const component = 'RENDER-' + lvl;
            if (lvl === 'DEBUG') {
                // Route DEBUG through a dedicated channel main.js logs via
                // logLineDebug (gated on HANDQ_FRONTEND_DEBUG).
                ipcRenderer.send('handq:logDebug', {
                    component, msg: String(msg == null ? '' : msg),
                });
            } else {
                ipcRenderer.send('handq:log', {
                    component, msg: String(msg == null ? '' : msg),
                });
            }
        } catch (_) { /* logging must never throw */ }
    },
});

// Liquid Glass: desktopCapturer API for the renderer's WebGL refraction effect.
contextBridge.exposeInMainWorld('glassCapture', {
    getScreenSource: () => ipcRenderer.invoke('glass:getScreenSource'),
    getWindowBounds: () => ipcRenderer.invoke('glass:getWindowBounds'),
    // Subscribe to push updates from main.js on window move/resize. Avoids
    // the per-frame IPC poll that made window drag feel laggy.
    onBoundsChanged: (cb) => {
        if (typeof cb !== 'function') return;
        ipcRenderer.on('glass:boundsChanged', (_evt, bounds) => {
            try { cb(bounds); } catch (_) { /* swallow */ }
        });
    },
    // Ask main to toggle WDA_EXCLUDEFROMCAPTURE. webgl mode needs it ON (the
    // shader samples the real desktop and would otherwise capture itself);
    // veil mode is pure CSS and turns it OFF so the window appears in ordinary
    // OS screenshots. Fire-and-forget from the renderer's point of view.
    setContentProtection: (on) => ipcRenderer.invoke('glass:setContentProtection', on),
    // Live-set Win11's system frosted-glass material. Session-only, no
    // persistence — main.js re-creates the window with default (transparent-
    // only) options on next launch. Values: 'none' / 'auto' / 'mica' /
    // 'acrylic' / 'tabbed'. macOS + Linux ignore this.
    setBackgroundMaterial: (material) => ipcRenderer.invoke('window:setBackgroundMaterial', material),
});
