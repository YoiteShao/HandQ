// HandQ renderer.
//
// Wires the DOM in index.html to window.handq (preload). All bridge events
// arrive through window.handq.on*; outbound messages go through
// window.handq.sendRequest / getConfig / setConfig.
//
// Layout (post-redesign):
//   * Custom titlebar with min / max / close-to-tray buttons (frameless window).
//   * Conversation pane — bubbles aligned left (assistant/system/error) or
//     right (user); no role labels.
//   * Composer + Send.
//   * Shortcut bar with Settings / Status / New (+ a small status pill).
//   * Settings is an overlay, opened from the shortcut, closed on Save.
//   * Status is an overlay summarising the latest session state.

'use strict';

// --- renderer-side logging ------------------------------------------------

(function installHandqLog() {
    const buffer = [];
    const MAX_LINES = 2000;
    let panelEl = null;
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
            'background:rgba(255,255,255,0.92);color:#1d1f24;' +
            'font:12px/1.4 ui-monospace,Menlo,Consolas,monospace;' +
            'border-top:1px solid rgba(15,20,30,0.18);z-index:99999;' +
            'display:flex;flex-direction:column;backdrop-filter:blur(14px);';

        const bar = document.createElement('div');
        bar.style.cssText =
            'display:flex;align-items:center;gap:8px;padding:4px 8px;' +
            'background:rgba(255,255,255,0.85);' +
            'border-bottom:1px solid rgba(15,20,30,0.10);';
        const label = document.createElement('span');
        label.textContent = 'HandQ debug log (Ctrl+Shift+L to hide)';
        label.style.flex = '1';
        const copyBtn = document.createElement('button');
        copyBtn.textContent = 'Copy';
        copyBtn.style.cssText =
            'font:inherit;padding:2px 10px;background:#fff;color:#1d1f24;' +
            'border:1px solid rgba(15,20,30,0.18);border-radius:4px;cursor:pointer;';
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

    window.__handqTrunc = function (value, n) {
        const limit = (typeof n === 'number' && n > 0) ? n : 200;
        const s = safeStringify(value);
        return s.length > limit ? s.slice(0, limit) + '…(' + s.length + ')' : s;
    };

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
    const winCtl = window.windowControls || null;
    window.__handqLog('INFO', 'renderer init');

    // ----- DOM refs --------------------------------------------------------

    const conversation = document.getElementById('conversation');
    const composer = document.getElementById('composer');
    const composerInput = document.getElementById('composer-input');

    const statusPill = document.getElementById('status-pill');

    // Shortcut bar
    const scSettings = document.getElementById('sc-settings');
    const scStatus   = document.getElementById('sc-status');
    const scNew      = document.getElementById('sc-new');

    // Titlebar
    const tbMin   = document.getElementById('tb-min');
    const tbMax   = document.getElementById('tb-max');
    const tbClose = document.getElementById('tb-close');

    // Overlays
    const overlayStatus    = document.getElementById('overlay-status');
    const overlaySettings  = document.getElementById('overlay-settings');
    const statusCloseBtn   = document.getElementById('status-close');
    const settingsCancel   = document.getElementById('settings-cancel');

    // Status overlay readouts
    const stState    = document.getElementById('st-state');
    const stProgress = document.getElementById('st-progress');
    const stStep     = document.getElementById('st-step');
    const stLast     = document.getElementById('st-last');
    const stEvents   = document.getElementById('st-events');

    // Settings form
    const settingsForm     = document.getElementById('settings-form');
    const settingsLoadBtn  = document.getElementById('settings-load');
    const settingsStatus   = document.getElementById('settings-status');
    const settingsToast    = document.getElementById('settings-toast');

    const cfgLlmApiKey       = document.getElementById('cfg-llm-api-key');
    const cfgLlmApiKeyToggle = document.getElementById('cfg-llm-api-key-toggle');
    const cfgLlmMaxTokens    = document.getElementById('cfg-llm-max-tokens');
    const cfgLlmRoleTabs     = document.getElementById('cfg-llm-role-tabs');
    const cfgLlmRolePanes = {
        planner:      document.getElementById('cfg-llm-planner'),
        receptionist: document.getElementById('cfg-llm-receptionist'),
        agent:        document.getElementById('cfg-llm-agent'),
        helper:       document.getElementById('cfg-llm-helper'),
    };
    const cfgSessionLogLevel      = document.getElementById('cfg-session-log-level');
    const cfgSessionStepThreshold = document.getElementById('cfg-session-step-threshold');
    const cfgSessionVenvPath      = document.getElementById('cfg-session-venv-path');
    const cfgSwToolWrite = document.getElementById('cfg-sw-tool-write');
    const cfgSwToolEdit  = document.getElementById('cfg-sw-tool-edit');
    const cfgSwToolBash  = document.getElementById('cfg-sw-tool-bash');
    const cfgSwHighRisk  = document.getElementById('cfg-sw-high-risk');

    let originalConfig = null;

    // ----- session/state tracking (drives Status overlay + pill) -----------

    const session = {
        state:      'idle',
        progress:   '',
        currentStep:'',
        lastUpdate: '',
        events:     [],
    };
    const EVENT_RING = 30;

    // taskCompleted "locks" the pill to the completion banner until the user
    // submits a new message (or hits New). Backend often emits a stray
    // state_changed→idle after task_completed; without the lock the user
    // would see the pill flip from "complete" back to "idle" instantly.
    let taskCompleted = false;

    // Session generation watermark. The bridge tags every outbound envelope
    // with a `gen` field (see stdio_bridge.py: _StdioUI._generation). When
    // New is clicked, the renderer optimistically bumps `currentGen` BEFORE
    // sending new_session — so any in-flight emit from the OLD flow that
    // arrives during cleanup carries an older gen and gets dropped here.
    // This is the only mechanism that protects the new conversation from
    // a wedged old subtask whose blocking syscall (e.g. ssh_tool's
    // run_in_executor + time.sleep retry, ssh_setup getpass) prevents
    // Python from killing the OS thread on Windows.
    //
    // Bridge confirms with `final {generation}`; we sync to it (max-rule)
    // so rapid double-clicks stay consistent.
    let currentGen = 0;

    function gateGen(evt) {
        // Returns true if the event should be DROPPED.
        if (!evt) return true;
        const g = evt.gen;
        // Legacy or bridge-meta envelopes without a gen tag pass through —
        // the bridge tags everything in this build, but be permissive so
        // a half-upgraded combo (renderer new, bridge old) still works.
        if (typeof g !== 'number') return false;
        if (g < currentGen) return true;
        if (g > currentGen) currentGen = g;
        return false;
    }

    function setPill(text, opts) {
        const o = opts || {};
        // The "tooltip" always tracks the latest backend event so users can
        // hover the pill mid-completion to see what's happening underneath.
        statusPill.title = text || '';
        if (taskCompleted && !o.force) return;
        statusPill.textContent = text || 'idle';
    }

    function markCompleted(summary) {
        taskCompleted = true;
        statusPill.classList.add('complete');
        statusPill.textContent = 'complete';
        statusPill.title = summary
            ? ('complete — ' + truncate(summary, 200))
            : 'complete';
        session.state = 'complete';
        if (summary) addAssistantTextBubble(summary);
        recordEvent('task completed' + (summary ? ': ' + truncate(summary, 80) : ''));
    }

    function clearCompleted() {
        if (!taskCompleted) return;
        taskCompleted = false;
        statusPill.classList.remove('complete');
        statusPill.textContent = 'idle';
        statusPill.title = '';
        session.state = 'idle';
    }

    function truncate(s, n) {
        if (!s) return '';
        return s.length > n ? s.slice(0, n - 1) + '…' : s;
    }

    function recordEvent(line) {
        if (!line) return;
        session.events.push('[' + new Date().toLocaleTimeString() + '] ' + line);
        if (session.events.length > EVENT_RING) session.events.shift();
        session.lastUpdate = new Date().toLocaleTimeString();
    }

    function refreshStatusPanel() {
        stState.textContent    = session.state || 'idle';
        stProgress.textContent = session.progress || '—';
        stStep.textContent     = session.currentStep || '—';
        stLast.textContent     = session.lastUpdate || '—';
        stEvents.textContent   = session.events.join('\n');
        stEvents.scrollTop     = stEvents.scrollHeight;
    }

    // ----- chat state ------------------------------------------------------

    let activeAssistantBubble = null;
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
        bubble.appendChild(el('div', 'bubble-body', text));
        conversation.appendChild(bubble);
        scrollToBottom();
    }

    function startAssistantBubble() {
        const bubble = el('div', 'bubble assistant streaming');
        const body = el('div', 'bubble-body');
        bubble.appendChild(body);
        bubble._body = body;
        bubble._textNode = null;
        conversation.appendChild(bubble);
        activeAssistantBubble = bubble;
        scrollToBottom();
        return bubble;
    }

    function ensureAssistantBubble() {
        if (!activeAssistantBubble) return startAssistantBubble();
        return activeAssistantBubble;
    }

    function appendTextDelta(text) {
        if (!text) return;
        const bubble = ensureAssistantBubble();
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

    function addAssistantTextBubble(text) {
        // Single-shot non-streaming assistant message (e.g. receptionist reply).
        const bubble = el('div', 'bubble assistant');
        bubble.appendChild(el('div', 'bubble-body', text || ''));
        conversation.appendChild(bubble);
        scrollToBottom();
    }

    function addSystemBubble(text) {
        const bubble = el('div', 'bubble system');
        bubble.appendChild(el('div', 'bubble-body', text || ''));
        conversation.appendChild(bubble);
        scrollToBottom();
    }

    function addErrorBubble(message, where) {
        const bubble = el('div', 'bubble error');
        const prefix = where ? '[' + where + '] ' : '';
        bubble.appendChild(el('div', 'bubble-body', prefix + (message || '(no message)')));
        conversation.appendChild(bubble);
        scrollToBottom();
    }

    // ----- bridge events ---------------------------------------------------

    handq.onTokenStream((evt) => {
        if (gateGen(evt)) return;
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
        if (gateGen(evt)) return;
        if (!evt) return;
        window.__handqLog('DEBUG', 'onStatus', {
            type: evt.type,
            id: evt.id,
            kind: evt.kind,
            payload: window.__handqTrunc(evt, 200),
        });

        const args = Array.isArray(evt.args) ? evt.args : [];

        if (evt.kind === 'state_changed' && evt.state) {
            session.state = evt.state;
            recordEvent('state → ' + evt.state);
            setPill(evt.state);
        } else if (evt.kind === 'progress') {
            const cur = evt.current || 0;
            const tot = evt.total || 0;
            const text = 'progress ' + cur + '/' + tot;
            session.progress = cur + '/' + tot;
            recordEvent(text);
            setPill(text);
        } else if (evt.kind === 'step_started') {
            const desc = String(evt.desc || args[1] || '');
            session.currentStep = desc;
            recordEvent('step started: ' + desc);
            setPill('▶ ' + truncate(desc, 80));
        } else if (evt.kind === 'step_completed') {
            const desc = String(evt.desc || args[1] || '');
            recordEvent('step completed: ' + desc);
            setPill('✓ ' + truncate(desc, 80));
        } else if (evt.kind === 'step_confidence') {
            const conf = parseFloat(args[0]);
            if (!Number.isNaN(conf)) {
                recordEvent('confidence: ' + conf.toFixed(2));
                setPill('confidence ' + conf.toFixed(2));
            }
        } else if (evt.kind === 'decision_made') {
            const iter = args[0] || '';
            const reasoning = args[1] || '';
            recordEvent('decision[' + iter + ']: ' + truncate(reasoning, 120));
            setPill('💭 iter ' + iter + ' · ' + truncate(reasoning, 80));
        } else if (evt.kind === 'tool_execution_started') {
            const iter   = args[0] || '';
            const tool   = args[1] || '';
            const params = args[2] || '';
            const output = args[3];
            const isPre  = !output || output === 'None' || output === 'null';
            const tag    = isPre ? '⊙' : '✓';
            recordEvent(tag + ' tool[' + iter + '] ' + tool + ' ' + truncate(String(params), 120));
            setPill(tag + ' ' + tool + ' · ' + truncate(String(params), 80));
        } else if (evt.kind === 'task_completed') {
            const summary = evt.summary
                || (args.length ? String(args[0]) : '')
                || '';
            markCompleted(summary);
        } else if (evt.kind === 'bridge_exit') {
            session.state = 'bridge exited';
            recordEvent('bridge exited');
            setPill('bridge exited', { force: true });
        } else if (evt.kind === 'reply') {
            addAssistantTextBubble(evt.text || '');
        } else if (evt.kind === 'message') {
            addSystemBubble(evt.text || '');
        } else if (evt.kind === 'receptionist_thinking_on') {
            recordEvent('receptionist thinking…');
            setPill('thinking…');
        } else if (evt.kind === 'receptionist_thinking_off') {
            recordEvent('receptionist idle');
        }
        if (!overlayStatus.classList.contains('hidden')) {
            refreshStatusPanel();
        }
    });

    handq.onFinal((evt) => {
        if (gateGen(evt)) return;
        window.__handqLog('INFO', 'onFinal', {
            type: evt && evt.type,
            id: evt && evt.id,
            payload: window.__handqTrunc(window.__handqRedact(evt), 200),
        });
        if (!evt || !evt.result) return;

        if (activeAssistantBubble) sealActiveBubble('final');

        if (evt.result && evt.result.config && evt.result.config_path !== undefined) {
            window.__handqLog('INFO', 'config_get final received',
                { path: evt.result.config_path });
            applyConfigToForm(evt.result.config);
            settingsStatus.textContent = 'loaded';
            return;
        }

        if (evt.result && typeof evt.result.success === 'boolean') {
            const summary = el('div', 'bubble system');
            summary.appendChild(el('div', 'bubble-body',
                (evt.result.success ? '✓ ' : '✗ ') +
                (evt.result.message || '(no message)')));
            conversation.appendChild(summary);
            scrollToBottom();
        }
    });

    handq.onError((evt) => {
        if (gateGen(evt)) return;
        if (!evt) return;
        window.__handqLog('ERROR', 'onError', {
            type: evt.type,
            id: evt.id,
            where: evt.where,
            fatal: !!evt.fatal,
            payload: window.__handqTrunc(evt, 200),
        });
        addErrorBubble(evt.message, evt.where);
        recordEvent('error: ' + (evt.message || '(no message)'));
        if (evt.fatal) {
            session.state = 'fatal';
            setPill('fatal', { force: true });
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
        activeAssistantBubble = null;
        // A new turn — release the "complete" pill lock so live status text
        // resumes flowing.
        clearCompleted();

        if (!firstSendDone) {
            firstSendDone = true;
            window.__handqLog('INFO', 'composer submit (first; type=request)',
                { len: text.length, preview: window.__handqTrunc(text, 200) });
            handq.sendRequest({ type: 'request', goal: text });
        } else {
            window.__handqLog('INFO', 'composer submit (type=user_input)',
                { len: text.length, preview: window.__handqTrunc(text, 200) });
            handq.sendRequest({ type: 'user_input', kind: 'message', text: text });
        }
    });

    composerInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            composer.requestSubmit();
        }
    });

    // ----- titlebar window controls ----------------------------------------

    if (winCtl) {
        if (tbMin)   tbMin.addEventListener('click', () => winCtl.minimize());
        if (tbMax)   tbMax.addEventListener('click', () => winCtl.toggleMaximize());
        if (tbClose) tbClose.addEventListener('click', () => winCtl.hide());
    } else {
        window.__handqLog('WARN', 'windowControls preload bridge missing');
    }

    // ----- overlay helpers -------------------------------------------------

    function openOverlay(node) {
        node.classList.remove('hidden');
        node.setAttribute('aria-hidden', 'false');
    }
    function closeOverlay(node) {
        node.classList.add('hidden');
        node.setAttribute('aria-hidden', 'true');
    }

    // Click-outside on the overlay backdrop closes it.
    overlayStatus.addEventListener('click', (e) => {
        if (e.target === overlayStatus) closeOverlay(overlayStatus);
    });
    overlaySettings.addEventListener('click', (e) => {
        if (e.target === overlaySettings) closeOverlay(overlaySettings);
    });
    statusCloseBtn.addEventListener('click', () => closeOverlay(overlayStatus));
    settingsCancel.addEventListener('click', () => closeOverlay(overlaySettings));

    window.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            if (!overlayStatus.classList.contains('hidden'))   closeOverlay(overlayStatus);
            if (!overlaySettings.classList.contains('hidden')) closeOverlay(overlaySettings);
        }
    });

    // ----- shortcut buttons ------------------------------------------------

    scSettings.addEventListener('click', () => {
        openOverlay(overlaySettings);
        loadConfig();
    });

    scStatus.addEventListener('click', () => {
        refreshStatusPanel();
        openOverlay(overlayStatus);
    });

    scNew.addEventListener('click', () => {
        // Real "new session": ask the bridge to tear down the current
        // FlowController + services and reset the InteractionManager
        // singleton, then wipe the conversation pane. The next composer
        // submit goes out as a fresh `request` against a brand-new flow
        // (see stdio_bridge.py:_do_new_session).
        //
        // CRITICAL: bump currentGen BEFORE sending. The old flow may
        // still emit notify_* calls during the bridge's bounded drain
        // (and indefinitely if it's wedged on a Windows blocking I/O
        // that we can't kill — ssh_tool's run_in_executor, etc.). Any
        // such envelope carries the OLD generation; with our watermark
        // already at new gen, gateGen() drops them silently. The bridge
        // confirms the actual new gen in the new_session final result —
        // we sync via the max-rule in gateGen.
        currentGen += 1;
        window.__handqLog('INFO', 'scNew clicked — sending new_session',
            { optimistic_gen: currentGen });
        try {
            handq.sendRequest({ type: 'new_session' });
        } catch (err) {
            window.__handqLog('ERROR', 'new_session dispatch failed',
                { err: err && err.message });
        }

        conversation.innerHTML = '';
        toolCardsByCallId.clear();
        activeAssistantBubble = null;
        firstSendDone = false;
        clearCompleted();
        session.state = 'idle';
        session.progress = '';
        session.currentStep = '';
        session.events = [];
        session.lastUpdate = '';
        setPill('idle');
        composerInput.focus();
    });

    // ----- settings form helpers (unchanged from prior implementation) -----

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

    const PLANNER_MIN_VERSION = [4, 5];

    function modelVersion(modelStr) {
        const name = String(modelStr).split('::').pop().split(':')[0];
        let m = name.match(/claude-(\d+)-(\d+)-/);
        if (m) return [parseInt(m[1], 10), parseInt(m[2], 10)];
        m = name.match(/claude-(\d+)-[a-z]/);
        if (m) return [parseInt(m[1], 10), 0];
        return [0, 0];
    }
    function versionGte(a, b) {
        if (a[0] !== b[0]) return a[0] > b[0];
        return a[1] >= b[1];
    }
    function assignRoles(allModels) {
        const all = Array.isArray(allModels) ? allModels.slice() : [];
        const capable = all.filter((m) => versionGte(modelVersion(m), PLANNER_MIN_VERSION));
        if (capable.length === 0) {
            return {
                agent: all.slice(),
                planner: all.slice(),
                receptionist: all.slice(),
                helper: all.slice(),
            };
        }
        const n = capable.length;
        const opusN = capable.filter((m) => m.includes('opus')).length;
        let recepSkip;
        let fdataSkip;
        if (opusN > 0) {
            recepSkip = Math.min(opusN, n - 1);
            fdataSkip = Math.min(opusN + 2, n - 1);
        } else {
            recepSkip = Math.min(1, n - 1);
            fdataSkip = Math.min(2, n - 1);
        }
        return {
            agent: all.slice(),
            planner: capable.slice(),
            receptionist: capable.slice(recepSkip),
            helper: capable.slice(fdataSkip),
        };
    }

    function selectRoleTab(role) {
        const tabs = cfgLlmRoleTabs ? cfgLlmRoleTabs.querySelectorAll('.role-tab') : [];
        tabs.forEach((btn) => {
            const active = btn.dataset.role === role;
            btn.classList.toggle('active', active);
            btn.setAttribute('aria-selected', active ? 'true' : 'false');
        });
        for (const key of Object.keys(cfgLlmRolePanes)) {
            const pane = cfgLlmRolePanes[key];
            if (!pane) continue;
            pane.hidden = (key !== role);
        }
    }
    if (cfgLlmRoleTabs) {
        cfgLlmRoleTabs.addEventListener('click', (e) => {
            const btn = e.target && e.target.closest('.role-tab');
            if (!btn) return;
            const role = btn.dataset.role;
            if (role) selectRoleTab(role);
        });
    }

    function showToast(message, kind) {
        settingsToast.textContent = message;
        settingsToast.classList.remove('hidden', 'ok', 'err');
        settingsToast.classList.add(kind === 'err' ? 'err' : 'ok');
        clearTimeout(showToast._t);
        showToast._t = setTimeout(() => {
            settingsToast.classList.add('hidden');
        }, 4000);
    }

    function applyConfigToForm(cfg) {
        cfg = cfg || {};
        originalConfig = JSON.parse(JSON.stringify(cfg));

        const llm      = cfg.llm || {};
        const sessCfg  = cfg.session || {};
        const switches = cfg.interaction_switches || {};

        cfgLlmApiKey.value =
            (llm.API_KEY === undefined || llm.API_KEY === null) ? '' : String(llm.API_KEY);
        cfgLlmMaxTokens.value =
            (llm.max_tokens === undefined || llm.max_tokens === null) ? '' : String(llm.max_tokens);

        const rolesObj = (llm.roles && typeof llm.roles === 'object') ? llm.roles : null;
        if (rolesObj) {
            cfgLlmRolePanes.planner.value      = modelsToText(rolesObj.planner);
            cfgLlmRolePanes.receptionist.value = modelsToText(rolesObj.receptionist);
            cfgLlmRolePanes.agent.value        = modelsToText(rolesObj.agent);
            cfgLlmRolePanes.helper.value       = modelsToText(rolesObj.helper);
        } else if (Array.isArray(llm.models) && llm.models.length > 0) {
            const derived = assignRoles(llm.models);
            cfgLlmRolePanes.planner.value      = modelsToText(derived.planner);
            cfgLlmRolePanes.receptionist.value = modelsToText(derived.receptionist);
            cfgLlmRolePanes.agent.value        = modelsToText(derived.agent);
            cfgLlmRolePanes.helper.value       = modelsToText(derived.helper);
        } else {
            cfgLlmRolePanes.planner.value      = '';
            cfgLlmRolePanes.receptionist.value = '';
            cfgLlmRolePanes.agent.value        = '';
            cfgLlmRolePanes.helper.value       = '';
        }
        selectRoleTab('planner');

        cfgSessionLogLevel.value = sessCfg.log_level || '';
        cfgSessionStepThreshold.value =
            (sessCfg.step_verification_threshold === undefined ||
             sessCfg.step_verification_threshold === null)
                ? '' : String(sessCfg.step_verification_threshold);
        cfgSessionVenvPath.value = sessCfg.venv_path || '';

        function readSwitch(name) {
            const v = switches[name];
            if (v && typeof v === 'object' && 'auto_approve' in v) {
                return Boolean(v.auto_approve);
            }
            return false;
        }
        cfgSwToolWrite.checked = readSwitch('tool_write');
        cfgSwToolEdit.checked  = readSwitch('tool_edit');
        cfgSwToolBash.checked  = readSwitch('tool_bash');
        cfgSwHighRisk.checked  = readSwitch('high_risk');
    }

    function readFormToConfig() {
        const out = originalConfig
            ? JSON.parse(JSON.stringify(originalConfig))
            : {};
        const llm      = out.llm     && typeof out.llm     === 'object' ? out.llm     : {};
        const sess     = out.session && typeof out.session === 'object' ? out.session : {};
        const switches = out.interaction_switches
            && typeof out.interaction_switches === 'object'
                ? out.interaction_switches : {};

        if ('api_key_env' in llm) delete llm.api_key_env;
        if ('api_key' in llm) delete llm.api_key;
        llm.API_KEY = cfgLlmApiKey.value;

        if (cfgLlmMaxTokens.value === '') {
            delete llm.max_tokens;
        } else {
            const n = parseInt(cfgLlmMaxTokens.value, 10);
            if (!Number.isNaN(n)) llm.max_tokens = n;
        }

        if ('models' in llm) delete llm.models;
        llm.roles = {
            planner:      textToModels(cfgLlmRolePanes.planner.value),
            receptionist: textToModels(cfgLlmRolePanes.receptionist.value),
            agent:        textToModels(cfgLlmRolePanes.agent.value),
            helper:       textToModels(cfgLlmRolePanes.helper.value),
        };

        if (cfgSessionLogLevel.value) sess.log_level = cfgSessionLogLevel.value;
        else delete sess.log_level;

        if (cfgSessionStepThreshold.value === '') {
            delete sess.step_verification_threshold;
        } else {
            const f = parseFloat(cfgSessionStepThreshold.value);
            if (!Number.isNaN(f)) sess.step_verification_threshold = f;
        }
        if ('workspace_base' in sess) delete sess.workspace_base;
        if (cfgSessionVenvPath.value) sess.venv_path = cfgSessionVenvPath.value;
        else delete sess.venv_path;

        function writeSwitch(name, checked) {
            if (!switches[name] || typeof switches[name] !== 'object') {
                switches[name] = {};
            }
            switches[name].auto_approve = Boolean(checked);
        }
        writeSwitch('tool_write', cfgSwToolWrite.checked);
        writeSwitch('tool_edit',  cfgSwToolEdit.checked);
        writeSwitch('tool_bash',  cfgSwToolBash.checked);
        writeSwitch('high_risk',  cfgSwHighRisk.checked);

        out.llm = llm;
        out.session = sess;
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
            settingsStatus.textContent = 'load failed: ' + (err && err.message);
            showToast('Load failed: ' + (err && err.message), 'err');
        });
    }

    settingsLoadBtn.addEventListener('click', loadConfig);

    if (cfgLlmApiKeyToggle) {
        const eyeShow = cfgLlmApiKeyToggle.querySelector('.eye-show');
        const eyeHide = cfgLlmApiKeyToggle.querySelector('.eye-hide');

        function applyEyeState(revealed) {
            if (eyeShow) {
                eyeShow.style.display = revealed ? 'none' : '';
                eyeShow.removeAttribute('hidden');
                if (revealed) eyeShow.setAttribute('hidden', '');
            }
            if (eyeHide) {
                eyeHide.style.display = revealed ? '' : 'none';
                eyeHide.removeAttribute('hidden');
                if (!revealed) eyeHide.setAttribute('hidden', '');
            }
            const labelText = revealed ? 'Hide API key' : 'Show API key';
            cfgLlmApiKeyToggle.setAttribute('aria-label', labelText);
            cfgLlmApiKeyToggle.setAttribute('title', labelText);
            cfgLlmApiKeyToggle.setAttribute('aria-pressed', revealed ? 'true' : 'false');
        }
        applyEyeState(cfgLlmApiKey.type !== 'password');

        cfgLlmApiKeyToggle.addEventListener('click', (e) => {
            e.preventDefault();
            const wasMasked = cfgLlmApiKey.type === 'password';
            cfgLlmApiKey.type = wasMasked ? 'text' : 'password';
            applyEyeState(cfgLlmApiKey.type === 'text');
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
                settingsStatus.textContent = 'saved';
                showToast('Settings saved.', 'ok');
                // Per spec: clicking Save returns the user to the chat view.
                setTimeout(() => closeOverlay(overlaySettings), 350);
            } else {
                settingsStatus.textContent = 'save returned no confirmation';
                showToast('Save returned no confirmation.', 'err');
            }
        }).catch((err) => {
            settingsStatus.textContent = 'save failed: ' + (err && err.message);
            showToast('Save failed: ' + (err && err.message), 'err');
        });
    });
})();
