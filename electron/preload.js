 // HandQ Electron preload.
//
// Exposes a minimal, safe surface to the renderer via contextBridge. No Node
// APIs (require, child_process, fs, path) are leaked — every privileged
// operation routes through ipcMain in main.js.
//
// Event-type dispatch happens here so each renderer-side callback only sees
// the envelopes it cares about. Per porting_design.md §(2) the backend emits
// `token_stream`, `status`, `final`, `error` envelopes; renderer-initiated
// requests are correlated by `id` and answered with a `final` envelope of
// the matching id.

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

const tokenStreamListeners = [];
const finalListeners = [];
const errorListeners = [];
const statusListeners = [];

// One-shot id-correlated waiters for getConfig / setConfig responses, which
// the bridge delivers as `final` envelopes (porting_design.md §(2.7), §(2.8)).
const pendingByid = new Map();

ipcRenderer.on('handq:event', (_evt, evt) => {
    if (!evt || typeof evt !== 'object') return;
    const t = evt.type;
    preloadLog('event', { type: t, id: evt.id });

    if (t === 'token_stream') {
        for (const cb of tokenStreamListeners) {
            try { cb(evt); } catch (_) { /* swallow */ }
        }
    } else if (t === 'status') {
        for (const cb of statusListeners) {
            try { cb(evt); } catch (_) { /* swallow */ }
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
    listeners: ['token_stream', 'final', 'error', 'status'],
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
     * onTokenStream(cb) — cb receives every {type:"token_stream", event, ...}
     * envelope. event ∈ {"text_delta","tool_call","done"}.
     */
    onTokenStream: (cb) => {
        preloadLog('onTokenStream', { registered: typeof cb === 'function' });
        if (typeof cb === 'function') tokenStreamListeners.push(cb);
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
        preloadLog('onStatus', { registered: typeof cb === 'function' });
        if (typeof cb === 'function') statusListeners.push(cb);
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
            pendingByid.set(id, resolve);
            ipcRenderer.invoke('handq:getConfig', id).catch(reject);
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
            pendingByid.set(id, resolve);
            ipcRenderer.invoke('handq:setConfig', { id: id, config: config }).catch(reject);
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
});
