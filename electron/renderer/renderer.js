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
        // Forward to main so renderer logs also land in handq-frontend.log —
        // previously __handqLog only reached the console + in-window panel
        // (Ctrl+Shift+L), which is why the file was all PRELOAD lines and this
        // whole class of renderer bug was invisible in logs. DEBUG is gated on
        // the main side (HANDQ_FRONTEND_DEBUG) so the per-event onStatus /
        // reply_delta firehose doesn't flood the file by default.
        try {
            if (window.handqLog && typeof window.handqLog.write === 'function') {
                const msg = args
                    .map((a) => (typeof a === 'string' ? a : safeStringify(a)))
                    .join(' ');
                window.handqLog.write(lvl, msg);
            }
        } catch (_) { /* logging must never throw */ }
    };

    window.__handqTrunc = function (value, n) {
        const limit = (typeof n === 'number' && n > 0) ? n : 200;
        const s = safeStringify(value);
        return s.length > limit ? s.slice(0, limit) + '…(' + s.length + ')' : s;
    };

    // Keys whose values must never reach a log file. Beyond the LLM API key
    // this covers the remote-control bearer token and the
    // `handq://host:port/token` pairing string that embeds it — both travel
    // through the remote_* envelopes, and holding a machine's token is enough to
    // open agent sessions on it. `capability` is the per-session equivalent.
    const _REDACT_KEYS = new Set([
        'API_KEY', 'api_key', 'api_key_env',
        'token', 'pairing', 'capability', 'remote_control_token',
    ]);

    window.__handqRedact = function (value) {
        if (!value || typeof value !== 'object') return value;
        if (Array.isArray(value)) return value.map(window.__handqRedact);
        const out = {};
        for (const k of Object.keys(value)) {
            const v = value[k];
            if (_REDACT_KEYS.has(k)) {
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
        // ── Ctrl+Shift+P / Ctrl+Shift+; : Performance mode A/B toggle ──
        if (e.ctrlKey && e.shiftKey && (e.key === 'P' || e.key === 'p' || e.key === ';')) {
            e.preventDefault();
            document.documentElement.classList.toggle('perf-mode');
            const on = document.documentElement.classList.contains('perf-mode');
            window.__handqLog('INFO', `perf-mode ${on ? 'ON' : 'OFF'}`);
        }
    });
})();

// ----- Window-resize compositor relief -------------------------------------
// Until now NOTHING in the stylesheet reacted to a plain OS window resize,
// even though a resize is the most expensive thing this UI does: every frame
// Chromium re-lays-out the document AND re-rasterizes each backdrop-filter
// region at its new size, several of which are 30px blurs spanning most of
// the window.
//
// The reason this shows up as "everything involving a size change feels
// choppy" is that a window resize is not a rare, deliberate act here — the
// auto-resize IPC (see _sendAutoResizeIpc) fires one on session create and
// close, on rail open and close, and on every sidebar toggle. The two
// existing suppression scopes miss all of that: body.card-width-instant is
// added only by the sidebar's own toggle path, and html.vt-active only for
// the duration of a View Transition.
//
// html.window-resizing is set on the first resize tick and cleared a beat
// after the last one, so it also covers DWM's trailing frames rather than
// snapping the glass back mid-animation. The paired CSS block flattens the
// expensive surfaces for exactly that span.
(function () {
    let clearTimer = 0;
    // Long enough to bridge the gap between DWM animation frames (which can
    // stutter under load) without leaving the glass flat after the resize
    // has visibly finished.
    const CLEAR_DELAY_MS = 140;
    window.addEventListener('resize', () => {
        if (!clearTimer) document.documentElement.classList.add('window-resizing');
        clearTimeout(clearTimer);
        clearTimer = setTimeout(() => {
            clearTimer = 0;
            document.documentElement.classList.remove('window-resizing');
        }, CLEAR_DELAY_MS);
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
    const stageRail = document.getElementById('stage-rail');
    const stageRailList = document.getElementById('stage-rail-list');

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

    // bridge_main.py's _timed_import() reports raw dotted Python module
    // paths (e.g. "src.infrastructure.long_term_memory") in its importing/
    // imported boot_progress events. Those read as source code to a user
    // watching the boot screen, so map the known ones to plain nouns.
    // Anything not in this table (a future import bridge_main.py adds)
    // falls back to its last dotted segment with underscores turned into
    // spaces, rather than the full module path.
    const MODULE_FRIENDLY_NAMES = {
        'src.bridge.stdio_bridge':             'core services',
        'src.infrastructure.skills':           'skills',
        'src.infrastructure.long_term_memory': 'memory',
        'src.infrastructure.personality':      'activity monitor',
        'src.infrastructure.scheduler':        'scheduler',
    };

    function friendlyModuleName(mod) {
        const raw = String(mod || '');
        const known = MODULE_FRIENDLY_NAMES[raw];
        if (known) return known;
        const last = raw.split('.').pop() || 'component';
        return last.replace(/_/g, ' ');
    }

    function formatPhase(evt) {
        const phase = String(evt && evt.phase || '');
        let base = BOOT_PHASE_LABELS[phase] || phase.replace(/_/g, ' ');
        if (phase === 'importing' && evt.module) {
            base = 'loading ' + friendlyModuleName(evt.module);
        } else if (phase === 'imported' && evt.module) {
            base = 'loaded ' + friendlyModuleName(evt.module) +
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
    // Remote control has no settings fields. Becoming a server, pairing,
    // opening and destroying remote sessions are all actions with immediate
    // effect on another machine, so they live in the Connect panel
    // (connect-panel.js) — not in a form you Save. See the comment block where
    // this fieldset used to be in index.html for what was removed and why.

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
            // Session-resume state (rendering moved to right sidebar —
            // session-sidebar.js; these fields just track the bridge-side
            // offer lifecycle for IPC routing).
            pendingResume: null,  // {candidates:[...]} while an offer shows
            _resumeTimeoutId: null, // cosmetic TTL auto-hide timer
            // Per-session activity-strip state ("idle"|"thinking"|"executing").
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
            activeCoordinatorBubble: null,
            thinkingBubble: null,
            // Workspace info from session_started event.
            sessionDir: '',
            workspaceDir: '',
            // Has unseen activity since the user last looked at this tab.
            unread: false,
            // Stage-Manager placement — 'main' (one of the 2 center-stage
            // slots) or 'rail' (minimized thumbnail in the left rail).
            // Assigned by _placeSession; null until first placed.
            slot: null,
            // Which of the 2 mainSlots entries this sid occupies, or -1
            // when not in main (kept for quick lookups; mainSlots is the
            // source of truth).
            mainIndex: -1,
            // Bumped from the module-level _focusCounter every time this
            // session is focused — the LRU signal _placeSession uses to
            // decide which main occupant to evict when both slots are full.
            lastFocusedAt: 0,
            // Per-card minimize/maximize button refs (only shown once a
            // 2nd concurrent session exists — see _updateLayout).
            minBtn: null,
            maxBtn: null,
            // The .stage-rail-thumb wrapper currently holding this
            // session's card, or null while it's on the main stage. Set by
            // _moveToRail / cleared by _occupyMainSlot.
            _railWrap: null,
            // Scroll-restore state captured when this card leaves the main
            // stage (main → rail). appendChild reparenting resets the
            // .session-card-body scrollTop to 0, and the rail thumbnail's CSS
            // `zoom` perturbs the scroll coordinate system, so we snapshot the
            // MAIN-stage scroll offset on the way out and restore it on the way
            // back in (see _moveToRail / _occupyMainSlot). Null until the card
            // has been to the rail at least once.
            _savedScrollTop: null,
            // Whether the reader was pinned to the bottom (following the live
            // stream) when the card left main. If so we re-pin to the latest
            // bottom on return rather than restoring the stale absolute offset,
            // so content that arrived while off-stage stays followed.
            _savedAtBottom: false,
        };
    }

    /** @type {Map<string, ReturnType<typeof _newSessionState>>} */
    const sessions = new Map();
    let activeSid = null;
    // Sids explicitly closed by the user. Prevents straggler events from
    // resurrecting a closed tab via lazy-mount (zombie tab).
    const closedSessions = new Set();
    // Stage-Manager main stage — exactly ONE session visible in the center
    // #conversation row at a time; everything else lives in the left
    // .stage-rail as a live-but-minimized thumbnail. null = empty slot.
    //
    // NOTE: the array is length 2 for legacy compatibility — many downstream
    // functions (_ensureSomeMainOccupant, _updateLayout's mainCount,
    // _maximizeSession, closeSession, _minimizeSession) hardcode
    // `mainSlots[0] || mainSlots[1]` or write `mainSlots[1] = null`. Rather
    // than rewrite every one of those (and re-verify each edge case), the
    // "only one active card" constraint is enforced at the SINGLE placement
    // entry point: _findFreeMainSlot() below only ever reports slot 0 as
    // free, so slot 1 stays perpetually null. Every other path that reads
    // mainSlots[1] gracefully treats it as an empty slot (`null || X === X`).
    // This keeps the fix surgical.
    const mainSlots = [null, null];
    // Monotonic counter for LRU eviction — see _placeSession.
    let _focusCounter = 0;

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
        // Disable per-card backdrop-filter unconditionally during the
        // position interpolation — prevents re-blur every frame of the
        // transition. Used to be perf-mode-only; the cost applies
        // equally regardless of perf-mode, so it's unconditional now.
        document.documentElement.classList.add('vt-active');
        let transition;
        try {
            transition = document.startViewTransition(mutate);
        } catch (err) {
            window.__handqLog('WARN', 'startViewTransition threw', { err: err && err.message });
            mutate();
            delete document.documentElement.dataset.vtScope;
            document.documentElement.classList.remove('vt-active');
            return;
        }
        try { await transition.finished; } catch (_) { /* aborted transitions are ok */ }
        delete document.documentElement.dataset.vtScope;
        document.documentElement.classList.remove('vt-active');
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

    // Build the DOM scaffolding for a new session: its session card (header
    // + scrollable body + inline confirmation host + per-pane composer).
    // Does NOT attach the card to the DOM or decide main-vs-rail placement —
    // callers place it via _placeSession / _placeSessionPassive right after.
    function _mountSession(sid, name) {
        const s = _newSessionState(sid, name);

        // ── Card (parented into either #conversation or a rail thumbnail
        // wrapper by the placement functions below) ──────────────────────
        const card = document.createElement('div');
        card.className = 'session-card';
        card.dataset.sid = sid;
        // Unique VT name so this card gets its own snapshot pair when it
        // enters/leaves the DOM — required for per-element FLIP on the
        // remaining cards. Static per card lifetime.
        card.style.viewTransitionName = 'session-' + sid;

        // Header: name · status pill · close.
        // (Minimize/maximize buttons were removed from the header — the user
        // asked for a plainer session card. The _minimizeSession /
        // _maximizeSession helpers below are kept as private functions since
        // the stage-rail promote/demote flow may still call them through
        // other code paths; only the header affordance is gone.)
        const head = el('div', 'session-card-head');
        const title = el('span', 'session-card-title', name);
        title.addEventListener('dblclick', () => _startRenameSession(sid));
        const pill = el('span', 'session-card-pill', 'idle');

        // Remote-control badge — hidden for a local session, and the ONLY
        // chrome that distinguishes a remote tab. Everything else about the
        // card is identical because the bridge replays the remote machine's UI
        // events through the same delegate a local session uses.
        const remoteBadge = el('span', 'session-card-remote hidden');

        const cardClose = el('button', 'session-card-close');
        cardClose.type = 'button';
        cardClose.setAttribute('aria-label', 'Close session');
        cardClose.title = 'Close session';
        // SVG cross instead of the '×' text glyph — the glyph's ink isn't
        // centered in its own em-box in the system font (reads visibly
        // off-center in a 22px circle), same reason the titlebar close
        // button (#tb-close) uses two drawn lines instead of a character.
        cardClose.innerHTML =
            '<svg viewBox="0 0 12 12" aria-hidden="true">' +
            '<line x1="3" y1="3" x2="9" y2="9" stroke="currentColor" ' +
            'stroke-width="1.4" stroke-linecap="round"/>' +
            '<line x1="9" y1="3" x2="3" y2="9" stroke="currentColor" ' +
            'stroke-width="1.4" stroke-linecap="round"/></svg>';
        head.appendChild(title);
        head.appendChild(pill);
        head.appendChild(remoteBadge);
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

        s.card = card;
        s.pane = body;
        s.titleEl = title;
        s.pillEl = pill;
        s.remoteBadgeEl = remoteBadge;
        s.confirmEl = confirm;
        s.composerInput = ta;
        // Min/max buttons removed from the card header — kept as null refs
        // so _updateLayout's `if (s.minBtn)` guards continue to no-op.
        s.minBtn = null;
        s.maxBtn = null;

        // Clicking anywhere on a MAIN-slot card focuses it (jump aid + unread
        // clear); while minimized the card itself is pointer-events:none (see
        // .stage-rail-thumb .session-card in styles.css) so this never fires
        // for a rail thumbnail — the thumbnail wrapper's own click handler
        // (wired in _moveToRail) is what restores it. The close button stops
        // propagation below.
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
        _MentionAutocomplete.attach(ta);

        sessions.set(sid, s);
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
        if (window.SessionSidebar) window.SessionSidebar.setSessionName(sid, name);
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
        switchSession(sid, opts && opts.originRect);
        return sid;
    }

    // Single stable entry point every call site uses to "go to" a session —
    // dispatches on its current placement so callers never need to know
    // whether a sid is a fresh mount, a main-stage occupant, or a rail
    // thumbnail. This is what lets old call sites (lazy-mount, the card's
    // own mousedown handler, closeSession's neighbour-switch) keep working
    // unchanged after the Stage-Manager rewrite below. `originRect` is only
    // meaningful for the "brand new" branch (see _placeSession's Genie
    // entrance) — existing main/rail sessions ignore it.
    function switchSession(sid, originRect) {
        const s = sessions.get(sid);
        if (!s) return;
        if (s.slot === 'main') { _focusMainSession(sid); return; }
        if (s.slot === 'rail') { _promoteFromRail(sid); return; }
        _placeSession(sid, originRect); // brand new, never placed yet
    }

    // ── Stage-Manager placement state machine ─────────────────────────────
    //
    // At most 2 sessions are ever visible in the main #conversation stage
    // (mainSlots[0]/[1]); every other session lives as a live thumbnail in
    // .stage-rail. "Focus" (activeSid, the right-hand detail panel's
    // subject, the composer that receives keyboard focus) is only ever a
    // main-slot occupant — rail thumbnails are look-don't-touch (see
    // .stage-rail-thumb .session-card { pointer-events: none } in
    // styles.css) and must be promoted back to main before they're usable.

    function _findFreeMainSlot() {
        // Single-slot policy: only slot 0 is ever considered "free" for new
        // placements. Slot 1 exists in the array only for legacy readers
        // (see mainSlots' declaration comment) — never fill it. This is the
        // one and only enforcement point for "exactly one card in main."
        return mainSlots[0] ? -1 : 0;
    }

    // Index (0/1) of the main occupant least recently focused, or -1 if
    // both slots are empty. Excludes `excludeSid` so a session can look for
    // "who else is in main" without matching itself.
    function _lruMainIndex(excludeSid) {
        let best = -1, bestAt = Infinity;
        for (let i = 0; i < mainSlots.length; i++) {
            const sid = mainSlots[i];
            if (!sid || sid === excludeSid) continue;
            const occ = sessions.get(sid);
            const at = occ ? occ.lastFocusedAt : 0;
            if (at < bestAt) { bestAt = at; best = i; }
        }
        return best;
    }

    // Moves sid's card DOM node into the main stage at slot `index`,
    // unwrapping it from a rail thumbnail first if that's where it was.
    // Does not touch focus — callers decide whether to focus afterward.
    function _occupyMainSlot(index, sid) {
        const s = sessions.get(sid);
        if (!s) return;
        if (s._railWrap) {
            if (s._railWrap.parentNode) s._railWrap.parentNode.removeChild(s._railWrap);
            s._railWrap = null;
        }
        mainSlots[index] = sid;
        s.slot = 'main';
        s.mainIndex = index;
        // Re-append every occupied slot in order 0-then-1 so DOM order always
        // matches slot order regardless of which slot just changed —
        // appendChild on an already-attached node moves it, so this is a
        // cheap no-op for whichever slot didn't just move.
        for (let i = 0; i < mainSlots.length; i++) {
            const occSid = mainSlots[i];
            const occ = occSid && sessions.get(occSid);
            if (occ && occ.card) conversation.appendChild(occ.card);
        }
        // Restore this card's main-stage scroll position. The appendChild
        // above just reparented the card back into #conversation, which reset
        // its .session-card-body scrollTop to 0 (and it was under a `zoom`
        // transform while in the rail). We captured the pre-move offset in
        // _moveToRail; re-apply it now. If the reader was pinned to the bottom
        // when the card left, re-pin to the CURRENT bottom instead so any
        // content that streamed in off-stage stays followed. Deferred to the
        // next frame: the card's real (un-zoomed) layout — and thus its final
        // scrollHeight — isn't settled until after this synchronous reparent
        // and the caller's _updateLayout have committed.
        if (s._savedScrollTop !== null && s.pane) {
            const pane = s.pane;
            const wantBottom = s._savedAtBottom;
            const savedTop = s._savedScrollTop;
            s._savedScrollTop = null;
            s._savedAtBottom = false;
            requestAnimationFrame(() => {
                if (wantBottom) pane.scrollTop = pane.scrollHeight;
                else pane.scrollTop = savedTop;
            });
        }
    }

    // Moves sid's card DOM node into a rail thumbnail wrapper. Also clears
    // any mainSlot entry that still points at this sid — the function used
    // to depend on the caller to null out mainSlots first, but that led to
    // subtle "someone forgot to clear" bugs. Now the invariant holds no
    // matter who calls it: after this returns, sid is in the rail AND
    // nowhere else in mainSlots.
    function _moveToRail(sid) {
        const s = sessions.get(sid);
        if (!s || !stageRailList) return;
        // Snapshot this card's scroll position while it's STILL on the main
        // stage (un-zoomed, real coordinates) so _occupyMainSlot can restore
        // it when the card is promoted back. The upcoming reparent into the
        // rail wrapper would otherwise reset scrollTop to 0, and the rail's
        // `zoom` makes the offset read while docked there meaningless.
        // `_savedAtBottom` records whether the reader was following the live
        // tail (within a small tolerance) so we re-pin to the newest bottom on
        // return rather than a stale absolute offset.
        if (s.pane) {
            const pane = s.pane;
            const distanceFromBottom =
                pane.scrollHeight - pane.scrollTop - pane.clientHeight;
            s._savedAtBottom = distanceFromBottom <= 8;
            s._savedScrollTop = pane.scrollTop;
        }
        // Clear any stale mainSlot reference to this sid so slot state
        // stays consistent even when callers forget.
        for (let i = 0; i < mainSlots.length; i++) {
            if (mainSlots[i] === sid) mainSlots[i] = null;
        }
        // If this session already has a rail wrap (e.g. from a prior move
        // that wasn't unwound), remove the stale wrap first so we don't
        // leave an empty wrapper in the rail. Then build a fresh one.
        if (s._railWrap && s._railWrap.parentNode) {
            s._railWrap.parentNode.removeChild(s._railWrap);
            s._railWrap = null;
        }
        const wrap = el('div', 'stage-rail-thumb');
        wrap.dataset.sid = sid;
        wrap.addEventListener('click', () => switchSession(sid));
        wrap.appendChild(s.card); // moves the card node out of #conversation
        stageRailList.insertBefore(wrap, stageRailList.firstChild);
        s._railWrap = wrap;
        s.slot = 'rail';
        s.mainIndex = -1;
        s.card.classList.remove('active');
    }

    // Sets activeSid to a session that's ALREADY a main occupant — updates
    // the .active highlight, bumps LRU recency, and repoints the right-hand
    // detail panel. Does not move any DOM (the card is already on stage).
    function _focusMainSession(sid) {
        const s = sessions.get(sid);
        if (!s || s.slot !== 'main') return;
        if (activeSid && activeSid !== sid && sessions.has(activeSid)) {
            const old = sessions.get(activeSid);
            if (old.card) old.card.classList.remove('active');
        }
        activeSid = sid;
        s.lastFocusedAt = ++_focusCounter;
        if (s.card) s.card.classList.add('active');
        s.unread = false;
        _repaintActivityForActive();
        if (window.SessionSidebar) {
            window.SessionSidebar.setActiveSession(sid, s.name || sid);
        }
        // Defer the two forced-synchronous-layout operations (scrollIntoView +
        // composer focus) to the next frame. Run inline here — as they were —
        // each forces a full layout of a document full of backdrop-filter
        // surfaces, and they sandwich setActiveSession's own sidebar rebuild
        // (+ its getBoundingClientRect width recompute), so the switch path
        // did "mutate → forced layout → rebuild → forced layout" = layout
        // thrash (measured ~150-175ms of synchronous layout on the switch
        // path via long-animation-frame; NOT the View Transition — disabling
        // animations didn't change it). Batching both reads into one rAF, AFTER
        // every mutation above has committed, collapses that to a single layout
        // pass. The ~16ms delay before the composer takes focus is
        // imperceptible; the guard drops stale rAFs so a rapid A→B→A switch
        // only ever focuses the session that ended up active.
        requestAnimationFrame(() => {
            const cur = sessions.get(sid);
            if (!cur || activeSid !== sid || !cur.card) return;
            try { cur.card.scrollIntoView({ inline: 'nearest', block: 'nearest' }); }
            catch (_) { /* ignore */ }
            try { if (cur.composerInput) cur.composerInput.focus(); } catch (_) { /* ignore */ }
        });
        window.__handqLog('INFO', 'focusMainSession', { sid });
    }

    // Places a never-yet-placed session onto the main stage (free slot if
    // one exists, otherwise LRU-evicts a current main occupant to the rail)
    // and focuses it. Used for brand-new sessions the user explicitly
    // created — an explicit action is expected to grab focus.
    //
    // Single-slot enforcement: the main stage holds EXACTLY one session.
    // If someone else is currently in slot 0, they get evicted to the rail
    // FIRST (with mainSlots[0] cleared so _occupyMainSlot doesn't see a
    // stale reference), THEN the new session takes slot 0. Written this
    // explicitly (rather than routing through _findFreeMainSlot) so no
    // future refactor accidentally reintroduces "if there's a free slot
    // just use it" logic and revives the 2-cards-in-main layout.
    //
    // `originRect` (the "+New" button's on-screen rect, passed down from
    // createSession/switchSession) triggers the Genie entrance: the new
    // card grows from the button instead of the plain liquid-entrance
    // fade, and the evicted card (if any) shrinks toward its rail slot on
    // the same shared timeline instead of running its own independently-
    // timed keyframe. Omitted or under prefers-reduced-motion, this falls
    // straight back to the pre-existing CSS entrance/rail-width behavior.
    function _placeSession(sid, originRect) {
        const currentSid = mainSlots[0];
        const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        const doGenie = !!originRect && !reduced;
        const railWasEmpty = !stageRailList || stageRailList.children.length === 0;

        let evictedSid = null;
        let evictedOldRect = null;
        if (currentSid && currentSid !== sid) {
            const evictedS = sessions.get(currentSid);
            if (doGenie && evictedS && evictedS.card) {
                // Measure BEFORE the move — this is the card's real
                // on-stage rect, which _moveToRail is about to invalidate.
                evictedOldRect = evictedS.card.getBoundingClientRect();
                evictedSid = currentSid;
            }
            mainSlots[0] = null;
            _moveToRail(currentSid);
            if (doGenie && evictedSid) {
                const wrap = sessions.get(evictedSid)._railWrap;
                // _playGenieEntrance's own WAAPI transform replaces this
                // mount-time keyframe for this specific placement.
                if (wrap) wrap.style.animation = 'none';
            }
        }
        // Belt-and-suspenders: also flush any stray occupant of slot 1 (a
        // leftover from historical 2-slot placement that a stale in-memory
        // state might still carry after hot-reload during dev). Nothing
        // should ever put a session in slot 1 under the new code, but if
        // one is there, evict it too so the main stage is guaranteed to
        // hold exactly one card after this call returns.
        if (mainSlots[1] && mainSlots[1] !== sid) {
            const strayS1 = mainSlots[1];
            mainSlots[1] = null;
            _moveToRail(strayS1);
        }

        const newS = sessions.get(sid);
        // Only true when this placement is the one making the rail go
        // from empty to non-empty — that's the only case where the rail's
        // own width needs to animate at all.
        const railOpening = doGenie && evictedSid && railWasEmpty;
        if (doGenie && newS && newS.card) {
            // Genie's own WAAPI transform+opacity replaces liquid-entrance
            // for this specific placement.
            newS.card.style.animation = 'none';
        }
        if (railOpening && stageRail) {
            // Suspend the rail's CSS width transition — _playGenieEntrance
            // drives the same width change via Element.animate() on the
            // same shared timeline as the card transforms below, instead
            // of letting _updateLayout's data-empty flip trigger the CSS
            // transition on its own independent clock. Both must happen
            // BEFORE the animate() call below: .stage-rail[data-empty=
            // "true"]'s width:0 is !important and outranks a WAAPI
            // animation outright, and data-empty otherwise wouldn't flip
            // to "false" until _updateLayout runs at the end of this
            // function — after the animation has already started.
            stageRail.classList.add('genie-active');
            stageRail.dataset.empty = 'false';
        }

        _occupyMainSlot(0, sid);
        _focusMainSession(sid);

        if (doGenie && newS && newS.card) {
            _playGenieEntrance(newS.card, originRect, evictedSid, evictedOldRect, railOpening);
        }

        _updateLayout();
    }

    const GENIE_DURATION = 320;
    // WAAPI easing can't reference CSS custom properties — this literal
    // mirrors --ease-mac-pop (styles.css).
    const GENIE_EASING = 'cubic-bezier(0.16, 1, 0.3, 1)';
    // Must match .stage-rail's declared width in styles.css.
    const GENIE_RAIL_WIDTH = 160;

    // Orchestrates the Genie new-session entrance on one shared timeline:
    // the new card scales up from the "+New" button's screen position,
    // the evicted card (if any) shrinks toward its rail thumbnail, and the
    // rail itself widens if it was empty — all via Element.animate() calls
    // issued synchronously in this one function call, rather than each
    // running its own independently-timed CSS transition/keyframe (which
    // is what made the un-fixed +New flow read as 3-4 separate stutters
    // instead of one coherent motion).
    function _playGenieEntrance(newCard, originRect, evictedSid, evictedOldRect, railOpening) {
        const newRect = newCard.getBoundingClientRect();
        const dx = (originRect.left + originRect.width / 2) - (newRect.left + newRect.width / 2);
        const dy = (originRect.top + originRect.height / 2) - (newRect.top + newRect.height / 2);
        const newAnim = newCard.animate([
            { transform: `translate(${dx}px, ${dy}px) scale(0.06)`, opacity: 0 },
            { transform: 'translate(0, 0) scale(1)', opacity: 1 },
        ], { duration: GENIE_DURATION, easing: GENIE_EASING, fill: 'both' });
        newAnim.finished.catch(() => {}).then(() => { try { newAnim.cancel(); } catch (_) { /* ignore */ } });

        if (evictedSid && evictedOldRect) {
            const evictedS = sessions.get(evictedSid);
            const wrap = evictedS && evictedS._railWrap;
            if (wrap) {
                const wrapRect = wrap.getBoundingClientRect();
                const wdx = evictedOldRect.left - wrapRect.left;
                const wdy = evictedOldRect.top - wrapRect.top;
                const wsx = evictedOldRect.width / Math.max(1, wrapRect.width);
                const wsy = evictedOldRect.height / Math.max(1, wrapRect.height);
                const evictAnim = wrap.animate([
                    { transform: `translate(${wdx}px, ${wdy}px) scale(${wsx}, ${wsy})` },
                    { transform: 'none' },
                ], { duration: GENIE_DURATION, easing: GENIE_EASING, fill: 'both' });
                evictAnim.finished.catch(() => {}).then(() => { try { evictAnim.cancel(); } catch (_) { /* ignore */ } });
            }
        }

        if (railOpening && stageRail) {
            const railAnim = stageRail.animate([
                { width: '0px' },
                { width: GENIE_RAIL_WIDTH + 'px' },
            ], { duration: GENIE_DURATION, easing: GENIE_EASING, delay: 40, fill: 'both' });
            railAnim.finished.catch(() => {}).then(() => {
                try { railAnim.cancel(); } catch (_) { /* ignore */ }
                stageRail.classList.remove('genie-active');
            });
        }
    }

    // Same placement logic as _placeSession, but for a session that's
    // already mounted and may already be sitting in the rail — used for
    // background/lazy-mounted sessions (e.g. a scheduler-dispatched task)
    // that should become VISIBLE without stealing focus from whatever the
    // user is currently looking at. Only the very first session (nothing
    // active yet) is auto-focused, matching pre-Stage-Manager boot behavior.
    //
    // Single-slot enforcement: if main is already occupied, the NEW session
    // goes to the rail (does NOT evict the current occupant — passive
    // placement never steals the user's focused card). If main is empty,
    // the new session takes slot 0.
    function _placeSessionPassive(sid) {
        if (!mainSlots[0]) {
            _occupyMainSlot(0, sid);
            if (!activeSid) _focusMainSession(sid);
        } else {
            _moveToRail(sid);
        }
        _updateLayout();
    }

    // Promotes a rail thumbnail back onto the main stage (freeing a slot
    // via LRU eviction if both are full) and focuses it — the "click a
    // minimized session to bring it back" affordance. Wrapped in the same
    // View Transition helper session open/close uses (_runVT) so the card
    // morphs (position + size interpolate) from its rail thumbnail into
    // its main-stage slot instead of snapping there instantly — each
    // .session-card already carries a stable view-transition-name (set
    // once in _mountSession), so the browser can track it across the
    // reparent from .stage-rail-thumb back into #conversation.
    async function _promoteFromRail(sid) {
        const s = sessions.get(sid);
        if (!s || s.slot !== 'rail') return;
        await _runVT(() => {
            // Single-slot enforcement: same as _placeSession — if someone
            // else is in slot 0, swap them into the rail before this
            // session takes the main stage. Written explicitly (not routed
            // through _findFreeMainSlot) so the "one card in main" invariant
            // is visible at the call site.
            const currentSid = mainSlots[0];
            if (currentSid && currentSid !== sid) {
                mainSlots[0] = null;
                _moveToRail(currentSid);
            }
            _occupyMainSlot(0, sid);
            _focusMainSession(sid);
        });
        _updateLayout();
    }

    // Never leaves the stage with zero main occupants while a session still
    // exists in the rail — promotes whichever rail session was focused most
    // recently. No-op if a main occupant already exists or the rail is
    // empty too (that combination means every session is gone, which
    // callers handle separately by spawning a fresh one).
    function _ensureSomeMainOccupant() {
        if (mainSlots[0] || mainSlots[1]) return;
        let bestSid = null, bestAt = -1;
        for (const [sid, s] of sessions) {
            if (s.slot === 'rail' && s.lastFocusedAt > bestAt) {
                bestAt = s.lastFocusedAt;
                bestSid = sid;
            }
        }
        if (bestSid) _promoteFromRail(bestSid);
    }

    // Minimize: main → rail. Only reachable via the card's minimize button,
    // itself only shown when sessions.size > 1 (see _updateLayout), so a
    // rail session promoted by _ensureSomeMainOccupant below is always a
    // real, valid fallback candidate — minimizing can never actually empty
    // the stage while other sessions exist. Wrapped in _runVT for the same
    // morph animation as _promoteFromRail above.
    async function _minimizeSession(sid) {
        const s = sessions.get(sid);
        if (!s || s.slot !== 'main') return;
        await _runVT(() => {
            mainSlots[s.mainIndex] = null;
            _moveToRail(sid);
            if (activeSid === sid) {
                activeSid = null;
                const otherSid = mainSlots[0] || mainSlots[1];
                if (otherSid) _focusMainSession(otherSid);
                else _ensureSomeMainOccupant();
            }
        });
        _updateLayout();
        window.__handqLog('INFO', 'minimizeSession', { sid });
    }

    // Maximize: sid becomes the SOLE main occupant — any other main
    // occupant (and sid itself, if it was in the rail) gets reshuffled so
    // sid ends up alone in slot 0 and slot 1 is empty. Wrapped in _runVT so
    // every card that moves (evicted sibling → rail, sid → main) morphs in
    // one cohesive transition rather than snapping.
    async function _maximizeSession(sid) {
        const s = sessions.get(sid);
        if (!s) return;
        await _runVT(() => {
            for (let i = 0; i < mainSlots.length; i++) {
                const occSid = mainSlots[i];
                if (occSid && occSid !== sid) {
                    mainSlots[i] = null;
                    _moveToRail(occSid);
                }
            }
            mainSlots[0] = null;
            mainSlots[1] = null;
            _occupyMainSlot(0, sid);
            _focusMainSession(sid);
        });
        _updateLayout();
        window.__handqLog('INFO', 'maximizeSession', { sid });
    }

    function _repaintActivityForActive() {
        // No-op — per-session activity is rendered inline in each pane.
    }

    async function closeSession(sid) {
        if (!sessions.has(sid)) return;
        closedSessions.add(sid);
        window.__handqLog('INFO', 'closeSession', { sid });
        // Drop the sidebar's per-session state before the pane is unmounted
        // (does nothing visible if the session wasn't active).
        if (window.SessionSidebar) window.SessionSidebar.notifySessionClosed(sid);
        // Finalize any tool cards still spinning before the session's data is
        // dropped below — nothing will ever deliver their matching result now.
        _forceFinalizePendingActivity(sid);
        // Kill the floating pop-out editor if it was pointed at this session
        // — its backing textarea is about to be removed from the DOM.
        try { _FloatingComposer.destroyFor(sid); } catch (_) { /* ignore */ }
        // v6 fix: tell remote-control.js this sid is gone so its own
        // remoteSessions bookkeeping (local tab ↔ rc-xxx) drops the entry.
        // Without this, a stale entry survives the tab close and the Connect
        // panel's session chip ▶ later finds it, calls switchSession on a
        // dead sid (silent no-op), and re-adopt looks broken — see
        // remote-control.js's notifyLocalTabClosed for the full explanation.
        if (window.HandQRemote && window.HandQRemote.notifyLocalTabClosed) {
            window.HandQRemote.notifyLocalTabClosed(sid);
        }
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
            if (s) {
                if (s.composerInput) _MentionAutocomplete.detach(s.composerInput);
                if (s.slot === 'main' && s.mainIndex >= 0) mainSlots[s.mainIndex] = null;
                // Whichever node is actually the card's current DOM parent —
                // the rail wrapper if minimized, the card itself if on stage.
                const node = s._railWrap || s.card;
                if (node && node.parentNode) node.parentNode.removeChild(node);
            }
            sessions.delete(sid);
            if (sessions.size === 0) {
                // Last session closed — auto-spawn a fresh one so the
                // composer always has a session_id to ride on.
                createSession();
                return;
            }
            if (activeSid === sid) {
                activeSid = null;
                const otherSid = mainSlots[0] || mainSlots[1];
                if (otherSid) _focusMainSession(otherSid);
                // A freed main slot is deliberately NOT auto-refilled from
                // the rail here (closing is an explicit reduction, not a
                // request for HandQ to pick a replacement) — UNLESS doing
                // so is the only way to avoid leaving the stage with zero
                // main occupants while sessions still exist in the rail.
                else _ensureSomeMainOccupant();
            }
            _updateLayout();
        });
    }

    // Rail's rendered width while visible. Must match .stage-rail's `width`
    // in styles.css, which is in turn coupled to main.js's
    // AUTO_RESIZE_RAIL_DELTA — see the comment on that CSS rule for the three
    // values that have to move together.
    const RAIL_WIDTH_PX = 160;

    function _chatRegionW() {
        const el = document.getElementById('chat-region');
        return el ? el.getBoundingClientRect().width : 0;
    }

    // "How wide can the rail be right now while the chat region keeps the
    // width cardW?" Mirror image of session-sidebar.js's _rawDriven. Any
    // constant this misses is absorbed by the drive's start-up calibration,
    // so it only has to be right about the terms that MOVE.
    function _railRawDriven(cardW) {
        const mainEl = document.querySelector('.main');
        if (!mainEl) return 0;
        const sb = document.getElementById('session-sidebar');
        let sbExtras = 0;
        if (sb) {
            const w = sb.getBoundingClientRect().width;
            if (w > 0) sbExtras = w + 10;   // sidebar + its margin-right
        }
        return mainEl.getBoundingClientRect().width - sbExtras - cardW - 10;
    }

    function _startRailReveal(startW, finalW, cardW) {
        if (!stageRail || !window.HandQLayoutDrive) return;
        window.HandQLayoutDrive.start({
            el: stageRail,
            prop: '--rail-driven-w',
            cls: 'rail-driving',
            startW: startW,
            finalW: finalW,
            solve: () => _railRawDriven(cardW),
            // Nothing to pin at the end: the rail's settled width comes
            // straight from CSS (160px, or 0 via [data-empty="true"]), unlike
            // the sidebar whose width is dynamic and lives in an inline style.
        });
    }

    function _updateLayout() {
        // Rail collapses to zero width when nothing is minimized — same
        // "no dead stub" idiom as .session-sidebar[data-collapsed].
        // Immediate DOM sync — attributes and class toggles must be
        // current-tick so the DOM is consistent for the next paint. Only
        // the window-resize IPC is deferred (see below).
        const railEmpty = !stageRailList || stageRailList.children.length === 0;
        if (stageRail) {
            const wasEmpty = stageRail.dataset.empty === 'true';
            const nextEmpty = !!railEmpty;
            if (wasEmpty === nextEmpty) {
                stageRail.dataset.empty = nextEmpty ? 'true' : 'false';
            } else {
                // Real toggle: measure the pre-flip geometry, then hand the
                // width to layout-drive.js for the duration of the window
                // resize this same _updateLayout is about to request. Without
                // it the rail animates on its own clock while the window
                // animates on DWM's, and the flex:1 chat region in between
                // absorbs the difference — ~30px of bulge-then-settle on a
                // card whose width a rail toggle shouldn't change at all.
                const startW = stageRail.getBoundingClientRect().width;
                const cardW0 = _chatRegionW();
                stageRail.dataset.empty = nextEmpty ? 'true' : 'false';
                _startRailReveal(startW, nextEmpty ? 0 : RAIL_WIDTH_PX, cardW0);
            }
        }

        // Slide the titlebar window-control buttons right by the rail's
        // settled width whenever it's open, so they sit over the chat
        // card's corner instead of the app's — see .titlebar-controls in
        // styles.css for the CSS transition that actually animates this.
        document.documentElement.style.setProperty(
            '--tb-controls-shift', railEmpty ? '0px' : RAIL_WIDTH_PX + 'px');

        // Minimize/maximize only make sense once >1 session exists at all
        // — the literal condition the user specified — independent of how
        // many of them currently happen to be in main vs rail.
        const showControls = sessions.size > 1;
        for (const [, s] of sessions) {
            if (s.minBtn) s.minBtn.classList.toggle('hidden', !showControls);
            if (s.maxBtn) s.maxBtn.classList.toggle('hidden', !showControls);
        }

        // Defer the auto-resize IPC to next animation frame. Multiple
        // triggers in the same tick (session move + sidebar toggle +
        // active-focus change all firing on one user action) would each
        // dispatch a separate window-resize IPC, and main.js would apply
        // intermediates before landing on the settled state. Coalescing
        // to one IPC per frame lets main see the final state directly.
        _scheduleAutoResizeIpc();
    }

    // Ask main.js to grow / shrink the window to fit the CURRENT visible
    // layout: baseline main-card, plus rail if any minimized session,
    // plus sidebar if the detail panel is expanded. rAF-batched (see
    // _updateLayout) and payload-deduplicated (skip if the last IPC we
    // sent was identical to what we'd send now — many _updateLayout
    // triggers change DOM state but NOT window-layout inputs, e.g.
    // toggling a min/max button's hidden class doesn't affect any of
    // {sessions, sidebarOpen, sidebarWidth, railOpen}).
    let _autoResizeRafId = 0;
    let _lastAutoResizeKey = null;
    function _scheduleAutoResizeIpc() {
        if (_autoResizeRafId) return;
        _autoResizeRafId = requestAnimationFrame(() => {
            _autoResizeRafId = 0;
            _sendAutoResizeIpc();
        });
    }
    function _sendAutoResizeIpc() {
        // Read layout state directly here (not passed in) — the rAF gap
        // between "_updateLayout called" and "IPC actually sent" means
        // the state may have moved on again; reading fresh at send time
        // means we send the SETTLED state, not a stale intermediate.
        const railEmpty2 = !stageRailList || stageRailList.children.length === 0;
        const totalSessions = sessions.size;
        const sidebarEl = document.getElementById('session-sidebar');
        const sidebarOpen = !!(sidebarEl && sidebarEl.getAttribute('data-collapsed') !== 'true');
        // Sidebar width is now dynamic (see session-sidebar.js's
        // SIDEBAR_TO_CARD_RATIO / user drag pin), so main.js can't use a
        // fixed AUTO_RESIZE_SIDEBAR_DELTA anymore — send the actual
        // measured pixel width and let main add the 10px margin.
        //
        // Read `sidebarEl.style.width` (the TARGET set by
        // _refreshSidebarWidth), NOT `getBoundingClientRect().width` (the
        // currently-animating value). This IPC is rAF-batched, so by the
        // time it fires the sidebar's reveal is already in flight and
        // getBoundingClientRect returns a mid-animation width (say ~100px
        // into a 0→512 reveal), which would make main.js grow the window by
        // only that intermediate amount, leaving chat-card squeezed and the
        // 4:2.5 ratio broken. Inline style.width stays authoritative for the
        // whole reveal: _setCollapsed's open path writes it synchronously
        // before starting the drive, and session-sidebar.js's _startReveal /
        // _driveReveal animate through a --ss-driven-w custom property
        // instead of touching it. Fall back to getBoundingClientRect only if
        // no inline width is set (should not happen with the current
        // session-sidebar.js, but keeps rare paths safe).
        // When the sidebar is closed it's out of layout (width:0 via
        // CSS), so 0 is what we report; main.js falls back to the legacy
        // delta only if sidebarOpen is true AND sidebarWidth is
        // 0/missing.
        let sidebarWidth = 0;
        if (sidebarOpen && sidebarEl) {
            const inlineW = parseFloat(sidebarEl.style.width);
            sidebarWidth = Number.isFinite(inlineW) && inlineW > 0
                ? inlineW
                : sidebarEl.getBoundingClientRect().width;
        }
        const railOpen = !railEmpty2;

        // Payload dedup key — round sidebarWidth to the nearest px so
        // sub-px jitter (from fractional flex distribution during window
        // drag) doesn't defeat the cache. Everything else is discrete.
        const key = totalSessions + '|' + (sidebarOpen ? 1 : 0) + '|' +
                    Math.round(sidebarWidth) + '|' + (railOpen ? 1 : 0);
        if (key === _lastAutoResizeKey) return;
        _lastAutoResizeKey = key;

        if (window.windowControls && typeof window.windowControls.autoResize === 'function') {
            try {
                window.windowControls.autoResize({
                    sessions: Math.max(1, totalSessions),
                    sidebarOpen: sidebarOpen,
                    sidebarWidth: sidebarWidth,
                    railOpen: railOpen,
                });
            } catch (_) { /* ignore */ }
        }
    }

    // Re-layout the window whenever the sidebar toggles open/closed. This
    // is the signal that lets the window grow when the sidebar opens for
    // the first time — without it, the sidebar would eat into the main
    // card's width instead of pushing the window wider from the baseline.
    // Fired from session-sidebar.js's _setCollapsed via a CustomEvent
    // (decoupled — no direct import).
    window.addEventListener('session-sidebar-toggle', () => {
        // Fires immediately, including in perf-mode. This used to be delayed
        // by 370ms under perf-mode so the native window resize wouldn't
        // overlap the sidebar's own CSS width transition. That reasoning is
        // obsolete: the sidebar no longer animates its width independently
        // — session-sidebar.js's _driveReveal DERIVES the sidebar's width
        // from the window's actual width every frame, precisely so the two
        // can't drift apart. Delaying the resize now starves the drive of
        // the signal it follows: it would see a motionless window, sit at
        // its start width until the 700ms bail-out, and only then would the
        // window jump — squeezing the chat card exactly as before. The
        // resize must lead, not trail.
        try { _updateLayout(); } catch (_) { /* ignore */ }
    });

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
            _MentionAutocomplete.attach(textareaEl);

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

    // ----- @-mention path autocomplete -----------------------------------
    // Attaches to any composer textarea (session card + floating variant).
    // On input, detects an @-token before the cursor and queries the main
    // process's PowerShell worker via window.handq.searchPaths. The dropdown
    // sits under the textarea. Enter/Tab inserts; Esc cancels; ↑↓ navigates.
    // Paths containing whitespace are auto-wrapped in double quotes so the
    // backend preprocess_mentions (normalize_at_quoted) parses them as one
    // mention.
    const _MentionAutocomplete = (function () {
        const DEBOUNCE_MS = 200;
        const MIN_LEN = 2;
        const MAX_LEN = 50;

        // Backend lookbehind (src/controller_v2/mention_preprocessing.py):
        // '@' must not be preceded by [\w/.] — avoids emails, decorators,
        // /@paths. Quoted form allows whitespace inside the @-token; bare
        // form does not.
        const QUOTED_RE = /(?:^|[^\w/.])@"([^"]*)$/;
        const BARE_RE   = /(?:^|[^\w/.])@([^\s"]*)$/;

        let dropdownEl = null;
        let itemsEl = null;
        const attached = new Set();

        let activeTextarea = null;
        let currentResults = [];
        let selectedIndex = 0;
        let currentAtStart = -1;
        let debounceTimer = null;
        let latestQueryId = 0;

        function _ensureDropdown() {
            if (dropdownEl) return dropdownEl;
            dropdownEl = document.createElement('div');
            dropdownEl.className = 'mention-dropdown hidden';
            itemsEl = document.createElement('div');
            dropdownEl.appendChild(itemsEl);
            document.body.appendChild(dropdownEl);
            // Keep focus on the textarea when clicking a candidate.
            dropdownEl.addEventListener('mousedown', (ev) => ev.preventDefault());
            return dropdownEl;
        }

        function _detectMention(textarea) {
            const cursor = textarea.selectionStart;
            const before = textarea.value.slice(0, cursor);
            const mQuoted = before.match(QUOTED_RE);
            if (mQuoted) {
                const atOffset = mQuoted[0].indexOf('@');
                return { query: mQuoted[1], atStart: mQuoted.index + atOffset };
            }
            const mBare = before.match(BARE_RE);
            if (mBare) {
                const atOffset = mBare[0].indexOf('@');
                return { query: mBare[1], atStart: mBare.index + atOffset };
            }
            return null;
        }

        function _renderItems() {
            itemsEl.innerHTML = '';
            currentResults.forEach((r, idx) => {
                const item = document.createElement('div');
                item.className = 'mention-item' + (idx === selectedIndex ? ' selected' : '');

                const icon = document.createElement('span');
                icon.className = 'mention-item-icon';
                icon.innerHTML = _pathIconSvg(r.isDir);
                item.appendChild(icon);

                const name = document.createElement('span');
                name.className = 'mention-item-name';
                name.textContent = r.name || (r.path || '').split(/[\\/]/).pop() || '(unnamed)';
                item.appendChild(name);

                if (r.parent) {
                    const parent = document.createElement('span');
                    parent.className = 'mention-item-parent';
                    parent.textContent = r.parent;
                    item.appendChild(parent);
                }

                item.addEventListener('click', () => {
                    selectedIndex = idx;
                    _insert();
                });
                itemsEl.appendChild(item);
            });
        }

        function _positionDropdown(textarea) {
            const rect = textarea.getBoundingClientRect();
            const margin = 8;
            const width = Math.max(320, Math.min(rect.width, 560));

            // Horizontal: prefer aligned to textarea left edge, but clamp so the
            // right edge doesn't spill outside the window (Electron/Chromium
            // won't render page content past the window border).
            let left = rect.left;
            if (left + width > window.innerWidth - margin) {
                left = window.innerWidth - width - margin;
            }
            if (left < margin) left = margin;

            // Vertical: flip above the textarea when there isn't enough room
            // below. Height is only measurable while the element has layout,
            // so _show() reveals the element (visibility:hidden) before calling
            // us — offsetHeight is then valid.
            const height = dropdownEl.offsetHeight || 320;
            const spaceBelow = window.innerHeight - rect.bottom - margin;
            const spaceAbove = rect.top - margin;
            let top;
            if (spaceBelow >= height + 4) {
                top = rect.bottom + 4;
            } else if (spaceAbove > spaceBelow) {
                top = Math.max(margin, rect.top - height - 4);
            } else {
                // Neither side comfortable — anchor below and let CSS max-height
                // + overflow-y handle any clipping.
                top = rect.bottom + 4;
            }

            dropdownEl.style.left = Math.round(left) + 'px';
            dropdownEl.style.top = Math.round(top) + 'px';
            dropdownEl.style.width = width + 'px';
        }

        function _show(textarea) {
            _ensureDropdown();
            _renderItems();
            // Reveal with visibility:hidden first so offsetHeight is measurable
            // (display:none via .hidden zeroes it), position, then reveal for
            // real. Avoids a one-frame flash at the previous position.
            dropdownEl.style.visibility = 'hidden';
            dropdownEl.classList.remove('hidden');
            _positionDropdown(textarea);
            dropdownEl.style.visibility = '';
        }

        function _hide() {
            if (dropdownEl) dropdownEl.classList.add('hidden');
            activeTextarea = null;
            currentResults = [];
            selectedIndex = 0;
            currentAtStart = -1;
            latestQueryId += 1;   // invalidate any in-flight response
        }

        function _insert() {
            if (!activeTextarea || !currentResults[selectedIndex]) return _hide();
            const path = currentResults[selectedIndex].path || '';
            const t = activeTextarea;
            const cursor = t.selectionStart;
            const before = t.value.slice(0, cursor);
            const after = t.value.slice(cursor);
            if (currentAtStart < 0) return _hide();
            const hasSpace = /\s/.test(path);
            const token = hasSpace ? '@"' + path + '"' : '@' + path;
            t.value = before.slice(0, currentAtStart) + token + ' ' + after;
            const pos = currentAtStart + token.length + 1;
            t.setSelectionRange(pos, pos);
            // Mirror into the floating composer (if this was the source).
            t.dispatchEvent(new Event('input', { bubbles: true }));
            _hide();
        }

        async function _runQuery(textarea, detection) {
            const query = detection.query;
            const myId = ++latestQueryId;
            activeTextarea = textarea;
            currentAtStart = detection.atStart;

            // UNC branch: fs.readdir over the parent path. SystemIndex ignores
            // network shares, so a bare LIKE query would always return empty.
            // Wait until the user has typed at least "\\host\share\" (i.e.,
            // parent is listable) — otherwise stay silent.
            if (query.startsWith('\\\\')) {
                const parsed = _parseUncQuery(query);
                if (!parsed) {
                    _hide();
                    return;
                }
                let resp;
                try {
                    resp = await window.handq.listDirectory(parsed.parent, parsed.filter);
                } catch (_) {
                    resp = { results: [] };
                }
                if (myId !== latestQueryId) return;
                if (activeTextarea !== textarea) return;
                currentResults = (resp && resp.results) || [];
                selectedIndex = 0;
                if (!currentResults.length) {
                    _hide();
                    return;
                }
                _show(textarea);
                return;
            }

            // SystemIndex branch — min length applies here to avoid firing on
            // single-char queries that match a huge slice of the index.
            if (query.length < MIN_LEN || query.length > MAX_LEN) {
                _hide();
                return;
            }

            let resp;
            try {
                resp = await window.handq.searchPaths(query);
            } catch (_) {
                resp = { results: [] };
            }
            if (myId !== latestQueryId) return;         // superseded
            if (activeTextarea !== textarea) return;    // focus moved

            currentResults = (resp && resp.results) || [];
            selectedIndex = 0;
            if (!currentResults.length) {
                _hide();
                return;
            }
            _show(textarea);
        }

        function _parseUncQuery(query) {
            // Expects query starts with "\\". Splits into { parent, filter }
            // where parent is a listable path (\\host\share or deeper).
            // Returns null when parent isn't listable yet — the user is still
            // typing host or share.
            if (!query.startsWith('\\\\')) return null;
            const lastBs = query.lastIndexOf('\\');
            const parent = query.slice(0, lastBs);
            const filter = query.slice(lastBs + 1);
            if (!parent.startsWith('\\\\')) return null;
            // After the "\\" prefix the parent must contain at least one more
            // backslash ("host\share" pattern) to be a listable UNC path.
            if (!parent.slice(2).includes('\\')) return null;
            return { parent, filter };
        }

        function _onInput(ev) {
            const t = ev.currentTarget;
            const detection = _detectMention(t);
            if (!detection) {
                _hide();
                return;
            }
            clearTimeout(debounceTimer);
            debounceTimer = setTimeout(() => _runQuery(t, detection), DEBOUNCE_MS);
        }

        function _onKeydown(ev) {
            if (!dropdownEl || dropdownEl.classList.contains('hidden')) return;
            if (activeTextarea !== ev.currentTarget) return;
            if (ev.key === 'ArrowDown') {
                ev.preventDefault(); ev.stopPropagation();
                selectedIndex = (selectedIndex + 1) % currentResults.length;
                _renderItems();
            } else if (ev.key === 'ArrowUp') {
                ev.preventDefault(); ev.stopPropagation();
                selectedIndex = (selectedIndex - 1 + currentResults.length) % currentResults.length;
                _renderItems();
            } else if (ev.key === 'Enter' && !ev.ctrlKey) {
                // Ctrl+Enter falls through to the composer's submit handler.
                ev.preventDefault(); ev.stopPropagation();
                _insert();
            } else if (ev.key === 'Tab') {
                ev.preventDefault(); ev.stopPropagation();
                _insert();
            } else if (ev.key === 'Escape') {
                ev.preventDefault(); ev.stopPropagation();
                _hide();
            }
        }

        function _onBlur(ev) {
            // Small delay so a candidate click registers first.
            setTimeout(() => {
                if (activeTextarea === ev.currentTarget
                    && document.activeElement !== ev.currentTarget) {
                    _hide();
                }
            }, 120);
        }

        function attach(textarea) {
            if (!textarea || attached.has(textarea)) return;
            attached.add(textarea);
            // capture:true so we intercept Enter/Tab before the composer's
            // own submit/focus handlers.
            textarea.addEventListener('keydown', _onKeydown, true);
            textarea.addEventListener('input', _onInput);
            textarea.addEventListener('blur', _onBlur);
        }

        function detach(textarea) {
            if (!textarea || !attached.has(textarea)) return;
            attached.delete(textarea);
            textarea.removeEventListener('keydown', _onKeydown, true);
            textarea.removeEventListener('input', _onInput);
            textarea.removeEventListener('blur', _onBlur);
            if (activeTextarea === textarea) _hide();
        }

        return { attach, detach };
    })();

    // ----- end multi-session state ---------------------------------------

    // Session straggler filter. gateGen() drops envelopes that arrive for
    // a session we've already closed (its session bucket is gone) or that
    // target a session_id we don't recognise. This protects a fresh tab
    // from a wedged old subtask whose blocking syscall prevents Python
    // from killing the OS thread on Windows.

    // Thinking bubble shown in chat while the coordinator prepares a reply.
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
        // A session_id we've never seen (e.g. a scheduler-dispatched
        // "sched-xxx" fire) must NOT be dropped here — onStatus's lazy-mount
        // path (below) is what creates its tab. Dropping it here means the
        // event never reaches _mountSession and no card ever appears.
        // activeSid-fallback events (no session_id on the envelope at all)
        // still gate on "do we have an active session" as before.
        if (!evt.session_id) {
            const sid = _resolveSid(evt);
            if (sid && !sessions.has(sid)) return true;
        }
        return false;
    }

    function truncate(s, n) {
        if (!s) return '';
        return s.length > n ? s.slice(0, n - 1) + '…' : s;
    }

    // Whether the agent has a real task in flight, for the dispatch
    // session. Used to gate coordinator-side pill updates so a chat reply
    // mid-task can't reset that session's pill to "idle". `sessionState` is set
    // by state_changed events (V2 emits thinking / executing / idle);
    // `activeExecCount` covers the brief window where a tool is running
    // before the next state_changed lands. Callers run inside _onStatusBody, so
    // _dispatchSession() resolves the session the in-flight event belongs to.
    function isTaskRunning() {
        const s = _dispatchSession();
        if (!s) return false;
        return s.sessionState === 'thinking'
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

    // Folder / file glyph shared by the @-mention dropdown and rendered chips.
    const _FOLDER_ICON_SVG =
        '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2 4.5a1 1 0 0 1 1-1h3.4l1.2 1.4H13a1 1 0 0 1 1 1v6.6a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V4.5Z" stroke="currentColor" stroke-width="1.2" fill="none" stroke-linejoin="round"/></svg>';
    const _FILE_ICON_SVG =
        '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4.5 2h5l2.5 2.5v8a1 1 0 0 1-1 1h-6.5a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1Z" stroke="currentColor" stroke-width="1.2" fill="none" stroke-linejoin="round"/><path d="M9.5 2v2.5H12" stroke="currentColor" stroke-width="1.2" fill="none" stroke-linejoin="round"/></svg>';
    function _pathIconSvg(isDir) {
        return isDir ? _FOLDER_ICON_SVG : _FILE_ICON_SVG;
    }


    // @-path mention chip — recognises three forms in already-HTML-escaped
    // text (as emitted by escapeHtml above):
    //   - quoted:      @&quot;<any-non-&-non-quote>&quot;
    //   - drive path:  @C:\... or @C:/...
    //   - UNC \\:      @\\host\share\...
    //   - UNC //:      @//host/share/...   (post-normalisation form)
    // Backend lookbehind is mirrored: '@' must not follow [\w/.] so we don't
    // touch emails, decorators, or /@paths. The bare-path body is a whitelist
    // (letters, digits, . _ - / \, plus CJK U+4E00–U+9FFF for Chinese file
    // names) — anything outside terminates the match, so trailing Chinese/
    // Western punctuation like '。' or ',' does not get swallowed.
    const _AT_PATH_RE =
        /(?<![\w/.])@(?:&quot;([^&\n"]+?)&quot;|([A-Za-z]:[\\/][A-Za-z0-9._\-\\/一-鿿]+|\\\\[A-Za-z0-9._\-一-鿿]+\\[A-Za-z0-9._\-\\/一-鿿]+|\/\/[A-Za-z0-9._\-一-鿿]+\/[A-Za-z0-9._\-\\/一-鿿]+))/g;

    function _renderMentionChip(rawPath) {
        // rawPath is captured from already-escaped text and does not carry
        // HTML metacharacters (they can't survive the source escape without
        // being '&…;', and our regex bails on '&'). Re-escape anyway before
        // embedding into HTML attributes / text content.
        const path = String(rawPath || '');
        const isDir = /[\\/]$/.test(path);
        const stripped = path.replace(/[\\/]+$/, '');
        const sepIdx = Math.max(stripped.lastIndexOf('\\'), stripped.lastIndexOf('/'));
        const rawLabel = sepIdx >= 0 ? stripped.slice(sepIdx + 1) : stripped;
        const label = rawLabel || path;
        const icon = _pathIconSvg(isDir);
        const safePath = escapeHtml(path);
        const safeLabel = escapeHtml(label);
        return '<span class="mention-chip" data-path="' + safePath + '" title="' + safePath + '">' +
               '<span class="mention-chip-icon" aria-hidden="true">' + icon + '</span>' +
               '<span class="mention-chip-label">' + safeLabel + '</span>' +
               '</span>';
    }

    function renderMarkdownInline(s) {
        // Operates on already-HTML-escaped text. Inline code is captured
        // first so its content is shielded from emphasis processing.
        const codeSpans = [];
        s = s.replace(/`([^`\n]+)`/g, (_, p) => {
            codeSpans.push(p);
            return _MD_INLINE + (codeSpans.length - 1) + _MD_INLINE;
        });

        // @-path mentions — extract BEFORE markdown syntax so characters like
        // '_' or '*' inside a path don't get eaten by emphasis matching.
        // Restored to HTML at the end alongside the code-span restore.
        const chipTokens = [];
        s = s.replace(_AT_PATH_RE, (_m, quoted, bare) => {
            chipTokens.push(quoted !== undefined ? quoted : bare);
            return _MD_INLINE + 'CHIP' + (chipTokens.length - 1) + _MD_INLINE;
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

        // Restore mention chips.
        s = s.replace(new RegExp(_MD_INLINE + 'CHIP(\\d+)' + _MD_INLINE, 'g'),
            (_, i) => _renderMentionChip(chipTokens[+i]));

        return s;
    }

    // Lightweight formatter for short, mostly-single-paragraph strings that
    // never went through renderMarkdown: system/error/notice bubbles and the
    // right-hand activity feed. These carry free-form LLM text (decision
    // reasoning, notify_user messages, tool result summaries) that can
    // contain **bold** or literal newlines, but block constructs (headings,
    // lists, tables) don't apply here — inline formatting plus explicit
    // <br> for line breaks is enough, and cheaper than the full block parser.
    function renderInlineHtml(text) {
        return renderMarkdownInline(escapeHtml(text || '')).replace(/\n/g, '<br>');
    }
    // Exposed for session-sidebar.js (loads before this file, but only
    // calls in at event time, well after both scripts have finished
    // executing) so the activity feed formats the same way chat bubbles do.
    window.HandQFormat = { renderInline: renderInlineHtml };

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

    // ----- Activity feed (right-hand detail panel) ------------------------
    //
    // Activity used to render inline as collapsible <details> groups
    // between chat bubbles in each session's own pane. It now lives
    // exclusively in the right-hand detail panel (session-sidebar.js's
    // flat activity list) — these are thin wrappers that resolve the
    // in-flight dispatch session's sid and forward to SessionSidebar, so
    // every existing call site below (pushActivity('⚠', ...), etc.) keeps
    // working unchanged.

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

    function pushActivity(icon, label, content, opts) {
        const s = _dispatchSession();
        if (!s || !window.SessionSidebar) return null;
        return window.SessionSidebar.pushActivity(s.sid, {
            icon, label, content,
            iter: opts && opts.iter,
            tool: opts && opts.tool,
            pending: opts && opts.pending,
        });
    }

    function updateActivityResult(iter, tool, resultIcon, headLabel, resultContent) {
        const s = _dispatchSession();
        if (!s || !window.SessionSidebar) return;
        window.SessionSidebar.updateActivityResult(s.sid, iter, tool, resultIcon, headLabel, resultContent);
    }

    function clearActivity() {
        // Reset this session's activity history. Called from scNew (the
        // "reset current tab" shortcut).
        const s = _dispatchSession() || active();
        if (!s || !window.SessionSidebar) return;
        window.SessionSidebar.clearActivity(s.sid);
    }

    function _forceFinalizePendingActivity(sid) {
        // Defensive cleanup for activity entries left stuck in the
        // "running" spinner state (pending:true) because the task ended
        // abnormally (bridge crash, fatal error, session closed) before the
        // matching tool-result event arrived to flip them via
        // updateActivityResult. Takes a sid (not a session bucket) since
        // the detail panel's activity state now lives in session-sidebar.js,
        // not on the session bucket.
        if (!sid || !window.SessionSidebar) return;
        window.SessionSidebar.forceFinalizePendingActivity(sid);
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
        pane.appendChild(bubble);
        scrollToBottom();
    }

    function _findSafeCommitBoundary(lines, fromLine) {
        // Returns how many LEADING LINES (from the start of `lines`, i.e. an
        // absolute index) are guaranteed final — no text appended after this
        // point could ever change how those lines render. Scans only from
        // `fromLine` onward: every prior call already established that
        // `fromLine` is itself a safe boundary, and a safe boundary is by
        // definition never inside an open fence (see below) — so resuming
        // the scan from there with inFence=false is always correct and
        // avoids re-walking the whole accumulated text every frame.
        //
        // A line index is a safe cut point when the line is blank AND we are
        // not inside an unclosed fenced code block at that point. Every
        // block-continuation loop in renderMarkdown (list/table/blockquote/
        // paragraph) stops at a blank line and never looks past it, so
        // content up to and including a blank line can never be retroactively
        // reinterpreted by later text — UNLESS that blank line is actually
        // inside an open ``` fence (fences intentionally preserve blank
        // lines as code content, and a fence's own closing ``` might not
        // have arrived yet).
        let inFence = false;
        let safeLineCount = fromLine;
        for (let idx = fromLine; idx < lines.length; idx++) {
            const line = lines[idx];
            if (/^```/.test(line)) {
                inFence = !inFence;
                continue; // the fence delimiter line itself is never a cut point
            }
            if (!inFence && line.trim() === '') {
                safeLineCount = idx + 1;
            }
        }
        return safeLineCount;
    }

    function scheduleMarkdownRender(span) {
        if (span._renderPending) return;
        span._renderPending = true;
        requestAnimationFrame(() => {
            span._renderPending = false;
            try {
                const text = span._rawText || '';
                const lines = text.split('\n');
                const committedLineCount = span._committedLineCount || 0;
                const safeLineCount = _findSafeCommitBoundary(lines, committedLineCount);
                if (safeLineCount > committedLineCount) {
                    // Newly-finalized lines since the last commit: render just
                    // that slice and append its HTML to the committed prefix
                    // instead of re-parsing everything accumulated so far.
                    const newlyCommitted = lines.slice(committedLineCount, safeLineCount);
                    span._committedHtml = (span._committedHtml || '') +
                        renderMarkdown(newlyCommitted.join('\n'));
                    span._committedLineCount = safeLineCount;
                }
                // The tail — content after the last safe boundary — is the
                // only part still re-parsed every frame. Its size is bounded
                // by the length of the last in-progress block (typically one
                // paragraph/list/table), not the whole accumulated reply.
                const tailLines = lines.slice(span._committedLineCount || 0);
                span.innerHTML = (span._committedHtml || '') + renderMarkdown(tailLines.join('\n'));
            } catch (err) {
                // Fallback to plain text if the parser ever throws — never
                // strand a streaming bubble blank.
                span.textContent = span._rawText || '';
            }
        });
    }

    function addAssistantTextBubble(text) {
        // Single-shot non-streaming assistant message (e.g. coordinator reply,
        // task completion summary). Markdown-render the body too.
        const bubble = el('div', 'bubble assistant');
        const body = el('div', 'bubble-body');
        const span = el('div', 'text-stream md-rendered');
        try { span.innerHTML = renderMarkdown(text || ''); }
        catch (_) { span.textContent = text || ''; }
        body.appendChild(span);
        bubble.appendChild(body);
        const pane = _dispatchPane();
        pane.appendChild(bubble);
        scrollToBottom();
    }

    // Streaming coordinator reply — incremental markdown render per delta.
    // The "currently streaming" bubble is tracked PER SESSION via the
    // session bucket's `activeCoordinatorBubble` field; this allows two
    // concurrent sessions to each have their own streaming bubble in-flight.

    function appendCoordinatorDelta(text) {
        if (!text) return;
        const s = _dispatchSession();
        if (!s) return;
        if (!s.activeCoordinatorBubble) {
            s.activeCoordinatorBubble = el('div', 'bubble assistant streaming');
            const body = el('div', 'bubble-body');
            s.activeCoordinatorBubble.appendChild(body);
            s.activeCoordinatorBubble._body = body;
            s.activeCoordinatorBubble._currentTextSpan = null;
            const pane = s.pane || conversation;
            // New assistant turn starts here.
            pane.appendChild(s.activeCoordinatorBubble);
        }
        var span = s.activeCoordinatorBubble._currentTextSpan;
        if (!span) {
            span = el('div', 'text-stream md-rendered');
            span._rawText = '';
            s.activeCoordinatorBubble._body.appendChild(span);
            s.activeCoordinatorBubble._currentTextSpan = span;
        }
        span._rawText += text;
        scheduleMarkdownRender(span);
        scrollToBottom();
    }

    function sealCoordinatorBubble() {
        const s = _dispatchSession();
        if (!s || !s.activeCoordinatorBubble) return;
        s.activeCoordinatorBubble.classList.remove('streaming');
        s.activeCoordinatorBubble.classList.add('complete');
        if (s.activeCoordinatorBubble._currentTextSpan) {
            var span = s.activeCoordinatorBubble._currentTextSpan;
            try { span.innerHTML = renderMarkdown(span._rawText || ''); }
            catch (_) { span.textContent = span._rawText || ''; }
        }
        s.activeCoordinatorBubble = null;
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
        const body = el('div', 'bubble-body');
        body.innerHTML = renderInlineHtml(text || '');
        bubble.appendChild(body);
        const pane = _dispatchPane();
        pane.appendChild(bubble);
        scrollToBottom();
    }

    function addGlobalSystemBubble(text) {
        // Global events (network, llm_fallback) affect all sessions — render
        // in every mounted session pane so the user sees it regardless of
        // which tab is active. Without this, the bubble only appears in
        // activeSid and background sessions have no visibility.
        const html = renderInlineHtml(text || '');
        for (const [, s] of sessions) {
            const bubble = el('div', 'bubble system global-notice');
            const body = el('div', 'bubble-body');
            body.innerHTML = html;
            bubble.appendChild(body);
            s.pane.appendChild(bubble);
        }
        scrollToBottom();
    }

    function addStepBubble(icon, desc) {
        // Step events (backend inline_event) are activity-class; route them
        // into the right-hand detail panel's activity feed instead of
        // producing standalone chat bubbles — keeps the conversation
        // thread focused on user/assistant messages.
        pushActivity(icon || '·', String(desc || ''), '');
    }

    function addErrorBubble(message, where) {
        const bubble = el('div', 'bubble error');
        const prefix = where ? '[' + where + '] ' : '';
        const body = el('div', 'bubble-body');
        body.innerHTML = renderInlineHtml(prefix + (message || '(no message)'));
        bubble.appendChild(body);
        const pane = _dispatchPane();
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

    // ask_human structured fields — rendered fresh into `fieldsHost` on every
    // showConfirmationModal call (the field set differs per agent call, so
    // unlike the rest of the confirm card this can't be built once and reused).
    // Returns one {id, type, controls} descriptor per field for
    // _collectAskHumanAnswers to read back at submit time.
    function _renderAskHumanFields(fieldsHost, fields) {
        fieldsHost.textContent = '';
        fieldsHost.classList.remove('hidden');
        const metas = [];
        for (const f of fields) {
            const wrap = el('div', 'scc-field');
            if (f.label) wrap.appendChild(el('div', 'scc-field-label', f.label));
            if (f.type === 'radio' || f.type === 'checkbox') {
                const opts = el('div', 'scc-field-options');
                const controls = [];
                for (const opt of (f.options || [])) {
                    const row = document.createElement('label');
                    row.className = 'scc-field-option';
                    const input = document.createElement('input');
                    input.type = f.type;
                    input.name = 'field-' + f.id;
                    input.value = opt;
                    row.appendChild(input);
                    row.appendChild(el('span', null, opt));
                    opts.appendChild(row);
                    controls.push(input);
                }
                wrap.appendChild(opts);
                metas.push({ id: f.id, type: f.type, controls });
            } else {
                const input = f.type === 'textarea'
                    ? document.createElement('textarea')
                    : document.createElement('input');
                if (f.type !== 'textarea') input.type = 'text';
                input.className = 'scc-field-input';
                if (f.type === 'textarea') input.rows = 3;
                if (f.placeholder) input.placeholder = f.placeholder;
                wrap.appendChild(input);
                metas.push({ id: f.id, type: f.type, controls: [input] });
            }
            fieldsHost.appendChild(wrap);
        }
        return metas;
    }

    function _collectAskHumanAnswers(fieldMetas) {
        const out = {};
        for (const m of (fieldMetas || [])) {
            if (m.type === 'radio') {
                const picked = m.controls.find((c) => c.checked);
                out[m.id] = picked ? picked.value : '';
            } else if (m.type === 'checkbox') {
                out[m.id] = m.controls.filter((c) => c.checked).map((c) => c.value);
            } else {
                out[m.id] = m.controls[0] ? m.controls[0].value : '';
            }
        }
        return out;
    }

    function _ensureConfirmUI(s) {
        if (s.confirmUI) return s.confirmUI;
        const host = s.confirmEl;
        const card = el('div', 'session-confirm-card');
        const titleEl = el('div', 'scc-title');
        const descEl = el('div', 'scc-desc');
        const decisionEl = el('pre', 'scc-decision hidden');
        const fieldsHost = el('div', 'scc-fields hidden');
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
        guidanceEl.placeholder = 'Optional guidance for the agent (leave blank to just approve)…';
        const actions = el('div', 'scc-actions');
        const rejectBtn = el('button', 'scc-reject', 'Reject');
        rejectBtn.type = 'button';
        const submitBtn = el('button', 'scc-submit primary', 'Approve');
        submitBtn.type = 'button';
        actions.appendChild(rejectBtn);
        actions.appendChild(submitBtn);
        card.appendChild(titleEl);
        card.appendChild(descEl);
        card.appendChild(decisionEl);
        card.appendChild(fieldsHost);
        card.appendChild(secretWrap);
        card.appendChild(guidanceEl);
        card.appendChild(actions);
        host.appendChild(card);

        const sid = s.sid;
        // No dedicated button for the guidance box — typing into it
        // re-labels Approve to "Send guidance" so the button never claims
        // to approve when a click would actually just send a note instead
        // (a real click would previously silently do that while still
        // reading "Approve"). approveLabel is stashed by showConfirmationModal.
        guidanceEl.addEventListener('input', () => {
            const hasText = !!(guidanceEl.value || '').trim();
            const approveLabel = (s.pendingConfirm && s.pendingConfirm.approveLabel) || 'Approve';
            submitBtn.textContent = hasText ? 'Send guidance' : approveLabel;
        });
        submitBtn.addEventListener('click', () => {
            const kind = s.pendingConfirm && s.pendingConfirm.kind;
            if (kind === 'ask_human') {
                const answers = _collectAskHumanAnswers(s.pendingConfirm.fieldMetas);
                sendConfirmationAnswer(sid, JSON.stringify(answers));
            } else if (kind === 'secret_input') {
                sendConfirmationAnswer(sid, secretIn.value || '');
            } else if (!guidanceEl.classList.contains('hidden') && (guidanceEl.value || '').trim()) {
                sendConfirmationAnswer(sid, guidanceEl.value.trim());
            } else {
                sendConfirmationAnswer(sid, 'yes');
            }
        });
        rejectBtn.addEventListener('click', () => sendConfirmationAnswer(sid, 'no'));
        secretIn.addEventListener('keydown', (e) => {
            const kind = s.pendingConfirm && s.pendingConfirm.kind;
            if (e.key === 'Enter' && kind === 'secret_input') {
                e.preventDefault();
                sendConfirmationAnswer(sid, secretIn.value || '');
            }
        });

        s.confirmUI = {
            card, titleEl, descEl, decisionEl, fieldsHost,
            secretWrap, secretIn, guidanceEl,
            rejectBtn, submitBtn,
        };
        return s.confirmUI;
    }

    // ── Top-layer secret prompt ─────────────────────────────────────────────
    //
    // secret_input gets its own dialog rather than the inline per-session
    // confirmation card, because the inline card is not reachable in the two
    // situations that actually raise a credential prompt:
    //
    //   1. The Connect panel (#overlay-connect) is a full-window .overlay at
    //      z-index 100 with live pointer events, and it stays open for the whole
    //      SSH bootstrap — nothing on the pairing path closes it. The inline card
    //      is an ordinary flow child of the session card with no z-index of its
    //      own (max 2 in that subtree), so a password prompt raised during
    //      Connect-panel Linux pairing renders BEHIND the panel. Worse than
    //      invisible: the card still calls focus(), so keystrokes would land in a
    //      password field the user cannot see.
    //   2. Only one session occupies the main stage; the others are rail
    //      thumbnails at zoom 0.43 with pointer-events:none. A secret_input for a
    //      railed session was unusable even with no panel open.
    //
    // Risk/tool confirmations deliberately stay inline — they carry tool
    // parameters and a guidance textarea and belong beside the conversation that
    // produced them. A credential prompt is modal and app-level.
    //
    // Everything below the presentation layer is reused as-is: the same
    // secret_input envelope, the same sendConfirmationAnswer reply (so the
    // masked durable record still lands in the chat), the same bridge-side
    // future. No new IPC.
    let secretDialogEl = null;
    let secretPending = null;   // { sid, promptId } while a prompt is open

    function _buildSecretDialog() {
        if (secretDialogEl) return secretDialogEl;
        const wrap = document.createElement('div');
        wrap.className = 'overlay hidden';
        wrap.id = 'overlay-secret-input';
        // Above the Connect panel (100) and both remote-control dialogs
        // (2000 pairing, 2100 confirm), so it is reachable from any of them.
        wrap.style.zIndex = '2200';

        const card = document.createElement('div');
        card.className = 'overlay-card rc-dialog-card';
        card.setAttribute('role', 'dialog');
        card.setAttribute('aria-modal', 'true');

        const title = document.createElement('div');
        title.className = 'rc-dialog-title';
        title.textContent = 'Input required';
        card.appendChild(title);

        // .rc-dialog-body already carries white-space: pre-line, which these
        // prompts need — ssh_setup sends a two-line message.
        const body = document.createElement('div');
        body.className = 'rc-dialog-body';
        card.appendChild(body);

        const input = document.createElement('input');
        input.type = 'password';
        input.className = 'scc-secret';
        input.autocomplete = 'off';
        input.spellcheck = false;
        card.appendChild(input);

        const status = document.createElement('div');
        status.className = 'rc-dialog-status';
        card.appendChild(status);

        const actions = document.createElement('div');
        actions.className = 'scc-actions rc-dialog-actions';
        const cancelBtn = document.createElement('button');
        cancelBtn.type = 'button';
        cancelBtn.textContent = 'Cancel';
        const okBtn = document.createElement('button');
        okBtn.type = 'button';
        okBtn.className = 'primary';
        okBtn.textContent = 'Submit';
        actions.appendChild(cancelBtn);
        actions.appendChild(okBtn);
        card.appendChild(actions);

        wrap.appendChild(card);
        document.body.appendChild(wrap);

        cancelBtn.addEventListener('click', () => _settleSecret(''));
        okBtn.addEventListener('click', () => _settleSecret(input.value));
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') { e.preventDefault(); _settleSecret(input.value); }
        });
        // Backdrop and Escape cancel, matching the other dialogs in this app.
        wrap.addEventListener('mousedown', (ev) => {
            if (ev.target === wrap) _settleSecret('');
        });
        document.addEventListener('keydown', (e) => {
            if (!secretDialogEl || wrap.classList.contains('hidden')) return;
            if (e.key === 'Escape') { e.preventDefault(); _settleSecret(''); }
        });

        secretDialogEl = { wrap, title, body, input, status };
        return secretDialogEl;
    }

    // Answer the open prompt, then clear the field so the secret does not sit in
    // the DOM. An empty answer is a valid, meaningful reply — ssh_setup reads it
    // as "no password provided" and aborts cleanly — so Cancel / Escape /
    // backdrop all resolve the bridge-side future instead of abandoning it.
    function _settleSecret(value) {
        const dlg = secretDialogEl;
        const pending = secretPending;
        secretPending = null;
        if (dlg) {
            dlg.wrap.classList.add('hidden');
            dlg.input.value = '';
            dlg.status.textContent = '';
        }
        if (!pending) return;
        sendConfirmationAnswer(pending.sid, String(value || ''));
    }

    function showSecretPrompt(evt) {
        const sid = _resolveSid(evt);
        const s = sessions.get(sid);
        if (!s) {
            window.__handqLog('ERROR', 'secret_input for unknown session',
                { sid, id: evt && evt.id });
            return;
        }
        const promptId = String(evt.id || '');
        // A fresh prompt arriving while one is still open (ssh_setup retries up
        // to 3 times) must not orphan the previous future — answer it empty,
        // then re-target the dialog.
        if (secretPending && secretPending.promptId &&
            secretPending.promptId !== promptId) {
            _settleSecret('');
        }
        const dlg = _buildSecretDialog();
        dlg.body.textContent = String(evt.prompt || 'Enter value:');
        dlg.input.value = '';
        dlg.status.textContent = '';
        dlg.wrap.classList.remove('hidden');
        // sendConfirmationAnswer reads the prompt id and kind off the session and
        // writes the masked durable record into that session's chat.
        s.pendingConfirm = { id: promptId, kind: 'secret_input' };
        secretPending = { sid, promptId };
        if (sid !== activeSid) s.unread = true;
        try { dlg.input.focus(); } catch (_) { /* ignore */ }
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
            // Not reached from the status dispatcher any more — secret_input is
            // routed to showSecretPrompt()'s top-layer dialog. Kept as a working
            // fallback so a direct caller degrades to the old inline card
            // instead of falling through to the risk/tool branch below and
            // rendering a password prompt as an Approve/Reject modal.
            ui.titleEl.textContent = 'Input required';
            ui.descEl.classList.remove('md-rendered');
            ui.descEl.textContent = String(evt.prompt || 'Enter value:');
            _renderDecisionInto(ui.decisionEl, null);
            ui.fieldsHost.classList.add('hidden');
            ui.fieldsHost.textContent = '';
            ui.secretWrap.classList.remove('hidden');
            ui.secretIn.value = '';
            try { ui.secretIn.type = 'password'; } catch (_) { /* ignore */ }
            ui.guidanceEl.classList.add('hidden');
            ui.guidanceEl.value = '';
            ui.rejectBtn.classList.add('hidden');
            ui.submitBtn.textContent = 'Submit';
        } else if (evt.kind === 'ask_human') {
            ui.titleEl.textContent = 'Question from agent';
            const question = String(evt.question || evt.prompt || 'The agent has a question:');
            try {
                ui.descEl.innerHTML = renderMarkdown(question);
                ui.descEl.classList.add('md-rendered');
            } catch (_) {
                ui.descEl.classList.remove('md-rendered');
                ui.descEl.textContent = question;
            }
            _renderDecisionInto(ui.decisionEl, null);
            const fields = Array.isArray(evt.fields) && evt.fields.length
                ? evt.fields
                : [{ id: 'answer', label: '', type: 'textarea' }];
            s.pendingConfirm.fieldMetas = _renderAskHumanFields(ui.fieldsHost, fields);
            ui.secretWrap.classList.add('hidden');
            ui.secretIn.value = '';
            ui.guidanceEl.classList.add('hidden');
            ui.guidanceEl.value = '';
            ui.rejectBtn.classList.add('hidden');
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
            ui.descEl.classList.remove('md-rendered');
            if (isRisk || description) {
                ui.descEl.textContent = description;
            } else {
                ui.descEl.textContent =
                    'The agent wants to run "' + (evt.tool || 'tool') +
                    '" with the parameters below.';
            }
            _renderDecisionInto(ui.decisionEl, evt.decision);
            ui.fieldsHost.classList.add('hidden');
            ui.fieldsHost.textContent = '';
            ui.secretWrap.classList.add('hidden');
            ui.secretIn.value = '';
            ui.guidanceEl.classList.remove('hidden');
            ui.guidanceEl.value = '';
            ui.rejectBtn.classList.remove('hidden');
            const approveLabel = customApprove
                || (isDesktopTakeover ? 'Approve task-wide' : 'Approve');
            s.pendingConfirm.approveLabel = approveLabel;
            ui.submitBtn.textContent = approveLabel;
            if (isDesktopTakeover) ui.card.classList.add('desktop-takeover');
        }

        s.confirmEl.classList.remove('hidden');
        // Mark unread if this isn't the focused card so the user notices.
        if (sid !== activeSid) {
            s.unread = true;
        }
        if (evt.kind === 'secret_input') {
            try { ui.secretIn.focus(); } catch (_) { /* ignore */ }
        } else if (evt.kind === 'ask_human') {
            try {
                const first = ui.fieldsHost.querySelector('input, textarea');
                if (first) first.focus();
            } catch (_) { /* ignore */ }
        }
        try { s.confirmEl.scrollIntoView({ block: 'nearest' }); } catch (_) { /* ignore */ }
    }

    function _describeConfirmationAnswer(kind, answer, fieldMetas) {
        // Human-readable echo of what the user just submitted, for the
        // chat-bubble record added by sendConfirmationAnswer. Keeps secrets
        // masked and turns the ask_human JSON blob back into readable text.
        if (kind === 'secret_input') {
            return answer ? '(value submitted, hidden)' : '(submitted empty)';
        }
        if (kind === 'ask_human') {
            let parsed = {};
            try { parsed = JSON.parse(answer || '{}'); } catch (_) { /* ignore */ }
            const metas = Array.isArray(fieldMetas) ? fieldMetas : [];
            if (metas.length <= 1) {
                const only = metas[0];
                const val = only ? parsed[only.id] : Object.values(parsed)[0];
                return Array.isArray(val) ? (val.join(', ') || '(none selected)') : String(val || '(no answer)');
            }
            return metas.map((m) => {
                const val = parsed[m.id];
                const shown = Array.isArray(val) ? (val.join(', ') || '(none)') : String(val || '(none)');
                return (m.label || m.id) + ': ' + shown;
            }).join('\n');
        }
        // risk / tool confirmation
        if (answer === 'yes') return 'Approved';
        if (answer === 'no') return 'Rejected';
        return 'Guidance: ' + answer;
    }

    function sendConfirmationAnswer(sid, answer) {
        const s = sessions.get(sid);
        if (!s || !s.pendingConfirm) return;
        const promptId = s.pendingConfirm.id;
        const kind = s.pendingConfirm.kind;
        const fieldMetas = s.pendingConfirm.fieldMetas;
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

        // Leave a durable record of the answer in the chat itself — the
        // modal disappears immediately, so without this the user's own
        // reply to ask_human/risk/tool prompts had zero trace in the
        // conversation thread.
        const prevDispatch = _dispatchSid;
        _dispatchSid = sid;
        try {
            addUserBubble(_describeConfirmationAnswer(kind, String(answer || ''), fieldMetas));
        } finally {
            _dispatchSid = prevDispatch;
        }
    }

    // ask_human's 30-minute deadline expired (locally, or because a
    // Connect-panel remote session's own timeout cancelled the relayed
    // prompt — both paths converge on the same ask_human_expired envelope,
    // see stdio_bridge.py's _await_user_response). Close the stale modal and
    // leave a durable transcript record instead of letting it linger with no
    // signal that the agent already moved on with a default.
    function _closeExpiredAskHuman(evt) {
        const sid = _resolveSid(evt);
        const s = sessions.get(sid);
        if (!s) return;
        // Guard against a race: if the user answered in the last instant
        // before expiry, sendConfirmationAnswer already cleared pendingConfirm,
        // so a slightly-late expiry envelope for THAT id becomes a no-op
        // rather than wrongly closing a fresh, unrelated modal.
        if (!s.pendingConfirm || s.pendingConfirm.id !== String(evt.id || '')) return;
        s.pendingConfirm = null;
        if (s.confirmEl) s.confirmEl.classList.add('hidden');
        addSystemBubble('⊗ A question was asked but went unanswered for 30 minutes — the agent proceeded with a default.');
    }

    // ── Session-resume candidate card (§6.4.1) ──────────────────────────────
    //
    // A SOFT offer, not a modal: the user can accept, explicitly dismiss, or
    // just ignore it (type a new message / let it expire) with zero effect
    // in the last two cases — see docs/session_resume_design.md §2.4/§6.4.
    // Deliberately NOT built on the confirmation-Future machinery above
    // (_ensureConfirmUI/sendConfirmationAnswer): that path assumes exactly
    // one pending question that MUST be answered before anything else in
    // that session proceeds, which is the opposite of what this needs.

    // Resume candidate rendering moved to the RIGHT sidebar panel
    // (session-sidebar.js's showResumeCandidates / hideResume). The old
    // _ensureResumeUI / _renderResumeCandidateRow functions lived here but
    // are removed — rendering now lives in session-sidebar.js so the
    // candidates have more space and don't overflow the session card.

    function _showResumeCandidates(evt) {
        const sid = _resolveSid(evt);
        const s = sessions.get(sid);
        if (!s) {
            window.__handqLog('ERROR', 'resume_candidates for unknown session',
                { sid });
            return;
        }
        const candidates = Array.isArray(evt.candidates) ? evt.candidates : [];
        if (!candidates.length) {
            // Continuous search (§ interaction upgrade) means a LATER
            // message can legitimately un-hit the gate after an earlier
            // one hit it — the bridge sends this same envelope with an
            // empty array as the explicit "clear" signal.
            if (s.pendingResume) _hideResumeCard(s);
            return;
        }

        s.pendingResume = { candidates };

        // Delegate rendering to the right-side session detail panel.
        const holdSeconds = Number(evt.hold_seconds) || 0;
        const triggerText = String(evt.trigger_text || '');
        if (window.SessionSidebar) {
            window.SessionSidebar.showResumeCandidates(sid, candidates, holdSeconds, {
                onConfirm: (sessionDir) => _sendResumeConfirm(sid, sessionDir),
                onNotResuming: () => _notResuming(sid),
            }, triggerText);
        }

        // Soft auto-dismiss on the backend's own TTL (default 120s).
        const ttlMs = (Number(evt.ttl_seconds) || 120) * 1000;
        if (s._resumeTimeoutId) clearTimeout(s._resumeTimeoutId);
        s._resumeTimeoutId = setTimeout(() => {
            if (s.pendingResume) _hideResumeCard(s);
        }, ttlMs);
    }

    function _hideResumeCard(s) {
        s.pendingResume = null;
        if (s._resumeTimeoutId) {
            clearTimeout(s._resumeTimeoutId);
            s._resumeTimeoutId = null;
        }
        if (window.SessionSidebar) {
            window.SessionSidebar.hideResume(s.sid);
        }
    }

    function _sendResumeConfirm(sid, sessionDir) {
        const s = sessions.get(sid);
        if (!s || !s.pendingResume) return;
        try {
            handq.sendRequest({
                type: 'resume_confirm',
                session_id: sid,
                session_dir: String(sessionDir || ''),
            });
        } catch (e) {
            window.__handqLog('ERROR', 'resume_confirm send failed',
                { sid, error: String(e) });
        }
        _hideResumeCard(s);
    }

    // "Not resuming" — merged New Task + No Resume: runs the held message
    // as a new task + permanently stops resume searching for this session
    // (session identity is now settled). Sends resume_disable_for_session
    // which does both on the bridge side.
    function _notResuming(sid) {
        const s = sessions.get(sid);
        if (!s) return;
        try {
            handq.sendRequest({ type: 'resume_disable_for_session', session_id: sid });
        } catch (e) {
            window.__handqLog('ERROR', 'resume_disable_for_session send failed',
                { sid, error: String(e) });
        }
        _hideResumeCard(s);
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
        btnHide.title = 'Close session';
        btnHide.textContent = '×';
        // Closes ONLY the current (active) tab — not the whole panel. It is
        // hidden by CSS while the active session is still alive (see
        // updatePanelCloseVisibility), so this can only fire on a dead session.
        btnHide.addEventListener('click', () => {
            if (_activeSessionId) removeSessionTerminal(_activeSessionId);
        });

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

        // Re-fit active terminal on resize, throttled with a trailing call.
        //
        // fitAddon.fit() measures the viewport and, on any cols/rows change,
        // calls terminal.resize() — which reflows the ENTIRE scrollback buffer
        // and re-renders. Running that once per frame for the whole of a
        // window resize is the most expensive thing in the renderer while the
        // terminal panel is open, and the intermediate fits are all superseded
        // anyway. A pure trailing debounce would be wrong in the other
        // direction: dragging the panel's OWN resize handle would then leave
        // the text un-reflowed until the pointer stopped. Throttling keeps the
        // drag tracking at a reflow rate a human reads as "keeping up", and
        // the trailing call guarantees the settled geometry is exact.
        let fitTimer = 0;
        let lastFitTs = 0;
        const FIT_MIN_INTERVAL_MS = 120;
        const doFit = () => {
            lastFitTs = performance.now();
            const entry = _sessionTerminals.get(_activeSessionId);
            if (entry) entry.fitAddon.fit();
        };
        const ro = new ResizeObserver(() => {
            if (performance.now() - lastFitTs >= FIT_MIN_INTERVAL_MS) doFit();
            clearTimeout(fitTimer);
            fitTimer = setTimeout(() => { fitTimer = 0; doFit(); }, FIT_MIN_INTERVAL_MS);
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
            /* Font — aligned with the app-wide typography scale. 13px matches
               --fs-body (macOS body Text Style) and sits inside the doc's
               "code editor / terminal" 12-14pt range (§11.2). fontFamily
               mirrors the stylesheet's --mono token (ui-monospace first
               so macOS gets the OS-optimized mono, then explicit fallbacks
               for older / non-Apple platforms). */
            fontSize: 13,
            fontFamily: 'ui-monospace, "SF Mono", SFMono-Regular, Menlo, Monaco, "Cascadia Mono", Consolas, "Liberation Mono", monospace',
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
        const tabClose = document.createElement('button');
        tabClose.type = 'button';
        tabClose.className = 'tab-close';
        tabClose.setAttribute('aria-label', 'Close terminal tab');
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
        // The panel-level × tracks the active session's liveness — refresh it
        // whenever the active session changes.
        updatePanelCloseVisibility();
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

    // Controls whether the panel-level × (close) button is shown. With
    // concurrent sessions the panel is shared, so the button must reflect the
    // CURRENTLY VISIBLE (active) session, not "is any session alive". A dead
    // active session gets a close button even while other tabs keep running.
    function updatePanelCloseVisibility() {
        if (!_terminalPanelEl) return;
        const active = _sessionTerminals.get(_activeSessionId);
        const activeAlive = !!active && !active.tab.classList.contains('dead');
        _terminalPanelEl.classList.toggle('active-alive', activeAlive);
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

        // rAF-batched. Windows fires `resize` faster than the compositor's
        // frame rate during a DWM animated resize, and the raw handler did
        // three forced layout reads (getBoundingClientRect here, then
        // offsetWidth/offsetHeight inside clampPosition) followed by two style
        // writes — a read/write thrash repeated several times per frame for a
        // panel that needs re-clamping at most once per frame. The write is
        // also skipped when the clamp is a no-op, which is the common case:
        // the panel is only near a window edge some of the time.
        let clampRaf = 0;
        window.addEventListener('resize', () => {
            if (clampRaf) return;
            clampRaf = requestAnimationFrame(() => {
                clampRaf = 0;
                if (panel.classList.contains('hidden')) return;
                const rect = panel.getBoundingClientRect();
                const clamped = clampPosition(rect.left, rect.top);
                if (Math.abs(clamped.left - rect.left) >= 1) {
                    panel.style.left = clamped.left + 'px';
                }
                if (Math.abs(clamped.top - rect.top) >= 1) {
                    panel.style.top = clamped.top + 'px';
                }
            });
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

    // ── Overlay-card drag-to-resize (Settings/Connect/Remote/Admin/
    // Schedules/Skills panels) ────────────────────────────────────────────
    //
    // Unlike the terminal/composer floaters above, overlay-cards are
    // centered by their `.overlay` flex container rather than absolutely
    // positioned — so resizing only needs to grow/shrink the card itself
    // (recentering falls out of the flex layout for free), and the size
    // that matters to persist is width/height, not position.
    function initOverlayCardResize(handle) {
        const card = handle.closest('.overlay-card');
        const overlay = handle.closest('.overlay');
        if (!card || !overlay) return;
        const storeKey = 'handq:overlay:size:' + overlay.id;
        let resizing = false, startX, startY, startW, startH;

        try {
            const saved = JSON.parse(localStorage.getItem(storeKey) || 'null');
            if (saved && saved.w && saved.h) {
                card.style.width = saved.w + 'px';
                card.style.height = saved.h + 'px';
            }
        } catch { /* corrupt/absent — fall back to the CSS default size */ }

        handle.addEventListener('mousedown', (e) => {
            resizing = true;
            startX = e.clientX;
            startY = e.clientY;
            const rect = card.getBoundingClientRect();
            startW = rect.width;
            startH = rect.height;
            document.body.classList.add('overlay-resizing');
            e.preventDefault();
        });

        document.addEventListener('mousemove', (e) => {
            if (!resizing) return;
            const maxW = window.innerWidth * 0.98;
            const maxH = window.innerHeight * 0.94;
            const newW = Math.max(420, Math.min(startW + (e.clientX - startX), maxW));
            const newH = Math.max(320, Math.min(startH + (e.clientY - startY), maxH));
            card.style.width = newW + 'px';
            card.style.height = newH + 'px';
        });

        document.addEventListener('mouseup', () => {
            if (!resizing) return;
            resizing = false;
            document.body.classList.remove('overlay-resizing');
            localStorage.setItem(storeKey, JSON.stringify({
                w: card.offsetWidth,
                h: card.offsetHeight,
            }));
        });
    }
    document.querySelectorAll('.overlay-resize-handle').forEach(initOverlayCardResize);

    // Task-plan + agent-todo panels used to render inline at the top of
    // each session's chat pane; they now live in the right-hand detail
    // panel exclusively (session-sidebar.js's _renderPlanBar), fed via
    // SessionSidebar.setTaskPlan / .setAgentTodo from the onStatus
    // dispatcher below — kept the chat pane free of anything but the
    // actual conversation.


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
            _placeSessionPassive(evt.session_id);
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
        } catch (err) {
            // Without this catch, a throw partway through a kind-specific
            // branch (e.g. mid-pushActivity) is swallowed by the browser's
            // default unhandled-exception handling — invisible in
            // handq-frontend.log, since the 'onStatus' debug line above
            // already logged the event as "received" before the throw.
            window.__handqLog('ERROR', 'onStatus handler threw', {
                kind: evt && evt.kind,
                error: err && err.message,
                stack: err && err.stack,
            });
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

        if (evt.kind === 'ask_human_expired') {
            _closeExpiredAskHuman(evt);
            return;
        }

        if (evt.kind === 'secret_input') {
            // Top layer, not the inline card — see _buildSecretDialog() for why
            // a credential prompt cannot live inside the session card.
            try { showSecretPrompt(evt); }
            catch (e) { window.__handqLog('ERROR', 'showSecretPrompt failed',
                                           { error: String(e) }); }
            return;
        }

        if (evt.kind === 'risk_confirmation' ||
            evt.kind === 'tool_confirmation' ||
            evt.kind === 'ask_human') {
            // Show the confirmation modal and stop further dispatch — these
            // envelopes are not informational status updates.
            try { showConfirmationModal(evt); }
            catch (e) { window.__handqLog('ERROR', 'showConfirmationModal failed',
                                           { error: String(e) }); }
            return;
        }

        if (evt.kind === 'resume_candidates') {
            // Soft offer (§6.4) — unlike the hard-block modal above, this
            // does NOT prevent normal dispatch from continuing: the temp
            // session it's attached to is already running its own work in
            // parallel, and subsequent status events (state_changed, reply,
            // etc.) for the SAME sid must keep rendering normally.
            try { _showResumeCandidates(evt); }
            catch (e) { window.__handqLog('ERROR', '_showResumeCandidates failed',
                                           { error: String(e) }); }
            return;
        }

        if (evt.kind === 'state_changed' && evt.state) {
            const s = _dispatchSession();
            if (s) s.sessionState = evt.state;
            // V2 activity-strip vocabulary (see controller_v2):
            //   thinking  — agent has the LLM stream open (reasoning + tools)
            //   executing — agent dispatching tools / between think-streams
            //   idle      — task settled, final reply sent
            // The first two are live working phases → animated label,
            // consistent with the coordinator's "thinking…". idle clears the
            // working animation and rests the strip.
            if (evt.state === 'thinking') {
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
            // Render with addStepBubble so it visually matches other step
            // events instead of the chunkier system bubble used by display_message.
            addStepBubble(String(evt.icon || '·'), String(evt.desc || ''));
        } else if (evt.kind === 'recall_started') {
            // LTM recall is in flight (orchestrator INTENT/PLAN gather, or a
            // per-item / stagnation agent recall). Show a transient working
            // label on the activity strip; the next state_changed / decision /
            // tool event (or a streamed chat reply) supersedes it.
            setWorking('recalling…');
            // Also nudge the detail sidebar to auto-expand — recall alone
            // wouldn't otherwise fire _maybeAutoExpand, so a session whose
            // ONLY current signal is "recall in flight" (very common during
            // orchestrator INTENT/PLAN prep, before any tool or decision has
            // fired) would leave the panel collapsed even though the agent
            // is clearly working. We use nudgeExpand (not pushActivity)
            // because there's no matching recall_finished signal, so a
            // pushed "recalling…" entry would linger forever with a
            // misleading placeholder as its content — the pill already
            // owns the transient status.
            const s = _dispatchSession();
            if (s && window.SessionSidebar) {
                window.SessionSidebar.nudgeExpand(s.sid);
            }
        } else if (evt.kind === 'decision_made') {
            const iter = args[0] || '';
            const reasoning = args[1] || '';
            const s = _dispatchSession();
            if (s) s.lastThinking = reasoning;
            pushActivity('▶', 'thinking' + (iter ? ' · iter ' + iter : ''), reasoning);
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
            // The whole backend process died — every session's in-flight
            // tool cards would otherwise spin forever. Finalize them all,
            // not just the dispatching session's.
            for (const sid of sessions.keys()) _forceFinalizePendingActivity(sid);
        } else if (evt.kind === 'reply') {
            addAssistantTextBubble(evt.text || '');
        } else if (evt.kind === 'task_completed') {
            // Fired ALONGSIDE 'reply' (same text) purely so main.js can raise
            // a system notification for a real completion — see
            // Orchestrator._emit_completion_reply's on_task_completed_notify.
            // The chat bubble itself already came from 'reply' above; this
            // only adds the activity-feed marker, so it must not re-render
            // the text as a second bubble.
            pushActivity('🏁', 'Task completed',
                          String(evt.summary || '').slice(0, 80));
        } else if (evt.kind === 'user_message_echo') {
            // Replay of the operator's OWN past message (see
            // stdio_bridge.py's _StdioUI.show_user_message_echo) — the local
            // DOM bubble from addUserBubble's synchronous submit-time call
            // never persists across a tab close, so a reattach redraws it
            // through this same event stream instead.
            addUserBubble(evt.text || '');
        } else if (evt.kind === 'reply_delta') {
            // Coordinator is streaming a chat reply. Always clear the chat-side
            // thinking bubble so the streaming text replaces it. The activity
            // strip pill is owned by the agent's task state — only reset
            // it when no task is in flight; otherwise the pill would flash to
            // "idle" mid-task while the coordinator chats.
            removeThinkingBubble();
            if (!isTaskRunning()) {
                clearWorking();
                setPill('');
            }
            appendCoordinatorDelta(evt.text || '');
        } else if (evt.kind === 'reply_done') {
            sealCoordinatorBubble();
        } else if (evt.kind === 'coordinator_thinking_on') {
            // Show the chat-side thinking bubble unconditionally; only steal
            // the activity strip pill when no real task is running, otherwise
            // the agent's working indicator would be hidden by "thinking…".
            showThinkingBubble();
            if (!isTaskRunning()) {
                setWorking('thinking…');
            }
        } else if (evt.kind === 'coordinator_thinking_off') {
            removeThinkingBubble();
            if (!isTaskRunning()) {
                // clearWorking() only strips the spin animation class — it never
                // resets pillText/textContent. Without setPill(''), a leftover
                // label from an intermediate signal (e.g. "recalling…") stays
                // stuck on the pill forever on the chat path, which never visits
                // state_changed→idle or reply_delta (the only other resets).
                clearWorking();
                setPill('');
            }
        } else if (evt.kind === 'session_event') {
            handleSessionEvent(evt.event, evt.data || {});
        } else if (evt.kind === 'session_started') {
            if (evt.session_name) _renameSession(_dispatchSid, evt.session_name);
        } else if (evt.kind === 'scheduled_task_started') {
            const name = evt.session_name || ('⏱ ' + (evt.name || 'Scheduled'));
            _renameSession(_dispatchSid, name);
        } else if (evt.kind === 'task_plan') {
            // Task plan now renders exclusively in the right-hand detail
            // panel (session-sidebar.js's plan bar) — it also groups file
            // touches by task item and needs each item's current status
            // (running/done/pending) to drive glyph + Undo affordance.
            if (window.SessionSidebar) {
                window.SessionSidebar.setTaskPlan(
                    _resolveSid(evt),
                    Array.isArray(evt.items) ? evt.items : [],
                );
            }
        } else if (evt.kind === 'file_touch') {
            // Live file activity → session sidebar (change list). Fired by
            // write/edit/read tools right after a successful op; the
            // sidebar accumulates per-session and re-renders when the
            // event belongs to the active session. `reversible` gates the
            // per-file ↺ button — only true when the backend held a
            // capture_before snapshot (write/edit/notebook_edit), never for
            // shell mtime hits or read/grep/glob.
            if (window.SessionSidebar) {
                window.SessionSidebar.ingestFileTouch(_resolveSid(evt), {
                    path:       evt.path,
                    touch:      evt.touch,
                    tool:       evt.tool,
                    item_id:    evt.item_id,
                    reversible: evt.reversible === true,
                });
            }
        } else if (evt.kind === 'agent_todo') {
            if (window.SessionSidebar) {
                window.SessionSidebar.setAgentTodo(
                    _resolveSid(evt),
                    Array.isArray(evt.todos) ? evt.todos : [],
                );
            }
        } else if (evt.kind === 'model_stats') {
            if (window.SessionSidebar) {
                window.SessionSidebar.setModelStats(
                    _resolveSid(evt),
                    Array.isArray(evt.models) ? evt.models : [],
                );
            }
        } else if (evt.kind === 'llm_server_error') {
            const retryIn = (typeof evt.retry_in === 'number' && evt.retry_in > 0)
                ? ' — retrying in ' + evt.retry_in + 's'
                : '';
            const attLeft = (typeof evt.attempts_left === 'number' && evt.attempts_left > 0)
                ? ' (' + evt.attempts_left + ' attempt' + (evt.attempts_left !== 1 ? 's' : '') + ' remaining)'
                : '';
            const errSummary = (evt.message || 'API server issue') + retryIn + attLeft;
            addGlobalSystemBubble('○ ' + errSummary
                + '\nThis is a temporary API server issue, not a HandQ problem.'
                + ' Retrying automatically — please wait.');
            pushActivity('○', 'API retry', errSummary);
            setPill('retrying…');
        } else if (evt.kind === 'llm_fallback') {
            const fromModel = String(evt.from_model || '?');
            const toModel   = String(evt.to_model   || '?');
            // The error belongs to fromModel — it is WHY we are falling back.
            // Appending it after "trying <toModel>" read as if toModel had
            // failed: a 2026-08-03 429 for claude-4-6-sonnet rendered as
            // "↪ claude-4-6-sonnet failed; trying claude-4-5-sonnet — Rate
            // limit exceeded for model: anthropic::claude-4-6-sonnet" and was
            // reported as a fallback bug. Bind the reason to its own model.
            const reason    = evt.error ? ' (' + evt.error + ')' : '';
            addGlobalSystemBubble('↪ ' + fromModel + ' failed' + reason + '; trying ' + toModel);
            pushActivity('↪', 'Model fallback', fromModel + ' → ' + toModel);
        } else if (evt.kind === 'agent_notice') {
            // The agent is telling the user something it needs them to know
            // NOW (e.g. "stop clicking that button while I reboot the device").
            // Deliberately a standalone system bubble, not a step bubble — a
            // step bubble scrolls away inside the tool trace, which is
            // exactly how the 2026-08-03 run lost its own critical
            // instruction. Unlike its neighbors below (llm_fallback,
            // llm_server_error, network_*), this envelope DOES carry a real
            // session_id (stamped by show_user_notice) — it's this session's
            // agent addressing this session's user, so it renders only into
            // the dispatch session's own pane via addSystemBubble, not
            // broadcast into every open tab.
            const urgent = !!evt.urgent;
            addSystemBubble((urgent ? '⚠ ' : '↩ ') + String(evt.message || ''));
            pushActivity(urgent ? '⚠' : '↩', 'Agent notice',
                         String(evt.message || '').slice(0, 80));
        } else if (evt.kind === 'network_down') {
            addGlobalSystemBubble('⊗ ' + (evt.message || '网络中断，等待恢复…')
                + '\nHandQ will resume automatically once the connection is restored.');
            pushActivity('⊗', 'Network down', 'waiting for LLM endpoint');
            setPill('offline…');
        } else if (evt.kind === 'network_waiting') {
            const retryIn = (typeof evt.retry_in === 'number' && evt.retry_in > 0)
                ? evt.retry_in + 's' : '…';
            pushActivity('⊗', 'Still offline', 'attempt ' + (evt.attempt || '?') + ', next probe in ' + retryIn);
        } else if (evt.kind === 'network_restored') {
            addGlobalSystemBubble('✓ ' + (evt.message || '网络已恢复，继续执行'));
            pushActivity('✓', 'Network restored', 'resuming');
            setPill('working…');
        }
    }

    handq.onFinal((evt) => {
        if (gateGen(evt)) return;
        if (evt && evt.session_id && closedSessions.has(evt.session_id)) return;
        _dispatchSid = _resolveSid(evt);
        try {
            return _onFinalBody(evt);
        } catch (err) {
            window.__handqLog('ERROR', 'onFinal handler threw', {
                error: err && err.message,
                stack: err && err.stack,
            });
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

        // file_undo response — {ok, mode, item_id, restored:[{path,was_absent}],
        // conflicts:[{path,conflict,detail}]}. Dispatch to the sidebar so it
        // cleans up state (removes deleted files, suppresses ↺ on the ones we
        // just reverted), then author the user-visible confirmation as a
        // per-session system bubble + one activity feed row. Detected by the
        // presence of the {interrupt|notice} mode — no other final response
        // carries that shape.
        if (evt.result && (evt.result.mode === 'interrupt' || evt.result.mode === 'notice')) {
            const sid = _resolveSid(evt);
            const restored = Array.isArray(evt.result.restored) ? evt.result.restored : [];
            const conflicts = Array.isArray(evt.result.conflicts) ? evt.result.conflicts : [];
            if (window.SessionSidebar) {
                window.SessionSidebar.notifyFilesRestored(sid, restored, conflicts);
            }
            const lines = [];
            if (restored.length) {
                lines.push('↺ Reverted ' + restored.length + ' file(s) from task '
                    + (evt.result.item_id || '') + '.');
                if (evt.result.mode === 'interrupt') {
                    lines.push('Agent turn was interrupted — awaiting your next instruction.');
                }
            } else {
                lines.push('↺ Undo requested, but no files were reverted.');
            }
            if (conflicts.length) {
                lines.push('⚠ ' + conflicts.length + ' file(s) could NOT be reverted:');
                for (const c of conflicts) {
                    lines.push('  • ' + (c.path || '?') + ' — ' + (c.conflict || '?')
                        + (c.detail ? ' (' + c.detail + ')' : ''));
                }
            }
            addSystemBubble(lines.join('\n'));
            pushActivity('↺', 'Undo · ' + (evt.result.mode || ''),
                'reverted ' + restored.length + '; ' + conflicts.length + ' conflicts');
            return;
        }

        if (evt.result && typeof evt.result.success === 'boolean') {
            const summary = el('div', 'bubble system');
            const summaryBody = el('div', 'bubble-body');
            summaryBody.innerHTML = renderInlineHtml(
                (evt.result.success ? '✓ ' : '✗ ') +
                (evt.result.message || '(no message)'));
            summary.appendChild(summaryBody);
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
        } catch (err) {
            window.__handqLog('ERROR', 'onError handler threw', {
                error: err && err.message,
                stack: err && err.stack,
            });
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
            _forceFinalizePendingActivity(s && s.sid);
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

        // NOTE: we intentionally do NOT tear down the resume card here.
        // Under the current "coordinator and resume are independent" model
        // (§ resume — HANDQ_DESIGN §2.16, 2026-08-01) every user message
        // re-triggers a fresh continuous search on the bridge, and that
        // search sends back EITHER a refreshed candidate set (which
        // showResumeCandidates renders wholesale, replacing the old card)
        // OR an explicit empty-array clear (which _showResumeCandidates
        // translates into _hideResumeCard). Preemptively hiding here just
        // makes the sidebar flash "close → open" every time the user
        // continues to chat while an offer is up, which reads as a bug
        // even though the card is about to come back with the very next
        // envelope. The stale-card window (a few hundred ms while the
        // bridge re-searches) is short enough that leaving the previous
        // card in place is preferable to the flicker.

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
        // Dim titlebar chrome when the window loses OS focus, like a real
        // native window's inactive-title state.
        if (typeof winCtl.onActiveState === 'function') {
            winCtl.onActiveState((state) => {
                document.documentElement.classList.toggle('window-inactive', !(state && state.active));
            });
        }
        // Double-click anywhere on the bare drag surface maximizes/restores,
        // matching every native titlebar. Bails if the dblclick landed on a
        // real control (window buttons, the brand island, the serve
        // indicator) so their own click handlers stay the only thing that
        // fires there.
        //
        // Listens on 'mousedown' rather than 'dblclick': -webkit-app-region:
        // drag hands the initial mousedown to the OS for native window-move
        // simulation, and Chromium does not reliably follow through with the
        // composite click/dblclick DOM events afterward on that region — so
        // a 'dblclick' listener here silently never fires. MouseEvent.detail
        // on 'mousedown' still carries the OS's live click-count (1, 2, 3…)
        // regardless of that, so it's the reliable signal to key off.
        const titlebarEl = document.getElementById('titlebar');
        if (titlebarEl) {
            titlebarEl.addEventListener('mousedown', (e) => {
                if (e.button !== 0 || e.detail < 2) return;
                if (e.target.closest('.tb-btn, .titlebar-island, .titlebar-new-btn, .titlebar-serve-indicator')) return;
                winCtl.toggleMaximize();
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
        // island-morphing: suppresses the SVG url() distortion filter for
        // the duration of the width/height morph (see styles.css) — that
        // filter isn't GPU-compositable, so animating geometry underneath
        // it forces a re-rasterize every frame. 400ms covers the 380ms
        // CSS transition plus a little settle margin.
        island.classList.add('island-morphing');
        island.classList.remove('expanded');
        island.style.height = '';   // back to the collapsed rule's 20px
        if (islandTrigger) islandTrigger.setAttribute('aria-expanded', 'false');
        if (islandMenu) islandMenu.setAttribute('aria-hidden', 'true');
        setTimeout(() => island.classList.remove('island-morphing'), 400);
    }

    function expandIsland() {
        if (!island || island.classList.contains('expanded')) return;
        island.classList.add('island-morphing');
        island.classList.add('expanded');
        // The expanded height is NOT a fixed CSS value — styles.css used to
        // hardcode 140px, tuned for exactly 3 menu items, and clipped the
        // bottom item the moment a 4th was added (Remote machines). Measuring
        // the real content here means adding/removing items in the future
        // needs no CSS changes at all.
        //
        // `.titlebar-island` is a column flexbox holding BOTH the trigger
        // button and the menu <ul> as siblings — the island's total height is
        // trigger + menu, not menu alone. scrollHeight is read on each
        // (rather than offsetHeight) because the menu is still
        // `opacity:0`/`pointer-events:none` at this instant; scrollHeight
        // reflects the laid-out content regardless of that.
        if (islandTrigger && islandMenu) {
            const total = islandTrigger.scrollHeight + islandMenu.scrollHeight;
            island.style.height = total + 'px';
        }
        if (islandTrigger) islandTrigger.setAttribute('aria-expanded', 'true');
        if (islandMenu) islandMenu.setAttribute('aria-hidden', 'false');
        setTimeout(() => island.classList.remove('island-morphing'), 400);
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

    // Deliberately NOT wrapped in _runVT — the View Transition adds a pre-
    // animation snapshot pause that scales with the number of existing
    // `.session-card`s (each carries a view-transition-name and a
    // backdrop-filter, both expensive to re-rasterize), then plays a 400ms
    // `::view-transition-new` fade-in that overlaps with `.session-card`'s
    // own 380ms `liquid-entrance` CSS animation (styles.css). Two entrance
    // animations on one fresh card + N backdrop-filter snapshots = 200-400ms
    // of "hitch" on +New that reads to users as "waiting for the backend"
    // (there is no backend on this path — createSession is fully local).
    // The fix is to let `liquid-entrance` be the single entrance animation
    // and skip VT here entirely. Old-main-card → rail is a same-frame DOM
    // reparent; its rail wrapper gets a lightweight `stage-rail-thumb-enter`
    // CSS animation (styles.css) so it doesn't hard-snap either. All the
    // OTHER VT wrappers (minimize/maximize/promote-from-rail/close) stay —
    // those genuinely benefit from FLIP because they animate an existing
    // card between two positions, which liquid-entrance can't express.
    //
    // originRect (the button's own on-screen position) feeds _placeSession's
    // Genie entrance — the new card grows from the button instead of just
    // fading in place. See _playGenieEntrance.
    document.getElementById('sc-new-session').addEventListener('click', (e) => {
        const originRect = e.currentTarget.getBoundingClientRect();
        const sid = createSession({ originRect });
        window.__handqLog('INFO', 'sc-new-session clicked', { sid });
    });

    // ── Right sidebar (session detail: plan / activity / files) ──────────
    // The sidebar consumes file_touch / task_plan status envelopes plumbed
    // through the existing onStatus dispatcher above. Its ↺ Undo buttons
    // fan back out to the bridge's existing file_undo IPC (see
    // stdio_bridge.py _handle → msg_type=='file_undo'), scoped to the sid
    // the button was rendered for — matching how per-session mutations flow.
    //
    // MUST run BEFORE the bootstrap createSession() below — otherwise the
    // first session's _focusMainSession → SessionSidebar.setActiveSession
    // fires while dom.host is undefined, and the resulting exception
    // aborts _placeSession before it can call _updateLayout(). That was
    // the "new session covers current, rail not showing" bug: rail children
    // WERE moved correctly, but rail's data-empty attribute never got
    // toggled to false, so it stayed at 0 width.
    if (window.SessionSidebar) {
        try {
            window.SessionSidebar.init();
            window.SessionSidebar.onUndoRequest((sid, itemId) => {
                if (!sid || !itemId) return;
                window.__handqLog('INFO', 'sidebar undo', { sid, itemId });
                try {
                    handq.sendRequest({
                        type: 'file_undo',
                        session_id: sid,
                        item_id: itemId,
                    });
                } catch (err) {
                    window.__handqLog('ERROR', 'file_undo dispatch failed',
                        { err: err && err.message });
                }
            });
        } catch (err) {
            window.__handqLog('ERROR', 'SessionSidebar init failed',
                { err: err && err.message });
        }
    }

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

    // DIAGNOSTIC (2026-08-20): tracking a reported "Agent tab checkbox
    // won't check, Helper tab works fine" bug that isn't currently
    // reproducible. Every rebuild wipes and recreates the <input> nodes via
    // innerHTML — if that happens between a click's mousedown and its click
    // (e.g. triggered by the async getConfig() response landing while the
    // Agent tab, the default-visible one, is showing), the click can land on
    // a node that's already been torn out. These logs exist to catch that
    // race the next time it happens; remove once root-caused or once this
    // theory is ruled out.
    function renderModelCheckboxes(source) {
        window.__handqLog('INFO', 'renderModelCheckboxes rebuild', {
            source: source || 'unknown',
            activeTab: document.querySelector('.model-tab.active')?.dataset.tab,
        });
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
                container.innerHTML = '<span style="color:var(--fg-mute);font-size:var(--fs-subheadline)">Add models above first</span>';
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
    cfgLlmAvailableModels.addEventListener('input', () => renderModelCheckboxes('textarea-input'));

    // DIAGNOSTIC (2026-08-20): raw pointer/click trace on the two checkbox
    // lists — see the rebuild race theory above. Capture phase so we see the
    // event even if something upstream stops propagation. Logs the event
    // target's connectedness: a click landing on a detached (already
    // rebuilt-away) node is the smoking gun for the race.
    for (const [container, label] of [
        [cfgLlmAgentChecks, 'agent'],
        [cfgLlmHelperChecks, 'helper'],
    ]) {
        for (const evtName of ['mousedown', 'click']) {
            container.addEventListener(evtName, (ev) => {
                const t = ev.target;
                window.__handqLog('INFO', 'model-checks ' + evtName, {
                    list: label,
                    tag: t && t.tagName,
                    type: t && t.type,
                    value: t && t.value,
                    isConnected: t && t.isConnected,
                    checkedAfter: t && t.checked,
                });
            }, true);
        }
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
        const emailCfg = cfg.email || {};

        cfgLlmApiKey.value =
            (llm.API_KEY === undefined || llm.API_KEY === null) ? '' : String(llm.API_KEY);

        const resolved = resolveModelsAndHelper(llm);
        cfgLlmAvailableModels.value = modelsToText(resolved.pool);
        renderModelCheckboxes('applyConfigToForm');
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
        if ('max_tokens' in llm) delete llm.max_tokens;
        llm.API_KEY = cfgLlmApiKey.value;

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

        // No remote-control fields. `out` is a deep copy of the config we
        // loaded, so any existing `remote_control:` section is preserved
        // untouched — the settings form neither reads nor writes it. That is the
        // point: the section is advanced/yaml-only now, and a form that
        // round-tripped it would silently rewrite a hand-edited security switch.

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

    // ----- Remote control surface ---------------------------------------------
    //
    // A remote session's chat, tool cards, confirmations, task plan and activity
    // feed all work with no code here: the bridge replays the remote machine's
    // UI events onto this session's _StdioUI, so they arrive as the same
    // envelopes a local session produces. The only visible difference a tab
    // needs is a connection badge — chiefly so a dropped link reads as
    // "reconnecting, the remote agent is still working" instead of as a failure.

    const REMOTE_BADGE = {
        pending:      { text: '⇄ 连接中',  cls: 'pending' },
        connected:    { text: '⇄ 已连接',  cls: 'connected' },
        reconnecting: { text: '⇄ 重连中',  cls: 'reconnecting' },
        superseded:   { text: '⇄ 已被接管', cls: 'superseded' },
        closed:       { text: '⇄ 已结束',  cls: 'closed' },
        failed:       { text: '⇄ 连接失败', cls: 'failed' },
    };

    function markRemoteSessionState(sid, state, detail) {
        const s = sessions.get(sid);
        if (!s || !s.remoteBadgeEl) return;
        const spec = REMOTE_BADGE[state] || REMOTE_BADGE.pending;
        const badge = s.remoteBadgeEl;
        badge.className = 'session-card-remote ' + spec.cls;
        badge.textContent = spec.text;
        badge.title = detail
            ? `${spec.text} · ${detail}`
            : spec.text;
        if (s.card) s.card.dataset.remoteState = state;
        // Reconnecting is worth one activity row: the user needs to know why the
        // stream went quiet, and that it is expected to resume.
        if (state === 'reconnecting') {
            pushActivity('⇄', '远程连接中断', detail || '正在自动重连');
        } else if (state === 'connected' && s.card
                   && s.card.dataset.remoteWasDown === '1') {
            s.card.dataset.remoteWasDown = '';
            pushActivity('⇄', '远程连接已恢复', detail || '');
        }
        if (state === 'reconnecting' && s.card) s.card.dataset.remoteWasDown = '1';
    }

    if (window.HandQRemote) {
        try {
            window.HandQRemote.init();
        } catch (err) {
            window.__handqLog('ERROR', 'HandQRemote.init failed',
                { error: String(err && err.message) });
        }
    }

    // v6 Connect panel — new full-screen overlay for role selection + As
    // Server / As Client dashboards. Runs alongside HandQRemote, which still
    // owns the pairing dialogs and the local-tab ⇄ rc-session index this panel
    // delegates to (newSession / focusOrMount / addManual / addLinuxAuto).
    if (window.HandQConnect) {
        try {
            window.HandQConnect.init();
        } catch (err) {
            window.__handqLog('ERROR', 'HandQConnect.init failed',
                { error: String(err && err.message) });
        }
    }

    // Surface the handful of renderer internals the remote-control module needs.
    // Deliberately narrow — it drives sessions through the same public entry
    // points the UI itself uses, rather than reaching into `sessions`.
    window.HandQRenderer = {
        createSession,
        switchSession,
        closeSession,
        markRemoteSessionState,
        addGlobalSystemBubble,
        hasSession: (sid) => sessions.has(sid),
        // Panel-initiated operations that may need to prompt the user (e.g. a
        // first-time SSH password during Linux pairing) must stamp a session_id
        // so the bridge can route the prompt somewhere. There is always a
        // session to name: boot creates one and closeSession re-spawns when the
        // last is closed.
        currentSid,
    };
})();

// ----- Custom glass tooltip (replaces native title="" popups) --------------
//
// Chromium's default title= tooltip is an unstyled OS box that clashes with
// the liquid-glass aesthetic. A single shared floating element is driven via
// mouseover/mouseout delegation on document — one element, not one listener
// per node, so both DOM present at load AND session cards/list rows created
// later at runtime pick up tooltip behavior for free.
//
// Migration from title= to data-tooltip happens lazily on first hover
// (rather than a one-time DOMContentLoaded sweep) specifically because many
// callers across renderer.js/session-sidebar.js set `.title = '...'` on
// buttons they create well after page load (session close/send buttons,
// terminal panel controls, resume-card actions) — a one-time sweep would
// miss all of those.
(function () {
    const tip = document.getElementById('hq-tooltip');
    if (!tip) return;

    let showTimer = 0;
    let hideTimer = 0;
    let activeEl = null;

    function migrate(el) {
        const t = el.getAttribute('title');
        if (t) {
            el.setAttribute('data-tooltip', t);
            el.removeAttribute('title');
        }
        return el.getAttribute('data-tooltip');
    }

    function place(el) {
        const r = el.getBoundingClientRect();
        tip.style.left = '0px';
        tip.style.top = '0px';
        tip.classList.remove('hidden');
        const tw = tip.offsetWidth;
        let left = r.left + (r.width - tw) / 2;
        left = Math.max(4, Math.min(left, window.innerWidth - tw - 4));
        const top = r.bottom + 6;
        tip.style.left = left + 'px';
        tip.style.top = top + 'px';
    }

    function show(el, text) {
        activeEl = el;
        tip.textContent = text;
        place(el);
    }

    function hide() {
        activeEl = null;
        tip.classList.add('hidden');
    }

    document.addEventListener('mouseover', (e) => {
        const el = e.target.closest('[title], [data-tooltip]');
        if (!el || el === activeEl) return;
        const text = migrate(el);
        if (!text) return;
        clearTimeout(hideTimer);
        clearTimeout(showTimer);
        showTimer = setTimeout(() => show(el, text), 150);
    });

    document.addEventListener('mouseout', (e) => {
        const el = e.target.closest('[data-tooltip]');
        if (!el) return;
        clearTimeout(showTimer);
        hideTimer = setTimeout(hide, 150);
    });

    // Any click/scroll invalidates the current position rather than
    // tracking it live — tooltips are momentary, not worth a rAF loop.
    window.addEventListener('scroll', hide, true);
    document.addEventListener('mousedown', hide);
})();

// ----- Compositor prewarm for the sidebar's glass surfaces -----------------
//
// The right sidebar boots collapsed (data-collapsed="true", .ss-inner
// display:none — index.html/styles.css). The FIRST time it opens, Chromium
// paints .ss-section's backdrop-filter (blur+saturate) for the very first
// time ever — allocating an offscreen compositor buffer at that paint,
// which is measurably more expensive than every subsequent open (which
// reuses an already-warm buffer at the same size). That one-time cost is
// exactly the "first toggle is janky, then it's smooth" pattern users hit.
//
// Fix: paint an identical blurred surface once, off-screen and invisible,
// during idle time after boot — same border-radius/background/blur recipe
// as .ss-section, sized to the real sidebar's current width so the
// compositor buffer Chromium allocates is the one actually reused on first
// real open. visibility:hidden (not display:none) keeps it in the paint
// pipeline without making it interactive or visible; removed right after
// the paint has had a frame to land.
(function () {
    function prewarm() {
        const sidebar = document.getElementById('session-sidebar');
        if (!sidebar) return;
        const w = sidebar.getBoundingClientRect().width || 320;
        const ghost = document.createElement('div');
        ghost.className = 'ss-section';
        ghost.style.position = 'fixed';
        ghost.style.left = '-9999px';
        ghost.style.top = '0';
        ghost.style.width = w + 'px';
        ghost.style.height = '120px';
        ghost.style.visibility = 'hidden';
        ghost.style.pointerEvents = 'none';
        document.body.appendChild(ghost);
        requestAnimationFrame(() => {
            requestAnimationFrame(() => ghost.remove());
        });
    }
    if ('requestIdleCallback' in window) {
        requestIdleCallback(prewarm, { timeout: 2000 });
    } else {
        setTimeout(prewarm, 800);
    }
})();


