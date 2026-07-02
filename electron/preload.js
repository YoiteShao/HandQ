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
function redactApiKey(value) {
    if (!value || typeof value !== 'object') return value;
    if (Array.isArray(value)) return value.map(redactApiKey);
    const out = {};
    for (const k of Object.keys(value)) {
        const v = value[k];
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
    preloadLog('event', { type: t, id: evt.id });

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
    // Ask main to grow the window based on the current session count.
    // Grow-only + capped at maximize when count reaches the threshold (6).
    // Renderer calls this every time _updateLayout runs.
    autoResize: (count) => {
        preloadLog('window:auto-resize', { count });
        ipcRenderer.send('window:auto-resize', count);
    },
    // Subscribe to maximize / unmaximize events so the custom titlebar
    // can swap the max-button icon (single square ↔ two overlapping squares).
    onMaxState: (cb) => {
        if (typeof cb !== 'function') return;
        ipcRenderer.on('window:maxState', (_evt, state) => {
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
});

contextBridge.exposeInMainWorld('appInfo', {
    getVersion: () => {
        preloadLog('app:getVersion');
        return ipcRenderer.invoke('app:getVersion');
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
});
