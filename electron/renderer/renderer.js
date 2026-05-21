// HandQ renderer.
//
// Wires the DOM in index.html to window.handq (preload). All bridge events
// arrive through window.handq.on*; outbound messages go through
// window.handq.sendRequest / getConfig / setConfig.
//
// Event-type contract (porting_design.md §(2)):
//   token_stream / event=text_delta  -> append evt.text to the active bubble
//   token_stream / event=tool_call   -> render a collapsible tool-call card
//   token_stream / event=done        -> seal the active bubble
//   status                           -> update the sidebar status pill
//   final                            -> mark the bubble complete + log result
//   error                            -> append a red error bubble
//
// Settings panel:
//   On open  -> handq.getConfig() -> populate every form field from the
//               loaded config payload.
//   On Save  -> read the form into a config object whose shape mirrors the
//               loaded payload, then handq.setConfig(obj). Show a toast
//               banner "Settings saved." on success.
//
// The settings UI is intentionally configuration-format-agnostic from the
// user's perspective; persistence is the backend's responsibility.

'use strict';

// --- renderer-side logging ------------------------------------------------
//
// window.__handqLog(level, ...args) is a small, dependency-free helper that:
//   * prefixes [RENDER] + ISO timestamp + level
//   * mirrors to console.{log|debug|warn|error} as appropriate
//   * appends to a hidden <pre id="debug-panel"> drawer that can be toggled
//     on Ctrl+Shift+L. The panel is created lazily on first toggle so it
//     adds zero DOM weight unless the developer asks for it.
//
// The drawer is fixed-position at the bottom of the viewport, has a
// "copy to clipboard" button, and never blocks pointer events on the rest
// of the UI when hidden.

(function installHandqLog() {
    const buffer = [];      // ring buffer (most recent ~2000 lines)
    const MAX_LINES = 2000;
    let panelEl = null;     // <pre id="debug-panel"> — created on first toggle
    let panelBodyEl = null;
    let panelVisible = false;

    function safeStringify(v) {
        if (typeof v === 'string') return v;
        try { return JSON.stringify(v); } catch (_) { return String(v); }
    }

    function formatLine(level, args) {
        const ts = new Date().toISOString();
        const parts = [];
        for (const a of args) parts.push(safeStringify(a));
        return '[' + ts + '] [RENDER] [' + level + '] ' + parts.join(' ');
    }

    function appendToPanel(line) {
        buffer.push(line);
        if (buffer.length > MAX_LINES) buffer.shift();
        if (panelBodyEl) {
            panelBodyEl.textContent += line + '\n';
            panelBodyEl.scrollTop = panelBodyEl.scrollHeight;
        }
    }

    function ensurePanel() {
        if (panelEl) return panelEl;
        panelEl = document.createElement('div');
        panelEl.id = 'debug-panel-wrap';
        panelEl.style.cssText =
            'position:fixed;left:0;right:0;bottom:0;height:30vh;' +
            'background:rgba(20,20,24,0.95);color:#dfe3ea;' +
            'font:12px/1.4 ui-monospace,Menlo,Consolas,monospace;' +
            'border-top:1px solid #3a3f4a;z-index:99999;' +
            'display:flex;flex-direction:column;';

        const bar = document.createElement('div');
        bar.style.cssText =
            'display:flex;align-items:center;gap:8px;padding:4px 8px;' +
            'background:#1a1c20;border-bottom:1px solid #3a3f4a;';
        const label = document.createElement('span');
        label.textContent = 'HandQ debug log (Ctrl+Shift+L to hide)';
        label.style.flex = '1';
        const copyBtn = document.createElement('button');
        copyBtn.textContent = 'Copy';
        copyBtn.style.cssText =
            'font:inherit;padding:2px 10px;background:#2c313a;color:#dfe3ea;' +
            'border:1px solid #4a5060;border-radius:3px;cursor:pointer;';
        copyBtn.addEventListener('click', () => {
            const text = buffer.join('\n');
            try {
                if (navigator.clipboard && navigator.clipboard.writeText) {
                    navigator.clipboard.writeText(text);
                    copyBtn.textContent = 'Copied!';
                    setTimeout(() => { copyBtn.textContent = 'Copy'; }, 1200);
                }
            } catch (_) { /* ignore */ }
        });
        const clearBtn = document.createElement('button');
        clearBtn.textContent = 'Clear';
        clearBtn.style.cssText = copyBtn.style.cssText;
        clearBtn.addEventListener('click', () => {
            buffer.length = 0;
            if (panelBodyEl) panelBodyEl.textContent = '';
        });
        bar.appendChild(label);
        bar.appendChild(copyBtn);
        bar.appendChild(clearBtn);
        panelEl.appendChild(bar);

        panelBodyEl = document.createElement('pre');
        panelBodyEl.id = 'debug-panel';
        panelBodyEl.style.cssText =
            'flex:1;margin:0;padding:6px 8px;overflow:auto;white-space:pre-wrap;' +
            'word-break:break-word;color:inherit;background:transparent;';
        panelBodyEl.textContent = buffer.join('\n') + (buffer.length ? '\n' : '');
        panelEl.appendChild(panelBodyEl);

        document.body.appendChild(panelEl);
        return panelEl;
    }

    function togglePanel() {
        ensurePanel();
        panelVisible = !panelVisible;
        panelEl.style.display = panelVisible ? 'flex' : 'none';
    }

    window.__handqLog = function (level, ...args) {
        const lvl = (level || 'INFO').toString().toUpperCase();
        const line = formatLine(lvl, args);
        try {
            const sink =
                lvl === 'ERROR' ? console.error :
                lvl === 'WARN'  ? console.warn  :
                lvl === 'DEBUG' ? console.debug :
                                  console.log;
            sink.call(console, line);
        } catch (_) { /* ignore */ }
        appendToPanel(line);
    };

    // Truncate any single argument to N characters when stringified, so a
    // multi-MB token_stream payload doesn't fill the buffer. The truncation
    // policy (200 chars) matches the goal's instrumentation requirement.
    window.__handqTrunc = function (value, n) {
        const limit = (typeof n === 'number' && n > 0) ? n : 200;
        const s = safeStringify(value);
        return s.length > limit ? s.slice(0, limit) + '…(' + s.length + ')' : s;
    };

    // Strip API_KEY (and legacy api_key / api_key_env) out of any object
    // we're about to log (defence-in-depth; main.js and preload.js redact
    // independently).
    window.__handqRedact = function (value) {
        if (!value || typeof value !== 'object') return value;
        if (Array.isArray(value)) return value.map(window.__handqRedact);
        const out = {};
        for (const k of Object.keys(value)) {
            const v = value[k];
            if (k === 'API_KEY' || k === 'api_key' || k === 'api_key_env') {
                out[k] = (v === undefined || v === null || v === '') ? v : '<redacted>';
            } else if (v && typeof v === 'object') {
                out[k] = window.__handqRedact(v);
            } else {
                out[k] = v;
            }
        }
        return out;
    };

    // Ctrl+Shift+L toggles the drawer.
    window.addEventListener('keydown', (e) => {
        if (e.ctrlKey && e.shiftKey && (e.key === 'L' || e.key === 'l')) {
            e.preventDefault();
            togglePanel();
        }
    });
})();

(function () {
    const handq = window.handq;
    if (!handq) {
        window.__handqLog('ERROR', 'preload missing — window.handq is undefined');
        document.body.innerHTML =
            '<pre style="color:red">window.handq is missing — preload failed to load.</pre>';
        return;
    }
    window.__handqLog('INFO', 'renderer init');

    // ----- DOM refs --------------------------------------------------------

    const conversation = document.getElementById('conversation');
    const composer = document.getElementById('composer');
    const composerInput = document.getElementById('composer-input');

    const navChat = document.getElementById('nav-chat');
    const navSettings = document.getElementById('nav-settings');
    const viewChat = document.getElementById('view-chat');
    const viewSettings = document.getElementById('view-settings');
    const statusPill = document.getElementById('status-pill');

    const settingsForm = document.getElementById('settings-form');
    const settingsLoadBtn = document.getElementById('settings-load');
    const settingsStatus = document.getElementById('settings-status');
    const settingsToast = document.getElementById('settings-toast');

    // Visible fields.
    const cfgLlmApiKey = document.getElementById('cfg-llm-api-key');
    const cfgLlmApiKeyToggle = document.getElementById('cfg-llm-api-key-toggle');
    const cfgLlmMaxTokens = document.getElementById('cfg-llm-max-tokens');
    const cfgLlmModels = document.getElementById('cfg-llm-models');
    const cfgSessionLogLevel = document.getElementById('cfg-session-log-level');
    const cfgSessionStepThreshold =
        document.getElementById('cfg-session-step-threshold');
    const cfgSessionWorkspaceBase =
        document.getElementById('cfg-session-workspace-base');
    const cfgSessionVenvPath = document.getElementById('cfg-session-venv-path');
    const cfgSwToolWrite = document.getElementById('cfg-sw-tool-write');
    const cfgSwToolEdit = document.getElementById('cfg-sw-tool-edit');
    const cfgSwToolBash = document.getElementById('cfg-sw-tool-bash');
    const cfgSwHighRisk = document.getElementById('cfg-sw-high-risk');

    // Stash the full config as loaded so hidden fields (version,
    // api_key_env, high_risk_commands, switch descriptions, etc.)
    // round-trip unchanged on Save.
    let originalConfig = null;

    // ----- chat state ------------------------------------------------------

    /** The assistant bubble currently receiving streaming tokens. */
    let activeAssistantBubble = null;
    /** Map from call_id -> tool-call card element (for in-place updates). */
    const toolCardsByCallId = new Map();

    function el(tag, className, textContent) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (textContent !== undefined) node.textContent = textContent;
        return node;
    }

    function scrollToBottom() {
        conversation.scrollTop = conversation.scrollHeight;
    }

    function addUserBubble(text) {
        const bubble = el('div', 'bubble user');
        bubble.appendChild(el('div', 'bubble-role', 'You'));
        bubble.appendChild(el('div', 'bubble-body', text));
        conversation.appendChild(bubble);
        scrollToBottom();
    }

    function startAssistantBubble() {
        const bubble = el('div', 'bubble assistant streaming');
        bubble.appendChild(el('div', 'bubble-role', 'Assistant'));
        const body = el('div', 'bubble-body');
        bubble.appendChild(body);
        bubble._body = body;
        bubble._textNode = null; // lazily created
        conversation.appendChild(bubble);
        activeAssistantBubble = bubble;
        scrollToBottom();
        return bubble;
    }

    function ensureAssistantBubble() {
        if (!activeAssistantBubble) {
            return startAssistantBubble();
        }
        return activeAssistantBubble;
    }

    function appendTextDelta(text) {
        if (!text) return;
        const bubble = ensureAssistantBubble();
        // Use a single text node for incremental append — preserves
        // whitespace and avoids re-parsing HTML on every chunk.
        if (!bubble._textNode) {
            bubble._textNode = document.createTextNode('');
            const span = el('span', 'text-stream');
            span.appendChild(bubble._textNode);
            bubble._body.appendChild(span);
        }
        bubble._textNode.data += text;
        scrollToBottom();
    }

    function renderToolCall(callId, toolName, args, blockIndex) {
        const bubble = ensureAssistantBubble();
        const card = el('details', 'tool-card');
        card.open = false;

        const summary = el('summary');
        summary.appendChild(el('span', 'tool-badge', 'tool'));
        summary.appendChild(el('span', 'tool-name', toolName || '(unnamed)'));
        if (typeof blockIndex === 'number') {
            summary.appendChild(el('span', 'tool-index', '#' + blockIndex));
        }
        card.appendChild(summary);

        const pre = el('pre', 'tool-args');
        try {
            pre.textContent = JSON.stringify(args, null, 2);
        } catch (_) {
            pre.textContent = String(args);
        }
        card.appendChild(pre);

        bubble._body.appendChild(card);
        if (callId) toolCardsByCallId.set(callId, card);
        scrollToBottom();
    }

    function sealActiveBubble(extraClass) {
        if (!activeAssistantBubble) return;
        activeAssistantBubble.classList.remove('streaming');
        activeAssistantBubble.classList.add('complete');
        if (extraClass) activeAssistantBubble.classList.add(extraClass);
        activeAssistantBubble = null;
    }

    function addErrorBubble(message, where) {
        const bubble = el('div', 'bubble error');
        bubble.appendChild(el('div', 'bubble-role',
            'Error' + (where ? ' (' + where + ')' : '')));
        bubble.appendChild(el('div', 'bubble-body', message || '(no message)'));
        conversation.appendChild(bubble);
        scrollToBottom();
    }

    // ----- token_stream dispatch ------------------------------------------
    // Per backend_surface.md §(2):
    //   StreamTextDeltaEvent  -> { event:"text_delta", text }
    //   StreamToolCallEvent   -> { event:"tool_call", call_id, tool_name, args, block_index }
    //   StreamDoneEvent       -> { event:"done", result: LLMChatResult }

    handq.onTokenStream((evt) => {
        const kind = evt && evt.event;
        window.__handqLog('DEBUG', 'onTokenStream', {
            type: evt && evt.type,
            id: evt && evt.id,
            event: kind,
            payload: window.__handqTrunc(evt, 200),
        });
        if (kind === 'text_delta') {
            appendTextDelta(evt.text || '');
        } else if (kind === 'tool_call') {
            renderToolCall(evt.call_id, evt.tool_name, evt.args, evt.block_index);
        } else if (kind === 'done') {
            sealActiveBubble();
        }
    });

    handq.onStatus((evt) => {
        if (!evt) return;
        window.__handqLog('DEBUG', 'onStatus', {
            type: evt.type,
            id: evt.id,
            kind: evt.kind,
            payload: window.__handqTrunc(evt, 200),
        });
        if (evt.kind === 'state_changed' && evt.state) {
            statusPill.textContent = evt.state;
        } else if (evt.kind === 'progress') {
            statusPill.textContent =
                'progress ' + (evt.current || 0) + '/' + (evt.total || 0);
        } else if (evt.kind === 'step_started' && evt.desc) {
            statusPill.textContent = 'step: ' + evt.desc.slice(0, 40);
        } else if (evt.kind === 'task_completed') {
            statusPill.textContent = 'done';
        } else if (evt.kind === 'bridge_exit') {
            statusPill.textContent = 'bridge exited';
        }
    });

    handq.onFinal((evt) => {
        // The 'final' envelope can be either:
        //   * the end of a request (id = the request id) -> seal the bubble
        //   * a config_get / config_set response -> handled by getConfig/setConfig
        //     promises in preload.js (we still see the event here for logging)
        window.__handqLog('INFO', 'onFinal', {
            type: evt && evt.type,
            id: evt && evt.id,
            payload: window.__handqTrunc(window.__handqRedact(evt), 200),
        });
        if (!evt || !evt.result) return;

        // Seal the streaming bubble if one is still open.
        if (activeAssistantBubble) {
            sealActiveBubble('final');
        }

        // Catch config_get responses regardless of id-correlation success in
        // preload.js, by their unique shape ({config_path, config}). Idempotent
        // if the id-correlated Promise also resolves with the same payload.
        if (evt.result && evt.result.config && evt.result.config_path !== undefined) {
            window.__handqLog('INFO', 'config_get final received',
                { path: evt.result.config_path });
            applyConfigToForm(evt.result.config);
            settingsStatus.textContent = 'loaded';
            return;
        }

        // Surface a concise result line if the request envelope finished.
        if (evt.result && typeof evt.result.success === 'boolean') {
            const summary = el('div', 'bubble system');
            summary.appendChild(el('div', 'bubble-role', 'Final'));
            summary.appendChild(el('div', 'bubble-body',
                (evt.result.success ? '✓ ' : '✗ ') +
                (evt.result.message || '(no message)')));
            conversation.appendChild(summary);
            scrollToBottom();
        }
    });

    handq.onError((evt) => {
        if (!evt) return;
        window.__handqLog('ERROR', 'onError', {
            type: evt.type,
            id: evt.id,
            where: evt.where,
            fatal: !!evt.fatal,
            payload: window.__handqTrunc(evt, 200),
        });
        addErrorBubble(evt.message, evt.where);
        if (evt.fatal) {
            statusPill.textContent = 'fatal';
        }
    });

    // ----- composer --------------------------------------------------------

    let firstSendDone = false;

    composer.addEventListener('submit', (e) => {
        e.preventDefault();
        const text = composerInput.value.trim();
        if (!text) return;

        addUserBubble(text);
        composerInput.value = '';
        // Pre-emptively start a fresh assistant bubble for the response.
        activeAssistantBubble = null;

        if (!firstSendDone) {
            firstSendDone = true;
            window.__handqLog('INFO', 'composer submit (first; type=request)',
                { len: text.length, preview: window.__handqTrunc(text, 200) });
            handq.sendRequest({
                type: 'request',
                goal: text,
            });
        } else {
            window.__handqLog('INFO', 'composer submit (type=user_input)',
                { len: text.length, preview: window.__handqTrunc(text, 200) });
            handq.sendRequest({
                type: 'user_input',
                kind: 'message',
                text: text,
            });
        }
    });

    // Enter to send, Shift+Enter for newline.
    composerInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            composer.requestSubmit();
        }
    });

    // ----- view switching --------------------------------------------------

    function showView(name) {
        if (name === 'settings') {
            viewChat.classList.add('hidden');
            viewSettings.classList.remove('hidden');
            navChat.classList.remove('active');
            navSettings.classList.add('active');
        } else {
            viewSettings.classList.add('hidden');
            viewChat.classList.remove('hidden');
            navSettings.classList.remove('active');
            navChat.classList.add('active');
        }
    }
    navChat.addEventListener('click', () => showView('chat'));
    navSettings.addEventListener('click', () => {
        showView('settings');
        loadConfig();
    });

    // ----- settings: helpers ----------------------------------------------

    function modelsToText(models) {
        if (!Array.isArray(models)) return '';
        return models.map((m) => String(m)).join('\n');
    }

    function textToModels(text) {
        if (!text) return [];
        return text
            .split(/\r?\n/)
            .map((s) => s.trim())
            .filter((s) => s.length > 0);
    }

    function showToast(message, kind) {
        // kind: 'ok' | 'err'
        settingsToast.textContent = message;
        settingsToast.classList.remove('hidden', 'ok', 'err');
        settingsToast.classList.add(kind === 'err' ? 'err' : 'ok');
        // Auto-hide after 4s.
        clearTimeout(showToast._t);
        showToast._t = setTimeout(() => {
            settingsToast.classList.add('hidden');
        }, 4000);
    }

    // ----- settings: load --------------------------------------------------

    function applyConfigToForm(cfg) {
        cfg = cfg || {};
        // Stash the full original so hidden fields round-trip on Save.
        originalConfig = JSON.parse(JSON.stringify(cfg));

        const llm = cfg.llm || {};
        const session = cfg.session || {};
        const switches = cfg.interaction_switches || {};

        cfgLlmApiKey.value =
            (llm.API_KEY === undefined || llm.API_KEY === null)
                ? '' : String(llm.API_KEY);
        cfgLlmMaxTokens.value =
            (llm.max_tokens === undefined || llm.max_tokens === null)
                ? '' : String(llm.max_tokens);
        cfgLlmModels.value = modelsToText(llm.models);

        cfgSessionLogLevel.value = session.log_level || '';
        cfgSessionStepThreshold.value =
            (session.step_verification_threshold === undefined ||
             session.step_verification_threshold === null)
                ? '' : String(session.step_verification_threshold);
        cfgSessionWorkspaceBase.value = session.workspace_base || '';
        cfgSessionVenvPath.value = session.venv_path || '';

        function readSwitch(name) {
            const v = switches[name];
            if (v && typeof v === 'object' && 'auto_approve' in v) {
                return Boolean(v.auto_approve);
            }
            return false;
        }
        cfgSwToolWrite.checked = readSwitch('tool_write');
        cfgSwToolEdit.checked = readSwitch('tool_edit');
        cfgSwToolBash.checked = readSwitch('tool_bash');
        cfgSwHighRisk.checked = readSwitch('high_risk');
    }

    function readFormToConfig() {
        // Start from a deep clone of the original so version, api_key_env,
        // high_risk_commands, switch descriptions, and any other fields the
        // UI does not surface are preserved verbatim.
        const out = originalConfig
            ? JSON.parse(JSON.stringify(originalConfig))
            : {};

        const llm = out.llm && typeof out.llm === 'object' ? out.llm : {};
        const session = out.session && typeof out.session === 'object'
            ? out.session : {};
        const switches = out.interaction_switches
            && typeof out.interaction_switches === 'object'
                ? out.interaction_switches : {};

        // Hard cut on the legacy api_key_env / api_key fields — they are no
        // longer read by the backend, and we don't want to leave stale values
        // in the YAML that could confuse later editors.
        if ('api_key_env' in llm) delete llm.api_key_env;
        if ('api_key' in llm) delete llm.api_key;

        // llm.API_KEY — empty string means "clear it" so we still set it.
        llm.API_KEY = cfgLlmApiKey.value;

        if (cfgLlmMaxTokens.value === '') {
            delete llm.max_tokens;
        } else {
            const n = parseInt(cfgLlmMaxTokens.value, 10);
            if (!Number.isNaN(n)) llm.max_tokens = n;
        }
        llm.models = textToModels(cfgLlmModels.value);

        if (cfgSessionLogLevel.value) {
            session.log_level = cfgSessionLogLevel.value;
        } else {
            delete session.log_level;
        }
        if (cfgSessionStepThreshold.value === '') {
            delete session.step_verification_threshold;
        } else {
            const f = parseFloat(cfgSessionStepThreshold.value);
            if (!Number.isNaN(f)) session.step_verification_threshold = f;
        }
        if (cfgSessionWorkspaceBase.value) {
            session.workspace_base = cfgSessionWorkspaceBase.value;
        } else {
            delete session.workspace_base;
        }
        if (cfgSessionVenvPath.value) {
            session.venv_path = cfgSessionVenvPath.value;
        } else {
            delete session.venv_path;
        }

        function writeSwitch(name, checked) {
            if (!switches[name] || typeof switches[name] !== 'object') {
                switches[name] = {};
            }
            switches[name].auto_approve = Boolean(checked);
        }
        writeSwitch('tool_write', cfgSwToolWrite.checked);
        writeSwitch('tool_edit', cfgSwToolEdit.checked);
        writeSwitch('tool_bash', cfgSwToolBash.checked);
        writeSwitch('high_risk', cfgSwHighRisk.checked);

        out.llm = llm;
        out.session = session;
        out.interaction_switches = switches;
        return out;
    }

    function loadConfig() {
        window.__handqLog('INFO', 'loadConfig: dispatching getConfig');
        settingsStatus.textContent = 'loading…';
        handq.getConfig().then((result) => {
            const cfg = (result && result.config) || {};
            window.__handqLog('INFO', 'loadConfig: success',
                { path: result && result.config_path });
            applyConfigToForm(cfg);
            settingsStatus.textContent = 'loaded';
        }).catch((err) => {
            window.__handqLog('ERROR', 'loadConfig: failure',
                { err: err && err.message });
            settingsStatus.textContent =
                'load failed: ' + (err && err.message);
            showToast('Load failed: ' + (err && err.message), 'err');
        });
    }

    settingsLoadBtn.addEventListener('click', loadConfig);

    // Show/Hide toggle for the API_KEY input.
    if (cfgLlmApiKeyToggle) {
        cfgLlmApiKeyToggle.addEventListener('click', () => {
            const masked = cfgLlmApiKey.type === 'password';
            cfgLlmApiKey.type = masked ? 'text' : 'password';
            cfgLlmApiKeyToggle.textContent = masked ? 'Hide' : 'Show';
            cfgLlmApiKeyToggle.setAttribute(
                'aria-label',
                masked ? 'Hide API key' : 'Show API key',
            );
        });
    }

    settingsForm.addEventListener('submit', (e) => {
        e.preventDefault();
        settingsStatus.textContent = 'saving…';
        const cfg = readFormToConfig();
        window.__handqLog('INFO', 'settings submit: setConfig dispatch',
            { config: window.__handqRedact(cfg) });
        handq.setConfig(cfg).then((result) => {
            if (result && result.saved) {
                window.__handqLog('INFO', 'settings submit: saved',
                    { path: result.path });
                settingsStatus.textContent = 'saved';
                showToast('Settings saved.', 'ok');
            } else {
                window.__handqLog('WARN',
                    'settings submit: save returned no confirmation',
                    { result: result });
                settingsStatus.textContent = 'save returned no confirmation';
                showToast('Save returned no confirmation.', 'err');
            }
        }).catch((err) => {
            window.__handqLog('ERROR', 'settings submit: failure',
                { err: err && err.message });
            settingsStatus.textContent =
                'save failed: ' + (err && err.message);
            showToast('Save failed: ' + (err && err.message), 'err');
        });
    });
})();
