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

    // Shortcut bar
    const scSettings = document.getElementById('sc-settings');
    const scScheduler = document.getElementById('sc-scheduler');
    const scSkills = document.getElementById('sc-skills');
    // (Legacy scNew "New" button removed — sessions are created via the
    // "+" button in the session tab bar.)


    // Titlebar
    const tbMin   = document.getElementById('tb-min');
    const tbMax   = document.getElementById('tb-max');
    const tbClose = document.getElementById('tb-close');

    // Boot overlay — visible by default; we hide it on first real status
    // event (or bridge_exit if boot crashed).
    const bootOverlay  = document.getElementById('boot-overlay');
    const bootPhaseEl  = document.getElementById('boot-phase');
    const bootElapsedEl= document.getElementById('boot-elapsed');
    const bootHintEl   = document.getElementById('boot-hint');
    const bootErrorEl  = document.getElementById('boot-error');
    const bootTitleEl  = document.getElementById('boot-title');

    let bootHidden = false;
    const bootStartedAt = Date.now();
    let bootElapsedTimer = null;
    let bootSlowHintTimer = null;
    let bootErrorState = false;

    // Friendly labels for the structured phase names emitted by
    // bridge_main.py:_emit_boot_progress. Anything we don't have a label
    // for falls back to a humanised version of the phase string itself.
    const BOOT_PHASE_LABELS = {
        fd_setup_done:           'preparing IPC channel',
        config_resolved:         'reading config',
        logging_ready:           'starting log handlers',
        importing:               'loading module',
        imported:                'module loaded',
        import_failed:           'module failed to load',
        imports_done:            'all modules loaded',
        ltm_init_start:          'opening long-term memory',
        ltm_init_done:           'long-term memory ready',
        ltm_init_failed:         'long-term memory failed',
        personality_start_start: 'starting activity monitor',
        personality_start_done:  'activity monitor ready',
        personality_start_failed:'activity monitor failed',
        scheduler_start_start:   'starting scheduler',
        scheduler_start_done:    'scheduler ready',
        scheduler_start_failed:  'scheduler failed',
        stdio_loop_ready:        'ready',
    };

    function formatPhase(evt) {
        const phase = String(evt && evt.phase || '');
        let base = BOOT_PHASE_LABELS[phase] || phase.replace(/_/g, ' ');
        if (phase === 'importing' && evt.module) {
            base = 'loading ' + String(evt.module);
        } else if (phase === 'imported' && evt.module) {
            base = 'loaded ' + String(evt.module) +
                   (evt.took_ms ? ' (' + evt.took_ms + 'ms)' : '');
        } else if (phase === 'imports_done' && evt.took_ms) {
            base = 'all modules loaded (' + evt.took_ms + 'ms)';
        } else if (phase.endsWith('_done') && evt.took_ms) {
            base += ' (' + evt.took_ms + 'ms)';
        }
        return base;
    }

    function refreshBootElapsed() {
        if (!bootElapsedEl) return;
        const sec = (Date.now() - bootStartedAt) / 1000;
        bootElapsedEl.textContent = sec.toFixed(1) + 's';
    }

    function updateBootProgress(evt) {
        if (bootHidden || bootErrorState) return;
        if (bootPhaseEl) bootPhaseEl.textContent = formatPhase(evt);
        refreshBootElapsed();
    }

    function showBootError(message) {
        if (!bootOverlay) return;
        bootErrorState = true;
        if (bootTitleEl) bootTitleEl.textContent = 'HandQ failed to start';
        if (bootErrorEl) {
            bootErrorEl.textContent = String(message || 'unknown error');
            bootErrorEl.classList.remove('hidden');
        }
        if (bootHintEl) bootHintEl.classList.add('hidden');
        const spin = bootOverlay.querySelector('.boot-spinner');
        if (spin) spin.style.display = 'none';
        // Don't auto-hide on error; user has to read it. Stop the elapsed
        // clock so the number freezes at the failure time.
        if (bootElapsedTimer) {
            clearInterval(bootElapsedTimer);
            bootElapsedTimer = null;
        }
        if (bootSlowHintTimer) {
            clearTimeout(bootSlowHintTimer);
            bootSlowHintTimer = null;
        }
    }

    function hideBootOverlay() {
        if (bootHidden || bootErrorState) return;
        bootHidden = true;
        if (bootOverlay) bootOverlay.classList.add('hidden');
        if (bootElapsedTimer) {
            clearInterval(bootElapsedTimer);
            bootElapsedTimer = null;
        }
        if (bootSlowHintTimer) {
            clearTimeout(bootSlowHintTimer);
            bootSlowHintTimer = null;
        }
    }

    if (bootOverlay) {
        // Tick the elapsed clock every 100ms so the user sees the number
        // moving — proof of life when the bridge is silent for long
        // stretches (e.g. unzipping _internal/ on first launch).
        bootElapsedTimer = setInterval(refreshBootElapsed, 100);
        // After 30s with no "ready" signal, swap the phase line for an
        // explanatory hint. The bridge keeps emitting boot_progress so
        // formatPhase() will keep updating, but we add a yellow hint
        // strip below it so the user knows nothing is wrong.
        bootSlowHintTimer = setTimeout(() => {
            if (bootHintEl && !bootHidden && !bootErrorState) {
                bootHintEl.textContent =
                    "First launch can take up to a minute as the runtime " +
                    "unpacks. Subsequent launches are much faster.";
                bootHintEl.classList.remove('hidden');
            }
        }, 30000);
    }

    // Overlays
    const overlaySettings  = document.getElementById('overlay-settings');
    const settingsCancel   = document.getElementById('settings-cancel');
    // (The legacy global #overlay-confirmation modal is retired — confirmations
    //  now render inline per session card; see _ensureConfirmUI / UI3.)

    // Settings form
    const settingsForm     = document.getElementById('settings-form');
    const settingsLoadBtn  = document.getElementById('settings-load');
    const settingsStatus   = document.getElementById('settings-status');
    const settingsToast    = document.getElementById('settings-toast');

    const cfgLlmApiKey       = document.getElementById('cfg-llm-api-key');
    const cfgLlmApiKeyToggle = document.getElementById('cfg-llm-api-key-toggle');
    const cfgLlmMaxTokens    = document.getElementById('cfg-llm-max-tokens');
    const cfgLlmAvailableModels = document.getElementById('cfg-llm-available-models');
    const cfgLlmAgentChecks     = document.getElementById('cfg-llm-agent-checks');
    const cfgLlmHelperChecks    = document.getElementById('cfg-llm-helper-checks');
    const cfgSessionLogLevel      = document.getElementById('cfg-session-log-level');
    const cfgSessionVenvPath      = document.getElementById('cfg-session-venv-path');
    const cfgSwToolWrite = document.getElementById('cfg-sw-tool-write');
    const cfgSwToolEdit  = document.getElementById('cfg-sw-tool-edit');
    const cfgSwToolBash  = document.getElementById('cfg-sw-tool-bash');
    const cfgSwToolBrowserAuto    = document.getElementById('cfg-sw-tool-browser-auto');
    const cfgSwToolDesktopAuto    = document.getElementById('cfg-sw-tool-desktop-auto');
    const cfgSwHighRisk  = document.getElementById('cfg-sw-high-risk');
    const cfgEmailFolderBlacklist = document.getElementById('cfg-email-folder-blacklist');
    // Personalization fields — surface for git-hook learning + privacy
    // controls. yaml is the source of truth; bridge_main re-syncs hooks
    // on every launch based on personalization.git_hook_repos.
    const cfgPersEnabled = document.getElementById('cfg-pers-enabled');
    const cfgPersExcludedApps = document.getElementById('cfg-pers-excluded-apps');
    const cfgPersGitHookRepos = document.getElementById('cfg-pers-git-hook-repos');

    // Hotkey field
    const cfgHotkey = document.getElementById('cfg-hotkey');

    let originalConfig = null;

    // Settings loading overlay (created lazily on first settings open)
    let settingsLoadingEl = null;

    function ensureSettingsLoadingOverlay() {
        if (settingsLoadingEl) return settingsLoadingEl;
        const card = overlaySettings.querySelector('.settings-card');
        if (!card) return null;
        settingsLoadingEl = el('div', 'settings-loading-overlay hidden');
        settingsLoadingEl.appendChild(el('span', 'loading-text', 'Loading configuration…'));
        card.appendChild(settingsLoadingEl);
        return settingsLoadingEl;
    }

    function showSettingsLoading() {
        const ov = ensureSettingsLoadingOverlay();
        if (ov) ov.classList.remove('hidden');
    }
    function hideSettingsLoading() {
        if (settingsLoadingEl) settingsLoadingEl.classList.add('hidden');
    }

    // ----- Multi-session state -------------------------------------------
    //
    // The bridge supports many concurrent FlowController instances keyed by
    // session_id. The renderer mints a UUID per tab and stamps it on every
    // outbound IPC envelope; inbound envelopes carry session_id and we
    // route them to the matching tab's state bucket.
    //
    // All sessions are visible at once as tiled `.session-card` items in the
    // horizontal `#conversation` row (UI1) — there is no show/hide. Every
    // card holds its own bubble DOM, inline confirmation host, and per-pane
    // composer; switching the "active" session is only a focus/scroll aid
    // (border highlight + unread clear), not a CSS display toggle.

    function _uuid() {
        // Crypto-strong if available; falls back to math.random for safety.
        try {
            if (window.crypto && typeof window.crypto.randomUUID === 'function') {
                return window.crypto.randomUUID();
            }
        } catch (_) { /* ignore */ }
        // RFC4122 v4-ish fallback
        return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
            const r = Math.random() * 16 | 0;
            const v = c === 'x' ? r : (r & 0x3 | 0x8);
            return v.toString(16);
        });
    }

    function _newSessionState(sid, name) {
        return {
            sid,
            name,
            // Outer tiled card (grid item) for this session.
            card: null,
            // DOM scroll container holding this session's chat bubbles.
            // (the card's body — bubble helpers append here via _dispatchPane).
            pane: null,
            // Per-card header bits.
            titleEl: null,
            pillEl: null,
            // Per-card composer textarea (UI2 — each pane sends on its own sid).
            composerInput: null,
            // Inline confirmation host inside the card (UI3 — a popup in one
            // session renders in its own pane and never blocks another).
            confirmEl: null,
            // Lazily-built confirmation control refs (see _ensureConfirmUI).
            confirmUI: null,
            // Currently-pending confirmation for this session: {id, kind} or
            // null. The owning sid IS this session, so the answer is always
            // stamped with `sid` (never the globally-active tab).
            pendingConfirm: null,
            // Per-session activity feed (cap 30 items, mirrors the legacy
            // global `activityItems` ring buffer semantics).
            activityItems: [],
            // Per-session activity-strip state ("idle"|"planning"|...).
            sessionState: 'idle',
            // Current pill text (separate from sessionState so tooltips
            // can carry richer text without overwriting state).
            pillText: 'idle',
            // Most-recent decision_made reasoning, used by the strip.
            lastThinking: '',
            // Last tool dispatch ("write: path", etc.).
            lastCalledTool: '',
            // Count of in-flight tool executions; drives isTaskRunning().
            activeExecCount: 0,
            // First-send tracking — controls request vs user_input.
            firstSendDone: false,
            // Streaming bubbles + thinking placeholder.
            activeReceptionistBubble: null,
            thinkingBubble: null,
            // Boundary state for the checklist popover.
            checklistItems: null,
            checklistExpanded: false,
            // Workspace info from session_started event.
            sessionDir: '',
            workspaceDir: '',
            // Has unseen activity since the user last looked at this tab.
            unread: false,
        };
    }

    /** @type {Map<string, ReturnType<typeof _newSessionState>>} */
    const sessions = new Map();
    let activeSid = null;
    // Sids explicitly closed by the user. Prevents straggler events from
    // resurrecting a closed tab via lazy-mount (zombie tab).
    const closedSessions = new Set();

    // macOS-style card open/close: run the DOM mutation inside a View
    // Transition so the closing card scales+fades (::view-transition-old)
    // and remaining cards FLIP into their new slots (::view-transition-group).
    // The `data-vt-scope` attribute scopes the pseudo styles in styles.css so
    // unrelated future transitions won't inherit them.
    async function _runVT(mutate) {
        const supported = typeof document.startViewTransition === 'function';
        const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        if (!supported || reduced) {
            mutate();
            return;
        }
        document.documentElement.dataset.vtScope = 'session';
        let transition;
        try {
            transition = document.startViewTransition(mutate);
        } catch (err) {
            window.__handqLog('WARN', 'startViewTransition threw', { err: err && err.message });
            mutate();
            delete document.documentElement.dataset.vtScope;
            return;
        }
        try { await transition.finished; } catch (_) { /* aborted transitions are ok */ }
        delete document.documentElement.dataset.vtScope;
    }

    function sessionState(sid) {
        return sessions.get(sid);
    }

    function active() {
        return sessions.get(activeSid);
    }

    function getActivePane() {
        const s = active();
        return s ? s.pane : null;
    }

    function getPaneFor(sid) {
        const s = sessionState(sid);
        return s ? s.pane : null;
    }

    // Build the DOM scaffolding for a new session: its tiled session card
    // (header + scrollable body + inline confirmation host + per-pane
    // composer) and its tab button. Does NOT switch to it — caller decides.
    function _mountSession(sid, name) {
        const s = _newSessionState(sid, name);

        // ── Outer card (a flex item in the tiled #conversation row) ──────
        const card = document.createElement('div');
        card.className = 'session-card';
        card.dataset.sid = sid;
        // Unique VT name so this card gets its own snapshot pair when it
        // enters/leaves the DOM — required for per-element FLIP on the
        // remaining cards. Static per card lifetime.
        card.style.viewTransitionName = 'session-' + sid;

        // Header: name · status pill · close.
        const head = el('div', 'session-card-head');
        const title = el('span', 'session-card-title', name);
        title.addEventListener('dblclick', () => _startRenameSession(sid));
        const pill = el('span', 'session-card-pill', 'idle');
        const cardClose = el('button', 'session-card-close', '×');
        cardClose.type = 'button';
        cardClose.setAttribute('aria-label', 'Close session');
        cardClose.title = 'Close session';
        head.appendChild(title);
        head.appendChild(pill);
        head.appendChild(cardClose);

        // Body: the scroll container where chat bubbles + activity groups go.
        const body = el('div', 'session-card-body');
        body.dataset.sid = sid;

        // Inline confirmation host (UI3) — populated lazily on first prompt.
        const confirm = el('div', 'session-card-confirm hidden');

        // Per-pane composer (UI2) — its own textarea + send, stamps this sid.
        const form = document.createElement('form');
        form.className = 'session-card-composer';
        const wrap = el('div', 'composer-input-wrap');
        const ta = document.createElement('textarea');
        ta.className = 'session-card-input';
        ta.rows = 2;
        ta.placeholder = 'Type a message… (Ctrl+Enter to send)';
        const send = document.createElement('button');
        send.type = 'submit';
        send.setAttribute('aria-label', 'Send');
        send.title = 'Ctrl+Enter';
        send.innerHTML =
            '<svg viewBox="0 0 20 20" width="16" height="16" aria-hidden="true">' +
            '<path d="M10 4 L10 16 M10 4 L5 9 M10 4 L15 9" fill="none" ' +
            'stroke="currentColor" stroke-width="2" stroke-linecap="round" ' +
            'stroke-linejoin="round"/></svg>';
        wrap.appendChild(ta);
        wrap.appendChild(send);
        form.appendChild(wrap);

        card.appendChild(head);
        card.appendChild(body);
        card.appendChild(confirm);
        card.appendChild(form);
        conversation.appendChild(card);

        s.card = card;
        s.pane = body;
        s.titleEl = title;
        s.pillEl = pill;
        s.confirmEl = confirm;
        s.composerInput = ta;

        // Clicking anywhere on the card focuses this session (jump aid +
        // unread clear). The close button stops propagation below.
        card.addEventListener('mousedown', () => {
            if (activeSid !== sid) switchSession(sid);
        });
        cardClose.addEventListener('click', (ev) => {
            ev.stopPropagation();
            closeSession(sid);
        });
        form.addEventListener('submit', (ev) => {
            ev.preventDefault();
            submitText(sid, ta.value, ta);
        });
        ta.addEventListener('keydown', (ev) => {
            if (ev.key === 'Enter' && ev.ctrlKey) {
                ev.preventDefault();
                form.requestSubmit();
            }
        });
        ta.addEventListener('input', () => {
            _FloatingComposer.onSourceInput(sid, ta,
                () => (sessions.get(sid) || {}).name || sid.slice(0, 8),
                (finalText) => submitText(sid, finalText, ta));
        });

        sessions.set(sid, s);
        _updateLayout();
        return s;
    }

    function _autoNameForNewSession() {
        // Number tabs in creation order. Sessions are not removed from the
        // numbering pool — keeps names stable as old tabs are closed.
        return 'Session ' + (sessions.size + 1);
    }

    function _renameSession(sid, name) {
        const s = sessions.get(sid);
        if (!s || !name) return;
        s.name = name;
        if (s.titleEl) s.titleEl.textContent = name;
    }

    function _startRenameSession(sid) {
        const s = sessions.get(sid);
        if (!s || !s.titleEl) return;
        const el_ = s.titleEl;
        const oldName = el_.textContent;
        const input = document.createElement('input');
        input.type = 'text';
        input.className = 'session-card-title-input';
        input.value = oldName;
        el_.replaceWith(input);
        input.focus();
        input.select();
        function commit() {
            const newName = input.value.trim() || oldName;
            const span = document.createElement('span');
            span.className = 'session-card-title';
            span.textContent = newName;
            span.addEventListener('dblclick', () => _startRenameSession(sid));
            input.replaceWith(span);
            s.titleEl = span;
            s.name = newName;
        }
        input.addEventListener('blur', commit);
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') { e.preventDefault(); input.blur(); }
            if (e.key === 'Escape') { input.value = oldName; input.blur(); }
        });
    }

    function createSession(opts) {
        const sid = (opts && opts.sid) || _uuid();
        const name = (opts && opts.name) || _autoNameForNewSession();
        _mountSession(sid, name);
        switchSession(sid);
        return sid;
    }

    function switchSession(sid) {
        if (!sessions.has(sid)) return;
        // All cards are visible in the tiled layout; "switching" just moves
        // the focus highlight + scrolls the card into view + clears unread.
        if (activeSid && activeSid !== sid && sessions.has(activeSid)) {
            const old = sessions.get(activeSid);
            if (old.card) old.card.classList.remove('active');
        }
        activeSid = sid;
        const s = sessions.get(sid);
        if (s.card) {
            s.card.classList.add('active');
            try { s.card.scrollIntoView({ inline: 'nearest', block: 'nearest' }); }
            catch (_) { /* ignore */ }
        }
        s.unread = false;
        // Repaint UI affordances that reflect the active session's state.
        _repaintActivityForActive();
        try { if (s.composerInput) s.composerInput.focus(); } catch (_) { /* ignore */ }
        window.__handqLog('INFO', 'switchSession', { sid });
    }

    function _repaintActivityForActive() {
        // No-op — per-session activity is rendered inline in each pane.
    }

    async function closeSession(sid) {
        if (!sessions.has(sid)) return;
        closedSessions.add(sid);
        window.__handqLog('INFO', 'closeSession', { sid });
        // Kill the floating pop-out editor if it was pointed at this session
        // — its backing textarea is about to be removed from the DOM.
        try { _FloatingComposer.destroyFor(sid); } catch (_) { /* ignore */ }
        // Send close_session to bridge so flow.destroy() releases resources.
        try {
            handq.sendRequest({ type: 'close_session', session_id: sid });
        } catch (err) {
            window.__handqLog('ERROR', 'close_session dispatch failed',
                { err: err && err.message });
        }
        // All layout-affecting mutations happen inside one View Transition so
        // the browser snapshots once, then animates the closing card out and
        // the remaining cards into their new slots.
        await _runVT(() => {
            const s = sessions.get(sid);
            if (s && s.card && s.card.parentNode) {
                s.card.parentNode.removeChild(s.card);
            }
            sessions.delete(sid);
            _updateLayout();
            if (activeSid === sid) {
                // Switch to neighbour tab, or auto-spawn a fresh one if this
                // was the last session.
                const next = sessions.keys().next();
                if (next && !next.done) {
                    switchSession(next.value);
                } else {
                    createSession();
                }
            }
        });
    }

    function _updateLayout() {
        const n = sessions.size;
        conversation.classList.remove('layout-row', 'layout-grid', 'layout-scroll');
        if (n <= 3) conversation.classList.add('layout-row');
        else if (n <= 6) conversation.classList.add('layout-grid');
        else conversation.classList.add('layout-scroll');
        // Ask main to grow the window to fit the new tile count (grow-only;
        // maximized at 6). Main is authoritative on display bounds + clamps.
        if (window.windowControls && typeof window.windowControls.autoResize === 'function') {
            try { window.windowControls.autoResize(n); }
            catch (_) { /* ignore */ }
        }
    }

    function currentSid() {
        return activeSid;
    }

    // ----- Floating composer (long-text pop-out editor) ------------------
    // One instance per session. Auto-opens when the session's inline textarea
    // passes >3 rows OR >200 chars, and stays independent from other
    // sessions' floaters — dragging/typing in one never disturbs the others.
    // Two-way synced with the source textarea while open. Draggable header +
    // bottom-right resize handle. Position clamped inside the viewport
    // (respects titlebar so it never overlaps window controls).
    const _FloatingComposer = (function () {
        const MARGIN = 8;
        const TITLEBAR_H = 42;
        const MIN_W = 320;
        const MIN_H = 180;
        const THRESHOLD_CHARS = 200;
        const THRESHOLD_ROWS = 3;
        const CASCADE_STEP = 28;

        const instances = new Map(); // sid -> instance state

        function _bounds() {
            return {
                left: MARGIN,
                top: TITLEBAR_H + MARGIN,
                right: window.innerWidth - MARGIN,
                bottom: window.innerHeight - MARGIN,
            };
        }

        function _clampBox(box) {
            const b = _bounds();
            const maxW = b.right - b.left;
            const maxH = b.bottom - b.top;
            let w = Math.max(MIN_W, Math.min(box.w, maxW));
            let h = Math.max(MIN_H, Math.min(box.h, maxH));
            let x = Math.max(b.left, Math.min(box.x, b.right - w));
            let y = Math.max(b.top,  Math.min(box.y, b.bottom - h));
            return { x, y, w, h };
        }

        function _visibleCount() {
            let n = 0;
            for (const inst of instances.values()) if (_isVisible(inst)) n++;
            return n;
        }

        function _defaultBox(cascadeIndex) {
            const b = _bounds();
            const w = Math.min(620, b.right - b.left);
            const h = Math.min(340, b.bottom - b.top);
            const cx = Math.round((window.innerWidth  - w) / 2);
            const cy = Math.round((window.innerHeight - h) / 2);
            const off = cascadeIndex * CASCADE_STEP;
            return _clampBox({ x: cx + off, y: cy + off, w, h });
        }

        function _applyPos(el, p) {
            el.style.left   = p.x + 'px';
            el.style.top    = p.y + 'px';
            el.style.width  = p.w + 'px';
            el.style.height = p.h + 'px';
        }

        function _build(sid) {
            const el = document.createElement('div');
            el.className = 'floating-composer hidden';
            el.dataset.sid = sid;

            const headEl = document.createElement('div');
            headEl.className = 'fc-head';

            const titleEl = document.createElement('div');
            titleEl.className = 'fc-title';
            titleEl.textContent = 'Editor';

            const hint = document.createElement('div');
            hint.className = 'fc-hint';
            hint.textContent = 'Ctrl+Enter · Esc';

            const closeBtn = document.createElement('button');
            closeBtn.type = 'button';
            closeBtn.className = 'fc-close';
            closeBtn.setAttribute('aria-label', 'Close');
            closeBtn.textContent = '×';

            headEl.appendChild(titleEl);
            headEl.appendChild(hint);
            headEl.appendChild(closeBtn);

            const body = document.createElement('div');
            body.className = 'fc-body';

            const textareaEl = document.createElement('textarea');
            textareaEl.className = 'fc-textarea';
            textareaEl.placeholder = 'Type here… (Ctrl+Enter to send)';
            body.appendChild(textareaEl);

            const resizeEl = document.createElement('div');
            resizeEl.className = 'fc-resize';
            resizeEl.setAttribute('aria-label', 'Resize');
            body.appendChild(resizeEl);

            el.appendChild(headEl);
            el.appendChild(body);
            document.body.appendChild(el);

            const inst = {
                sid, el, headEl, titleEl, closeBtn, textareaEl, resizeEl,
                pos: null,
                mirroring: false,
                currentInput: null,
                currentSend: null,
                userClosed: false,
            };
            instances.set(sid, inst);

            // Bring this floater on top when clicked anywhere.
            el.addEventListener('mousedown', () => _bringToFront(inst));

            closeBtn.addEventListener('click', (ev) => {
                ev.stopPropagation();
                inst.userClosed = true;
                _hide(inst);
            });

            textareaEl.addEventListener('input', () => {
                if (inst.mirroring) return;
                if (!inst.currentInput) return;
                inst.mirroring = true;
                try {
                    inst.currentInput.value = textareaEl.value;
                    inst.currentInput.dispatchEvent(new Event('input', { bubbles: true }));
                } finally { inst.mirroring = false; }
            });
            textareaEl.addEventListener('keydown', (ev) => {
                if (ev.key === 'Enter' && ev.ctrlKey) {
                    ev.preventDefault();
                    const t = textareaEl.value;
                    if (inst.currentSend) inst.currentSend(t);
                    _hide(inst);
                } else if (ev.key === 'Escape') {
                    ev.preventDefault();
                    inst.userClosed = true;
                    _hide(inst);
                }
            });

            _wireDrag(inst);
            _wireResize(inst);
            return inst;
        }

        function _bringToFront(inst) {
            // Small z-index bump so the most recently interacted floater is on top.
            // Cap growth so it stays under overlays (which sit at z=100).
            for (const other of instances.values()) {
                if (other.el) other.el.style.zIndex = '50';
            }
            inst.el.style.zIndex = '55';
        }

        function _wireDrag(inst) {
            let dragging = false;
            let dx = 0, dy = 0;
            inst.headEl.addEventListener('mousedown', (ev) => {
                if (ev.target === inst.closeBtn || inst.closeBtn.contains(ev.target)) return;
                dragging = true;
                inst.headEl.classList.add('dragging');
                dx = ev.clientX - inst.pos.x;
                dy = ev.clientY - inst.pos.y;
                ev.preventDefault();
            });
            window.addEventListener('mousemove', (ev) => {
                if (!dragging) return;
                inst.pos = _clampBox({
                    x: ev.clientX - dx,
                    y: ev.clientY - dy,
                    w: inst.pos.w,
                    h: inst.pos.h,
                });
                _applyPos(inst.el, inst.pos);
            });
            window.addEventListener('mouseup', () => {
                if (!dragging) return;
                dragging = false;
                inst.headEl.classList.remove('dragging');
            });
        }

        function _wireResize(inst) {
            let resizing = false;
            let startX = 0, startY = 0, startW = 0, startH = 0;
            inst.resizeEl.addEventListener('mousedown', (ev) => {
                resizing = true;
                startX = ev.clientX;
                startY = ev.clientY;
                startW = inst.pos.w;
                startH = inst.pos.h;
                ev.preventDefault();
                ev.stopPropagation();
            });
            window.addEventListener('mousemove', (ev) => {
                if (!resizing) return;
                inst.pos = _clampBox({
                    x: inst.pos.x,
                    y: inst.pos.y,
                    w: startW + (ev.clientX - startX),
                    h: startH + (ev.clientY - startY),
                });
                _applyPos(inst.el, inst.pos);
            });
            window.addEventListener('mouseup', () => {
                if (!resizing) return;
                resizing = false;
            });
        }

        function _show(inst, inputEl, title, sendCb) {
            inst.currentInput = inputEl;
            inst.currentSend = sendCb;
            inst.titleEl.textContent = title || 'Editor';
            inst.mirroring = true;
            try { inst.textareaEl.value = inputEl.value || ''; }
            finally { inst.mirroring = false; }
            // Make visible first so _visibleCount reflects post-show state.
            inst.el.classList.remove('hidden');
            if (!inst.pos) {
                // Cascade based on peers already visible (this one now counted).
                const idx = Math.max(0, _visibleCount() - 1);
                inst.pos = _defaultBox(idx);
            } else {
                inst.pos = _clampBox(inst.pos);
            }
            _applyPos(inst.el, inst.pos);
            _bringToFront(inst);
            try {
                inst.textareaEl.focus();
                const n = inst.textareaEl.value.length;
                inst.textareaEl.setSelectionRange(n, n);
            } catch (_) { /* ignore */ }
        }

        function _hide(inst) {
            if (!inst || !inst.el || inst.el.classList.contains('hidden')) return;
            inst.el.classList.add('hidden');
            inst.currentInput = null;
            inst.currentSend = null;
        }

        function _isVisible(inst) {
            return !!(inst && inst.el && !inst.el.classList.contains('hidden'));
        }

        function _isLong(inputEl) {
            const text = (inputEl && inputEl.value) || '';
            if (!text) return false;
            if (text.length > THRESHOLD_CHARS) return true;
            // Explicit newlines: count \n characters + 1.
            let newlineRows = 1;
            for (let i = 0; i < text.length; i++) {
                if (text.charCodeAt(i) === 10) newlineRows++;
            }
            if (newlineRows > THRESHOLD_ROWS) return true;
            // Visual rows: paragraph wrap without explicit newlines can still
            // fill many visible lines. Compare scrollHeight against a single
            // line's height to derive the true visible row count.
            if (inputEl && typeof inputEl.scrollHeight === 'number') {
                const cs = window.getComputedStyle(inputEl);
                const lh = parseFloat(cs.lineHeight) || 20;
                const padTop = parseFloat(cs.paddingTop) || 0;
                const padBot = parseFloat(cs.paddingBottom) || 0;
                const contentH = Math.max(0, inputEl.scrollHeight - padTop - padBot);
                const visualRows = Math.round(contentH / lh);
                if (visualRows > THRESHOLD_ROWS) return true;
            }
            return false;
        }

        function isOpenFor(sid) { return _isVisible(instances.get(sid)); }

        function onSourceInput(sid, inputEl, titleGetter, sendCb) {
            let inst = instances.get(sid);
            if (inst && inst.mirroring) return;
            // Keep mirror in sync while visible.
            if (inst && _isVisible(inst)) {
                inst.mirroring = true;
                try { inst.textareaEl.value = inputEl.value || ''; }
                finally { inst.mirroring = false; }
            }
            const long = _isLong(inputEl);
            if (!long) {
                if (inst) inst.userClosed = false;
                return;
            }
            if (inst && _isVisible(inst)) return;
            if (inst && inst.userClosed) return;
            if (!inst) inst = _build(sid);
            _show(inst, inputEl, titleGetter && titleGetter(), sendCb);
        }

        function closeFor(sid) { _hide(instances.get(sid)); }

        function destroyFor(sid) {
            const inst = instances.get(sid);
            if (!inst) return;
            try {
                if (inst.el && inst.el.parentNode) inst.el.parentNode.removeChild(inst.el);
            } catch (_) { /* ignore */ }
            instances.delete(sid);
        }

        window.addEventListener('resize', () => {
            for (const inst of instances.values()) {
                if (_isVisible(inst) && inst.pos) {
                    inst.pos = _clampBox(inst.pos);
                    _applyPos(inst.el, inst.pos);
                }
            }
        });

        return { onSourceInput, closeFor, destroyFor, isOpenFor };
    })();

    // ----- end multi-session state ---------------------------------------

    // Session straggler filter. gateGen() drops envelopes that arrive for
    // a session we've already closed (its session bucket is gone) or that
    // target a session_id we don't recognise. This protects a fresh tab
    // from a wedged old subtask whose blocking syscall prevents Python
    // from killing the OS thread on Windows.

    // Thinking bubble shown in chat while receptionist prepares a reply.
    // Per-session — looked up via _S().thinkingBubble during dispatch.

    // Module-level "current dispatch sid". Set at the top of each handq
    // listener (onStatus/onFinal/onError) so the bubble-appending helpers
    // can resolve to the right session's pane. Falls back to activeSid for
    // outbound paths (e.g. composer submit).
    let _dispatchSid = null;

    function _resolveSid(evt) {
        // Prefer envelope's session_id; bridge-meta envelopes (config, ltm,
        // cron) typically don't carry one — fall back to active session.
        if (evt && typeof evt.session_id === 'string' && evt.session_id) {
            return evt.session_id;
        }
        return activeSid;
    }

    function _dispatchSession() {
        // Returns the session bucket for the in-flight dispatch (or active).
        const sid = _dispatchSid || activeSid;
        return sessions.get(sid) || sessions.get(activeSid);
    }

    function _dispatchPane() {
        const s = _dispatchSession();
        return (s && s.pane) || conversation;
    }

    function gateGen(evt) {
        // Returns true if the event should be DROPPED.
        if (!evt) return true;
        if (evt.session_id && closedSessions.has(evt.session_id)) return true;
        const sid = _resolveSid(evt);
        if (sid && !sessions.has(sid)) return true;
        return false;
    }

    function truncate(s, n) {
        if (!s) return '';
        return s.length > n ? s.slice(0, n - 1) + '…' : s;
    }

    // Whether the planner/agent has a real task in flight, for the dispatch
    // session. Used to gate receptionist-side pill updates so a chat reply
    // mid-task can't reset that session's pill to "idle". `sessionState` is set
    // by state_changed events (V2 emits planning / thinking / executing /
    // idle); `activeExecCount` covers the brief window where a tool is running
    // before the next state_changed lands. Callers run inside _onStatusBody, so
    // _dispatchSession() resolves the session the in-flight event belongs to.
    function isTaskRunning() {
        const s = _dispatchSession();
        if (!s) return false;
        return s.sessionState === 'planning'
            || s.sessionState === 'thinking'
            || s.sessionState === 'executing'
            || s.activeExecCount > 0;
    }

    // ----- Markdown rendering ---------------------------------------------
    //
    // Inline parser — no external dep (CSP forbids cross-origin scripts and
    // we don't want to ship marked.js). Handles the subset LLM responses
    // typically use: headings, bold/italic/strike, inline + fenced code,
    // bulleted/ordered lists, blockquotes, links, hr.
    //
    // Re-runs on every streamed delta. Cost is O(n) per call which is fine
    // for the few-KB responses the chat surface receives. Streaming bubbles
    // debounce via requestAnimationFrame so the parser doesn't run more than
    // once per frame.

    const _MD_BLOCK = '';
    const _MD_INLINE = '';

    function escapeHtml(s) {
        return String(s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function renderMarkdownInline(s) {
        // Operates on already-HTML-escaped text. Inline code is captured
        // first so its content is shielded from emphasis processing.
        const codeSpans = [];
        s = s.replace(/`([^`\n]+)`/g, (_, p) => {
            codeSpans.push(p);
            return _MD_INLINE + (codeSpans.length - 1) + _MD_INLINE;
        });

        // Bold (**, __) before italic (*, _) so ** isn't half-eaten.
        s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
        s = s.replace(/__([^_\n]+)__/g, '<strong>$1</strong>');

        // Italic — single * or _ around content.
        s = s.replace(/(^|[^*\w])\*([^*\n]+)\*(?!\w)/g, '$1<em>$2</em>');
        s = s.replace(/(^|[^_\w])_([^_\n]+)_(?!\w)/g, '$1<em>$2</em>');

        // Strikethrough.
        s = s.replace(/~~([^~\n]+)~~/g, '<del>$1</del>');

        // Links — only http(s) and mailto allowed; anything else falls
        // through as plain text to avoid javascript: payloads.
        s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, text, url) => {
            if (/^(https?:\/\/|mailto:)/i.test(url)) {
                return '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + text + '</a>';
            }
            return m;
        });

        // Restore inline code spans.
        s = s.replace(new RegExp(_MD_INLINE + '(\\d+)' + _MD_INLINE, 'g'),
            (_, i) => '<code>' + codeSpans[+i] + '</code>');

        return s;
    }

    function renderMarkdown(md) {
        if (!md) return '';

        // Step 1 — extract fenced code blocks before HTML-escaping so ``` and
        // their content survive intact. Capture both closed and trailing-open
        // fences so a partially-streamed code block still renders as code.
        const codeBlocks = [];
        md = String(md).replace(/```([^\n`]*)\n([\s\S]*?)```/g, (_, lang, code) => {
            codeBlocks.push({ lang: lang.trim(), code: code });
            return _MD_BLOCK + 'C' + (codeBlocks.length - 1) + _MD_BLOCK;
        });
        md = md.replace(/```([^\n`]*)\n([\s\S]*)$/, (_, lang, code) => {
            codeBlocks.push({ lang: lang.trim(), code: code, partial: true });
            return _MD_BLOCK + 'C' + (codeBlocks.length - 1) + _MD_BLOCK;
        });

        // Step 2 — HTML-escape everything else.
        md = escapeHtml(md);

        const lines = md.split('\n');
        const out = [];
        let i = 0;

        const cbRe = new RegExp('^' + _MD_BLOCK + 'C(\\d+)' + _MD_BLOCK + '$');

        function emitCode(lang, code) {
            const langClass = lang ? ' class="lang-' + escapeHtml(lang) + '"' : '';
            return '<pre class="md-pre"><code' + langClass + '>' +
                   escapeHtml(code) + '</code></pre>';
        }

        function isBlockStart(line) {
            return /^(#{1,6}\s|[-*+]\s+|\d+\.\s+|&gt;\s|---+\s*$|\*\*\*+\s*$|\|)/.test(line)
                || cbRe.test(line);
        }

        while (i < lines.length) {
            const line = lines[i];

            // Code block placeholder.
            const cbMatch = line.match(cbRe);
            if (cbMatch) {
                const cb = codeBlocks[+cbMatch[1]];
                out.push(emitCode(cb.lang, cb.code));
                i++;
                continue;
            }

            // Heading (#–######).
            const hMatch = line.match(/^(#{1,6})\s+(.*)$/);
            if (hMatch) {
                const lvl = hMatch[1].length;
                out.push('<h' + lvl + ' class="md-h' + lvl + '">' +
                         renderMarkdownInline(hMatch[2]) + '</h' + lvl + '>');
                i++;
                continue;
            }

            // Horizontal rule.
            if (/^---+\s*$/.test(line) || /^\*\*\*+\s*$/.test(line)) {
                out.push('<hr class="md-hr">');
                i++;
                continue;
            }

            // Blockquote (consecutive `> ` lines, escaped to `&gt; `).
            if (/^&gt;\s?/.test(line)) {
                const bq = [];
                while (i < lines.length && /^&gt;\s?/.test(lines[i])) {
                    bq.push(lines[i].replace(/^&gt;\s?/, ''));
                    i++;
                }
                out.push('<blockquote class="md-bq">' +
                         renderMarkdownInline(bq.join('\n')).replace(/\n/g, '<br>') +
                         '</blockquote>');
                continue;
            }

            // Unordered list.
            if (/^[-*+]\s+/.test(line)) {
                const items = [];
                while (i < lines.length && /^[-*+]\s+/.test(lines[i])) {
                    items.push(lines[i].replace(/^[-*+]\s+/, ''));
                    i++;
                }
                out.push('<ul class="md-ul">' +
                         items.map((it) => '<li>' + renderMarkdownInline(it) + '</li>').join('') +
                         '</ul>');
                continue;
            }

            // Ordered list.
            if (/^\d+\.\s+/.test(line)) {
                const items = [];
                while (i < lines.length && /^\d+\.\s+/.test(lines[i])) {
                    items.push(lines[i].replace(/^\d+\.\s+/, ''));
                    i++;
                }
                out.push('<ol class="md-ol">' +
                         items.map((it) => '<li>' + renderMarkdownInline(it) + '</li>').join('') +
                         '</ol>');
                continue;
            }

            // Table — pipe-delimited GFM style.
            if (line.indexOf('|') >= 0 && i + 1 < lines.length &&
                /^\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/.test(lines[i + 1])) {
                var headerCells = line.split('|').map(function (c) { return c.trim(); })
                    .filter(function (c, idx, arr) {
                        return !(idx === 0 && c === '') && !(idx === arr.length - 1 && c === '');
                    });
                i += 2; // skip header + separator
                var bodyRows = [];
                while (i < lines.length && lines[i].indexOf('|') >= 0 && lines[i].trim() !== '') {
                    var cells = lines[i].split('|').map(function (c) { return c.trim(); })
                        .filter(function (c, idx, arr) {
                            return !(idx === 0 && c === '') && !(idx === arr.length - 1 && c === '');
                        });
                    bodyRows.push(cells);
                    i++;
                }
                var tableHtml = '<table class="md-table"><thead><tr>' +
                    headerCells.map(function (h) { return '<th>' + renderMarkdownInline(h) + '</th>'; }).join('') +
                    '</tr></thead><tbody>' +
                    bodyRows.map(function (row) {
                        return '<tr>' + row.map(function (c) { return '<td>' + renderMarkdownInline(c) + '</td>'; }).join('') + '</tr>';
                    }).join('') +
                    '</tbody></table>';
                out.push(tableHtml);
                continue;
            }

            // Empty line — paragraph break.
            if (!line.trim()) { i++; continue; }

            // Paragraph — collect contiguous non-empty lines until blank or
            // a block-level construct begins.
            const para = [line];
            i++;
            while (i < lines.length && lines[i].trim() && !isBlockStart(lines[i])) {
                para.push(lines[i]);
                i++;
            }
            out.push('<p class="md-p">' +
                     renderMarkdownInline(para.join('\n')).replace(/\n/g, '<br>') +
                     '</p>');
        }

        return out.join('');
    }

    // ----- Per-session activity groups ------------------------------------
    //
    // The legacy global activity strip + popover have been removed (multi-
    // session model — see plan #11/#12). Each session's pane now hosts its
    // own activity content as collapsible `<details>` groups interleaved
    // between chat bubbles. A group accumulates events (decision_made,
    // tool_execution_started, tool result, planner step, network event...)
    // since the last chat bubble; the next chat bubble seals the group and
    // a fresh one opens on the next activity event. Sealed groups stay in
    // the DOM as collapsed history.
    //
    // The session bucket holds the events as JS objects (`s.activityItems`)
    // so a session's activity persists across tab switches.

    const ACTIVITY_TRUNC = 2000;

    // Per-session status pill (UI1). The dispatch session is resolved from
    // `_dispatchSid` (set while handling each inbound event) so each card
    // reflects its own backend state; outbound paths fall back to active.
    function setPill(text) {
        const s = _dispatchSession();
        if (!s) return;
        s.pillText = text || 'idle';
        if (s.pillEl) {
            s.pillEl.textContent = text || 'idle';
            s.pillEl.title = text || '';
        }
    }
    function setWorking(text) {
        const s = _dispatchSession();
        if (!s) return;
        s.pillText = text || 'working…';
        if (s.pillEl) {
            s.pillEl.textContent = text || 'working…';
            s.pillEl.title = text || '';
            s.pillEl.classList.add('working');
        }
    }
    function clearWorking() {
        const s = _dispatchSession();
        if (!s) return;
        if (s.pillEl) s.pillEl.classList.remove('working');
    }

    // ── Activity group rendering inside per-session panes ───────────────

    function _getOrCreateActivityGroup(pane) {
        if (!pane) return null;
        if (pane._activeActivityGroup) return pane._activeActivityGroup;
        const group = document.createElement('details');
        group.className = 'activity-group';
        group.open = false;
        const summary = document.createElement('summary');
        summary.className = 'activity-group-summary';
        const sCount = document.createElement('span');
        sCount.className = 'ai-group-count';
        sCount.textContent = 'Activity (0)';
        summary.appendChild(sCount);
        group.appendChild(summary);
        const itemsContainer = document.createElement('div');
        itemsContainer.className = 'activity-group-items';
        group.appendChild(itemsContainer);
        group._itemsContainer = itemsContainer;
        group._count = 0;
        group._countLabel = sCount;
        pane.appendChild(group);
        pane._activeActivityGroup = group;
        return group;
    }

    function _sealActivityGroup(pane) {
        if (pane) pane._activeActivityGroup = null;
    }

    function _renderActivityItem(entry) {
        const item = el('div', 'activity-item');
        const head = el('div', 'ai-head');
        head.appendChild(el('span', 'ai-icon', entry.icon));
        head.appendChild(el('span', 'ai-label', entry.label));
        head.appendChild(el('span', 'ai-time', entry.time));
        item.appendChild(head);
        if (entry.content) {
            const contentIsJson = isJsonString(entry.content);
            const content = el('span', 'ai-content' + (contentIsJson ? ' ai-json' : ''));
            if (contentIsJson) content.appendChild(renderJsonContent(entry.content));
            else content.textContent = truncate(entry.content, ACTIVITY_TRUNC);
            item.appendChild(content);
            item.title = contentIsJson ? '' : entry.content;
        }
        if (entry.resultContent) {
            _appendActivityResult(item, entry);
        }
        item.addEventListener('click', (ev) => {
            // Don't toggle expansion when the click bubbled up from the
            // outer <details> summary — let the summary handle its own toggle.
            ev.stopPropagation();
            item.classList.toggle('expanded');
            const c = item.querySelector('.ai-content');
            if (c && !c.classList.contains('ai-json')) {
                c.textContent = item.classList.contains('expanded')
                    ? entry.content
                    : truncate(entry.content, ACTIVITY_TRUNC);
            }
            const r = item.querySelector('.ai-result');
            if (r && !r.classList.contains('ai-json')) {
                r.textContent = '↳ ' + (item.classList.contains('expanded')
                    ? entry.resultContent
                    : truncate(entry.resultContent, ACTIVITY_TRUNC));
            }
        });
        return item;
    }

    function _appendActivityResult(item, entry) {
        const resultIsJson = isJsonString(entry.resultContent);
        const resultEl = el('div', 'ai-result' + (resultIsJson ? ' ai-json' : ''));
        if (resultIsJson) {
            resultEl.appendChild(document.createTextNode('↳ '));
            resultEl.appendChild(renderJsonContent(entry.resultContent));
        } else {
            resultEl.textContent = '↳ ' + truncate(entry.resultContent, ACTIVITY_TRUNC);
        }
        item.appendChild(resultEl);
    }

    function pushActivity(icon, label, content, opts) {
        // Append an activity entry to the current dispatch session's
        // pane. The entry lands inside the session's "open" activity
        // group; if no group is open, one is created at the current
        // bottom of the pane (which is where it semantically belongs —
        // events that happen between two chat bubbles get grouped
        // together visually).
        const pane = _dispatchPane();
        const s = _dispatchSession();
        if (!pane || !s) return null;
        const time = new Date().toLocaleTimeString([], { hour12: false });
        const entry = {
            icon: icon || '·',
            label: label || '',
            content: content == null ? '' : String(content),
            time: time,
            iter: opts && opts.iter != null ? String(opts.iter) : null,
            tool: opts && opts.tool ? String(opts.tool) : null,
            pending: !!(opts && opts.pending),
            resultIcon: null,
            resultContent: null,
            _el: null,
        };
        s.activityItems.push(entry);
        const group = _getOrCreateActivityGroup(pane);
        const itemEl = _renderActivityItem(entry);
        entry._el = itemEl;
        group._itemsContainer.appendChild(itemEl);
        group._count += 1;
        group._countLabel.textContent = 'Activity (' + group._count + ')';
        scrollToBottom();
        return entry;
    }

    function updateActivityResult(iter, tool, resultIcon, headLabel, resultContent) {
        // Fold a tool's post-execution result into its matching pre-event
        // entry instead of pushing a separate "done" line. We scan THIS
        // session's items (newest first) for a pending entry with
        // matching (iter, tool).
        const s = _dispatchSession();
        if (!s) return;
        const iterStr = iter == null ? null : String(iter);
        let match = null;
        for (let i = s.activityItems.length - 1; i >= 0; i--) {
            const e = s.activityItems[i];
            if (!e.pending) continue;
            if (iterStr != null && e.iter !== iterStr) continue;
            if (tool && e.tool && e.tool !== tool) continue;
            match = e;
            break;
        }
        if (!match) {
            pushActivity(resultIcon || '✓',
                         (tool || 'tool') + ' done',
                         resultContent || '');
            return;
        }
        match.icon = resultIcon || '✓';
        match.label = headLabel || match.tool || match.label;
        match.resultIcon = resultIcon || '✓';
        match.resultContent = resultContent == null ? '' : String(resultContent);
        match.pending = false;
        if (match._el) {
            const head = match._el.querySelector('.ai-head');
            if (head) {
                const icon = head.querySelector('.ai-icon');
                const label = head.querySelector('.ai-label');
                if (icon)  icon.textContent  = match.icon;
                if (label) label.textContent = match.label;
            }
            if (!match._el.querySelector('.ai-result')) {
                _appendActivityResult(match._el, match);
            }
        }
    }

    function clearActivity() {
        // Reset this session's activity history. Called from scNew (the
        // "reset current tab" shortcut). The pane's bubbles have already
        // been cleared by the caller via innerHTML = ''; here we just
        // drop the in-memory ring + clear the active-group pointer.
        const s = _dispatchSession() || active();
        if (!s) return;
        s.activityItems = [];
        if (s.pane) s.pane._activeActivityGroup = null;
    }

    function briefToolContext(tool, params) {
        if (!params) return '';
        var obj = params;
        if (typeof obj === 'string') {
            if (obj === 'None' || obj === 'null') return '';
            try { obj = JSON.parse(obj); } catch (_) { return truncate(obj, 2000); }
        }
        if (typeof obj !== 'object' || obj === null) return truncate(String(params), 2000);
        var key = (tool === 'browser') ? 'url' :
                  (tool === 'bash' || tool === 'shell') ? 'command' :
                  (tool === 'write' || tool === 'edit' || tool === 'read') ? 'path' :
                  Object.keys(obj)[0] || '';
        var val = key ? String(obj[key] || '') : '';
        return truncate(val, 2000);
    }

    function formatResultReadable(tool, output) {
        if (!output || output === 'None' || output === 'null') return '';
        var obj = output;
        if (typeof obj === 'string') {
            try { obj = JSON.parse(obj); } catch (_) {
                return truncate(stripAnsi(obj).replace(/\s+/g, ' ').trim(), 2000);
            }
        }
        if (typeof obj !== 'object' || obj === null) {
            return truncate(stripAnsi(String(output)).replace(/\s+/g, ' ').trim(), 2000);
        }
        // For bash/shell results: show stdout (cleaned) or stderr if failed
        if ('stdout' in obj || 'exit_code' in obj || 'returncode' in obj) {
            var code = obj.exit_code || obj.returncode || '0';
            var out = stripAnsi(String(obj.stdout || '')).replace(/\s+/g, ' ').trim();
            var err = stripAnsi(String(obj.stderr || '')).replace(/\s+/g, ' ').trim();
            if (String(code) !== '0' && err) {
                return '✗ ' + truncate(err, 2000);
            }
            if (out) return truncate(out, 2000);
            if (err) return truncate(err, 2000);
            return code === '0' || code === 0 ? 'done' : '✗ exit ' + code;
        }
        // For common tools, pick the most informative field
        if (obj.output) return truncate(stripAnsi(String(obj.output)).replace(/\s+/g, ' ').trim(), 2000);
        if (obj.result) return truncate(String(obj.result).replace(/\s+/g, ' ').trim(), 2000);
        if (obj.content) return truncate(String(obj.content).replace(/\s+/g, ' ').trim(), 2000);
        if (obj.text) return truncate(String(obj.text).replace(/\s+/g, ' ').trim(), 2000);
        if (obj.status) return String(obj.status);
        // Fallback: show first few key=value pairs, skipping noise
        var skipKeys = new Set(['cwd_used', 'shell', 'venv', 'cwd', 'truncated']);
        var parts = [];
        var keys = Object.keys(obj);
        for (var ki = 0; ki < keys.length && parts.length < 3; ki++) {
            var k = keys[ki];
            if (skipKeys.has(k)) continue;
            var v = obj[k];
            if (v === 'None' || v === null || v === '' || v === 'null') continue;
            parts.push(k + ': ' + truncate(stripAnsi(String(v)), 500));
        }
        return parts.join(' | ') || 'done';
    }

    function stripAnsi(s) {
        return s.replace(/\x1b\[[0-9;]*m/g, '');
    }

    function setActivityState(text) {
        // Convenience wrapper — same lock semantics as setPill (preserves the
        // green "complete" message). Used by status events that aren't full
        // activity entries (state_changed, progress, thinking).
        setPill(text);
    }

    function formatToolParams(params) {
        if (params === undefined || params === null) return '';
        if (typeof params === 'string') return params;
        try { return JSON.stringify(params, null, 2); }
        catch (_) { return String(params); }
    }

    function isJsonString(s) {
        if (!s || typeof s !== 'string') return false;
        const trimmed = s.trim();
        if ((trimmed[0] === '{' && trimmed[trimmed.length - 1] === '}') ||
            (trimmed[0] === '[' && trimmed[trimmed.length - 1] === ']')) {
            try { JSON.parse(trimmed); return true; }
            catch (_) { return false; }
        }
        return false;
    }

    function renderJsonValue(value) {
        if (value === null) return el('span', 'ai-json-null', 'null');
        if (typeof value === 'boolean') return el('span', 'ai-json-bool', String(value));
        if (typeof value === 'number') return el('span', 'ai-json-num', String(value));
        if (typeof value === 'string') {
            const display = value.length > 120 ? value.slice(0, 117) + '…' : value;
            return el('span', 'ai-json-str', '"' + display + '"');
        }
        if (Array.isArray(value)) {
            if (value.length === 0) return el('span', 'ai-json-bracket', '[]');
            const ul = el('ul', 'ai-json-tree');
            for (let i = 0; i < value.length; i++) {
                const li = el('li', 'ai-json-entry');
                const idx = el('span', 'ai-json-key', '[' + i + '] ');
                li.appendChild(idx);
                li.appendChild(renderJsonValue(value[i]));
                ul.appendChild(li);
            }
            return ul;
        }
        if (typeof value === 'object') {
            const keys = Object.keys(value);
            if (keys.length === 0) return el('span', 'ai-json-bracket', '{}');
            const ul = el('ul', 'ai-json-tree');
            for (const k of keys) {
                const li = el('li', 'ai-json-entry');
                const keySpan = el('span', 'ai-json-key', k + ': ');
                li.appendChild(keySpan);
                li.appendChild(renderJsonValue(value[k]));
                ul.appendChild(li);
            }
            return ul;
        }
        return document.createTextNode(String(value));
    }

    function renderJsonContent(jsonStr) {
        try {
            const parsed = JSON.parse(jsonStr.trim());
            return renderJsonValue(parsed);
        } catch (_) {
            return document.createTextNode(jsonStr);
        }
    }

    // ----- chat state ------------------------------------------------------

    function el(tag, className, textContent) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (textContent !== undefined) node.textContent = textContent;
        return node;
    }

    function scrollToBottom() {
        // Scroll the dispatch session's own card body (each card scrolls
        // independently in the tiled layout).
        const p = _dispatchPane();
        if (p) p.scrollTop = p.scrollHeight;
    }

    function addUserBubble(text) {
        const bubble = el('div', 'bubble user');
        bubble.appendChild(el('div', 'bubble-body', text));
        const pane = _dispatchPane();
        _sealActivityGroup(pane);
        pane.appendChild(bubble);
        scrollToBottom();
    }

    function scheduleMarkdownRender(span) {
        if (span._renderPending) return;
        span._renderPending = true;
        requestAnimationFrame(() => {
            span._renderPending = false;
            try {
                span.innerHTML = renderMarkdown(span._rawText || '');
            } catch (err) {
                // Fallback to plain text if the parser ever throws — never
                // strand a streaming bubble blank.
                span.textContent = span._rawText || '';
            }
        });
    }

    function addAssistantTextBubble(text) {
        // Single-shot non-streaming assistant message (e.g. receptionist reply,
        // task completion summary). Markdown-render the body too.
        const bubble = el('div', 'bubble assistant');
        const body = el('div', 'bubble-body');
        const span = el('div', 'text-stream md-rendered');
        try { span.innerHTML = renderMarkdown(text || ''); }
        catch (_) { span.textContent = text || ''; }
        body.appendChild(span);
        bubble.appendChild(body);
        const pane = _dispatchPane();
        _sealActivityGroup(pane);
        pane.appendChild(bubble);
        scrollToBottom();
    }

    // Streaming receptionist reply — incremental markdown render per delta.
    // The "currently streaming" bubble is tracked PER SESSION via the
    // session bucket's `activeReceptionistBubble` field; this allows two
    // concurrent sessions to each have their own streaming bubble in-flight.

    function appendReceptionistDelta(text) {
        if (!text) return;
        const s = _dispatchSession();
        if (!s) return;
        if (!s.activeReceptionistBubble) {
            s.activeReceptionistBubble = el('div', 'bubble assistant streaming');
            const body = el('div', 'bubble-body');
            s.activeReceptionistBubble.appendChild(body);
            s.activeReceptionistBubble._body = body;
            s.activeReceptionistBubble._currentTextSpan = null;
            const pane = s.pane || conversation;
            // New assistant turn — seal any open activity group so it
            // settles above the upcoming streaming bubble.
            _sealActivityGroup(pane);
            pane.appendChild(s.activeReceptionistBubble);
        }
        var span = s.activeReceptionistBubble._currentTextSpan;
        if (!span) {
            span = el('div', 'text-stream md-rendered');
            span._rawText = '';
            s.activeReceptionistBubble._body.appendChild(span);
            s.activeReceptionistBubble._currentTextSpan = span;
        }
        span._rawText += text;
        scheduleMarkdownRender(span);
        scrollToBottom();
    }

    function sealReceptionistBubble() {
        const s = _dispatchSession();
        if (!s || !s.activeReceptionistBubble) return;
        s.activeReceptionistBubble.classList.remove('streaming');
        s.activeReceptionistBubble.classList.add('complete');
        if (s.activeReceptionistBubble._currentTextSpan) {
            var span = s.activeReceptionistBubble._currentTextSpan;
            try { span.innerHTML = renderMarkdown(span._rawText || ''); }
            catch (_) { span.textContent = span._rawText || ''; }
        }
        s.activeReceptionistBubble = null;
    }

    function showThinkingBubble() {
        const s = _dispatchSession();
        if (!s || s.thinkingBubble) return;
        s.thinkingBubble = el('div', 'bubble assistant thinking-indicator');
        var body = el('div', 'bubble-body');
        var dots = el('span', 'thinking-dots');
        dots.appendChild(el('span', 'dot'));
        dots.appendChild(el('span', 'dot'));
        dots.appendChild(el('span', 'dot'));
        body.appendChild(dots);
        s.thinkingBubble.appendChild(body);
        const pane = s.pane || conversation;
        _sealActivityGroup(pane);
        pane.appendChild(s.thinkingBubble);
        scrollToBottom();
    }

    function removeThinkingBubble() {
        const s = _dispatchSession();
        if (!s || !s.thinkingBubble) return;
        if (s.thinkingBubble.parentNode) {
            s.thinkingBubble.parentNode.removeChild(s.thinkingBubble);
        }
        s.thinkingBubble = null;
    }

    function addSystemBubble(text) {
        const bubble = el('div', 'bubble system');
        bubble.appendChild(el('div', 'bubble-body', text || ''));
        const pane = _dispatchPane();
        _sealActivityGroup(pane);
        pane.appendChild(bubble);
        scrollToBottom();
    }

    function addGlobalSystemBubble(text) {
        // Global events (network, llm_fallback) affect all sessions — render
        // in every mounted session pane so the user sees it regardless of
        // which tab is active. Without this, the bubble only appears in
        // activeSid and background sessions have no visibility.
        for (const [, s] of sessions) {
            const bubble = el('div', 'bubble system global-notice');
            bubble.appendChild(el('div', 'bubble-body', text || ''));
            _sealActivityGroup(s.pane);
            s.pane.appendChild(bubble);
        }
        scrollToBottom();
    }

    function addStepBubble(icon, desc) {
        // Step events (planner inline_event) are activity-class; route them
        // into the current session's activity group instead of producing
        // standalone bubbles. Keeps the conversation thread focused on
        // user/assistant messages while letting the user expand activity to
        // see step traces.
        pushActivity(icon || '·', String(desc || ''), '');
    }

    function addErrorBubble(message, where) {
        const bubble = el('div', 'bubble error');
        const prefix = where ? '[' + where + '] ' : '';
        bubble.appendChild(el('div', 'bubble-body', prefix + (message || '(no message)')));
        const pane = _dispatchPane();
        _sealActivityGroup(pane);
        pane.appendChild(bubble);
        scrollToBottom();
    }

    // ----- confirmation modal ---------------------------------------------
    //
    // Surfaces three envelope kinds emitted by _StdioUI in stdio_bridge.py:
    //   - risk_confirmation : high-risk operation gate
    //   - tool_confirmation : tool-specific gate (write/edit/bash/...)
    //   - secret_input      : hidden-text input (e.g. SSH password)
    //
    // Replies travel back via:
    //   handq.sendRequest({type:"user_input", kind:"confirmation",
    //                      answer: <"yes" | "no" | <free-text>>})
    // which the bridge dispatcher routes to InteractionManager.
    //
    // Free-text answers are interpreted by IM._resolve_confirmation: any
    // string other than yes/y/no/n becomes UC.with_message(text). The
    // engine then either treats it as user_message (tool path) or
    // injects it as risk_guidance (risk path).

    // UI3 — per-session confirmations. Each session renders its own prompt
    // INLINE inside its card (above its composer), so a popup in session B
    // never blocks or steals focus from session A. The pending prompt id is
    // tracked per session (s.pendingConfirm) and the answer is always stamped
    // with that session's own sid — never the globally-focused tab.

    function _renderDecisionInto(elNode, decision) {
        // decision: { tool_calls: [{tool_name, params: {...}}], reasoning }
        elNode.textContent = '';
        if (!decision) { elNode.classList.add('hidden'); return; }
        const calls = Array.isArray(decision.tool_calls) ? decision.tool_calls : [];
        if (calls.length === 0 && !decision.reasoning) {
            elNode.classList.add('hidden');
            return;
        }
        const lines = [];
        for (const tc of calls) {
            const params = tc && tc.params || {};
            const paramStr = Object.entries(params)
                .map(([k, v]) => '  ' + k + ': ' + String(v))
                .join('\n');
            lines.push('▸ ' + (tc.tool_name || 'unknown'));
            if (paramStr) lines.push(paramStr);
        }
        if (decision.reasoning) {
            lines.push('');
            lines.push('reasoning: ' + decision.reasoning);
        }
        elNode.textContent = lines.join('\n');
        elNode.classList.remove('hidden');
    }

    // Build the inline confirmation controls inside a session's confirm host
    // exactly once; wire the buttons to that session's sid. Returns the refs.
    function _ensureConfirmUI(s) {
        if (s.confirmUI) return s.confirmUI;
        const host = s.confirmEl;
        const card = el('div', 'session-confirm-card');
        const titleEl = el('div', 'scc-title');
        const descEl = el('div', 'scc-desc');
        const decisionEl = el('pre', 'scc-decision hidden');
        const secretWrap = el('label', 'scc-secret-wrap hidden');
        const secretLabel = el('span', 'scc-secret-label', 'Value:');
        const secretIn = document.createElement('input');
        secretIn.type = 'password';
        secretIn.className = 'scc-secret';
        secretWrap.appendChild(secretLabel);
        secretWrap.appendChild(secretIn);
        const guidanceEl = document.createElement('textarea');
        guidanceEl.className = 'scc-guidance hidden';
        guidanceEl.rows = 2;
        guidanceEl.placeholder = 'Optional guidance for the agent…';
        const actions = el('div', 'scc-actions');
        const rejectBtn = el('button', 'scc-reject', 'Reject');
        rejectBtn.type = 'button';
        const guidBtn = el('button', 'scc-guid', 'Cancel guidance');
        guidBtn.type = 'button';
        const submitBtn = el('button', 'scc-submit primary', 'Approve');
        submitBtn.type = 'button';
        actions.appendChild(rejectBtn);
        actions.appendChild(guidBtn);
        actions.appendChild(submitBtn);
        card.appendChild(titleEl);
        card.appendChild(descEl);
        card.appendChild(decisionEl);
        card.appendChild(secretWrap);
        card.appendChild(guidanceEl);
        card.appendChild(actions);
        host.appendChild(card);

        const sid = s.sid;
        submitBtn.addEventListener('click', () => {
            const kind = s.pendingConfirm && s.pendingConfirm.kind;
            if (kind === 'secret_input' || kind === 'ask_human') {
                sendConfirmationAnswer(sid, secretIn.value || '');
            } else if (!guidanceEl.classList.contains('hidden')) {
                const text = (guidanceEl.value || '').trim();
                sendConfirmationAnswer(sid, text || 'yes');
            } else {
                sendConfirmationAnswer(sid, 'yes');
            }
        });
        rejectBtn.addEventListener('click', () => sendConfirmationAnswer(sid, 'no'));
        guidBtn.addEventListener('click', () => {
            if (guidanceEl.classList.contains('hidden')) {
                guidanceEl.classList.remove('hidden');
                guidBtn.textContent = 'Cancel guidance';
                try { guidanceEl.focus(); } catch (_) { /* ignore */ }
            } else {
                guidanceEl.classList.add('hidden');
                guidanceEl.value = '';
                guidBtn.textContent = 'Provide guidance';
            }
        });
        secretIn.addEventListener('keydown', (e) => {
            const kind = s.pendingConfirm && s.pendingConfirm.kind;
            if (e.key === 'Enter' && (kind === 'secret_input' || kind === 'ask_human')) {
                e.preventDefault();
                sendConfirmationAnswer(sid, secretIn.value || '');
            }
        });

        s.confirmUI = {
            card, titleEl, descEl, decisionEl,
            secretWrap, secretIn, guidanceEl,
            rejectBtn, guidBtn, submitBtn,
        };
        return s.confirmUI;
    }

    function showConfirmationModal(evt) {
        const sid = _resolveSid(evt);
        const s = sessions.get(sid);
        if (!s) {
            window.__handqLog('ERROR', 'confirmation for unknown session',
                { sid, id: evt && evt.id });
            return;
        }
        const ui = _ensureConfirmUI(s);
        s.pendingConfirm = { id: String(evt.id || ''), kind: String(evt.kind || '') };

        ui.card.classList.remove('desktop-takeover');

        if (evt.kind === 'secret_input') {
            ui.titleEl.textContent = 'Input required';
            ui.descEl.textContent = String(evt.prompt || 'Enter value:');
            _renderDecisionInto(ui.decisionEl, null);
            ui.secretWrap.classList.remove('hidden');
            ui.secretIn.value = '';
            try { ui.secretIn.type = 'password'; } catch (_) { /* ignore */ }
            ui.guidanceEl.classList.add('hidden');
            ui.guidanceEl.value = '';
            ui.rejectBtn.classList.add('hidden');
            ui.guidBtn.classList.add('hidden');
            ui.submitBtn.textContent = 'Submit';
        } else if (evt.kind === 'ask_human') {
            ui.titleEl.textContent = 'Question from agent';
            ui.descEl.textContent = String(evt.prompt || 'The agent has a question:');
            _renderDecisionInto(ui.decisionEl, null);
            ui.secretWrap.classList.remove('hidden');
            ui.secretIn.value = '';
            try { ui.secretIn.type = 'text'; } catch (_) { /* ignore */ }
            ui.guidanceEl.classList.add('hidden');
            ui.guidanceEl.value = '';
            ui.rejectBtn.classList.add('hidden');
            ui.guidBtn.classList.add('hidden');
            ui.submitBtn.textContent = 'Send';
        } else {
            const isRisk = evt.kind === 'risk_confirmation';
            const isDesktopTakeover = evt.scope === 'task' && evt.tool === 'desktop';
            // evt.title / evt.approve_label are optional per-envelope overrides
            // populated by the bridge (see stdio_bridge.request_risk_confirmation).
            // They let a caller reframe the generic Approve/Reject modal — e.g.
            // browser.request_user_login shows "Login required" with an
            // "I've logged in" button so users don't read the flow as
            // "grant credentials to HandQ".
            const customTitle = evt.title ? String(evt.title) : '';
            const customApprove = evt.approve_label ? String(evt.approve_label) : '';
            ui.titleEl.textContent = customTitle || (isRisk
                ? 'High-risk operation'
                : (isDesktopTakeover
                    ? 'Grant desktop control for this task?'
                    : 'Confirm ' + (evt.tool || 'tool') + ' execution'));
            const description = evt.description ? String(evt.description) : '';
            if (isRisk || description) {
                ui.descEl.textContent = description;
            } else {
                ui.descEl.textContent =
                    'The agent wants to run "' + (evt.tool || 'tool') +
                    '" with the parameters below.';
            }
            _renderDecisionInto(ui.decisionEl, evt.decision);
            ui.secretWrap.classList.add('hidden');
            ui.secretIn.value = '';
            ui.guidanceEl.classList.remove('hidden');
            ui.guidanceEl.value = '';
            ui.rejectBtn.classList.remove('hidden');
            ui.guidBtn.classList.remove('hidden');
            ui.guidBtn.textContent = 'Cancel guidance';
            ui.submitBtn.textContent = customApprove
                || (isDesktopTakeover ? 'Approve task-wide' : 'Approve');
            if (isDesktopTakeover) ui.card.classList.add('desktop-takeover');
        }

        s.confirmEl.classList.remove('hidden');
        // Mark unread if this isn't the focused card so the user notices.
        if (sid !== activeSid) {
            s.unread = true;
        }
        if (evt.kind === 'secret_input' || evt.kind === 'ask_human') {
            try { ui.secretIn.focus(); } catch (_) { /* ignore */ }
        }
        try { s.confirmEl.scrollIntoView({ block: 'nearest' }); } catch (_) { /* ignore */ }
    }

    function sendConfirmationAnswer(sid, answer) {
        const s = sessions.get(sid);
        if (!s || !s.pendingConfirm) return;
        const promptId = s.pendingConfirm.id;
        try {
            handq.sendRequest({
                type: 'user_input',
                kind: 'confirmation',
                session_id: sid,
                id: promptId,
                answer: String(answer || ''),
            });
        } catch (e) {
            window.__handqLog('ERROR', 'confirm send failed',
                { sid, id: promptId, error: String(e) });
        }
        s.pendingConfirm = null;
        if (s.confirmEl) s.confirmEl.classList.add('hidden');
    }

    // ----- bridge events ---------------------------------------------------

    // ── Interactive Session Terminal Panel (xterm.js) ───────────────────────────
    //
    // Displays a real-time terminal for each interactive session. Uses xterm.js
    // to render ANSI output, with tabs for multiple concurrent sessions.

    const _sessionTerminals = new Map(); // session_id → {terminal, fitAddon, command, container, tab}
    let _terminalPanelEl = null;
    let _terminalHeaderEl = null;
    let _terminalTabsEl = null;
    let _terminalBodyEl = null;
    let _activeSessionId = null;
    let _terminalMinimized = false;

    function ensureTerminalPanel() {
        if (_terminalPanelEl) return;
        _terminalPanelEl = document.createElement('div');
        _terminalPanelEl.className = 'terminal-panel hidden';

        // Header (draggable)
        _terminalHeaderEl = document.createElement('div');
        _terminalHeaderEl.className = 'terminal-panel-header';

        _terminalTabsEl = document.createElement('div');
        _terminalTabsEl.className = 'terminal-tabs';

        const controls = document.createElement('div');
        controls.className = 'terminal-panel-controls';

        const btnMin = document.createElement('button');
        btnMin.type = 'button';
        btnMin.title = 'Minimize';
        btnMin.textContent = '―';
        btnMin.addEventListener('click', toggleTerminalMinimize);

        const btnHide = document.createElement('button');
        btnHide.type = 'button';
        btnHide.className = 'terminal-btn-close';
        btnHide.title = 'Close';
        btnHide.textContent = '×';
        btnHide.addEventListener('click', hideTerminalPanel);

        controls.appendChild(btnMin);
        controls.appendChild(btnHide);
        _terminalHeaderEl.appendChild(_terminalTabsEl);
        _terminalHeaderEl.appendChild(controls);

        // Body
        _terminalBodyEl = document.createElement('div');
        _terminalBodyEl.className = 'terminal-panel-body';

        // Resize handle
        const resizeHandle = document.createElement('div');
        resizeHandle.className = 'terminal-resize-handle';

        _terminalPanelEl.appendChild(_terminalHeaderEl);
        _terminalPanelEl.appendChild(_terminalBodyEl);
        _terminalPanelEl.appendChild(resizeHandle);
        document.body.appendChild(_terminalPanelEl);

        // Re-fit active terminal on resize
        const ro = new ResizeObserver(() => {
            const entry = _sessionTerminals.get(_activeSessionId);
            if (entry) requestAnimationFrame(() => entry.fitAddon.fit());
        });
        ro.observe(_terminalBodyEl);

        // Drag-to-move via header
        initPanelDrag(_terminalHeaderEl, _terminalPanelEl);
        // Drag-to-resize via handle
        initPanelResize(resizeHandle, _terminalPanelEl, _terminalBodyEl);
    }

    function createSessionTerminal(sessionId, command, description) {
        ensureTerminalPanel();
        _terminalPanelEl.classList.remove('hidden');
        if (_terminalMinimized) {
            _terminalMinimized = false;
            _terminalPanelEl.classList.remove('minimized');
        }

        const container = document.createElement('div');
        container.className = 'terminal-container';
        container.dataset.session = sessionId;
        _terminalBodyEl.appendChild(container);

        const terminal = new window.XTermLib.Terminal({
            fontSize: 15,
            fontFamily: '"SF Mono", ui-monospace, Menlo, Monaco, "Cascadia Mono", Consolas, "Liberation Mono", monospace',
            theme: {
                background: '#1e1e2e',
                foreground: '#cdd6f4',
                cursor: '#89b4fa',
                selectionBackground: '#45475a',
                black: '#45475a',
                red: '#f38ba8',
                green: '#a6e3a1',
                yellow: '#f9e2af',
                blue: '#89b4fa',
                magenta: '#cba6f7',
                cyan: '#94e2d5',
                white: '#bac2de',
            },
            scrollback: 5000,
            convertEol: true,
            cursorBlink: false,
            cursorStyle: 'underline',
            disableStdin: true,
            allowProposedApi: true,
        });
        const fitAddon = new window.XTermLib.FitAddon();
        terminal.loadAddon(fitAddon);
        terminal.open(container);

        // Tab button
        const tab = document.createElement('button');
        tab.type = 'button';
        tab.className = 'terminal-tab';
        tab.dataset.session = sessionId;
        const dot = document.createElement('span');
        dot.className = 'tab-dot';
        tab.appendChild(dot);
        const label = document.createTextNode(
            truncate(description || command || sessionId, 20)
        );
        tab.appendChild(label);
        const tabClose = document.createElement('span');
        tabClose.className = 'tab-close';
        tabClose.textContent = '×';
        tabClose.addEventListener('click', (e) => {
            e.stopPropagation();
            removeSessionTerminal(sessionId);
        });
        tab.appendChild(tabClose);
        tab.addEventListener('click', () => switchToSession(sessionId));
        _terminalTabsEl.appendChild(tab);

        _sessionTerminals.set(sessionId, {
            terminal, fitAddon, command, container, tab,
        });

        switchToSession(sessionId);
    }

    function destroySessionTerminal(sessionId) {
        removeSessionTerminal(sessionId);
    }

    function switchToSession(sessionId) {
        _activeSessionId = sessionId;
        for (const [id, entry] of _sessionTerminals) {
            const isActive = id === sessionId;
            entry.container.classList.toggle('active', isActive);
            entry.tab.classList.toggle('active', isActive);
        }
        const entry = _sessionTerminals.get(sessionId);
        if (entry) {
            requestAnimationFrame(() => entry.fitAddon.fit());
        }
    }

    function writeSessionData(sessionId, text) {
        const entry = _sessionTerminals.get(sessionId);
        if (entry) entry.terminal.write(text);
    }

    function writeSessionInput(sessionId, text) {
        const entry = _sessionTerminals.get(sessionId);
        if (!entry) return;
        entry.terminal.write('\x1b[32m$ ' + text + '\x1b[0m\r\n');
    }

    function toggleTerminalMinimize() {
        _terminalMinimized = !_terminalMinimized;
        _terminalPanelEl.classList.toggle('minimized', _terminalMinimized);
        if (_terminalMinimized) {
            _terminalPanelEl._savedTop = _terminalPanelEl.style.top;
            _terminalPanelEl._savedLeft = _terminalPanelEl.style.left;
            _terminalPanelEl._savedWidth = _terminalPanelEl.style.width;
            _terminalPanelEl._savedHeight = _terminalPanelEl.style.height;
            _terminalPanelEl.style.top = 'auto';
            _terminalPanelEl.style.bottom = '80px';
            _terminalPanelEl.style.left = _terminalPanelEl._savedLeft || '16px';
            _terminalPanelEl.style.right = 'auto';
            _terminalPanelEl.style.width = '';
            _terminalPanelEl.style.height = '';
        } else {
            _terminalPanelEl.style.bottom = 'auto';
            _terminalPanelEl.style.top = _terminalPanelEl._savedTop || '52px';
            _terminalPanelEl.style.left = _terminalPanelEl._savedLeft || '16px';
            _terminalPanelEl.style.right = 'auto';
            _terminalPanelEl.style.width = _terminalPanelEl._savedWidth || '';
            _terminalPanelEl.style.height = _terminalPanelEl._savedHeight || '';
            const entry = _sessionTerminals.get(_activeSessionId);
            if (entry) requestAnimationFrame(() => entry.fitAddon.fit());
        }
    }

    function hideTerminalPanel() {
        if (_terminalPanelEl) _terminalPanelEl.classList.add('hidden');
    }

    function showTerminalPanel() {
        if (_terminalPanelEl) {
            _terminalPanelEl.classList.remove('hidden');
            if (_terminalMinimized) {
                _terminalMinimized = false;
                _terminalPanelEl.classList.remove('minimized');
            }
            const entry = _sessionTerminals.get(_activeSessionId);
            if (entry) requestAnimationFrame(() => entry.fitAddon.fit());
        }
    }

    function updatePanelCloseVisibility() {
        if (!_terminalPanelEl) return;
        const hasAlive = [..._sessionTerminals.values()].some(
            e => !e.tab.classList.contains('dead')
        );
        _terminalPanelEl.classList.toggle('has-alive', hasAlive);
    }

    function removeSessionTerminal(sessionId) {
        const entry = _sessionTerminals.get(sessionId);
        if (!entry) return;
        entry.terminal.dispose();
        entry.container.remove();
        entry.tab.remove();
        _sessionTerminals.delete(sessionId);
        if (_activeSessionId === sessionId) {
            const next = _sessionTerminals.keys().next().value || null;
            if (next) {
                switchToSession(next);
            } else {
                _activeSessionId = null;
                _terminalPanelEl.classList.add('hidden');
            }
        }
    }

    // ── Drag to move (clamped to window boundaries) ─────────────────────────

    function initPanelDrag(handle, panel) {
        let dragging = false, startX, startY, origLeft, origTop;

        function clampPosition(left, top) {
            const w = panel.offsetWidth;
            const h = panel.offsetHeight;
            return {
                left: Math.max(0, Math.min(left, window.innerWidth - w)),
                top: Math.max(0, Math.min(top, window.innerHeight - h)),
            };
        }

        handle.addEventListener('mousedown', (e) => {
            if (e.target.closest('button') || e.target.closest('.tab-close')) return;
            dragging = true;
            startX = e.clientX;
            startY = e.clientY;
            const rect = panel.getBoundingClientRect();
            origLeft = rect.left;
            origTop = rect.top;
            e.preventDefault();
        });

        document.addEventListener('mousemove', (e) => {
            if (!dragging) return;
            const dx = e.clientX - startX;
            const dy = e.clientY - startY;
            const clamped = clampPosition(origLeft + dx, origTop + dy);
            panel.style.left = clamped.left + 'px';
            panel.style.top = clamped.top + 'px';
            panel.style.right = 'auto';
            panel.style.bottom = 'auto';
        });

        document.addEventListener('mouseup', () => { dragging = false; });

        window.addEventListener('resize', () => {
            if (panel.classList.contains('hidden')) return;
            const rect = panel.getBoundingClientRect();
            const clamped = clampPosition(rect.left, rect.top);
            panel.style.left = clamped.left + 'px';
            panel.style.top = clamped.top + 'px';
        });
    }

    // ── Drag to resize (clamped to window boundaries) ─────────────────────

    function initPanelResize(handle, panel, body) {
        let resizing = false, startX, startY, startW, startH;

        handle.addEventListener('mousedown', (e) => {
            resizing = true;
            startX = e.clientX;
            startY = e.clientY;
            startW = panel.offsetWidth;
            startH = panel.offsetHeight;
            e.preventDefault();
        });

        document.addEventListener('mousemove', (e) => {
            if (!resizing) return;
            const rect = panel.getBoundingClientRect();
            const maxW = window.innerWidth - rect.left;
            const maxH = window.innerHeight - rect.top;
            const newW = Math.max(360, Math.min(startW + (e.clientX - startX), maxW));
            const newH = Math.max(200, Math.min(startH + (e.clientY - startY), maxH));
            panel.style.width = newW + 'px';
            panel.style.height = newH + 'px';
        });

        document.addEventListener('mouseup', () => {
            if (resizing) {
                resizing = false;
                const entry = _sessionTerminals.get(_activeSessionId);
                if (entry) requestAnimationFrame(() => entry.fitAddon.fit());
            }
        });
    }

    // Live task panel collapse state — persists across re-renders within a
    // session; reset to collapsed on New (see scNew handler). Default collapsed
    // so the panel never blocks the top of the conversation; the header alone
    // (progress + current item) stays visible, and clicking it floats the full
    // list as an overlay (like the activity strip ↔ popover pair).
    let checklistExpanded = false;

    function renderChecklist(items) {
        // Per-session live task panel pinned at the top of the session's
        // conversation pane. Each session has its own panel — multiple
        // concurrent sessions never overwrite each other's checklist.
        // Stored as a class (not id) so multiple coexist in the DOM at the
        // same time; the panel lives inside that session's pane.
        const pane = _dispatchPane();
        const s = _dispatchSession();
        if (!pane || !s) return;
        let panel = pane.querySelector(':scope > .checklist-panel');
        if (!items || items.length === 0) {
            if (panel) panel.remove();
            return;
        }
        if (!panel) {
            panel = document.createElement('div');
            panel.className = 'checklist-panel';
            pane.insertBefore(panel, pane.firstChild);
        }
        const expanded = !!s.checklistExpanded;
        panel.classList.toggle('collapsed', !expanded);

        const GLYPH = {
            done: '✓', running: '▶', pending: '○', failed: '✗', interrupted: '⊗',
        };
        const doneCount = items.filter((it) => it && it.status === 'done').length;
        const current = items.find((it) => it && it.status === 'running');

        panel.innerHTML = '';

        // Header — always visible; clicking it toggles the floating list.
        const header = document.createElement('button');
        header.type = 'button';
        header.className = 'checklist-header';
        header.setAttribute('aria-expanded', String(expanded));
        const chevron = document.createElement('span');
        chevron.className = 'cl-chevron';
        chevron.textContent = expanded ? '▾' : '▸';
        const label = document.createElement('span');
        label.className = 'cl-summary';
        let summary = 'Task plan · ' + doneCount + '/' + items.length;
        if (!expanded && current) {
            // Collapsed: surface what's running so the panel is useful unopened.
            summary += ' · ▶ ' + String(current.instruction || '');
        } else {
            summary += ' done';
        }
        label.textContent = summary;
        header.appendChild(chevron);
        header.appendChild(label);
        header.addEventListener('click', () => {
            s.checklistExpanded = !s.checklistExpanded;
            renderChecklist(items);
        });
        panel.appendChild(header);

        // Items — built only when expanded; floats as an overlay (CSS absolute)
        // so it covers the conversation instead of pushing it down.
        if (expanded) {
            const list = document.createElement('div');
            list.className = 'checklist-items';
            for (const it of items) {
                const status = String((it && it.status) || 'pending');
                const row = document.createElement('div');
                row.className = 'checklist-item cl-' + status;
                const glyph = document.createElement('span');
                glyph.className = 'cl-glyph';
                glyph.textContent = GLYPH[status] || '·';
                const text = document.createElement('span');
                text.className = 'cl-text';
                text.textContent = String((it && it.instruction) || '');
                row.appendChild(glyph);
                row.appendChild(text);
                list.appendChild(row);
            }
            panel.appendChild(list);
        }
        // Track items on the session bucket so a session switch could
        // re-render in v2 (the pane's DOM already retains the rendered
        // panel, so this is for state persistence not re-render).
        s.checklistItems = items;
    }

    function handleSessionEvent(eventName, data) {
        if (eventName === 'session_opened') {
            createSessionTerminal(data.session_id, data.command, data.description);
            pushActivity('⬡', 'Session opened', data.command);
            updatePanelCloseVisibility();
        } else if (eventName === 'session_data') {
            writeSessionData(data.session_id, data.text || '');
        } else if (eventName === 'session_input') {
            writeSessionInput(data.session_id, data.text || '');
        } else if (eventName === 'session_exec_done') {
            // No-op: output already streamed via session_data
        } else if (eventName === 'session_closed') {
            const entry = _sessionTerminals.get(data.session_id);
            if (entry) {
                entry.tab.classList.add('dead');
                entry.terminal.write(
                    '\r\n\x1b[2m[session closed, exit=' +
                    (data.exit_code ?? '?') + ']\x1b[0m\r\n'
                );
            }
            pushActivity('⬡', 'Session closed', 'exit=' + (data.exit_code ?? '?'));
            updatePanelCloseVisibility();
        }
    }

    handq.onStatus((evt) => {
        if (gateGen(evt)) return;
        if (!evt) return;
        // Drop straggler events for sessions the user explicitly closed.
        // These can arrive after bridge teardown starts and would otherwise
        // render in the wrong pane.
        if (evt.session_id && closedSessions.has(evt.session_id)) return;
        // Stamp the dispatch context so bubble helpers route to this
        // session's pane (per-session DOM in the multi-session model).
        // If the event has no session_id, fall back to active.
        _dispatchSid = _resolveSid(evt);
        // If we received an event for a session we don't have a tab for,
        // create one lazily (e.g. scheduler-dispatched task to default).
        // But NEVER resurrect a tab the user explicitly closed — straggler
        // events from the dying flow must be silently dropped.
        if (evt.session_id && !sessions.has(evt.session_id) && !closedSessions.has(evt.session_id)) {
            _mountSession(evt.session_id, _autoNameForNewSession());
            if (!activeSid) switchSession(evt.session_id);
        }
        // Mark non-active tabs as having unread updates.
        if (_dispatchSid && _dispatchSid !== activeSid) {
            const ns = sessions.get(_dispatchSid);
            if (ns) {
                ns.unread = true;
            }
        }
        try {
            return _onStatusBody(evt);
        } finally {
            _dispatchSid = null;
        }
    });

    function _onStatusBody(evt) {
        window.__handqLog('DEBUG', 'onStatus', {
            type: evt.type,
            id: evt.id,
            kind: evt.kind,
            payload: window.__handqTrunc(evt, 200),
        });

        // Boot-progress envelope (emitted by bridge_main.py before stdio
        // bridge takes over). Three cases:
        //   * boot_progress phase!='stdio_loop_ready' — keep updating overlay
        //   * boot_progress phase=='stdio_loop_ready' — bridge is fully up;
        //     fade overlay NOW. Without this branch the renderer waits for
        //     the next non-boot_progress status event, which the bridge
        //     never sends proactively (it sits in stdio loop awaiting
        //     requests) — net effect: overlay stuck on "ready" indefinitely.
        //   * any other status                       — first real event ⇒ fade overlay out
        // bridge_exit during boot is handled separately at the bottom of
        // the dispatcher (it's already a status event with kind=bridge_exit
        // generated by main.js).
        if (evt.kind === 'boot_progress') {
            updateBootProgress(evt);
            if (evt.phase === 'stdio_loop_ready' && !bootHidden) {
                hideBootOverlay();
            }
            return;
        }
        if (!bootHidden && evt.kind !== 'bridge_exit') {
            hideBootOverlay();
        }
        if (evt.kind === 'bridge_exit') {
            // If the bridge died while the overlay is still up, the user
            // was waiting for it to start — translate the exit into a
            // visible failure. Don't double-fire if we already painted
            // an error.
            if (!bootHidden && !bootErrorState) {
                showBootError(
                    'Backend exited during startup ' +
                    '(code=' + (evt.code != null ? evt.code : '?') +
                    ', signal=' + (evt.signal != null ? evt.signal : '?') +
                    '). Check the bridge log under ' +
                    '%USERPROFILE%\\HandQ\\logs\\ for details.'
                );
            }
        }

        const args = Array.isArray(evt.args) ? evt.args : [];

        if (evt.kind === 'risk_confirmation' ||
            evt.kind === 'tool_confirmation' ||
            evt.kind === 'secret_input' ||
            evt.kind === 'ask_human') {
            // Show the confirmation modal and stop further dispatch — these
            // envelopes are not informational status updates.
            try { showConfirmationModal(evt); }
            catch (e) { window.__handqLog('ERROR', 'showConfirmationModal failed',
                                           { error: String(e) }); }
            return;
        }

        if (evt.kind === 'state_changed' && evt.state) {
            const s = _dispatchSession();
            if (s) s.sessionState = evt.state;
            // V2 activity-strip vocabulary (see controller_v2):
            //   planning  — orchestrator is composing/revising the checklist
            //   thinking  — agent has the LLM stream open (reasoning + tools)
            //   executing — agent dispatching tools / between think-streams
            //   idle      — task settled, final reply sent
            // The first three are live working phases → animated label,
            // consistent with the receptionist's "thinking…". idle clears the
            // working animation and rests the strip.
            if (evt.state === 'planning') {
                setWorking('designing…');
            } else if (evt.state === 'thinking') {
                // Show the actual thinking content (latest reasoning) rather
                // than a generic label; falls back to "thinking…" only before
                // any reasoning has streamed for this task.
                setWorking(s && s.lastThinking
                    ? 'thinking: ' + truncate(s.lastThinking, 120)
                    : 'thinking…');
            } else if (evt.state === 'executing') {
                setWorking('working…');
            } else if (evt.state === 'idle') {
                clearWorking();
                setPill('idle');
            } else {
                setPill(evt.state);
            }
        } else if (evt.kind === 'inline_event') {
            // Backend-emitted step-style line.
            // Render with addStepBubble so it visually matches planner step
            // events instead of the chunkier system bubble used by display_message.
            addStepBubble(String(evt.icon || '·'), String(evt.desc || ''));
        } else if (evt.kind === 'recall_started') {
            // LTM recall is in flight (orchestrator INTENT/PLAN gather, or a
            // per-item / stagnation agent recall). Show a transient working
            // label on the activity strip; the next state_changed / decision /
            // tool event (or a streamed chat reply) supersedes it.
            setWorking('🧠 recalling…');
        } else if (evt.kind === 'decision_made') {
            const iter = args[0] || '';
            const reasoning = args[1] || '';
            const s = _dispatchSession();
            if (s) s.lastThinking = reasoning;
            pushActivity('💭', 'thinking' + (iter ? ' · iter ' + iter : ''), reasoning);
            setWorking('thinking: ' + truncate(reasoning, 120));
        } else if (evt.kind === 'tool_execution_started') {
            const iter   = args[0] || '';
            var rawTool  = args[1] || '';
            const params = args[2];
            const output = args[3];
            const s = _dispatchSession();
            // Backend now sends tool_name in BOTH pre and post events; the
            // pre/post discriminator is the output field (null for pre,
            // populated for post). The "None"/"null" guards stay in place
            // for backwards-compatible payloads.
            var tool = (rawTool && rawTool !== 'None' && rawTool !== 'null') ? rawTool : '';
            var isPre = output === undefined || output === null
                        || output === 'None' || output === 'null';
            if (isPre && tool && s) s.lastCalledTool = tool;
            var effectiveTool = tool || (s && s.lastCalledTool) || 'action';
            const paramText = formatToolParams(params);
            if (isPre) {
                if (s) s.activeExecCount++;
                var ctx = briefToolContext(effectiveTool, params);
                var preLabel = 'Executing ' + effectiveTool;
                var preContent = ctx || paramText;
                pushActivity('⊙', preLabel, preContent, {
                    iter: iter, tool: effectiveTool, pending: true,
                });
                setWorking('⊙ ' + effectiveTool + (ctx ? ' · ' + ctx : ''));
            } else {
                if (s) s.activeExecCount = Math.max(0, s.activeExecCount - 1);
                var readable = formatResultReadable(effectiveTool, output);
                var resultText = readable || (output == null ? '' : String(output));
                var resultIcon = (resultText && resultText.charAt(0) === '✗') ? '✗' : '✓';
                updateActivityResult(iter, effectiveTool, resultIcon,
                                     effectiveTool, resultText);
                if (!s || s.activeExecCount === 0) {
                    clearWorking();
                    setPill(resultIcon + ' ' + effectiveTool +
                            (readable ? ' · ' + readable : ''));
                }
            }
        } else if (evt.kind === 'bridge_exit') {
            const s = _dispatchSession();
            if (s) s.sessionState = 'bridge exited';
            setPill('bridge exited');
            pushActivity('⚠', 'Bridge exited', 'code=' + evt.code + ' signal=' + evt.signal);
        } else if (evt.kind === 'reply') {
            addAssistantTextBubble(evt.text || '');
        } else if (evt.kind === 'reply_delta') {
            // Receptionist is streaming a chat reply. Always clear the chat-side
            // thinking bubble so the streaming text replaces it. The activity
            // strip pill is owned by the planner/agent task state — only reset
            // it when no task is in flight; otherwise the pill would flash to
            // "idle" mid-task while the receptionist chats.
            removeThinkingBubble();
            if (!isTaskRunning()) {
                clearWorking();
                setPill('');
            }
            appendReceptionistDelta(evt.text || '');
        } else if (evt.kind === 'reply_done') {
            sealReceptionistBubble();
        } else if (evt.kind === 'receptionist_thinking_on') {
            // Show the chat-side thinking bubble unconditionally; only steal
            // the activity strip pill when no real task is running, otherwise
            // the agent's working indicator would be hidden by "thinking…".
            showThinkingBubble();
            if (!isTaskRunning()) {
                setWorking('thinking…');
            }
        } else if (evt.kind === 'receptionist_thinking_off') {
            removeThinkingBubble();
            if (!isTaskRunning()) {
                clearWorking();
            }
        } else if (evt.kind === 'session_event') {
            handleSessionEvent(evt.event, evt.data || {});
        } else if (evt.kind === 'session_started') {
            if (evt.session_name) _renameSession(_dispatchSid, evt.session_name);
        } else if (evt.kind === 'scheduled_task_started') {
            const name = evt.session_name || ('⏱ ' + (evt.name || 'Scheduled'));
            _renameSession(_dispatchSid, name);
        } else if (evt.kind === 'checklist') {
            renderChecklist(Array.isArray(evt.items) ? evt.items : []);
        } else if (evt.kind === 'llm_server_error') {
            const retryIn = (typeof evt.retry_in === 'number' && evt.retry_in > 0)
                ? ' — retrying in ' + evt.retry_in + 's'
                : '';
            const attLeft = (typeof evt.attempts_left === 'number' && evt.attempts_left > 0)
                ? ' (' + evt.attempts_left + ' attempt' + (evt.attempts_left !== 1 ? 's' : '') + ' remaining)'
                : '';
            const errSummary = (evt.message || 'API server issue') + retryIn + attLeft;
            addGlobalSystemBubble('⏳ ' + errSummary
                + '\nThis is a temporary API server issue, not a HandQ problem.'
                + ' Retrying automatically — please wait.');
            pushActivity('⏳', 'API retry', errSummary);
            setPill('retrying…');
        } else if (evt.kind === 'llm_fallback') {
            const fromModel = String(evt.from_model || '?');
            const toModel   = String(evt.to_model   || '?');
            const reason    = evt.error ? ' — ' + evt.error : '';
            addGlobalSystemBubble('↪ ' + fromModel + ' failed; trying ' + toModel + reason);
            pushActivity('↪', 'Model fallback', fromModel + ' → ' + toModel);
        } else if (evt.kind === 'network_down') {
            addGlobalSystemBubble('📡 ' + (evt.message || '网络中断，等待恢复…')
                + '\nHandQ will resume automatically once the connection is restored.');
            pushActivity('📡', 'Network down', 'waiting for LLM endpoint');
            setPill('offline…');
        } else if (evt.kind === 'network_waiting') {
            const retryIn = (typeof evt.retry_in === 'number' && evt.retry_in > 0)
                ? evt.retry_in + 's' : '…';
            pushActivity('📡', 'Still offline', 'attempt ' + (evt.attempt || '?') + ', next probe in ' + retryIn);
        } else if (evt.kind === 'network_restored') {
            addGlobalSystemBubble('✅ ' + (evt.message || '网络已恢复，继续执行'));
            pushActivity('✅', 'Network restored', 'resuming');
            setPill('working…');
        }
    }

    handq.onFinal((evt) => {
        if (gateGen(evt)) return;
        if (evt && evt.session_id && closedSessions.has(evt.session_id)) return;
        _dispatchSid = _resolveSid(evt);
        try {
            return _onFinalBody(evt);
        } finally {
            _dispatchSid = null;
        }
    });

    function _onFinalBody(evt) {
        window.__handqLog('INFO', 'onFinal', {
            type: evt && evt.type,
            id: evt && evt.id,
            payload: window.__handqTrunc(window.__handqRedact(evt), 200),
        });
        if (!evt || !evt.result) return;
        // Any final response means the bridge is alive and serving — fade
        // the boot overlay if it's still up.
        if (!bootHidden) hideBootOverlay();

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
            _dispatchPane().appendChild(summary);
            scrollToBottom();
        }
    }

    handq.onError((evt) => {
        if (gateGen(evt)) return;
        if (evt && evt.session_id && closedSessions.has(evt.session_id)) return;
        _dispatchSid = _resolveSid(evt);
        try {
            return _onErrorBody(evt);
        } finally {
            _dispatchSid = null;
        }
    });

    function _onErrorBody(evt) {
        if (gateGen(evt)) return;
        if (!evt) return;
        window.__handqLog('ERROR', 'onError', {
            type: evt.type,
            id: evt.id,
            where: evt.where,
            fatal: !!evt.fatal,
            payload: window.__handqTrunc(evt, 200),
        });
        // Fatal startup errors (bridge spawn failure) arrive on the error
        // channel before any status events. Surface them on the boot
        // overlay rather than buried in the chat.
        if (!bootHidden && evt.fatal) {
            showBootError(
                (evt.where ? '[' + evt.where + '] ' : '') +
                String(evt.message || 'fatal error during startup')
            );
            return;
        }
        addErrorBubble(evt.message, evt.where);
        pushActivity('⚠', 'Error' + (evt.where ? ' · ' + evt.where : ''),
                     evt.message || '(no message)');
        if (evt.fatal) {
            const s = _dispatchSession();
            if (s) s.sessionState = 'fatal';
            setPill('fatal');
        }
    }

    // ----- composer --------------------------------------------------------

    // Note: firstSendDone is per-session — tracked on each session's state
    // bucket as `firstSendDone`. The first message in a session goes out as
    // `request` (and triggers _ensure_flow on the bridge); subsequent ones
    // go out as `user_input` of kind 'message'.

    // Shared send path for per-pane composers. Stamps the OWNING session's
    // sid on the outbound envelope and appends the user bubble into that
    // session's pane — so typing in pane B's box always talks to session B,
    // never the focused tab. `inputEl` is the textarea to clear after sending.
    function submitText(sid, rawText, inputEl) {
        const text = (rawText || '').trim();
        if (!text) return;

        // Dismiss the floating pop-out editor if it's open for this session —
        // the user has committed the draft, no reason to keep the panel.
        try { _FloatingComposer.closeFor(sid); } catch (_) { /* ignore */ }

        // Hidden admin command. Typing /memory toggles the LTM admin overlay
        // instead of dispatching to the bridge (no bubble, no flow trigger).
        if (/^\/memory\/?$/i.test(text)) {
            if (inputEl) inputEl.value = '';
            if (window.adminPanel) {
                if (window.adminPanel.isOpen()) window.adminPanel.close();
                else window.adminPanel.open();
            }
            return;
        }
        // Sister command: /schedules (alias /tasks) opens the scheduler.
        if (/^\/(schedules?|tasks?)\/?$/i.test(text)) {
            if (inputEl) inputEl.value = '';
            if (window.schedulePanel) {
                if (window.schedulePanel.isOpen()) window.schedulePanel.close();
                else window.schedulePanel.open();
            }
            return;
        }

        if (inputEl) inputEl.value = '';

        // Append the user bubble into the OWNING session's pane regardless of
        // which tab is focused.
        const prevDispatch = _dispatchSid;
        _dispatchSid = sid;
        try { addUserBubble(text); }
        finally { _dispatchSid = prevDispatch; }

        const s = sessions.get(sid);
        if (s && !s.firstSendDone) {
            s.firstSendDone = true;
            window.__handqLog('INFO', 'submit (first; type=request)',
                { sid, len: text.length, preview: window.__handqTrunc(text, 200) });
            handq.sendRequest({ type: 'request', session_id: sid, goal: text });
        } else {
            window.__handqLog('INFO', 'submit (type=user_input)',
                { sid, len: text.length, preview: window.__handqTrunc(text, 200) });
            handq.sendRequest({ type: 'user_input', kind: 'message',
                                session_id: sid, text: text });
        }
    }

    // ----- titlebar window controls ----------------------------------------

    if (winCtl) {
        if (tbMin)   tbMin.addEventListener('click', () => winCtl.minimize());
        if (tbMax)   tbMax.addEventListener('click', () => winCtl.toggleMaximize());
        if (tbClose) tbClose.addEventListener('click', () => winCtl.hide());
        // Swap the max-button icon (single square ↔ two overlapping squares)
        // whenever the OS-level maximize state changes. Main pushes both the
        // maximize/unmaximize events and one seed on did-finish-load.
        if (typeof winCtl.onMaxState === 'function') {
            winCtl.onMaxState((state) => {
                if (!tbMax) return;
                const maxed = !!(state && state.isMaximized);
                tbMax.classList.toggle('is-maximized', maxed);
                tbMax.setAttribute('aria-label', maxed ? 'Restore' : 'Maximize');
                tbMax.setAttribute('title', maxed ? 'Restore' : 'Maximize');
            });
        }
    } else {
        window.__handqLog('WARN', 'windowControls preload bridge missing');
    }

    // ----- titlebar Dynamic Island (brand pill morph + menu) ---------------
    //
    // The pill collapses/expands via the .expanded class on #titlebar-island.
    // Clicking a menu item inside the pill lets its existing sc-* handler
    // (registered below in `----- shortcut buttons -----`) fire first, then
    // collapses on the next tick via setTimeout so the two side effects don't
    // race. Click-outside and ESC also collapse.

    const island = document.getElementById('titlebar-island');
    const islandTrigger = document.getElementById('island-trigger');
    const islandMenu = document.getElementById('titlebar-island-menu');

    function collapseIsland() {
        if (!island || !island.classList.contains('expanded')) return;
        island.classList.remove('expanded');
        if (islandTrigger) islandTrigger.setAttribute('aria-expanded', 'false');
        if (islandMenu) islandMenu.setAttribute('aria-hidden', 'true');
    }

    function expandIsland() {
        if (!island || island.classList.contains('expanded')) return;
        island.classList.add('expanded');
        if (islandTrigger) islandTrigger.setAttribute('aria-expanded', 'true');
        if (islandMenu) islandMenu.setAttribute('aria-hidden', 'false');
    }

    if (island && islandTrigger) {
        islandTrigger.addEventListener('click', (e) => {
            e.stopPropagation();
            if (island.classList.contains('expanded')) {
                collapseIsland();
            } else {
                expandIsland();
            }
        });

        // Keep pointerdown inside the island from bubbling to the document
        // click-outside listener below.
        island.addEventListener('pointerdown', (e) => {
            e.stopPropagation();
        });

        for (const item of island.querySelectorAll('.island-menu-item')) {
            item.addEventListener('click', () => {
                // Defer so the sc-* click handler runs first (opens overlay
                // / creates session), then the island snaps shut.
                setTimeout(collapseIsland, 0);
            });
        }

        document.addEventListener('pointerdown', (e) => {
            if (!island.classList.contains('expanded')) return;
            if (island.contains(e.target)) return;
            collapseIsland();
        });

        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && island.classList.contains('expanded')) {
                collapseIsland();
            }
        });
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
    overlaySettings.addEventListener('click', (e) => {
        if (e.target === overlaySettings) closeOverlay(overlaySettings);
    });
    settingsCancel.addEventListener('click', () => closeOverlay(overlaySettings));

    // Confirmation button wiring is per-pane now (see _ensureConfirmUI): each
    // session's inline confirmation card owns its own Reject / Guidance /
    // Submit buttons bound to that session's sid (UI3). The legacy global
    // #overlay-confirmation modal is no longer opened.

    window.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            if (!overlaySettings.classList.contains('hidden')) closeOverlay(overlaySettings);
        }
    });

    // ----- shortcut buttons ------------------------------------------------

    scSettings.addEventListener('click', async () => {
        openOverlay(overlaySettings);
        loadConfig();
        const collapsedState = JSON.parse(localStorage.getItem('handq:settings:collapsed') || '{}');
        overlaySettings.querySelectorAll('.settings-section').forEach(sec => {
            const title = sec.querySelector('legend, .settings-section-header')?.textContent?.trim() || '';
            sec.classList.toggle('collapsed', collapsedState[title] === true);
            const header = sec.querySelector('.settings-section-header');
            if (header && !header.dataset.collapseBound) {
                header.dataset.collapseBound = '1';
                header.addEventListener('click', () => {
                    sec.classList.toggle('collapsed');
                    const state = JSON.parse(localStorage.getItem('handq:settings:collapsed') || '{}');
                    state[title] = sec.classList.contains('collapsed');
                    localStorage.setItem('handq:settings:collapsed', JSON.stringify(state));
                });
            }
        });
        try {
            const result = await window.appInfo.getVersion();
            document.getElementById('settings-version-number').textContent = result.version;
        } catch (_err) { /* version footer is non-critical */ }
    });

    // Scheduler shortcut: same toggle behaviour as the /schedules slash
    // command — opens admin-panel.js's overlay if closed, closes it if
    // already open. window.schedulePanel is set up in admin-panel.js.
    scScheduler.addEventListener('click', () => {
        if (!window.schedulePanel) return;
        if (window.schedulePanel.isOpen()) {
            window.schedulePanel.close();
        } else {
            window.schedulePanel.open();
        }
    });

    // Skills shortcut: toggles admin-panel.js's skill control panel
    // (installed skills, incl. auto-generated disabled ones). window.skillPanel
    // is set up in admin-panel.js.
    if (scSkills) {
        scSkills.addEventListener('click', () => {
            if (!window.skillPanel) return;
            if (window.skillPanel.isOpen()) {
                window.skillPanel.close();
            } else {
                window.skillPanel.open();
            }
        });
    }

    // (Legacy scNew "New" button handler removed — to start a fresh
    // parallel session use the "+" in the session tab bar; to wipe the
    // current session's chat, close its tab (X) and create a new one.)


    // ----- +New session button wiring --------------------------------------

    document.getElementById('sc-new-session').addEventListener('click', async () => {
        let sid;
        await _runVT(() => {
            sid = createSession();
        });
        window.__handqLog('INFO', 'sc-new-session clicked', { sid });
    });

    // Bootstrap the initial default session so the first composer submit has
    // a session_id to ride on. The bridge defaults a session_id-less request
    // to "default" too — using a UUID here keeps both sides in sync and
    // matches the multi-tab model from the start.
    createSession({ name: 'Session 1' });


    // ----- settings form helpers (model pool + Agent/Helper tabs) -----

    function textToModels(text) {
        if (!text) return [];
        return text
            .split(/\r?\n/)
            .map((s) => s.trim())
            .filter((s) => s.length > 0);
    }

    function modelsToText(models) {
        if (!Array.isArray(models)) return '';
        return models.map((m) => String(m)).join('\n');
    }

    // Mirror of src/infrastructure/role_resolver.resolve_models_and_helper —
    // handles both new schema (available_models + agent_models + helper_models)
    // and legacy schema (models + helper_models). On Save we always write the
    // new schema and drop legacy fields.
    function resolveModelsAndHelper(llm) {
        if (!llm || typeof llm !== 'object') return { pool: [], agent: [], helper: [] };
        // New schema: available_models + agent_models + helper_models
        if (Array.isArray(llm.available_models) && llm.available_models.length) {
            const pool = llm.available_models.filter(Boolean).map(String);
            const agent = Array.isArray(llm.agent_models)
                ? llm.agent_models.filter(Boolean).map(String)
                : [pool[0]];
            const helper = Array.isArray(llm.helper_models)
                ? llm.helper_models.filter(Boolean).map(String)
                : [pool[pool.length - 1]];
            return { pool, agent, helper };
        }
        // Legacy: `models` + `helper_models` (flat arrays)
        const modelsRaw = Array.isArray(llm.models) ? llm.models.filter(Boolean).map(String) : [];
        const helperRaw = Array.isArray(llm.helper_models) ? llm.helper_models.filter(Boolean).map(String) : [];
        const rolesRaw = (llm.roles && typeof llm.roles === 'object') ? llm.roles : null;
        // (1) Modern flat shape — models + explicit helper_models
        if (modelsRaw.length > 0) {
            if (helperRaw.length > 0) {
                const pool = [...new Set([...modelsRaw, ...helperRaw])];
                return { pool, agent: modelsRaw, helper: helperRaw };
            }
            // No helper_models but roles present → fall through to roles handler
            if (!rolesRaw) {
                const pool = [...new Set(modelsRaw)];
                return { pool, agent: modelsRaw, helper: [modelsRaw[modelsRaw.length - 1]] };
            }
        }
        // (2) Legacy roles shape
        if (rolesRaw) {
            const listOf = (key) =>
                Array.isArray(rolesRaw[key]) ? rolesRaw[key].filter(Boolean).map(String) : [];
            const seen = new Set();
            let models = [];
            for (const key of ['agent', 'planner', 'receptionist']) {
                for (const m of listOf(key)) {
                    if (!seen.has(m)) { seen.add(m); models.push(m); }
                }
            }
            const helper = listOf('helper').length ? listOf('helper') : listOf('from_data');
            if (models.length === 0 && modelsRaw.length > 0) models = modelsRaw;
            const pool = [...new Set([...models, ...helper])];
            return { pool, agent: models, helper };
        }
        return { pool: [], agent: [], helper: [] };
    }

    // ── Model selector tab switching + checkbox rendering ────────────
    document.querySelectorAll('.model-tab').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.model-tab').forEach(b => b.classList.remove('active'));
            document.querySelectorAll('.model-tab-panel').forEach(p => p.classList.add('hidden'));
            btn.classList.add('active');
            document.getElementById('model-panel-' + btn.dataset.tab).classList.remove('hidden');
        });
    });

    function renderModelCheckboxes() {
        const pool = textToModels(cfgLlmAvailableModels.value);
        for (const [container, group] of [
            [cfgLlmAgentChecks, 'agent'],
            [cfgLlmHelperChecks, 'helper'],
        ]) {
            const checked = new Set(
                [...container.querySelectorAll('input:checked')].map(el => el.value)
            );
            container.innerHTML = '';
            if (pool.length === 0) {
                container.innerHTML = '<span style="color:var(--fg-mute);font-size:11px">Add models above first</span>';
                continue;
            }
            for (const m of pool) {
                const lbl = document.createElement('label');
                lbl.title = m;
                const cb = document.createElement('input');
                cb.type = 'checkbox';
                cb.name = group + '_models';
                cb.value = m;
                if (checked.has(m)) cb.checked = true;
                const nameSpan = document.createElement('span');
                nameSpan.className = 'mcl-name';
                // Display only the model name — hide any "provider::" prefix.
                // The full id stays as cb.value (saved) and lbl.title (hover).
                nameSpan.textContent = m.includes('::') ? m.slice(m.lastIndexOf('::') + 2) : m;
                lbl.append(cb, nameSpan);
                container.append(lbl);
            }
        }
    }
    cfgLlmAvailableModels.addEventListener('input', renderModelCheckboxes);

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
        const emailCfg = cfg.email || {};

        cfgLlmApiKey.value =
            (llm.API_KEY === undefined || llm.API_KEY === null) ? '' : String(llm.API_KEY);
        cfgLlmMaxTokens.value =
            (llm.max_tokens === undefined || llm.max_tokens === null) ? '' : String(llm.max_tokens);

        const resolved = resolveModelsAndHelper(llm);
        cfgLlmAvailableModels.value = modelsToText(resolved.pool);
        renderModelCheckboxes();
        for (const m of resolved.agent) {
            const cb = cfgLlmAgentChecks.querySelector(`input[value="${CSS.escape(m)}"]`);
            if (cb) cb.checked = true;
        }
        for (const m of resolved.helper) {
            const cb = cfgLlmHelperChecks.querySelector(`input[value="${CSS.escape(m)}"]`);
            if (cb) cb.checked = true;
        }

        cfgSessionLogLevel.value = sessCfg.log_level || '';
        cfgSessionVenvPath.value = sessCfg.venv_path || '';

        // readSwitch reads `auto_approve` (or `enabled`) from a switch entry.
        // `enabled` defaults to true when missing (back-compat with older
        // configs that only carry `auto_approve`).
        function readSwitch(name, field) {
            const v = switches[name];
            if (!v || typeof v !== 'object') {
                return field === 'enabled' ? true : false;
            }
            if (field === 'enabled') {
                return ('enabled' in v) ? Boolean(v.enabled) : true;
            }
            return ('auto_approve' in v) ? Boolean(v.auto_approve) : false;
        }
        cfgSwToolWrite.checked = readSwitch('tool_write', 'auto_approve');
        cfgSwToolEdit.checked  = readSwitch('tool_edit',  'auto_approve');
        cfgSwToolBash.checked  = readSwitch('tool_bash',  'auto_approve');
        cfgSwToolBrowserAuto.checked    = readSwitch('tool_browser', 'auto_approve');
        cfgSwToolDesktopAuto.checked    = readSwitch('tool_desktop', 'auto_approve');
        cfgSwHighRisk.checked  = readSwitch('high_risk', 'auto_approve');

        const blacklist = emailCfg.folder_blacklist;
        cfgEmailFolderBlacklist.value = Array.isArray(blacklist)
            ? blacklist.join(', ')
            : (typeof blacklist === 'string' ? blacklist : '');

    // ── Personalization fields ────────────────────────────────
        const persCfg = cfg.personalization || {};
        cfgPersEnabled.checked = persCfg.enabled !== false;  // default true
        cfgPersExcludedApps.value = Array.isArray(persCfg.excluded_apps)
            ? persCfg.excluded_apps.join('\n')
            : '';
        cfgPersGitHookRepos.value = Array.isArray(persCfg.git_hook_repos)
            ? persCfg.git_hook_repos.join('\n')
            : '';
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
        const emailCfg = out.email && typeof out.email === 'object' ? out.email : {};

        if ('api_key_env' in llm) delete llm.api_key_env;
        if ('api_key' in llm) delete llm.api_key;
        llm.API_KEY = cfgLlmApiKey.value;

        if (cfgLlmMaxTokens.value === '') {
            delete llm.max_tokens;
        } else {
            const n = parseInt(cfgLlmMaxTokens.value, 10);
            if (!Number.isNaN(n)) llm.max_tokens = n;
        }

        // Model pool + checked subsets (new schema). Drop legacy fields.
        const pool = textToModels(cfgLlmAvailableModels.value);
        const agentModels = pool.filter(m =>
            cfgLlmAgentChecks.querySelector(`input[value="${CSS.escape(m)}"]:checked`)
        );
        const helperModels = pool.filter(m =>
            cfgLlmHelperChecks.querySelector(`input[value="${CSS.escape(m)}"]:checked`)
        );
        if (pool.length) llm.available_models = pool;
        else delete llm.available_models;
        if (agentModels.length) llm.agent_models = agentModels;
        else delete llm.agent_models;
        if (helperModels.length) llm.helper_models = helperModels;
        else delete llm.helper_models;
        delete llm.models;
        delete llm.roles;

        if (cfgSessionLogLevel.value) sess.log_level = cfgSessionLogLevel.value;
        else delete sess.log_level;

        if ('step_verification_threshold' in sess) delete sess.step_verification_threshold;
        if ('workspace_base' in sess) delete sess.workspace_base;
        if (cfgSessionVenvPath.value) sess.venv_path = cfgSessionVenvPath.value;
        else delete sess.venv_path;

        function writeSwitch(name, field, checked) {
            if (!switches[name] || typeof switches[name] !== 'object') {
                switches[name] = {};
            }
            switches[name][field] = Boolean(checked);
        }
        writeSwitch('tool_write', 'auto_approve', cfgSwToolWrite.checked);
        writeSwitch('tool_edit',  'auto_approve', cfgSwToolEdit.checked);
        writeSwitch('tool_bash',  'auto_approve', cfgSwToolBash.checked);
        writeSwitch('tool_browser', 'auto_approve', cfgSwToolBrowserAuto.checked);
        writeSwitch('tool_desktop', 'auto_approve', cfgSwToolDesktopAuto.checked);
        writeSwitch('high_risk',  'auto_approve', cfgSwHighRisk.checked);

        const rawBlacklist = cfgEmailFolderBlacklist.value;
        emailCfg.folder_blacklist = rawBlacklist
            ? rawBlacklist.split(',').map((s) => s.trim()).filter(Boolean)
            : [];

        // ── Personalization fields ────────────────────────────────
        // Roundtrip the textarea contents into yaml lists. Empty
        // lines and pure whitespace lines are dropped so a stray
        // blank doesn't show up in the saved file.
        const persCfg = (out.personalization && typeof out.personalization === 'object')
            ? out.personalization : {};
        persCfg.enabled = !!cfgPersEnabled.checked;
        persCfg.excluded_apps = (cfgPersExcludedApps.value || '')
            .split('\n').map((s) => s.trim()).filter(Boolean);
        persCfg.git_hook_repos = (cfgPersGitHookRepos.value || '')
            .split('\n').map((s) => s.trim()).filter(Boolean);
        out.personalization = persCfg;

        out.llm = llm;
        out.session = sess;
        out.interaction_switches = switches;
        out.email = emailCfg;
        return out;
    }

    function loadConfig() {
        window.__handqLog('INFO', 'loadConfig: dispatching getConfig');
        settingsStatus.textContent = 'loading…';
        showSettingsLoading();
        handq.getConfig().then((result) => {
            const cfg = (result && result.config) || {};
            window.__handqLog('INFO', 'loadConfig: success',
                { path: result && result.config_path });
            applyConfigToForm(cfg);
            settingsStatus.textContent = 'loaded';
            hideSettingsLoading();
        }).catch((err) => {
            window.__handqLog('ERROR', 'loadConfig: failure',
                { err: err && err.message });
            settingsStatus.textContent = 'load failed: ' + (err && err.message);
            showToast('Load failed: ' + (err && err.message), 'err');
            hideSettingsLoading();
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
                // Surface git hook sync outcome separately so install/
                // uninstall failures (e.g. repo path missing, foreign
                // hook in the way) are visible, not silently swallowed.
                const sync = result.git_hook_sync || {};
                const errs = Array.isArray(sync.errors) ? sync.errors : [];
                const ins = Array.isArray(sync.installed) ? sync.installed : [];
                const uns = Array.isArray(sync.uninstalled) ? sync.uninstalled : [];
                let msg = 'Settings saved.';
                if (ins.length) msg += ` Installed ${ins.length} hook${ins.length === 1 ? '' : 's'}.`;
                if (uns.length) msg += ` Uninstalled ${uns.length} hook${uns.length === 1 ? '' : 's'}.`;
                showToast(msg, 'ok');
                if (errs.length) {
                    const summary = errs
                        .map((e) => `${e.op} ${e.repo}: ${e.error}`)
                        .join(' | ');
                    showToast('Hook sync issues — ' + summary, 'err');
                }
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

    // ----- global hotkey configuration ----------------------------------------

    let pendingHotkey = null;

    function electronAccelerator(e) {
        const parts = [];
        if (e.ctrlKey)  parts.push('Ctrl');
        if (e.altKey)   parts.push('Alt');
        if (e.shiftKey) parts.push('Shift');
        if (e.metaKey)  parts.push('Super');
        const key = e.key;
        if (['Control', 'Alt', 'Shift', 'Meta'].includes(key)) return null;
        const mapped =
            key === ' ' ? 'Space' :
            key.length === 1 ? key.toUpperCase() :
            key;
        parts.push(mapped);
        return parts.join('+');
    }

    if (cfgHotkey) {
        cfgHotkey.addEventListener('keydown', (e) => {
            e.preventDefault();
            e.stopPropagation();
            const acc = electronAccelerator(e);
            if (!acc) return;
            cfgHotkey.value = acc;
            pendingHotkey = acc;
        });

        cfgHotkey.addEventListener('focus', () => {
            cfgHotkey.placeholder = 'Press a key combination…';
        });
        cfgHotkey.addEventListener('blur', () => {
            cfgHotkey.placeholder = 'Ctrl+Alt+W';
        });
    }

    function loadHotkeyToForm() {
        if (!window.hotkeySettings) return;
        window.hotkeySettings.get().then((result) => {
            if (result && result.hotkey) {
                cfgHotkey.value = result.hotkey;
                pendingHotkey = null;
            }
        }).catch(() => {});
    }

    function saveHotkeyIfChanged() {
        if (!pendingHotkey || !window.hotkeySettings) return Promise.resolve();
        return window.hotkeySettings.set(pendingHotkey).then((result) => {
            if (result && result.success) {
                pendingHotkey = null;
                window.__handqLog('INFO', 'hotkey saved', { hotkey: result.hotkey });
            } else {
                const err = (result && result.error) || 'Unknown error';
                showToast('Hotkey: ' + err, 'err');
                cfgHotkey.value = (result && result.hotkey) || '';
                pendingHotkey = null;
            }
        }).catch((err) => {
            showToast('Hotkey save failed: ' + (err && err.message), 'err');
        });
    }

    // Hook into the existing load/save flow.
    const _origScSettingsHandler = scSettings.onclick;
    scSettings.addEventListener('click', () => { loadHotkeyToForm(); });
    settingsLoadBtn.addEventListener('click', () => { loadHotkeyToForm(); });
    settingsForm.addEventListener('submit', () => { saveHotkeyIfChanged(); });
})();
