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
    const scStatus   = document.getElementById('sc-status');
    const scNew      = document.getElementById('sc-new');

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

    // Overlays
    const overlayStatus    = document.getElementById('overlay-status');
    const overlaySettings  = document.getElementById('overlay-settings');
    const overlayConfirm   = document.getElementById('overlay-confirmation');
    const statusCloseBtn   = document.getElementById('status-close');
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
    const cfgSwToolBrowser = document.getElementById('cfg-sw-tool-browser');
    const cfgSwToolDesktop = document.getElementById('cfg-sw-tool-desktop');
    const cfgSwHighRisk  = document.getElementById('cfg-sw-high-risk');

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
        recordEvent('task completed' + (summary ? ': ' + truncate(summary, 80) : ''));
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
    const ACTIVITY_TRUNC = 600;
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
            try { obj = JSON.parse(obj); } catch (_) { return truncate(obj, 240); }
        }
        if (typeof obj !== 'object' || obj === null) return truncate(String(params), 240);
        var key = (tool === 'browser') ? 'url' :
                  (tool === 'bash' || tool === 'shell') ? 'command' :
                  (tool === 'write' || tool === 'edit' || tool === 'read') ? 'path' :
                  Object.keys(obj)[0] || '';
        var val = key ? String(obj[key] || '') : '';
        return truncate(val, 240);
    }

    function formatResultReadable(tool, output) {
        if (!output || output === 'None' || output === 'null') return '';
        var obj = output;
        if (typeof obj === 'string') {
            try { obj = JSON.parse(obj); } catch (_) {
                return truncate(stripAnsi(obj).replace(/\s+/g, ' ').trim(), 400);
            }
        }
        if (typeof obj !== 'object' || obj === null) {
            return truncate(stripAnsi(String(output)).replace(/\s+/g, ' ').trim(), 400);
        }
        // For bash/shell results: show stdout (cleaned) or stderr if failed
        if ('stdout' in obj || 'exit_code' in obj || 'returncode' in obj) {
            var code = obj.exit_code || obj.returncode || '0';
            var out = stripAnsi(String(obj.stdout || '')).replace(/\s+/g, ' ').trim();
            var err = stripAnsi(String(obj.stderr || '')).replace(/\s+/g, ' ').trim();
            if (String(code) !== '0' && err) {
                return '✗ ' + truncate(err, 380);
            }
            if (out) return truncate(out, 400);
            if (err) return truncate(err, 400);
            return code === '0' || code === 0 ? 'done' : '✗ exit ' + code;
        }
        // For common tools, pick the most informative field
        if (obj.output) return truncate(stripAnsi(String(obj.output)).replace(/\s+/g, ' ').trim(), 400);
        if (obj.result) return truncate(String(obj.result).replace(/\s+/g, ' ').trim(), 400);
        if (obj.content) return truncate(String(obj.content).replace(/\s+/g, ' ').trim(), 400);
        if (obj.text) return truncate(String(obj.text).replace(/\s+/g, ' ').trim(), 400);
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
            parts.push(k + ': ' + truncate(stripAnsi(String(v)), 100));
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

    function pushActivity(icon, label, content) {
        if (!activityStrip) return;
        const time = new Date().toLocaleTimeString([], { hour12: false });
        const entry = {
            icon: icon || '·',
            label: label || '',
            content: content == null ? '' : String(content),
            time: time,
        };
        activityItems.push(entry);
        if (activityItems.length > ACTIVITY_RING) activityItems.shift();
        renderActivityFeed();
        // Refresh the strip text with a one-line preview of the latest entry.
        const preview = entry.icon + ' ' + entry.label +
            (entry.content ? ' · ' + truncate(entry.content.replace(/\s+/g, ' '), 120) : '');
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
            li.addEventListener('click', () => {
                li.classList.toggle('expanded');
                const c = li.querySelector('.ai-content');
                if (!c) return;
                if (c.classList.contains('ai-json')) return;
                c.textContent = li.classList.contains('expanded')
                    ? entry.content
                    : truncate(entry.content, ACTIVITY_TRUNC);
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
            confirmGuidanceEl.classList.add('hidden');
            confirmGuidanceEl.value = '';
            confirmRejectBtn.classList.add('hidden');
            confirmGuidBtn.classList.add('hidden');
            confirmSubmitBtn.textContent = 'Submit';
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
        // Focus management: passwords get the input; risk gates need an
        // explicit click on Approve so we don't auto-focus the primary.
        if (evt.kind === 'secret_input') {
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

        if (evt.kind === 'risk_confirmation' ||
            evt.kind === 'tool_confirmation' ||
            evt.kind === 'secret_input') {
            // Show the confirmation modal and stop further dispatch — these
            // envelopes are not informational status updates.
            recordEvent('confirmation requested: ' + evt.kind +
                        (evt.tool ? ' (' + evt.tool + ')' : ''));
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
                    recordEvent('state → designing');
                    setWorking('designing…');
                } else {
                    recordEvent('state → replanning');
                    setWorking('replanning…');
                }
            } else {
                recordEvent('state → ' + evt.state);
                if (evt.state === 'executing') {
                    clearWorking();
                }
                setPill(evt.state);
            }
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
            pushActivity('▶', 'Step started', desc);
            setWorking('▶ ' + truncate(desc, 120));
        } else if (evt.kind === 'step_completed') {
            const desc = String(evt.desc || args[1] || '');
            recordEvent('step completed: ' + desc);
            pushActivity('✓', 'Step completed', desc);
            setPill('✓ ' + truncate(desc, 120));
        } else if (evt.kind === 'step_confidence') {
            const conf = parseFloat(args[0]);
            if (!Number.isNaN(conf)) {
                recordEvent('confidence: ' + conf.toFixed(2));
                pushActivity('◎', 'Step confidence', conf.toFixed(2));
            }
        } else if (evt.kind === 'decision_made') {
            const iter = args[0] || '';
            const reasoning = args[1] || '';
            recordEvent('decision[' + iter + ']: ' + truncate(reasoning, 120));
            pushActivity('💭', 'Decision iter ' + iter, reasoning);
            setWorking('💭 ' + truncate(reasoning, 120));
        } else if (evt.kind === 'tool_execution_started') {
            const iter   = args[0] || '';
            var rawTool  = args[1] || '';
            const params = args[2];
            const output = args[3];
            // Backend sends "None" (string) for tool/params in post-execution events
            var tool = (rawTool && rawTool !== 'None' && rawTool !== 'null') ? rawTool : '';
            var isPre = output === undefined || output === null
                        || output === 'None' || output === 'null';
            if (isPre && tool) lastCalledTool = tool;
            var effectiveTool = tool || lastCalledTool || 'action';
            const tag    = isPre ? '⊙' : '✓';
            const paramText = formatToolParams(params);
            recordEvent(tag + ' ' + effectiveTool + '[' + iter + '] ' + truncate(paramText, 120));
            if (isPre) {
                activeExecCount++;
                var ctx = briefToolContext(effectiveTool, params);
                var preLabel = 'Executing ' + effectiveTool;
                var preContent = ctx || paramText;
                pushActivity(tag, preLabel, preContent);
                setWorking('⊙ ' + effectiveTool + (ctx ? ' · ' + ctx : ''));
            } else {
                activeExecCount = Math.max(0, activeExecCount - 1);
                var readable = formatResultReadable(effectiveTool, output);
                var postLabel = effectiveTool + ' done';
                pushActivity(tag, postLabel, readable || String(output));
                if (activeExecCount === 0) {
                    clearWorking();
                    setPill('✓ ' + effectiveTool + (readable ? ' · ' + readable : ''));
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
        } else if (evt.kind === 'bridge_exit') {
            session.state = 'bridge exited';
            recordEvent('bridge exited');
            setPill('bridge exited', { force: true });
            pushActivity('⚠', 'Bridge exited', 'code=' + evt.code + ' signal=' + evt.signal);
        } else if (evt.kind === 'reply') {
            addAssistantTextBubble(evt.text || '');
        } else if (evt.kind === 'reply_delta') {
            // Clear thinking indicator on first streaming chunk
            removeThinkingBubble();
            clearWorking();
            setPill('');
            appendReceptionistDelta(evt.text || '');
        } else if (evt.kind === 'reply_done') {
            sealReceptionistBubble();
        } else if (evt.kind === 'message') {
            addSystemBubble(evt.text || '');
        } else if (evt.kind === 'receptionist_thinking_on') {
            recordEvent('receptionist thinking…');
            setWorking('thinking…');
            showThinkingBubble();
        } else if (evt.kind === 'receptionist_thinking_off') {
            recordEvent('receptionist idle');
            removeThinkingBubble();
            clearWorking();
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
        if (e.key === 'Enter' && !e.shiftKey) {
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
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            composerInput.value = composerExpandedInput.value;
            closeExpanded();
            composer.requestSubmit();
        }
        if (e.key === 'Escape') {
            closeExpanded();
        }
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
    overlayStatus.addEventListener('click', (e) => {
        if (e.target === overlayStatus) closeOverlay(overlayStatus);
    });
    overlaySettings.addEventListener('click', (e) => {
        if (e.target === overlaySettings) closeOverlay(overlaySettings);
    });
    statusCloseBtn.addEventListener('click', () => closeOverlay(overlayStatus));
    settingsCancel.addEventListener('click', () => closeOverlay(overlaySettings));

    // ----- confirmation modal button wiring --------------------------------
    //
    // The modal is intentionally NOT click-outside-dismissible: a high-risk
    // confirmation requires an explicit Approve / Reject / Guidance choice,
    // and Esc is also disabled while a prompt is active so the user cannot
    // accidentally cancel an SSH credential prompt mid-typing.

    confirmSubmitBtn.addEventListener('click', () => {
        if (activePromptKind === 'secret_input') {
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

    // Pressing Enter in the secret input submits.
    confirmSecretIn.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && activePromptKind === 'secret_input') {
            e.preventDefault();
            sendConfirmationAnswer(confirmSecretIn.value || '');
        }
    });

    window.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            if (!overlayStatus.classList.contains('hidden'))   closeOverlay(overlayStatus);
            if (!overlaySettings.classList.contains('hidden')) closeOverlay(overlaySettings);
            if (popoverOpen) closePopover();
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
        activeReceptionistBubble = null;
        thinkingBubble = null;
        lastCalledTool = '';
        activeExecCount = 0;
        firstSendDone = false;
        firstReplanSeen = false;
        clearCompleted();
        clearWorking();
        session.state = 'idle';
        session.progress = '';
        session.currentStep = '';
        session.events = [];
        session.lastUpdate = '';
        setPill('idle');
        clearActivity();
        if (popoverOpen) closePopover();
        composerInput.focus();
    });

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
        cfgSwToolBrowser.checked = readSwitch('tool_browser');
        cfgSwToolDesktop.checked = readSwitch('tool_desktop');
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

        function writeSwitch(name, checked) {
            if (!switches[name] || typeof switches[name] !== 'object') {
                switches[name] = {};
            }
            switches[name].auto_approve = Boolean(checked);
        }
        writeSwitch('tool_write', cfgSwToolWrite.checked);
        writeSwitch('tool_edit',  cfgSwToolEdit.checked);
        writeSwitch('tool_bash',  cfgSwToolBash.checked);
        writeSwitch('tool_browser', cfgSwToolBrowser.checked);
        writeSwitch('tool_desktop', cfgSwToolDesktop.checked);
        writeSwitch('high_risk',  cfgSwHighRisk.checked);

        out.llm = llm;
        out.session = sess;
        out.interaction_switches = switches;
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
