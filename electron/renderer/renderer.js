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
    const composerExpanded = document.getElementById('composer-expanded');
    const composerExpandedInput = document.getElementById('composer-expanded-input');
    const composerExpandedClose = document.getElementById('composer-expanded-close');

    // Shortcut bar
    const scSettings = document.getElementById('sc-settings');
    const scScheduler = document.getElementById('sc-scheduler');
    const scNew      = document.getElementById('sc-new');
    const scTemplates = document.getElementById('sc-templates');

    // Titlebar
    const tbMin   = document.getElementById('tb-min');
    const tbMax   = document.getElementById('tb-max');
    const tbClose = document.getElementById('tb-close');

    // Activity strip (lives inline in the shortcuts bar) + popover (anchored
    // above it, holds the full feed and opens on click).
    const activityStrip   = document.getElementById('activity-strip');
    const activityCurrent = document.getElementById('activity-current');
    const activityPopover = document.getElementById('activity-popover');
    const activityClose   = document.getElementById('activity-close');
    const activityFeed    = document.getElementById('activity-feed');

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
    const overlayConfirm   = document.getElementById('overlay-confirmation');
    const overlayGep       = document.getElementById('overlay-gep');
    const overlayTemplates = document.getElementById('overlay-templates');
    const settingsCancel   = document.getElementById('settings-cancel');
    const confirmTitle     = document.getElementById('confirm-title');
    const confirmDescEl    = document.getElementById('confirm-description');
    const confirmDecisionEl= document.getElementById('confirm-decision');
    const confirmSecretWrap= document.getElementById('confirm-secret-wrap');
    const confirmSecretIn  = document.getElementById('confirm-secret-input');
    const confirmGuidanceEl= document.getElementById('confirm-guidance');
    const confirmGuidBtn   = document.getElementById('confirm-guidance-btn');
    const confirmRejectBtn = document.getElementById('confirm-reject');
    const confirmSubmitBtn = document.getElementById('confirm-submit');

    // Settings form
    const settingsForm     = document.getElementById('settings-form');
    const settingsLoadBtn  = document.getElementById('settings-load');
    const settingsStatus   = document.getElementById('settings-status');
    const settingsToast    = document.getElementById('settings-toast');

    const cfgLlmApiKey       = document.getElementById('cfg-llm-api-key');
    const cfgLlmApiKeyToggle = document.getElementById('cfg-llm-api-key-toggle');
    const cfgLlmMaxTokens    = document.getElementById('cfg-llm-max-tokens');
    const cfgLlmModels       = document.getElementById('cfg-llm-models');
    const cfgLlmRoleTabs     = document.getElementById('cfg-llm-role-tabs');
    const cfgLlmRolePanes = {
        planner:      document.getElementById('cfg-llm-role-pane-planner'),
        receptionist: document.getElementById('cfg-llm-role-pane-receptionist'),
        agent:        document.getElementById('cfg-llm-role-pane-agent'),
        helper:       document.getElementById('cfg-llm-role-pane-helper'),
    };
    const cfgSessionLogLevel      = document.getElementById('cfg-session-log-level');
    const cfgSessionStepThreshold = document.getElementById('cfg-session-step-threshold');
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

    // ----- session/state tracking (drives Status overlay + pill) -----------

    const session = {
        state:      'idle',
    };

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

    // Thinking bubble shown in chat while receptionist prepares a reply.
    let thinkingBubble = null;

    // First "replanning" state is displayed as "designing" (initial plan).
    let firstReplanSeen = false;

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
        // hover the strip mid-completion to see what's happening underneath.
        if (activityStrip) activityStrip.title = text || '';
        if (taskCompleted && !o.force) return;
        if (activityCurrent) activityCurrent.textContent = text || 'idle';
    }

    function markCompleted(summary) {
        taskCompleted = true;
        if (activityStrip) {
            activityStrip.classList.add('complete');
            activityStrip.title = summary
                ? ('complete — ' + truncate(summary, 200))
                : 'complete';
        }
        if (activityCurrent) activityCurrent.textContent = 'complete';
        session.state = 'complete';
        if (summary) addAssistantTextBubble(summary);
    }

    function clearCompleted() {
        if (!taskCompleted) return;
        taskCompleted = false;
        if (activityStrip) {
            activityStrip.classList.remove('complete');
            activityStrip.title = '';
        }
        if (activityCurrent) activityCurrent.textContent = 'idle';
        session.state = 'idle';
    }

    function truncate(s, n) {
        if (!s) return '';
        return s.length > n ? s.slice(0, n - 1) + '…' : s;
    }

    // Whether the planner/agent has a real task in flight. Used to gate
    // receptionist-side pill updates so a chat reply mid-task can't reset
    // the activity strip to "idle". `session.state` is set by state_changed
    // events; `activeExecCount` covers the brief window where a tool is
    // running before the next state_changed lands.
    function isTaskRunning() {
        return session.state === 'executing'
            || session.state === 'replanning'
            || activeExecCount > 0;
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

    // ----- Activity strip + popover ---------------------------------------
    //
    // The strip (inline in the shortcuts bar) is always visible and shows the
    // most recent agent event as a single muted line. Clicking the strip
    // opens the popover above it with the full ring-buffer feed.

    const ACTIVITY_RING  = 30;
    const ACTIVITY_TRUNC = 2000;
    const activityItems  = [];
    let   activityLiveTimer = null;
    let   popoverOpen = false;

    function pulseActivityLive() {
        if (!activityStrip) return;
        activityStrip.classList.add('live');
        if (activityLiveTimer) clearTimeout(activityLiveTimer);
        activityLiveTimer = setTimeout(() => {
            if (!activityStrip.classList.contains('working')) {
                activityStrip.classList.remove('live');
            }
        }, 2500);
    }

    function setWorking(text) {
        if (!activityStrip) return;
        if (taskCompleted) return;
        if (activityLiveTimer) clearTimeout(activityLiveTimer);
        activityStrip.classList.add('live', 'working');
        if (activityCurrent) activityCurrent.textContent = text || '';
        activityStrip.title = text || '';
    }

    function clearWorking() {
        if (!activityStrip) return;
        activityStrip.classList.remove('working');
    }

    // ----- Confidence gauge ------------------------------------------------
    //
    // A 5-character Unicode block sparkline (▁▂▃▄▅▆▇█) followed by the latest
    // confidence as an integer 0–100. Right-aligned: newest character on the
    // right. Grayscale only — no per-tier colors. Driven by the
    // step_confidence envelope (stdio_bridge.py `notify_step_confidence`).

    const activityGauge = document.getElementById('activity-gauge');
    const agSpark       = document.getElementById('ag-spark');
    const agNum         = document.getElementById('ag-num');
    const GAUGE_CAPACITY = 5;
    // ▁ U+2581 .. █ U+2588 — eight discrete heights, mapped from conf ∈ [0,1].
    const SPARK_CHARS = ['▁', '▂', '▃', '▄',
                         '▅', '▆', '▇', '█'];
    const confidenceHistory = [];

    function _confChar(c) {
        const idx = Math.max(1, Math.min(8, Math.ceil(c * 8)));
        return SPARK_CHARS[idx - 1];
    }

    function _renderGauge() {
        if (!agSpark || !agNum) return;
        if (confidenceHistory.length === 0) {
            agSpark.textContent = '';
            agNum.textContent = '';
            return;
        }
        agSpark.textContent = confidenceHistory.map(_confChar).join('');
        const latest = confidenceHistory[confidenceHistory.length - 1];
        agNum.textContent = String(Math.round(latest * 100)) + '%';
    }

    function setConfidenceGauge(conf) {
        if (!activityStrip || !activityGauge) return;
        let c = Number(conf);
        if (!Number.isFinite(c)) return;
        if (c < 0) c = 0;
        if (c > 1) c = 1;
        confidenceHistory.push(c);
        if (confidenceHistory.length > GAUGE_CAPACITY) confidenceHistory.shift();
        _renderGauge();
        activityStrip.title = activityStrip.title +
            (activityStrip.title ? ' · ' : '') +
            'conf ' + c.toFixed(2);
    }

    function resetGauge() {
        confidenceHistory.length = 0;
        _renderGauge();
    }

    // Track last tool name for post-execution display (backend sends None for tool in post events)
    var lastCalledTool = '';
    // Count of currently in-flight tool executions. When > 0, the strip stays
    // in "working" state showing the active execution rather than flipping to
    // a completed result.
    var activeExecCount = 0;

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

    function pushActivity(icon, label, content, opts) {
        if (!activityStrip) return null;
        const time = new Date().toLocaleTimeString([], { hour12: false });
        const entry = {
            icon: icon || '·',
            label: label || '',
            content: content == null ? '' : String(content),
            time: time,
            // Optional pairing tags — set by the tool_execution_started pre
            // branch so the matching post event can find this entry and
            // attach a result instead of pushing a separate Done item.
            iter: opts && opts.iter != null ? String(opts.iter) : null,
            tool: opts && opts.tool ? String(opts.tool) : null,
            pending: !!(opts && opts.pending),
            resultIcon: null,
            resultContent: null,
        };
        activityItems.push(entry);
        if (activityItems.length > ACTIVITY_RING) activityItems.shift();
        renderActivityFeed();
        // Refresh the strip text with a one-line preview of the latest entry.
        const preview = entry.icon + ' ' + entry.label +
            (entry.content ? ' · ' + truncate(entry.content.replace(/\s+/g, ' '), 120) : '');
        setPill(preview);
        pulseActivityLive();
        return entry;
    }

    // Find the oldest pending entry matching (iter, tool) and attach a
    // result to it — folding the post-execution event into the same
    // activity item that announced the pre-execution. Falls back to a
    // fresh activity entry if no matching pending entry is found (e.g.
    // it was evicted from the ring buffer).
    function updateActivityResult(iter, tool, resultIcon, headLabel, resultContent) {
        var iterStr = iter == null ? null : String(iter);
        var match = null;
        for (var i = 0; i < activityItems.length; i++) {
            var e = activityItems[i];
            if (!e.pending) continue;
            if (iterStr != null && e.iter !== iterStr) continue;
            if (tool && e.tool && e.tool !== tool) continue;
            match = e;
            break;
        }
        if (!match) {
            // Pending entry already evicted (or never recorded). Degrade to
            // the legacy behaviour so the result is still surfaced.
            pushActivity(resultIcon || '✓', (tool || 'tool') + ' done', resultContent || '');
            return;
        }
        match.icon = resultIcon || '✓';
        match.label = headLabel || match.tool || match.label;
        match.resultIcon = resultIcon || '✓';
        match.resultContent = resultContent == null ? '' : String(resultContent);
        match.pending = false;
        renderActivityFeed();
        // Refresh the strip text so the most recent completion is visible.
        const preview = match.icon + ' ' + match.label +
            (match.resultContent ? ' · ' + truncate(match.resultContent.replace(/\s+/g, ' '), 120) : '');
        setPill(preview);
        pulseActivityLive();
    }

    function renderActivityFeed() {
        if (!activityFeed) return;
        activityFeed.innerHTML = '';
        // Newest at the top — common for notification-style feeds.
        for (let i = activityItems.length - 1; i >= 0; i--) {
            const entry = activityItems[i];
            const li = el('li', 'activity-item');

            const head = el('div', 'ai-head');
            head.appendChild(el('span', 'ai-icon', entry.icon));
            head.appendChild(el('span', 'ai-label', entry.label));
            head.appendChild(el('span', 'ai-time', entry.time));
            li.appendChild(head);

            if (entry.content) {
                const contentIsJson = isJsonString(entry.content);
                const content = el('span', 'ai-content' + (contentIsJson ? ' ai-json' : ''));
                if (contentIsJson) {
                    content.appendChild(renderJsonContent(entry.content));
                } else {
                    content.textContent = truncate(entry.content, ACTIVITY_TRUNC);
                }
                li.appendChild(content);
                li.title = contentIsJson ? '' : entry.content;
            }

            // Threaded result line: when a pending exec entry has been
            // sealed by its post-event, render the result inside the same
            // <li> with a leading "↳" so it's clearly tied to the command
            // above. Collapsed by default; expands with the parent click.
            if (entry.resultContent) {
                const resultIsJson = isJsonString(entry.resultContent);
                const resultEl = el('div', 'ai-result' + (resultIsJson ? ' ai-json' : ''));
                if (resultIsJson) {
                    resultEl.appendChild(document.createTextNode('↳ '));
                    resultEl.appendChild(renderJsonContent(entry.resultContent));
                } else {
                    resultEl.textContent = '↳ ' + truncate(entry.resultContent, ACTIVITY_TRUNC);
                }
                li.appendChild(resultEl);
            }

            li.addEventListener('click', () => {
                li.classList.toggle('expanded');
                const c = li.querySelector('.ai-content');
                if (c && !c.classList.contains('ai-json')) {
                    c.textContent = li.classList.contains('expanded')
                        ? entry.content
                        : truncate(entry.content, ACTIVITY_TRUNC);
                }
                const r = li.querySelector('.ai-result');
                if (r && !r.classList.contains('ai-json')) {
                    r.textContent = '↳ ' + (li.classList.contains('expanded')
                        ? entry.resultContent
                        : truncate(entry.resultContent, ACTIVITY_TRUNC));
                }
            });

            activityFeed.appendChild(li);
        }
    }

    function clearActivity() {
        activityItems.length = 0;
        renderActivityFeed();
        if (activityStrip) activityStrip.classList.remove('live');
    }

    function openPopover() {
        if (!activityPopover) return;
        activityPopover.classList.remove('hidden');
        activityPopover.setAttribute('aria-hidden', 'false');
        if (activityStrip) {
            activityStrip.classList.add('open');
            activityStrip.setAttribute('aria-expanded', 'true');
        }
        popoverOpen = true;
    }

    function closePopover() {
        if (!activityPopover) return;
        activityPopover.classList.add('hidden');
        activityPopover.setAttribute('aria-hidden', 'true');
        if (activityStrip) {
            activityStrip.classList.remove('open');
            activityStrip.setAttribute('aria-expanded', 'false');
        }
        popoverOpen = false;
    }

    function togglePopover() {
        if (popoverOpen) closePopover();
        else openPopover();
    }

    if (activityStrip) {
        activityStrip.addEventListener('click', (e) => {
            e.stopPropagation();
            togglePopover();
        });
    }
    if (activityClose) {
        activityClose.addEventListener('click', (e) => {
            e.stopPropagation();
            closePopover();
        });
    }
    // Click outside both strip and popover closes the popover.
    document.addEventListener('click', (e) => {
        if (!popoverOpen) return;
        if (activityPopover && activityPopover.contains(e.target)) return;
        if (activityStrip && activityStrip.contains(e.target)) return;
        closePopover();
    });

    // ── Activity popover proportional resize (handle at bottom-left) ──────
    (function initActivityResize() {
        const handle = document.getElementById('activity-resize-handle');
        if (!handle || !activityPopover) return;
        let resizing = false, startX, startY, startW, startH, aspect;

        handle.addEventListener('mousedown', (e) => {
            e.preventDefault();
            e.stopPropagation();
            resizing = true;
            startX = e.clientX;
            startY = e.clientY;
            startW = activityPopover.offsetWidth;
            // Use max(offsetHeight, 200) to avoid an extreme aspect ratio when
            // the feed has very few items and the natural height is tiny.
            startH = Math.max(activityPopover.offsetHeight, 200);
            aspect = startW / startH;
            document.body.style.userSelect = 'none';
        });

        document.addEventListener('mousemove', (e) => {
            if (!resizing) return;
            // Dragging bottom-left handle: left decreases X → width grows,
            // down increases Y → height grows. Use the larger delta to drive
            // proportional scaling.
            const dx = startX - e.clientX; // positive = dragged left = bigger
            const dy = e.clientY - startY; // positive = dragged down = bigger
            // dy converts to width-delta via aspect (width/height), not aspect^2.
            const delta = Math.abs(dx) >= Math.abs(dy) ? dx : dy * aspect;
            let newW = startW + delta;
            let newH = newW / aspect;

            // Clamp to minimum
            if (newW < 320) { newW = 320; newH = newW / aspect; }
            if (newH < 200) { newH = 200; newW = newH * aspect; }

            // Clamp to app window size (can't exceed viewport)
            const maxW = window.innerWidth - 28;
            const maxH = window.innerHeight - 80;
            if (newW > maxW) { newW = maxW; newH = newW / aspect; }
            if (newH > maxH) { newH = maxH; newW = newH * aspect; }

            activityPopover.style.width = Math.round(newW) + 'px';
            // Use style.height (not maxHeight) so shrinking actually reduces
            // the rendered height regardless of content amount.
            activityPopover.style.height = Math.round(newH) + 'px';
        });

        document.addEventListener('mouseup', () => {
            if (resizing) {
                resizing = false;
                document.body.style.userSelect = '';
            }
        });
    })();

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
        // Most-recent text-stream segment. Reset to null whenever a tool
        // card is appended so subsequent text deltas land *after* the card
        // in DOM order (preserving the actual chronological interleaving).
        bubble._currentTextSpan = null;
        conversation.appendChild(bubble);
        activeAssistantBubble = bubble;
        scrollToBottom();
        return bubble;
    }

    function ensureAssistantBubble() {
        if (!activeAssistantBubble) return startAssistantBubble();
        return activeAssistantBubble;
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

    function appendTextDelta(text) {
        if (!text) return;
        const bubble = ensureAssistantBubble();
        let span = bubble._currentTextSpan;
        if (!span) {
            span = el('div', 'text-stream md-rendered');
            span._rawText = '';
            bubble._body.appendChild(span);
            bubble._currentTextSpan = span;
        }
        span._rawText += text;
        scheduleMarkdownRender(span);
        scrollToBottom();
    }

    function renderToolCall(callId, toolName, args, blockIndex) {
        const bubble = ensureAssistantBubble();
        // Close out the current text segment so any text that arrives next
        // appears in a NEW span placed after this tool card.
        bubble._currentTextSpan = null;

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
        // Force a final render so the last delta isn't stuck in rAF.
        if (activeAssistantBubble._currentTextSpan) {
            const span = activeAssistantBubble._currentTextSpan;
            try { span.innerHTML = renderMarkdown(span._rawText || ''); }
            catch (_) { span.textContent = span._rawText || ''; }
        }
        activeAssistantBubble = null;
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
        conversation.appendChild(bubble);
        scrollToBottom();
    }

    // Streaming receptionist reply — uses same pattern as appendTextDelta
    var activeReceptionistBubble = null;

    function appendReceptionistDelta(text) {
        if (!text) return;
        if (!activeReceptionistBubble) {
            activeReceptionistBubble = el('div', 'bubble assistant streaming');
            const body = el('div', 'bubble-body');
            activeReceptionistBubble.appendChild(body);
            activeReceptionistBubble._body = body;
            activeReceptionistBubble._currentTextSpan = null;
            conversation.appendChild(activeReceptionistBubble);
        }
        var span = activeReceptionistBubble._currentTextSpan;
        if (!span) {
            span = el('div', 'text-stream md-rendered');
            span._rawText = '';
            activeReceptionistBubble._body.appendChild(span);
            activeReceptionistBubble._currentTextSpan = span;
        }
        span._rawText += text;
        scheduleMarkdownRender(span);
        scrollToBottom();
    }

    function sealReceptionistBubble() {
        if (!activeReceptionistBubble) return;
        activeReceptionistBubble.classList.remove('streaming');
        activeReceptionistBubble.classList.add('complete');
        if (activeReceptionistBubble._currentTextSpan) {
            var span = activeReceptionistBubble._currentTextSpan;
            try { span.innerHTML = renderMarkdown(span._rawText || ''); }
            catch (_) { span.textContent = span._rawText || ''; }
        }
        activeReceptionistBubble = null;
    }

    function showThinkingBubble() {
        if (thinkingBubble) return;
        thinkingBubble = el('div', 'bubble assistant thinking-indicator');
        var body = el('div', 'bubble-body');
        var dots = el('span', 'thinking-dots');
        dots.appendChild(el('span', 'dot'));
        dots.appendChild(el('span', 'dot'));
        dots.appendChild(el('span', 'dot'));
        body.appendChild(dots);
        thinkingBubble.appendChild(body);
        conversation.appendChild(thinkingBubble);
        scrollToBottom();
    }

    function removeThinkingBubble() {
        if (!thinkingBubble) return;
        if (thinkingBubble.parentNode) thinkingBubble.parentNode.removeChild(thinkingBubble);
        thinkingBubble = null;
    }

    function addSystemBubble(text) {
        const bubble = el('div', 'bubble system');
        bubble.appendChild(el('div', 'bubble-body', text || ''));
        conversation.appendChild(bubble);
        scrollToBottom();
    }

    function addStepBubble(icon, desc) {
        const bubble = el('div', 'bubble step');
        const body = el('div', 'bubble-body');
        const time = new Date().toLocaleTimeString([], { hour12: false });
        body.textContent = time + '  ' + (icon ? icon + ' ' : '') + (desc || '');
        bubble.appendChild(body);
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

    // Track the prompt id of the currently shown modal so each Approve /
    // Reject / Submit click sends back a single response. Reset to null
    // after responding to avoid double-sends if the user rapid-clicks.
    let activePromptId = null;
    // Track whether the active prompt expects a yes/no answer (confirm)
    // or a free-form string (secret_input).
    let activePromptKind = null;

    function renderDecisionSummary(decision) {
        // decision: { tool_calls: [{tool_name, params: {...}}], reasoning }
        confirmDecisionEl.textContent = '';
        if (!decision) {
            confirmDecisionEl.classList.add('hidden');
            return;
        }
        const calls = Array.isArray(decision.tool_calls) ? decision.tool_calls : [];
        if (calls.length === 0 && !decision.reasoning) {
            confirmDecisionEl.classList.add('hidden');
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
        confirmDecisionEl.textContent = lines.join('\n');
        confirmDecisionEl.classList.remove('hidden');
    }

    function showConfirmationModal(evt) {
        // Idempotency: if a previous prompt was never answered (shouldn't
        // happen — the bridge serialises confirmations), the new one
        // overwrites the active id. The earlier waiter on the Python side
        // will time out only if we never send any response, but in
        // practice the user clicks one of the new buttons and that reply
        // unblocks whichever waiter is still pending in IM.
        activePromptId = String(evt.id || '');
        activePromptKind = String(evt.kind || '');

        // Reset any scope-specific styling from a previous modal.
        const confirmCard = overlayConfirm.querySelector('.confirmation-card');
        if (confirmCard) confirmCard.classList.remove('desktop-takeover');

        if (evt.kind === 'secret_input') {
            confirmTitle.textContent = 'Input required';
            confirmDescEl.textContent = String(evt.prompt || 'Enter value:');
            renderDecisionSummary(null);
            confirmSecretWrap.classList.remove('hidden');
            confirmSecretIn.value = '';
            try { confirmSecretIn.type = 'password'; } catch (_) { /* ignore */ }
            confirmGuidanceEl.classList.add('hidden');
            confirmGuidanceEl.value = '';
            confirmRejectBtn.classList.add('hidden');
            confirmGuidBtn.classList.add('hidden');
            confirmSubmitBtn.textContent = 'Submit';
        } else if (evt.kind === 'ask_human') {
            confirmTitle.textContent = 'Question from agent';
            confirmDescEl.textContent = String(evt.prompt || 'The agent has a question:');
            renderDecisionSummary(null);
            confirmSecretWrap.classList.remove('hidden');
            confirmSecretIn.value = '';
            // Non-masked input — the user is typing a clarifying answer,
            // not a secret. Reset to password before the next secret_input.
            try { confirmSecretIn.type = 'text'; } catch (_) { /* ignore */ }
            confirmGuidanceEl.classList.add('hidden');
            confirmGuidanceEl.value = '';
            confirmRejectBtn.classList.add('hidden');
            confirmGuidBtn.classList.add('hidden');
            confirmSubmitBtn.textContent = 'Send';
        } else {
            // risk_confirmation or tool_confirmation
            const isRisk = evt.kind === 'risk_confirmation';
            const isDesktopTakeover = evt.scope === 'task' && evt.tool === 'desktop';
            confirmTitle.textContent = isRisk
                ? 'High-risk operation'
                : (isDesktopTakeover
                    ? 'Grant desktop control for this task?'
                    : 'Confirm ' + (evt.tool || 'tool') + ' execution');
            // Backend now ships an explicit description for desktop
            // tool_confirmation (and could for any future task-scoped
            // gate). Prefer it whenever present, falling back to the
            // generic sentence for legacy / other tools.
            const description = evt.description ? String(evt.description) : '';
            if (isRisk) {
                confirmDescEl.textContent = description;
            } else if (description) {
                confirmDescEl.textContent = description;
            } else {
                confirmDescEl.textContent =
                    'The agent wants to run "' + (evt.tool || 'tool') +
                    '" with the parameters below.';
            }
            renderDecisionSummary(evt.decision);
            confirmSecretWrap.classList.add('hidden');
            confirmSecretIn.value = '';
            confirmGuidanceEl.classList.add('hidden');
            confirmGuidanceEl.value = '';
            confirmRejectBtn.classList.remove('hidden');
            confirmGuidBtn.classList.remove('hidden');
            confirmGuidBtn.textContent = 'Provide guidance';
            confirmSubmitBtn.textContent = isDesktopTakeover ? 'Approve task-wide' : 'Approve';
            // Tag the card so styles.css can paint it more loudly for the
            // task-scoped desktop approval.
            if (confirmCard && isDesktopTakeover) {
                confirmCard.classList.add('desktop-takeover');
            }
        }

        openOverlay(overlayConfirm);
        // Focus management: passwords / ask_human get the input; risk gates
        // need an explicit click on Approve so we don't auto-focus the primary.
        if (evt.kind === 'secret_input' || evt.kind === 'ask_human') {
            try { confirmSecretIn.focus(); } catch (_) { /* ignore */ }
        }
    }

    function sendConfirmationAnswer(answer) {
        if (!activePromptId) return;
        try {
            handq.sendRequest({
                type: 'user_input',
                kind: 'confirmation',
                answer: String(answer || ''),
            });
        } catch (e) {
            window.__handqLog('ERROR', 'confirm send failed',
                { id: activePromptId, error: String(e) });
        }
        activePromptId = null;
        activePromptKind = null;
        closeOverlay(overlayConfirm);
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
        // Token stream = bridge is fully alive and the engine is producing
        // output. Drop the boot overlay if it somehow lingered past the
        // first status event.
        if (!bootHidden) hideBootOverlay();
        if (kind === 'text_delta') {
            appendTextDelta(evt.text || '');
        } else if (kind === 'tool_call') {
            renderToolCall(evt.call_id, evt.tool_name, evt.args, evt.block_index);
        } else if (kind === 'done') {
            sealActiveBubble();
        }
    });

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
            session.state = evt.state;
            if (evt.state === 'replanning') {
                if (!firstReplanSeen) {
                    firstReplanSeen = true;
                    setWorking('designing…');
                } else {
                    setWorking('replanning…');
                }
            } else {
                if (evt.state === 'executing') {
                    clearWorking();
                }
                setPill(evt.state);
            }
            // Keep the Templates panel "Load history" button in sync with
            // the running-task state — it should refuse imports while a
            // task is in flight.
            try { refreshLoadHistoryEnabled(); } catch (_) { /* declared later */ }
        } else if (evt.kind === 'gep_intro') {
            try {
                gepRenderTemplate(evt.template || {});
            } catch (e) {
                window.__handqLog('ERROR', 'gep_intro render failed',
                    { error: String(e) });
            }
        } else if (evt.kind === 'gep_countdown') {
            try {
                gepUpdateCountdown(typeof evt.remaining === 'number' ? evt.remaining : -1);
            } catch (e) {
                window.__handqLog('ERROR', 'gep_countdown update failed',
                    { error: String(e) });
            }
        } else if (evt.kind === 'inline_event') {
            // Backend-emitted step-style line (e.g. GEP banner messages).
            // Render with addStepBubble so it visually matches planner step
            // events instead of the chunkier system bubble used by display_message.
            addStepBubble(String(evt.icon || '·'), String(evt.desc || ''));
        } else if (evt.kind === 'progress') {
            const cur = evt.current || 0;
            const tot = evt.total || 0;
            setPill('progress ' + cur + '/' + tot);
            addStepBubble('•', 'Progress ' + cur + '/' + tot);
        } else if (evt.kind === 'step_started') {
            const stepId = String(evt.step_id || args[0] || '');
            const desc = String(evt.desc || args[1] || '');
            pushActivity('▶', 'Step started', desc);
            setWorking('▶ ' + truncate(desc, 120));
            addStepBubble('▶', desc);
        } else if (evt.kind === 'step_completed') {
            const stepId = String(evt.step_id || args[0] || '');
            const desc = String(evt.desc || args[1] || '');
            pushActivity('✓', 'Step completed', desc);
            setPill('✓ ' + truncate(desc, 120));
            addStepBubble('✓', desc);
        } else if (evt.kind === 'step_confidence') {
            const conf = parseFloat(args[0]);
            if (!Number.isNaN(conf)) {
                pushActivity('◎', 'Step confidence', Math.round(conf * 100) + '%');
                setConfidenceGauge(conf);
            }
        } else if (evt.kind === 'decision_made') {
            const iter = args[0] || '';
            const reasoning = args[1] || '';
            pushActivity('💭', 'Decision iter ' + iter, reasoning);
            setWorking('💭 ' + truncate(reasoning, 120));
        } else if (evt.kind === 'tool_execution_started') {
            const iter   = args[0] || '';
            var rawTool  = args[1] || '';
            const params = args[2];
            const output = args[3];
            // Backend now sends tool_name in BOTH pre and post events; the
            // pre/post discriminator is the output field (null for pre,
            // populated for post). The "None"/"null" guards stay in place
            // for backwards-compatible payloads.
            var tool = (rawTool && rawTool !== 'None' && rawTool !== 'null') ? rawTool : '';
            var isPre = output === undefined || output === null
                        || output === 'None' || output === 'null';
            if (isPre && tool) lastCalledTool = tool;
            var effectiveTool = tool || lastCalledTool || 'action';
            const paramText = formatToolParams(params);
            if (isPre) {
                activeExecCount++;
                var ctx = briefToolContext(effectiveTool, params);
                var preLabel = 'Executing ' + effectiveTool;
                var preContent = ctx || paramText;
                pushActivity('⊙', preLabel, preContent, {
                    iter: iter, tool: effectiveTool, pending: true,
                });
                setWorking('⊙ ' + effectiveTool + (ctx ? ' · ' + ctx : ''));
            } else {
                activeExecCount = Math.max(0, activeExecCount - 1);
                var readable = formatResultReadable(effectiveTool, output);
                var resultText = readable || (output == null ? '' : String(output));
                var resultIcon = (resultText && resultText.charAt(0) === '✗') ? '✗' : '✓';
                updateActivityResult(iter, effectiveTool, resultIcon,
                                     effectiveTool, resultText);
                if (activeExecCount === 0) {
                    clearWorking();
                    setPill(resultIcon + ' ' + effectiveTool +
                            (readable ? ' · ' + readable : ''));
                }
            }
        } else if (evt.kind === 'task_completed') {
            const summary = evt.summary
                || (args.length ? String(args[0]) : '')
                || '';
            // Push to feed FIRST, then mark complete — the markCompleted
            // setter pins the strip text to "complete" via its taskCompleted
            // lock, and we want the entry to land in the popover regardless.
            pushActivity('🏁', 'Task completed', summary);
            markCompleted(summary);
        } else if (evt.kind === 'metrics_report') {
            addAssistantTextBubble(evt.text || '');
        } else if (evt.kind === 'bridge_exit') {
            session.state = 'bridge exited';
            setPill('bridge exited', { force: true });
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
        } else if (evt.kind === 'message') {
            addSystemBubble(evt.text || '');
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
        } else if (evt.kind === 'llm_server_error') {
            const retryIn = (typeof evt.retry_in === 'number' && evt.retry_in > 0)
                ? ' — retrying in ' + evt.retry_in + 's'
                : '';
            const attLeft = (typeof evt.attempts_left === 'number' && evt.attempts_left > 0)
                ? ' (' + evt.attempts_left + ' attempt' + (evt.attempts_left !== 1 ? 's' : '') + ' remaining)'
                : '';
            const errSummary = (evt.message || 'API server issue') + retryIn + attLeft;
            addSystemBubble('⏳ ' + errSummary
                + '\nThis is a temporary API server issue, not a HandQ problem.'
                + ' Retrying automatically — please wait.');
            pushActivity('⏳', 'API retry', errSummary);
            setPill('retrying…');
        } else if (evt.kind === 'llm_fallback') {
            const fromModel = String(evt.from_model || '?');
            const toModel   = String(evt.to_model   || '?');
            const reason    = evt.error ? ' — ' + evt.error : '';
            addSystemBubble('↪ ' + fromModel + ' failed; trying ' + toModel + reason);
            pushActivity('↪', 'Model fallback', fromModel + ' → ' + toModel);
        } else if (evt.kind === 'network_down') {
            addSystemBubble('📡 ' + (evt.message || '网络中断，等待恢复…')
                + '\nHandQ will resume automatically once the connection is restored.');
            pushActivity('📡', 'Network down', 'waiting for LLM endpoint');
            setPill('offline…', { force: true });
        } else if (evt.kind === 'network_waiting') {
            const retryIn = (typeof evt.retry_in === 'number' && evt.retry_in > 0)
                ? evt.retry_in + 's' : '…';
            pushActivity('📡', 'Still offline', 'attempt ' + (evt.attempt || '?') + ', next probe in ' + retryIn);
        } else if (evt.kind === 'network_restored') {
            addSystemBubble('✅ ' + (evt.message || '网络已恢复，继续执行'));
            pushActivity('✅', 'Network restored', 'resuming');
            setPill('working…');
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
        // Any final response means the bridge is alive and serving — fade
        // the boot overlay if it's still up.
        if (!bootHidden) hideBootOverlay();

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
            session.state = 'fatal';
            setPill('fatal', { force: true });
        }
    });

    // ----- composer --------------------------------------------------------

    let firstSendDone = false;

    composer.addEventListener('submit', (e) => {
        e.preventDefault();
        if (expandedOpen) {
            composerInput.value = composerExpandedInput.value;
            expandedOpen = false;
            composerExpanded.classList.add('hidden');
        }
        const text = composerInput.value.trim();
        if (!text) return;

        // Hidden admin command. Typing /memory in the composer opens
        // the LTM admin overlay instead of dispatching to the bridge.
        // Variants accepted: /memory, /memory/, /MEMORY, "  /memory  ".
        // We swallow the input (no bubble, no flow trigger) and just
        // toggle the panel — that way the admin surface stays hidden
        // from anyone who doesn't already know the magic word.
        if (/^\/memory\/?$/i.test(text)) {
            composerInput.value = '';
            composerExpandedInput.value = '';
            if (window.adminPanel) {
                if (window.adminPanel.isOpen()) window.adminPanel.close();
                else window.adminPanel.open();
            }
            return;
        }

        // Sister command: /schedules opens the recurring-task manager.
        // Functionally independent from /memory (the scheduler doesn't
        // touch memory.db). Accepts both /schedules and /tasks as an
        // alias since the feature is naturally called either thing
        // depending on the user's mental model.
        if (/^\/(schedules?|tasks?)\/?$/i.test(text)) {
            composerInput.value = '';
            composerExpandedInput.value = '';
            if (window.schedulePanel) {
                if (window.schedulePanel.isOpen()) window.schedulePanel.close();
                else window.schedulePanel.open();
            }
            return;
        }

        addUserBubble(text);
        composerInput.value = '';
        composerExpandedInput.value = '';
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
        if (e.key === 'Enter' && e.ctrlKey) {
            e.preventDefault();
            composer.requestSubmit();
        }
    });

    // ----- floating expanded editor ----------------------------------------

    let expandedOpen = false;

    function openExpanded() {
        if (expandedOpen) return;
        expandedOpen = true;
        composerExpandedInput.value = composerInput.value;
        composerExpanded.classList.remove('hidden');
        composerExpandedInput.focus();
        composerExpandedInput.selectionStart = composerExpandedInput.value.length;
    }

    function closeExpanded() {
        if (!expandedOpen) return;
        expandedOpen = false;
        composerInput.value = composerExpandedInput.value;
        composerExpanded.classList.add('hidden');
        composerInput.focus();
    }

    function checkOverflow() {
        if (expandedOpen) return;
        if (composerInput.scrollHeight > composerInput.clientHeight + 4) {
            openExpanded();
        }
    }

    composerInput.addEventListener('input', checkOverflow);
    composerExpandedClose.addEventListener('click', closeExpanded);

    composerExpandedInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && e.ctrlKey) {
            e.preventDefault();
            composerInput.value = composerExpandedInput.value;
            closeExpanded();
            composer.requestSubmit();
        }
        if (e.key === 'Escape') {
            closeExpanded();
        }
    });

    document.getElementById('composer-expanded-send').addEventListener('click', () => {
        composerInput.value = composerExpandedInput.value;
        closeExpanded();
        composer.requestSubmit();
    });

    // Drag-to-move: header acts as the drag handle.
    // Constrained to the app window boundaries.
    (function initExpandedDrag() {
        const header = document.querySelector('.composer-expanded-header');
        let dragging = false;
        let startX = 0, startY = 0, origLeft = 0, origTop = 0;

        function clampPosition(left, top) {
            const rect = composerExpanded.getBoundingClientRect();
            const w = rect.width;
            const h = rect.height;
            const maxLeft = window.innerWidth - w;
            const maxTop = window.innerHeight - h;
            return {
                left: Math.max(0, Math.min(left, maxLeft)),
                top: Math.max(0, Math.min(top, maxTop)),
            };
        }

        header.addEventListener('mousedown', (e) => {
            if (e.target.closest('.composer-expanded-close')) return;
            dragging = true;
            startX = e.clientX;
            startY = e.clientY;
            const rect = composerExpanded.getBoundingClientRect();
            origLeft = rect.left;
            origTop = rect.top;
            e.preventDefault();
        });

        document.addEventListener('mousemove', (e) => {
            if (!dragging) return;
            const dx = e.clientX - startX;
            const dy = e.clientY - startY;
            const clamped = clampPosition(origLeft + dx, origTop + dy);
            composerExpanded.style.left = clamped.left + 'px';
            composerExpanded.style.top = clamped.top + 'px';
            composerExpanded.style.right = 'auto';
            composerExpanded.style.bottom = 'auto';
        });

        document.addEventListener('mouseup', () => { dragging = false; });

        // Constrain resize: cap max-width/max-height based on current position.
        const ro = new ResizeObserver(() => {
            const rect = composerExpanded.getBoundingClientRect();
            const maxW = window.innerWidth - rect.left;
            const maxH = window.innerHeight - rect.top;
            if (rect.width > maxW) composerExpanded.style.width = maxW + 'px';
            if (rect.height > maxH) composerExpanded.style.height = maxH + 'px';
        });
        ro.observe(composerExpanded);

        // Re-clamp on window resize so it never sits outside bounds.
        window.addEventListener('resize', () => {
            if (composerExpanded.classList.contains('hidden')) return;
            const rect = composerExpanded.getBoundingClientRect();
            const clamped = clampPosition(rect.left, rect.top);
            composerExpanded.style.left = clamped.left + 'px';
            composerExpanded.style.top = clamped.top + 'px';
            composerExpanded.style.right = 'auto';
            composerExpanded.style.bottom = 'auto';
        });
    })();

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
    overlaySettings.addEventListener('click', (e) => {
        if (e.target === overlaySettings) closeOverlay(overlaySettings);
    });
    settingsCancel.addEventListener('click', () => closeOverlay(overlaySettings));

    // ----- confirmation modal button wiring --------------------------------
    //
    // The modal is intentionally NOT click-outside-dismissible: a high-risk
    // confirmation requires an explicit Approve / Reject / Guidance choice,
    // and Esc is also disabled while a prompt is active so the user cannot
    // accidentally cancel an SSH credential prompt mid-typing.

    confirmSubmitBtn.addEventListener('click', () => {
        if (activePromptKind === 'secret_input' || activePromptKind === 'ask_human') {
            sendConfirmationAnswer(confirmSecretIn.value || '');
        } else if (!confirmGuidanceEl.classList.contains('hidden')) {
            // Guidance mode active — submit the guidance text
            const text = (confirmGuidanceEl.value || '').trim();
            if (!text) {
                // Empty guidance falls back to "yes" (Approve semantics).
                sendConfirmationAnswer('yes');
            } else {
                sendConfirmationAnswer(text);
            }
        } else {
            sendConfirmationAnswer('yes');
        }
    });

    confirmRejectBtn.addEventListener('click', () => {
        sendConfirmationAnswer('no');
    });

    confirmGuidBtn.addEventListener('click', () => {
        // Toggle guidance textarea: first click reveals + relabels Submit.
        if (confirmGuidanceEl.classList.contains('hidden')) {
            confirmGuidanceEl.classList.remove('hidden');
            confirmGuidBtn.textContent = 'Cancel guidance';
            confirmSubmitBtn.textContent = 'Send guidance';
            try { confirmGuidanceEl.focus(); } catch (_) { /* ignore */ }
        } else {
            confirmGuidanceEl.classList.add('hidden');
            confirmGuidanceEl.value = '';
            confirmGuidBtn.textContent = 'Provide guidance';
            confirmSubmitBtn.textContent = 'Approve';
        }
    });

    // Pressing Enter in the secret/ask_human input submits.
    confirmSecretIn.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && (activePromptKind === 'secret_input' || activePromptKind === 'ask_human')) {
            e.preventDefault();
            sendConfirmationAnswer(confirmSecretIn.value || '');
        }
    });

    window.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            if (!overlaySettings.classList.contains('hidden')) closeOverlay(overlaySettings);
            if (popoverOpen) closePopover();
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
        activeReceptionistBubble = null;
        thinkingBubble = null;
        lastCalledTool = '';
        activeExecCount = 0;
        firstSendDone = false;
        firstReplanSeen = false;
        clearCompleted();
        clearWorking();
        resetGauge();
        session.state = 'idle';
        setPill('idle');
        clearActivity();
        if (popoverOpen) closePopover();
        composerInput.focus();
        // New session: reset GEP-confirm state.
        closeGepOverlay();
        gepInfo = null;
        gepCountdownActive = false;
        // Tear down any open session terminals — the generation bump above
        // blocks future session_closed events from the old flow, so we must
        // clean up the panel synchronously here.
        for (const sid of [..._sessionTerminals.keys()]) {
            removeSessionTerminal(sid);
        }
        hideTerminalPanel();
    });

    // ----- GEP parameter panel + Templates review panel --------------------
    //
    // GEP save no longer has an in-conversation Save button. The only entry
    // point is the Templates panel (Load history) — see further down. This
    // section sets up the GEP confirmation/parameter panel and the templates
    // browser overlay.

    let gepInfo = null;            // last template descriptor from gep_intro
    let gepCountdownActive = false;
    let gepTotalSecs = 300;        // updated when gep_intro lands

    // ── Templates review panel (browse / inspect / delete) ────────────────

    const templatesListEl     = document.getElementById('templates-list');
    const templatesCountEl    = document.getElementById('templates-count');
    const templatesRefreshBtn = document.getElementById('templates-refresh');
    const templatesLoadHistoryBtn = document.getElementById('templates-load-history');
    const templatesCloseBtn   = document.getElementById('templates-close');
    const templatesToastEl    = document.getElementById('templates-toast');
    const templatesDetailEl       = document.getElementById('templates-detail');
    const templatesDetailEmptyEl  = document.getElementById('templates-detail-empty');
    const templatesDetailNameEl    = document.getElementById('templates-detail-name');
    const templatesDetailVersionEl = document.getElementById('templates-detail-version');
    const templatesDetailDescEl    = document.getElementById('templates-detail-desc');
    const templatesDetailProblemsEl = document.getElementById('templates-detail-problems');
    const templatesDetailParamsEl  = document.getElementById('templates-detail-params');
    const templatesDetailParamsWrap= document.getElementById('templates-detail-params-wrap');
    const templatesDetailStepsEl   = document.getElementById('templates-detail-steps');
    const templatesDetailStepsWrap = document.getElementById('templates-detail-steps-wrap');
    const templatesDetailIdEl      = document.getElementById('templates-detail-id');
    const templatesDetailCreatedEl = document.getElementById('templates-detail-created');
    const templatesDetailDeleteBtn = document.getElementById('templates-detail-delete');

    let templatesCache = [];
    let templatesActiveId = null;
    // id-correlated response waiters for the Templates panel.
    // The bridge replies with a `final` envelope carrying the same id we
    // sent. handq.sendRequest itself is fire-and-forget, so we wire up a
    // small map and inspect each `final` arrival via handq.onFinal.
    const _templatesPending = new Map(); // id → {resolve, reject, timer}
    let _templatesNextId = 1;

    function templatesRpc(type, payload) {
        const id = `templates-${type}-${_templatesNextId++}-${Date.now()}`;
        return new Promise((resolve, reject) => {
            const timer = setTimeout(() => {
                if (_templatesPending.has(id)) {
                    _templatesPending.delete(id);
                    reject(new Error(`${type} timed out after 10s`));
                }
            }, 10000);
            _templatesPending.set(id, { resolve, reject, timer });
            try {
                handq.sendRequest(Object.assign({ type: type, id: id }, payload || {}));
            } catch (err) {
                clearTimeout(timer);
                _templatesPending.delete(id);
                reject(err);
            }
        });
    }

    handq.onFinal((evt) => {
        if (!evt || !evt.id || !_templatesPending.has(evt.id)) return;
        const pending = _templatesPending.get(evt.id);
        _templatesPending.delete(evt.id);
        clearTimeout(pending.timer);
        pending.resolve(evt.result || {});
    });
    handq.onError((evt) => {
        if (!evt || !evt.id || !_templatesPending.has(evt.id)) return;
        const pending = _templatesPending.get(evt.id);
        _templatesPending.delete(evt.id);
        clearTimeout(pending.timer);
        pending.reject(new Error(evt.message || 'bridge error'));
    });

    function templatesShowToast(text, kind) {
        if (!templatesToastEl) return;
        templatesToastEl.textContent = text;
        templatesToastEl.classList.remove('hidden', 'ok', 'err');
        templatesToastEl.classList.add(kind === 'err' ? 'err' : 'ok');
        clearTimeout(templatesShowToast._t);
        templatesShowToast._t = setTimeout(() => {
            templatesToastEl.classList.add('hidden');
        }, 3500);
    }

    function templatesRenderList() {
        if (!templatesListEl) return;
        templatesListEl.innerHTML = '';
        if (templatesCountEl) {
            templatesCountEl.textContent = templatesCache.length
                ? `${templatesCache.length} template${templatesCache.length === 1 ? '' : 's'}`
                : '';
        }
        for (const t of templatesCache) {
            const li = el('li', 'templates-list-item');
            li.dataset.id = String(t.id || '');
            if (Array.isArray(t.problems) && t.problems.length) li.classList.add('invalid');
            if (t.id === templatesActiveId) li.classList.add('active');

            const title = el('div', 'templates-list-item-title');
            title.appendChild(document.createTextNode(t.name || '(unnamed)'));
            if (t.version != null) {
                title.appendChild(el('span', 'templates-version-badge', `v${t.version}`));
            }
            if (Array.isArray(t.problems) && t.problems.length) {
                title.appendChild(el('span', 'templates-invalid-badge', 'invalid'));
            }
            li.appendChild(title);

            const desc = el('div', 'templates-list-item-desc',
                (t.description || '').replace(/\s+/g, ' '));
            li.appendChild(desc);

            li.addEventListener('click', () => {
                templatesActiveId = t.id;
                templatesRenderList();
                templatesRenderDetail(t);
            });
            templatesListEl.appendChild(li);
        }
    }

    function templatesRenderDetail(t) {
        if (!templatesDetailEl || !t) return;
        templatesDetailEmptyEl.classList.add('hidden');
        templatesDetailEl.classList.remove('hidden');

        templatesDetailNameEl.textContent = t.name || '(unnamed)';
        templatesDetailVersionEl.textContent = t.version != null ? `v${t.version}` : '';
        templatesDetailDescEl.textContent = t.description || '';
        templatesDetailIdEl.textContent = t.id || '';
        templatesDetailCreatedEl.textContent = t.created_at || '';

        const problems = Array.isArray(t.problems) ? t.problems : [];
        if (problems.length) {
            templatesDetailProblemsEl.innerHTML = '';
            const intro = el('strong', null, 'This template is invalid and will be skipped:');
            templatesDetailProblemsEl.appendChild(intro);
            const ul = document.createElement('ul');
            for (const p of problems) ul.appendChild(el('li', null, p));
            templatesDetailProblemsEl.appendChild(ul);
            templatesDetailProblemsEl.classList.remove('hidden');
        } else {
            templatesDetailProblemsEl.classList.add('hidden');
            templatesDetailProblemsEl.innerHTML = '';
        }

        const params = Array.isArray(t.params) ? t.params : [];
        templatesDetailParamsEl.innerHTML = '';
        if (params.length) {
            for (const p of params) {
                const row = el('div', 'templates-detail-param');
                if (p.emphasis) row.classList.add('emphasis');
                const left = el('div', 'templates-detail-param-name');
                left.appendChild(document.createTextNode(p.name || ''));
                if (p.type) left.appendChild(el('span', 'templates-detail-param-type', p.type));
                row.appendChild(left);

                const right = el('div');
                if (p.default !== null && p.default !== undefined && p.default !== '') {
                    const defaultStr = (typeof p.default === 'object')
                        ? JSON.stringify(p.default)
                        : String(p.default);
                    right.appendChild(el('span', 'templates-detail-param-default',
                        `default: ${defaultStr}`));
                }
                if (p.description) {
                    right.appendChild(el('div', 'templates-detail-param-desc', p.description));
                }
                row.appendChild(right);
                templatesDetailParamsEl.appendChild(row);
            }
            templatesDetailParamsWrap.hidden = false;
        } else {
            templatesDetailParamsWrap.hidden = true;
        }

        const steps = Array.isArray(t.steps) ? t.steps : [];
        templatesDetailStepsEl.innerHTML = '';
        if (steps.length) {
            for (const s of steps) {
                const li = el('li');
                li.appendChild(el('span', 'templates-detail-step-desc',
                    s.description || s.step_id || '(step)'));
                if (Array.isArray(s.tools_required) && s.tools_required.length) {
                    const tools = el('span', 'templates-detail-step-tools');
                    tools.textContent =
                        ' [' + s.tools_required.map(String).join(', ') + ']';
                    li.appendChild(tools);
                }
                if (s.goal) {
                    li.appendChild(el('span', 'templates-detail-step-goal', s.goal));
                }
                templatesDetailStepsEl.appendChild(li);
            }
            templatesDetailStepsWrap.hidden = false;
        } else {
            templatesDetailStepsWrap.hidden = true;
        }
    }

    function templatesLoad() {
        if (!handq.sendRequest) return;
        if (templatesCountEl) templatesCountEl.textContent = 'loading…';
        templatesRpc('gep_list_templates').then((result) => {
            templatesCache = (result && Array.isArray(result.templates))
                ? result.templates : [];
            if (templatesActiveId
                && !templatesCache.some((t) => t.id === templatesActiveId)) {
                templatesActiveId = null;
                templatesDetailEl.classList.add('hidden');
                templatesDetailEmptyEl.classList.remove('hidden');
            }
            templatesRenderList();
            if (templatesActiveId) {
                const active = templatesCache.find((t) => t.id === templatesActiveId);
                if (active) templatesRenderDetail(active);
            }
        }).catch((err) => {
            templatesShowToast('Load failed: ' + (err && err.message), 'err');
            if (templatesCountEl) templatesCountEl.textContent = '';
        });
    }

    function openTemplatesOverlay() {
        if (!overlayTemplates) return;
        overlayTemplates.classList.remove('hidden');
        overlayTemplates.setAttribute('aria-hidden', 'false');
        templatesLoad();
        refreshLoadHistoryEnabled();
    }
    function closeTemplatesOverlay() {
        if (!overlayTemplates) return;
        overlayTemplates.classList.add('hidden');
        overlayTemplates.setAttribute('aria-hidden', 'true');
    }

    if (scTemplates) {
        scTemplates.addEventListener('click', () => {
            if (overlayTemplates && !overlayTemplates.classList.contains('hidden')) {
                closeTemplatesOverlay();
            } else {
                openTemplatesOverlay();
            }
        });
    }
    if (templatesCloseBtn) templatesCloseBtn.addEventListener('click', closeTemplatesOverlay);
    if (templatesRefreshBtn) templatesRefreshBtn.addEventListener('click', templatesLoad);

    // Load-history is the SOLE entry point for generating a new template.
    // No in-conversation Save button exists anymore — every save goes
    // through this picker, which:
    //   1) requires no active task (server-side gate refuses if any),
    //   2) defaults to %USERPROFILE%\HandQ\History\ so users see their
    //      own session list without typing a path,
    //   3) sends gep_save with the chosen log_file.
    // The save flow then takes over the conversation pane just like any
    // other request — the user can refine the template via chat.
    function templatesIsBusy() {
        return isTaskRunning() || gepCountdownActive;
    }
    function refreshLoadHistoryEnabled() {
        if (!templatesLoadHistoryBtn) return;
        const busy = templatesIsBusy();
        templatesLoadHistoryBtn.disabled = busy;
        templatesLoadHistoryBtn.title = busy
            ? 'Finish or cancel the current task before importing a session log'
            : 'Generate a GEP template from any past session log';
    }
    if (templatesLoadHistoryBtn) {
        templatesLoadHistoryBtn.addEventListener('click', async () => {
            if (templatesIsBusy()) {
                templatesShowToast(
                    'Cannot import while a task is running — wait for completion or click New first.',
                    'err',
                );
                return;
            }
            const dlg = window.handqDialog;
            if (!dlg || !dlg.pickHistoryLog) {
                templatesShowToast('File picker unavailable.', 'err');
                return;
            }
            let result;
            try {
                result = await dlg.pickHistoryLog();
            } catch (err) {
                templatesShowToast('Picker failed: ' + (err && err.message), 'err');
                return;
            }
            if (!result || result.canceled || !result.path) return;
            window.__handqLog('INFO', 'load history picked', { path: result.path });
            try {
                handq.sendRequest({ type: 'gep_save', log_file: result.path });
                closeTemplatesOverlay();
                addStepBubble('⤓', 'Importing session log: ' + result.path);
                clearCompleted();
                setWorking('saving template…');
            } catch (err) {
                templatesShowToast('Send failed: ' + (err && err.message), 'err');
            }
        });
    }

    if (templatesDetailDeleteBtn) {
        templatesDetailDeleteBtn.addEventListener('click', () => {
            if (!templatesActiveId) return;
            const t = templatesCache.find((x) => x.id === templatesActiveId);
            if (!t) return;
            const ok = window.confirm(
                `Delete template "${t.name || t.id}"? This removes the JSON file from disk and cannot be undone.`
            );
            if (!ok) return;
            templatesRpc('gep_delete_template', { id: templatesActiveId })
                .then((result) => {
                    if (result && result.ok) {
                        templatesShowToast('Template deleted.', 'ok');
                        templatesActiveId = null;
                        templatesLoad();
                    } else {
                        templatesShowToast(
                            'Delete failed: ' + ((result && result.error) || 'unknown'),
                            'err',
                        );
                    }
                })
                .catch((err) => {
                    templatesShowToast('Delete failed: ' + (err && err.message), 'err');
                });
        });
    }

    if (overlayTemplates) {
        overlayTemplates.addEventListener('click', (e) => {
            if (e.target === overlayTemplates) closeTemplatesOverlay();
        });
    }

    // ── GEP parameter panel ────────────────────────────────────────────────

    const gepTitleEl       = document.getElementById('gep-title');
    const gepDescriptionEl = document.getElementById('gep-description');
    const gepStepsEl       = document.getElementById('gep-steps');
    const gepStepsSection  = document.getElementById('gep-steps-section');
    const gepKeyParamsEl       = document.getElementById('gep-key-params');
    const gepKeyParamsSection  = document.getElementById('gep-key-params-section');
    const gepOtherParamsEl       = document.getElementById('gep-other-params');
    const gepOtherParamsSection  = document.getElementById('gep-other-params-section');
    const gepCountdownEl   = document.getElementById('gep-countdown');
    const gepProgressBar   = document.getElementById('gep-progress-bar');
    const gepFormEl        = document.getElementById('gep-form');
    const gepCloseBtn      = document.getElementById('gep-close');
    const gepSkipBtn       = document.getElementById('gep-skip');
    const gepConfirmBtn    = document.getElementById('gep-confirm');

    function gepRenderParam(spec) {
        const row = el('div', 'gep-param-row');
        if (spec.emphasis) row.classList.add('emphasis');
        const label = el('label', 'gep-param-label');
        const name  = el('span', 'gep-param-name', String(spec.name || ''));
        label.appendChild(name);
        const typeStr = (spec.type || '').toString();
        if (typeStr) {
            const t = el('span', 'gep-param-type', '[' + typeStr + ']');
            label.appendChild(t);
        }
        const desc = (spec.description || '').toString();
        if (desc) {
            label.appendChild(el('span', 'gep-param-desc', desc));
        }
        row.appendChild(label);

        let input;
        const lower = typeStr.toLowerCase();
        if (lower === 'bool' || lower === 'boolean') {
            input = document.createElement('input');
            input.type = 'checkbox';
            input.checked = spec.default === true || spec.default === 'true';
        } else if (lower === 'int' || lower === 'integer' || lower === 'number') {
            input = document.createElement('input');
            input.type = 'number';
            if (spec.default !== null && spec.default !== undefined) {
                input.value = String(spec.default);
            }
        } else if (lower === 'list') {
            input = document.createElement('textarea');
            input.rows = 2;
            input.placeholder = 'one item per line';
            if (Array.isArray(spec.default)) {
                input.value = spec.default.join('\n');
            } else if (spec.default != null) {
                input.value = String(spec.default);
            }
        } else {
            input = document.createElement('input');
            input.type = 'text';
            if (spec.default !== null && spec.default !== undefined) {
                input.value = String(spec.default);
            }
        }
        input.dataset.paramName = String(spec.name || '');
        input.dataset.paramType = typeStr;
        input.classList.add('gep-param-input');
        row.appendChild(input);
        return row;
    }

    function gepCollectParams() {
        if (!gepFormEl) return [];
        const out = [];
        const inputs = gepFormEl.querySelectorAll('.gep-param-input');
        inputs.forEach((inp) => {
            const name = inp.dataset.paramName;
            if (!name) return;
            let value;
            if (inp.type === 'checkbox') {
                value = inp.checked ? 'true' : 'false';
            } else {
                value = (inp.value || '').toString();
            }
            out.push({ name: name, value: value });
        });
        return out;
    }

    function gepFormatParamsAsMessage() {
        const params = gepCollectParams();
        if (!params.length) return '';
        const lines = params
            .filter((p) => p.value !== '' && p.value != null)
            .map((p) => `- ${p.name} = ${p.value}`);
        if (!lines.length) return '';
        return 'GEP parameters:\n' + lines.join('\n');
    }

    function openGepOverlay() {
        if (!overlayGep) return;
        overlayGep.classList.remove('hidden');
        overlayGep.setAttribute('aria-hidden', 'false');
    }
    function closeGepOverlay() {
        if (!overlayGep) return;
        overlayGep.classList.add('hidden');
        overlayGep.setAttribute('aria-hidden', 'true');
    }

    function gepRenderTemplate(info) {
        gepInfo = info || {};
        if (gepTitleEl) gepTitleEl.textContent = info && info.name ? info.name : 'GEP Template';
        if (gepDescriptionEl) gepDescriptionEl.textContent = (info && info.description) || '';

        if (gepStepsEl) gepStepsEl.innerHTML = '';
        const steps = (info && Array.isArray(info.steps)) ? info.steps : [];
        if (steps.length && gepStepsEl) {
            for (const s of steps) {
                const li = el('li');
                const desc = (s.description || s.goal || '').toString();
                li.appendChild(el('span', 'gep-step-name', desc || s.step_id || '(step)'));
                if (s.goal && s.goal !== desc) {
                    li.appendChild(document.createElement('br'));
                    li.appendChild(el('span', 'gep-step-desc', s.goal));
                }
                gepStepsEl.appendChild(li);
            }
            if (gepStepsSection) gepStepsSection.hidden = false;
        } else if (gepStepsSection) {
            gepStepsSection.hidden = true;
        }

        const params = (info && Array.isArray(info.params)) ? info.params : [];
        const keyParams = params.filter((p) => p.emphasis);
        const otherParams = params.filter((p) => !p.emphasis);

        if (gepKeyParamsEl)   gepKeyParamsEl.innerHTML = '';
        if (gepOtherParamsEl) gepOtherParamsEl.innerHTML = '';

        if (keyParams.length && gepKeyParamsEl) {
            for (const p of keyParams) gepKeyParamsEl.appendChild(gepRenderParam(p));
            if (gepKeyParamsSection) gepKeyParamsSection.hidden = false;
        } else if (gepKeyParamsSection) {
            gepKeyParamsSection.hidden = true;
        }
        if (otherParams.length && gepOtherParamsEl) {
            for (const p of otherParams) gepOtherParamsEl.appendChild(gepRenderParam(p));
            if (gepOtherParamsSection) gepOtherParamsSection.hidden = false;
        } else if (gepOtherParamsSection) {
            gepOtherParamsSection.hidden = true;
        }

        gepTotalSecs = (info && Number(info.timeout_secs)) || 300;
        gepCountdownActive = true;
        gepUpdateCountdown(gepTotalSecs);
        openGepOverlay();
    }

    function gepUpdateCountdown(remaining) {
        if (!gepCountdownEl || !gepProgressBar) return;
        if (typeof remaining !== 'number' || remaining < 0) {
            // -1 from backend == clear
            gepCountdownActive = false;
            gepCountdownEl.textContent = '--';
            gepProgressBar.style.width = '0%';
            closeGepOverlay();
            return;
        }
        gepCountdownActive = true;
        const total = Math.max(1, gepTotalSecs || 300);
        const pct = Math.max(0, Math.min(100, (remaining / total) * 100));
        gepProgressBar.style.width = pct + '%';
        const mm = Math.floor(remaining / 60);
        const ss = remaining % 60;
        gepCountdownEl.textContent =
            (mm > 0 ? (mm + 'm ') : '') + (ss < 10 ? '0' + ss : ss) + 's';
        gepCountdownEl.classList.remove('warn', 'urgent');
        if (remaining <= 10)       gepCountdownEl.classList.add('urgent');
        else if (remaining <= 60)  gepCountdownEl.classList.add('warn');
    }

    function gepSendChatMessage(text) {
        if (!text) return;
        addUserBubble(text);
        try {
            handq.sendRequest({ type: 'user_input', kind: 'message', text: text });
        } catch (err) {
            addErrorBubble(String(err && err.message || err), 'gep');
        }
    }

    if (gepFormEl) {
        gepFormEl.addEventListener('submit', (e) => {
            e.preventDefault();
            const paramsBlock = gepFormatParamsAsMessage();
            const lines = ['yes'];
            if (paramsBlock) lines.push('', paramsBlock);
            gepSendChatMessage(lines.join('\n'));
            // Optimistically close the overlay; backend will clear the
            // countdown via gep_countdown=-1 when the template activates.
            closeGepOverlay();
        });
    }
    if (gepCloseBtn) {
        gepCloseBtn.addEventListener('click', () => {
            // "Close" only hides the overlay — countdown keeps running so
            // the user can re-open it via the chat or wait for auto-activation.
            closeGepOverlay();
        });
    }
    if (gepSkipBtn) {
        gepSkipBtn.addEventListener('click', () => {
            gepSendChatMessage('no');
            closeGepOverlay();
        });
    }
    // ----- settings form helpers (model master-list + per-role checkboxes) -----

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

    // Per-role checked state: { planner: Set, receptionist: Set, agent: Set, helper: Set }
    let roleSelections = {
        planner: new Set(),
        receptionist: new Set(),
        agent: new Set(),
        helper: new Set(),
    };

    function modelDisplayName(model) {
        const idx = model.indexOf('::');
        return idx >= 0 ? model.slice(idx + 2) : model;
    }

    function rebuildRoleCheckboxes() {
        const models = textToModels(cfgLlmModels.value);
        for (const role of Object.keys(cfgLlmRolePanes)) {
            const pane = cfgLlmRolePanes[role];
            if (!pane) continue;
            const sel = roleSelections[role] || new Set();
            pane.innerHTML = '';
            if (models.length === 0) {
                const empty = document.createElement('span');
                empty.className = 'help-text';
                empty.textContent = 'Add models in Available tab.';
                pane.appendChild(empty);
                continue;
            }
            for (const model of models) {
                const lbl = document.createElement('label');
                lbl.className = 'model-checkbox';
                const cb = document.createElement('input');
                cb.type = 'checkbox';
                cb.value = model;
                cb.checked = sel.has(model);
                cb.addEventListener('change', () => {
                    if (cb.checked) sel.add(model);
                    else sel.delete(model);
                });
                const span = document.createElement('span');
                span.textContent = modelDisplayName(model);
                span.title = model;
                lbl.appendChild(cb);
                lbl.appendChild(span);
                pane.appendChild(lbl);
            }
        }
    }

    if (cfgLlmModels) {
        cfgLlmModels.addEventListener('input', () => {
            rebuildRoleCheckboxes();
        });
    }

    function selectRoleTab(role) {
        const tabs = cfgLlmRoleTabs ? cfgLlmRoleTabs.querySelectorAll('.role-tab') : [];
        tabs.forEach((btn) => {
            const active = btn.dataset.role === role;
            btn.classList.toggle('active', active);
            btn.setAttribute('aria-selected', active ? 'true' : 'false');
        });
        // "available" tab controls the master textarea
        if (cfgLlmModels) cfgLlmModels.hidden = (role !== 'available');
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
        const emailCfg = cfg.email || {};

        cfgLlmApiKey.value =
            (llm.API_KEY === undefined || llm.API_KEY === null) ? '' : String(llm.API_KEY);
        cfgLlmMaxTokens.value =
            (llm.max_tokens === undefined || llm.max_tokens === null) ? '' : String(llm.max_tokens);

        // Determine master model list and per-role selections
        const rolesObj = (llm.roles && typeof llm.roles === 'object') ? llm.roles : null;
        let masterModels = [];
        let perRole = { planner: [], receptionist: [], agent: [], helper: [] };

        if (Array.isArray(llm.models) && llm.models.length > 0) {
            masterModels = llm.models.map(String);
        }

        if (rolesObj) {
            perRole.planner      = Array.isArray(rolesObj.planner) ? rolesObj.planner.map(String) : [];
            perRole.receptionist = Array.isArray(rolesObj.receptionist) ? rolesObj.receptionist.map(String) : [];
            perRole.agent        = Array.isArray(rolesObj.agent) ? rolesObj.agent.map(String) : [];
            perRole.helper       = Array.isArray(rolesObj.helper) ? rolesObj.helper.map(String) : [];
            // If master list is empty, derive it as de-duped union of all role lists
            if (masterModels.length === 0) {
                const seen = new Set();
                for (const role of ['planner', 'receptionist', 'agent', 'helper']) {
                    for (const m of perRole[role]) {
                        if (!seen.has(m)) { seen.add(m); masterModels.push(m); }
                    }
                }
            }
        } else if (masterModels.length > 0) {
            // Legacy: only llm.models exists, auto-assign roles
            const derived = assignRoles(masterModels);
            perRole.planner      = derived.planner;
            perRole.receptionist = derived.receptionist;
            perRole.agent        = derived.agent;
            perRole.helper       = derived.helper;
        }

        cfgLlmModels.value = modelsToText(masterModels);

        // Populate roleSelections Sets and rebuild checkboxes
        roleSelections.planner      = new Set(perRole.planner);
        roleSelections.receptionist = new Set(perRole.receptionist);
        roleSelections.agent        = new Set(perRole.agent);
        roleSelections.helper       = new Set(perRole.helper);
        rebuildRoleCheckboxes();
        selectRoleTab('planner');

        cfgSessionLogLevel.value = sessCfg.log_level || '';
        cfgSessionStepThreshold.value =
            (sessCfg.step_verification_threshold === undefined ||
             sessCfg.step_verification_threshold === null)
                ? '' : String(sessCfg.step_verification_threshold);
        cfgSessionVenvPath.value = sessCfg.venv_path || '';

        // readSwitch reads either `auto_approve` or `enabled` from a
        // switch entry. `enabled` defaults to true when missing (back-compat
        // with older configs that only carry `auto_approve`).
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

        // Master model list
        const masterModels = textToModels(cfgLlmModels.value);
        llm.models = masterModels;

        // Per-role selections (preserve master-list order)
        if ('models' in llm && llm.models.length === 0) delete llm.models;
        llm.roles = {};
        for (const role of ['planner', 'receptionist', 'agent', 'helper']) {
            const sel = roleSelections[role] || new Set();
            llm.roles[role] = masterModels.filter((m) => sel.has(m));
        }

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
