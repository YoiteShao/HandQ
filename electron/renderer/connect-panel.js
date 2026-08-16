/*
 * connect-panel.js — v6 Connect panel: role selection + As Server / As Client
 * dashboards. See docs/connect_v6_reference.md (the authoritative description;
 * docs/connect_panel_design_v6.md is a superseded design draft).
 *
 * This module owns the #overlay-connect surface. It shares state with
 * remote-control.js (the older Remote machines overlay) — both listen to the
 * bridge's remote_control_status / remote_serve_state broadcasts, so opening
 * either one shows current truth without needing the other closed first.
 *
 * Split from remote-control.js for size reasons and so the v6 flow can be
 * yanked cleanly if the design shifts again. Pairing dialogs (paste one
 * handq:// line, Linux SSH auto) and the confirm dialog reuse HandQRemote's
 * existing ones rather than re-implementing them.
 *
 * Session chips are shown for EVERY remote session until it is explicitly
 * destroyed — there is no "not worth remembering" filter. A session on the other
 * machine is a real agent with a real workspace whether or not it has been
 * classified as a task, so hiding one meant abandoning it (see
 * remote_control/hub.py's module docstring).
 */
(function () {
    'use strict';

    const RPC_TIMEOUT_MS = 45000;

    const pending = new Map();
    let nextRpcId = 1;

    // Last snapshot from remote_control_status. Populated by refresh() and
    // by the same push events remote-control.js listens to, so opening the
    // panel is instant and doesn't need a round trip.
    let serving = null;
    let targets = [];
    let serveState = { serving: false, sessionCount: 0, attachedCount: 0,
                       port: 0, endpoint: '', error: '' };
    // The currently-displayed page in the overlay: 'role' | 'server' | 'client'.
    // 'role' is the entry point; the two dashboards are populated on demand.
    let page = 'role';
    // Whether we've mounted the DOM listeners yet — init() is called from
    // renderer.js once at boot, and again when the module is warm-restarted;
    // the flag keeps a double-init from stacking listeners.
    let mounted = false;

    const dom = {};
    // Log lines by page, ring-buffered so a long-running session doesn't
    // grow the DOM without bound.
    const LOG_MAX = 200;
    const logLines = { server: [], client: [] };

    function log(level, msg, extra) {
        try {
            if (window.__handqLog) window.__handqLog(level, msg, extra);
        } catch (_) { /* ignore */ }
    }

    // ── RPC ─────────────────────────────────────────────────────────────
    // Same shape as remote-control.js: onFinal settles the RPC promise, and a
    // bridge-side {ok:false, error} is unwrapped into a rejection so callers
    // can try/catch. Duplicated instead of shared so this module can be
    // yanked without an inter-module handshake.

    function rpc(type, payload, timeoutMs) {
        const id = `cp-${type}-${nextRpcId++}-${Date.now()}`;
        const limit = timeoutMs || RPC_TIMEOUT_MS;
        return new Promise((resolve, reject) => {
            const timer = setTimeout(() => {
                if (pending.has(id)) {
                    pending.delete(id);
                    reject(new Error(`${type} timed out (no response in ${Math.round(limit / 1000)}s)`));
                }
            }, limit);
            pending.set(id, { resolve, reject, timer });
            try {
                window.handq.sendRequest(Object.assign({}, payload || {}, { type, id }));
            } catch (err) {
                clearTimeout(timer);
                pending.delete(id);
                reject(err);
            }
        });
    }

    function settleRpc(evt, isError) {
        if (!evt || !evt.id) return false;
        const entry = pending.get(evt.id);
        if (!entry) return false;
        pending.delete(evt.id);
        clearTimeout(entry.timer);
        if (isError) {
            entry.reject(new Error((evt && evt.message) || 'bridge error'));
        } else {
            entry.resolve(evt.result || {});
        }
        return true;
    }

    async function call(type, payload, timeoutMs) {
        const result = await rpc(type, payload, timeoutMs);
        if (result && result.ok === false) {
            const err = new Error(result.error || `${type} failed`);
            if (result.busy) err.busy = true;
            throw err;
        }
        return result || {};
    }

    // ── Log rendering ───────────────────────────────────────────────────

    function appendLog(which, message) {
        if (!logLines[which]) return;
        const line = `${new Date().toLocaleTimeString()}  ${message}`;
        const buf = logLines[which];
        buf.push(line);
        if (buf.length > LOG_MAX) buf.shift();
        const el = which === 'server' ? dom.serverLog : dom.clientLog;
        if (el) {
            el.textContent = buf.join('\n');
            el.scrollTop = el.scrollHeight;
        }
    }

    function clearLog(which) {
        if (logLines[which]) logLines[which].length = 0;
        const el = which === 'server' ? dom.serverLog : dom.clientLog;
        if (el) el.textContent = '';
    }

    // ── State refresh ───────────────────────────────────────────────────

    async function refresh() {
        try {
            const res = await call('remote_control_status', {});
            serving = res.serving || null;
            targets = res.targets || [];
            lastKnownRole = res.role || null;
            render();
        } catch (err) {
            log('WARN', 'connect-panel: refresh failed', { error: String(err) });
        }
    }
    // Last-persisted role from connect_state.json, refreshed on every
    // remote_control_status call. Consulted only as resolveActivePage's
    // final fallback (see below) — never used to auto-start anything.
    let lastKnownRole = null;

    /**
     * Determine which page to show based on actual runtime state:
     * - If this machine is currently serving (listener active) → server page
     * - If there are any connected targets → client page
     * - Otherwise fall back to the last role the user picked, if any
     * - Otherwise → role selection
     */
    function resolveActivePage() {
        if (serving && serving.serving) return 'server';
        if (targets.some(t => t.connected)) return 'client';
        if (lastKnownRole === 'server' || lastKnownRole === 'client') {
            return lastKnownRole;
        }
        return null; // goes to 'role' in open()
    }

    // ── Page switching ──────────────────────────────────────────────────

    function goto(next) {
        page = next;
        if (!dom.overlay) return;
        dom.overlay.querySelectorAll('.connect-page').forEach((el) => {
            el.classList.toggle('hidden', el.dataset.connectPage !== page);
        });
        if (dom.title) {
            dom.title.textContent =
                page === 'server' ? 'Connect · Server' :
                page === 'client' ? 'Connect · Client' : 'Connect';
        }
        render();
        // Landing on the client dashboard IS the demand for a connection. There
        // is no boot-time sweep any more, so without this every card would read
        // "Not connected" simply because nobody had asked yet — the exact
        // ambiguity that sweep's `restoring` banner existed to paper over. Fired
        // and not awaited: the panel has to paint now, and each card updates
        // itself as its own attempt settles (remote_target_state push).
        if (page === 'client') connectIdleTargets();
    }

    // Guards against a second attempt for the same machine while one is in
    // flight: goto('client') can happen repeatedly (open, role tile, back from
    // server view) and hub.ensure_client rejects a concurrent connect rather than
    // racing it.
    const _connectInFlight = new Set();

    async function connectIdleTargets() {
        const idle = targets.filter((t) => {
            const st = t.state || (t.connected ? 'connected' : 'offline');
            return st === 'offline' && t.target_id && !_connectInFlight.has(t.target_id);
        });
        if (!idle.length) return;
        await Promise.all(idle.map(async (t) => {
            _connectInFlight.add(t.target_id);
            try {
                await connectTarget(t);
            } finally {
                _connectInFlight.delete(t.target_id);
            }
        }));
    }

    // ── As Server rendering ─────────────────────────────────────────────

    function renderServer() {
        if (!serving || !serving.serving) {
            if (dom.serverPairing) dom.serverPairing.textContent = '—';
            dom.serverStatus.textContent = 'Not listening';
            dom.serverSessions.innerHTML = '';
            dom.serverSessions.setAttribute('data-empty', 'true');
            return;
        }
        // Full pairing string for copy
        if (dom.serverPairing) {
            dom.serverPairing.textContent = serving.pairing || serving.endpoint || '—';
        }
        // Client status
        if (serving.client_name) {
            dom.serverStatus.textContent =
                `Connected: ${serving.client_name} (${(serving.sessions || []).length} session${(serving.sessions || []).length === 1 ? '' : 's'})`;
        } else {
            dom.serverStatus.textContent = 'Waiting for connection…';
        }
        // Session list. Empty when nobody is driving us; populated from
        // remote_control_status.serving.sessions if the bridge exposes it,
        // else derived from serveState (count-only).
        const sessions = (serving && Array.isArray(serving.sessions))
            ? serving.sessions : [];
        dom.serverSessions.innerHTML = '';
        if (!sessions.length) {
            dom.serverSessions.setAttribute('data-empty', 'true');
            const empty = document.createElement('div');
            empty.className = 'connect-empty-hint';
            empty.textContent = 'No active remote sessions.';
            dom.serverSessions.appendChild(empty);
            return;
        }
        dom.serverSessions.removeAttribute('data-empty');
        sessions.forEach((s) => {
            dom.serverSessions.appendChild(renderServerSessionRow(s));
        });
    }

    function renderServerSessionRow(session) {
        const row = document.createElement('div');
        row.className = 'connect-session-row';
        const sid = session.session_id || session.id || '';

        const body = document.createElement('div');
        body.className = 'connect-session-body';

        // Title line: what this session is, not just its id. The id stays as
        // the fallback (and in the tooltip) because a session opened with no
        // goal genuinely has no title.
        const label = document.createElement('div');
        label.className = 'connect-session-label';
        const badge = document.createElement('span');
        badge.className = 'connect-chip-badge';
        badge.textContent = session.is_task ? 'Task' : 'Chat';
        const name = document.createElement('span');
        name.className = 'connect-session-title';
        name.textContent = session.title || sid || 'session';
        name.title = sid;
        label.appendChild(badge);
        label.appendChild(name);
        body.appendChild(label);

        // Meta line. This machine has no tab for a served session by design
        // (see stdio_bridge._emit_session), so this row is the ONLY place its
        // operator can see what is running on their hardware — every field
        // describe() carries gets rendered rather than dropped.
        const meta = document.createElement('div');
        meta.className = 'connect-session-meta';
        const bits = [];
        if (session.state) bits.push(session.state);
        const seq = Number(session.cur_seq) || 0;
        if (seq > 0) bits.push(`${seq} event${seq === 1 ? '' : 's'}`);
        if (session.attached === false) bits.push('detached');
        const ago = _ago(session.last_activity_at || session.created_at);
        if (ago) bits.push(ago);
        meta.textContent = bits.join(' · ');
        body.appendChild(meta);

        // A confirmation parked on a session whose controller has no tab open
        // blocks the agent on this machine indefinitely, and is invisible
        // everywhere else.
        const waiting = Number(session.pending_confirms) || 0;
        if (waiting > 0) {
            const warn = document.createElement('div');
            warn.className = 'connect-session-waiting';
            warn.textContent = waiting === 1
                ? '⏸ Waiting for the controller to confirm'
                : `⏸ Waiting for the controller to confirm ${waiting}`;
            warn.title = 'The agent on this machine is blocked on a human answer'
                       + ' that has to come from the controlling side.';
            body.appendChild(warn);
        }

        // Latest human-readable line (server.py's RemoteSession.last_message).
        if (session.last_message) {
            const last = document.createElement('div');
            last.className = 'connect-session-last';
            last.dataset.role = session.last_message_role || '';
            last.textContent = `${_roleGlyph(session.last_message_role)} ${session.last_message}`;
            last.title = session.last_message;
            body.appendChild(last);
        }

        const close = document.createElement('button');
        close.type = 'button';
        close.className = 'connect-session-close';
        close.textContent = 'Close';
        close.addEventListener('click', async () => {
            if (!sid) return;
            const ok = await confirm(
                'End this session?',
                `Session "${session.title || sid}" is running on this machine, driven by ${serving && serving.client_name || 'the remote controller'}`
                + '. Ending it means the other side cannot resume it — this is the local operator\'s'
                + ' final say, not a detach.\n\nThe agent will stop; its working directory stays on this machine.');
            if (!ok) return;
            close.disabled = true;
            try {
                await call('connect_close_session_server_side',
                           { session_id: sid });
                appendLog('server', `session ${sid} closed`);
                await refresh();
            } catch (err) {
                appendLog('server', `close failed: ${err.message || err}`);
                close.disabled = false;
            }
        });
        row.appendChild(body);
        row.appendChild(close);
        return row;
    }

    // Relative time for descriptor timestamps (seconds since epoch, as Python
    // time.time() produces them). Kept coarse on purpose — the row is a status
    // summary, and "3m ago" reads faster than a wall clock the operator then
    // has to subtract from.
    function _ago(ts) {
        const t = Number(ts) || 0;
        if (t <= 0) return '';
        const secs = Math.max(0, Math.round(Date.now() / 1000 - t));
        if (secs < 10) return 'just now';
        if (secs < 60) return `${secs}s ago`;
        if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
        if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
        return `${Math.floor(secs / 86400)}d ago`;
    }

    function _roleGlyph(role) {
        if (role === 'user') return '▸';
        if (role === 'error') return '⚠';
        if (role === 'notice') return '!';
        return '↩';   // agent reply, and the default for anything unlabelled
    }

    // ── As Client rendering ─────────────────────────────────────────────

    function renderClient() {
        dom.serverList.innerHTML = '';
        if (!targets.length) {
            dom.serverList.setAttribute('data-empty', 'true');
            const empty = document.createElement('div');
            empty.className = 'connect-empty-hint';
            empty.textContent = 'No servers connected yet. Use the buttons above to add one.';
            dom.serverList.appendChild(empty);
            return;
        }
        dom.serverList.removeAttribute('data-empty');
        targets.forEach((t) => dom.serverList.appendChild(renderTargetRow(t)));
    }

    function renderTargetRow(target) {
        // target.state: "connected" | "connecting" | "offline"
        const state = target.state || (target.connected ? 'connected' : 'offline');
        const stateLabel =
            state === 'connected' ? 'Connected' :
            state === 'connecting' ? 'Connecting…' : 'Not connected';
        const card = document.createElement('div');
        card.className = 'connect-target-card';
        card.dataset.state = state;

        const head = document.createElement('div');
        head.className = 'connect-target-head';
        head.innerHTML = `
            <span class="connect-target-dot" data-state="${escapeHtml(state)}"></span>
            <span class="connect-target-name">
                ${escapeHtml(target.name || target.server_name || target.host || 'server')}
            </span>
            <span class="connect-target-endpoint">
                (${escapeHtml(target.host || '')}:${target.port || 0})
            </span>
            <span class="connect-target-platform">${escapeHtml(target.platform || '')}</span>
            <span class="connect-target-state">${escapeHtml(stateLabel)}</span>
        `;
        card.appendChild(head);

        // Deferred-upgrade banner. A newer Linux package exists but wasn't
        // installed because the machine was busy (see hub.pair_linux_over_ssh).
        // New Session is disabled below while this is showing — new work belongs
        // on the new build — but existing chips keep working.
        const pendingUpgrade = target.upgrade_pending || null;
        const hasPendingUpgrade = !!(pendingUpgrade && pendingUpgrade.to);
        if (hasPendingUpgrade && state === 'connected') {
            const up = document.createElement('div');
            up.className = 'connect-upgrade-banner';
            const sessCount = (Array.isArray(target.sessions) ? target.sessions : []).length;
            const txt = document.createElement('span');
            txt.textContent =
                `新版本 ${pendingUpgrade.to} 待安装（当前 ${pendingUpgrade.from || '未知'}）`
                + (sessCount ? ` — ${sessCount} 个会话在运行，结束后升级最稳妥` : '');
            up.appendChild(txt);
            const upBtn = document.createElement('button');
            upBtn.type = 'button';
            upBtn.className = 'connect-upgrade-now';
            upBtn.textContent = '立即升级';
            upBtn.title = '重启远端守护进程并安装新版本 — 会销毁该机器上的全部会话，'
                        + '并需要重新配对（换新端口/令牌）';
            upBtn.addEventListener('click', () => upgradeTargetNow(target));
            up.appendChild(upBtn);
            card.appendChild(up);
        }

        // Session chips.
        const chipsWrap = document.createElement('div');
        chipsWrap.className = 'connect-target-sessions';
        const sessions = Array.isArray(target.sessions) ? target.sessions : [];
        if (!sessions.length) {
            const empty = document.createElement('span');
            empty.className = 'connect-target-sessions-empty';
            empty.textContent = 'No sessions yet';
            chipsWrap.appendChild(empty);
        } else {
            sessions.forEach((s) => chipsWrap.appendChild(renderSessionChip(target, s)));
        }
        card.appendChild(chipsWrap);

        // Three verbs, three different scopes, and the labels have to keep them
        // apart — conflating them is what made "Disconnect" leave a card behind
        // that then needed a second, redundant "Forget" click.
        //
        //   Connect / Disconnect — local only. A被控 machine is a server: we stop
        //     visiting, it keeps running, its sessions stay exactly where they
        //     are. Disconnecting also hands the machine back, since it serves one
        //     controller at a time.
        //   End … (destructive) — the one action that reaches across and destroys
        //     work. Only offered while connected, and worded per platform because
        //     it genuinely differs: a Linux daemon exits (its port and token die
        //     with it, so the pairing goes too), while a Windows server keeps
        //     listening for its owner and keeps its pairing.
        //   Forget — local bookkeeping only: drop the address and token. Offered
        //     when there is no live connection to act on.
        //
        // "connecting" is a THIRD state, not a flavour of offline: an attempt is
        // in flight, so no verb is true yet and both buttons say why they wait.
        // Offering Forget there would invite deleting a pairing that is seconds
        // from coming back.
        const actions = document.createElement('div');
        actions.className = 'connect-target-actions';
        const newBtn = document.createElement('button');
        newBtn.type = 'button';
        newBtn.textContent = '+ New Session';
        // Held back while an upgrade is pending: new work should start on the
        // new build, and it also gives the machine a chance to drain so the
        // upgrade can happen. Existing chips (▶ / ×) stay live.
        newBtn.disabled = (state !== 'connected') || hasPendingUpgrade;
        if (hasPendingUpgrade && state === 'connected') {
            newBtn.title = '有新版本待安装 — 结束当前会话后升级，再开新会话';
        }
        newBtn.addEventListener('click', () => newRemoteSession(target));
        actions.appendChild(newBtn);

        const isLinuxTarget =
            String(target.platform || '').toLowerCase().indexOf('linux') >= 0;

        if (state === 'connected') {
            const linkBtn = document.createElement('button');
            linkBtn.type = 'button';
            linkBtn.textContent = 'Disconnect';
            linkBtn.title = 'Stop talking to this machine. Nothing over there is'
                          + ' destroyed — its sessions keep running and you can'
                          + ' reconnect any time. Also frees it for another'
                          + ' controller, since it serves one at a time.';
            linkBtn.addEventListener('click', () => disconnectTarget(target));
            actions.appendChild(linkBtn);

            const uploadBtn = document.createElement('button');
            uploadBtn.type = 'button';
            uploadBtn.textContent = 'Upload Skill';
            uploadBtn.title = 'Pick skills from your own Skill folder and push them'
                             + ' to this machine — the same-named folder over there is'
                             + ' fully overwritten to match your copy.';
            uploadBtn.addEventListener('click', () => openSkillUpload(target));
            actions.appendChild(uploadBtn);

            const endBtn = document.createElement('button');
            endBtn.type = 'button';
            endBtn.className = 'destructive';
            endBtn.textContent = isLinuxTarget ? 'Shut down remote' : 'End my sessions';
            endBtn.title = isLinuxTarget
                ? 'Destroys every session on that machine and exits its daemon.'
                  + ' Its port and token die with it, so the pairing is removed'
                  + ' and using it again means pairing over SSH from scratch.'
                : 'Destroys every session this machine is running for you and'
                  + ' disconnects. That machine keeps serving (its owner put it in'
                  + ' server mode), so the pairing is kept.';
            endBtn.addEventListener('click', () => releaseTarget(target));
            actions.appendChild(endBtn);
        } else {
            const connectBtn = document.createElement('button');
            connectBtn.type = 'button';
            connectBtn.className = 'primary';
            connectBtn.textContent = 'Connect';
            connectBtn.disabled = (state === 'connecting');
            connectBtn.title = state === 'connecting'
                ? 'Already connecting…'
                : 'Connect to this machine and load whatever sessions it is holding';
            connectBtn.addEventListener('click', () => connectTarget(target));
            actions.appendChild(connectBtn);

            const forgetBtn = document.createElement('button');
            forgetBtn.type = 'button';
            forgetBtn.className = 'destructive';
            forgetBtn.textContent = 'Forget';
            forgetBtn.disabled = (state === 'connecting');
            forgetBtn.title = state === 'connecting'
                ? 'Connecting — wait for this to settle before removing the pairing'
                : 'Remove this machine from the pairing list. Local only: the'
                  + ' machine and any sessions on it are untouched.';
            forgetBtn.addEventListener('click', () => forgetTarget(target));
            actions.appendChild(forgetBtn);
        }
        card.appendChild(actions);
        return card;
    }

    function renderSessionChip(target, session) {
        const state = target.state || (target.connected ? 'connected' : 'offline');
        const controllable = session.controllable !== false;
        const pending = Number(session.pending_confirms) || 0;
        const chip = document.createElement('span');
        chip.className = 'connect-session-chip';
        chip.dataset.kind = session.is_task ? 'task' : 'chat';
        if (pending > 0) chip.dataset.pending = 'true';
        if (!controllable) chip.dataset.controllable = 'false';

        // Badge: task vs chat. Purely informational — both kinds live exactly as
        // long as each other and both need an explicit × to end. It used to
        // decide whether a session was remembered at all, which is why a
        // finished task could vanish just for having been reopened.
        const badge = document.createElement('span');
        badge.className = 'connect-chip-badge';
        badge.textContent = session.is_task ? 'Task' : 'Chat';
        chip.appendChild(badge);

        const label = document.createElement('span');
        label.className = 'connect-chip-label';
        // Real title first. The fallback is the raw rc- id, which is what every
        // chip used to show: the title was computed from the goal at open time
        // and then dropped by both sides instead of being stored.
        label.textContent = session.title || session.session_id || 'session';
        label.title = sessionTooltip(session);
        chip.appendChild(label);

        // A parked confirmation on a session with no open tab blocks the agent
        // over there indefinitely and is otherwise completely invisible, so it
        // gets its own marker rather than living in the tooltip.
        if (pending > 0) {
            const waiting = document.createElement('span');
            waiting.className = 'connect-chip-waiting';
            waiting.textContent = pending === 1 ? '⏸ Pending confirm' : `⏸ Pending confirm ${pending}`;
            waiting.title = 'The remote agent is waiting for your confirmation — open the session to answer it';
            chip.appendChild(waiting);
        } else if (session.state) {
            const st = document.createElement('span');
            st.className = 'connect-chip-state';
            st.textContent = session.state;
            chip.appendChild(st);
        }

        const play = document.createElement('button');
        play.type = 'button';
        play.className = 'connect-chip-play';
        play.textContent = '▶';
        play.title = controllable
            ? 'Open session'
            : 'This machine holds no credential for this session (it may have been created by another controller) and cannot open it';
        play.disabled = (state !== 'connected') || !controllable;
        play.addEventListener('click', () => openRemoteSession(target, session));
        const close = document.createElement('button');
        close.type = 'button';
        close.className = 'connect-chip-close';
        close.textContent = '×';
        const closeBlocked = (state !== 'connected');
        if (controllable) {
            close.title = 'Destroy session (the remote agent will stop)';
            close.disabled = closeBlocked;
            close.addEventListener('click', () => closeRemoteSession(target, session));
        } else {
            // No held capability, so a normal close is impossible — but the
            // auth token this controller already holds for the target is
            // enough to force-close (never to attach/drive) it. This is the
            // only way to reclaim a chip whose registry record was lost or
            // that belongs to another controller's orphaned session.
            close.classList.add('connect-chip-force-close');
            close.title = 'This machine holds no credential for this session, but can force-terminate it using the pairing credential (cannot view its content or history)';
            close.disabled = closeBlocked;
            close.addEventListener('click', () => forceCloseRemoteSession(target, session));
        }
        chip.appendChild(play);
        chip.appendChild(close);
        return chip;
    }

    function sessionTooltip(session) {
        const lines = [session.session_id || ''];
        if (session.is_task) lines.push('Type: Task');
        else lines.push('Type: Chat');
        if (session.state) lines.push(`State: ${session.state}`);
        if (session.attached) lines.push('A tab is currently attached to it');
        const ts = Number(session.last_activity_at) || Number(session.updated_at) || 0;
        if (ts > 0) {
            lines.push(`Last activity: ${new Date(ts * 1000).toLocaleString()}`);
        }
        if (session.alive === null || session.alive === undefined) {
            lines.push('(Not connected — can\'t confirm whether the remote side is still running)');
        }
        // The single most-misread thing about this feature: closing the tab is a
        // detach, not an end. Said here because the chip is where the operator
        // discovers a session they thought they had closed is still going.
        lines.push('Closing its tab only detaches — the remote agent keeps'
                   + ' working and you can reopen it from here. Only × ends it.');
        return lines.join('\n');
    }

    // ── Action handlers ─────────────────────────────────────────────────

    async function startServer() {
        appendLog('server', 'Starting server listener…');
        try {
            const res = await call('connect_start_server', {});
            serving = res.serving || null;
            appendLog('server',
                `Listening: ${serving && serving.endpoint || '(unknown)'}`);
            render();
        } catch (err) {
            appendLog('server', `Start failed: ${err.message || err}`);
        }
    }

    async function stopServer() {
        const live = (serving && Array.isArray(serving.sessions))
            ? serving.sessions.length : 0;
        if (live > 0) {
            const ok = await confirm(
                'Exit Server?',
                `There ${live === 1 ? 'is' : 'are'} currently ${live} remote session${live === 1 ? '' : 's'} running on this machine. Stopping the`
                + ' listener destroys them immediately (the other side sees "remote HandQ closed"), and any interrupted agent'
                + ' work will not resume automatically.\n\n'
                + 'Just want to kick out the current controller but keep listening? Use "Disconnect Client" instead.');
            if (!ok) return;
        }
        appendLog('server', 'Stopping server…');
        try {
            await call('connect_stop_server', {});
            serving = null;
            // Exit = truly leaving server role. Clear log and go back.
            clearLog('server');
            goto('role');
        } catch (err) {
            appendLog('server', `Stop failed: ${err.message || err}`);
        }
    }

    async function disconnectClient() {
        const live = (serving && Array.isArray(serving.sessions))
            ? serving.sessions.length : 0;
        const who = (serving && serving.client_name) || 'the current controller';
        const ok = await confirm(
            `Disconnect ${who}?`,
            'This is more final than a network drop: a drop leaves sessions parked waiting for reconnect, while this'
            + ` immediately destroys the ${live} session${live === 1 ? '' : 's'} it has running on this machine.\n\n`
            + 'This machine keeps listening — the next controller that connects gets a clean machine.');
        if (!ok) return;
        appendLog('server', 'Disconnecting current client…');
        try {
            const res = await call('connect_disconnect_client', {});
            appendLog('server', `Disconnected, destroyed ${res.destroyed || 0} session(s)`);
            await refresh();
        } catch (err) {
            appendLog('server', `Disconnect failed: ${err.message || err}`);
        }
    }

    async function connectTarget(target) {
        // On-demand connect. There is no boot-time sweep any more: "connected" is
        // not a durable property worth restoring at startup, it is "am I visiting
        // right now", so it is established when the operator (or opening this
        // panel) actually needs it.
        const name = target.name || target.host || target.target_id;
        appendLog('client', `Connecting to ${name}…`);
        try {
            const res = await call('remote_connect', { target_id: target.target_id });
            targets = res.targets || targets;
            const n = (res.sessions || []).length;
            appendLog('client',
                      `Connected to ${res.server_name || name}`
                      + (n ? ` — ${n} session${n === 1 ? '' : 's'} waiting there` : ''));
            render();
        } catch (err) {
            appendLog('client', `Could not connect to ${name}: ${err.message || err}`);
            await refresh();
        }
    }

    async function disconnectTarget(target) {
        // Local only, so no confirmation: nothing on the other machine changes.
        const name = target.name || target.host || target.target_id;
        appendLog('client', `Disconnecting from ${name}…`);
        try {
            const res = await call('remote_disconnect', { target_id: target.target_id });
            targets = res.targets || targets;
            appendLog('client',
                      `Disconnected from ${name} — its sessions keep running there`);
            render();
        } catch (err) {
            appendLog('client', `Failed to disconnect from ${name}: ${err.message || err}`);
            await refresh();
        }
    }

    async function releaseTarget(target) {
        const name = target.name || target.host || target.target_id;
        const sessions = Array.isArray(target.sessions) ? target.sessions : [];
        const isLinux = String(target.platform || '').toLowerCase().indexOf('linux') >= 0;
        // The only action in this panel that destroys work on another machine, so
        // it always confirms, and the text says exactly which of the two things it
        // does — they are not the same act.
        const ok = isLinux
            ? await confirm(
                `Shut down remote HandQ on ${name}?`,
                'This will:\n'
                + `  · Destroy the ${sessions.length} session${sessions.length === 1 ? '' : 's'} on that machine (the remote agent stops immediately)\n`
                + '  · Exit its daemon process\n'
                + '  · Remove it from the pairing list (its port and token die with the process)\n\n'
                + 'Using it again means pairing over SSH from scratch.\n\n'
                + 'Just done for now? Use "Disconnect" instead — the sessions keep'
                + ' running there and you can reconnect whenever you like.')
            : await confirm(
                `End your sessions on ${name}?`,
                'This will:\n'
                + `  · Destroy the ${sessions.length} session${sessions.length === 1 ? '' : 's'} that machine is running for you\n`
                + '  · Disconnect\n\n'
                + 'That machine keeps serving — its owner put it into server mode —'
                + ' so the pairing is kept and you can connect again.\n\n'
                + 'Just done for now? Use "Disconnect" instead: it leaves your'
                + ' sessions parked over there, exactly as you left them.');
        if (!ok) return;
        appendLog('client', isLinux ? `Shutting down ${name}…` : `Ending sessions on ${name}…`);
        try {
            const res = await call('connect_release_target',
                                   { target_id: target.target_id });
            targets = res.targets || [];
            appendLog('client',
                      res.forgot
                          ? `${name} shut down and removed from the pairing list`
                          : `Sessions on ${name} destroyed; pairing kept`);
            // Reported, not thrown: a Linux daemon that did exactly as asked often
            // cannot acknowledge, because it exits while the ack is in flight (see
            // hub.release_target). The local bookkeeping is done either way.
            if (res.warning) appendLog('client', `Note: ${res.warning}`);
            render();
        } catch (err) {
            appendLog('client', `Failed to end ${name}: ${err.message || err}`);
            await refresh();
        }
    }

    async function upgradeTargetNow(target) {
        // Apply a deferred Linux upgrade. This is the one place we deliberately
        // interrupt: bouncing the daemon to swap the binary destroys every
        // session on it and mints a new port+token, so the pairing has to be
        // re-established. That is why it is never automatic — the operator has
        // to choose it, having been told the cost.
        const name = target.name || target.host || target.target_id;
        const up = target.upgrade_pending || {};
        const sessions = Array.isArray(target.sessions) ? target.sessions : [];
        const ok = await confirm(
            `Upgrade ${name} to ${up.to || 'the new version'} now?`,
            `This restarts the remote daemon to install ${up.to || 'the new build'}`
            + `${up.from ? ` (currently ${up.from})` : ''}. It will:\n`
            + `  · Destroy the ${sessions.length} session${sessions.length === 1 ? '' : 's'} running on that machine\n`
            + '  · Re-pair automatically (the daemon comes back on a new port/token)\n\n'
            + 'Prefer to wait? Just let the running sessions finish — the upgrade'
            + ' offer stays until you take it.');
        if (!ok) return;
        appendLog('client', `Upgrading ${name} to ${up.to || '(new version)'}…`);
        try {
            // force:true re-runs the SSH bootstrap, which now bounces the daemon
            // and deploys because the caller has explicitly accepted the
            // interruption (see linux_bootstrap._require_idle_or_forced).
            const res = await call('remote_pair_linux', {
                target_id: target.target_id,
                credentials_file: target.credentials_file || '',
                ssh_target: target.ssh_target || '',
                name: target.name || '',
                force: true,
            });
            targets = res.targets || targets;
            appendLog('client', `${name} upgraded`);
            await refresh();
        } catch (err) {
            appendLog('client', `Upgrade failed for ${name}: ${err.message || err}`);
            await refresh();
        }
    }

    async function forgetTarget(target) {
        // The offline counterpart to releaseTarget: no live connection to sever,
        // so this only removes the pairing locally. The remote machine and any
        // sessions on it are untouched — we simply forget its address and token.
        const name = target.name || target.host || target.target_id;
        const ok = await confirm(
            `Remove ${name} from the list?`,
            'This machine is not currently connected, so there is no live connection to sever.\n\n'
            + '"Confirm" only removes it from this machine\'s pairing list (deleting the saved address and token).'
            + ' The remote machine and any sessions on it are untouched — pair again to control it.');
        if (!ok) return;
        appendLog('client', `Removing ${name}…`);
        try {
            const res = await call('remote_forget', { target_id: target.target_id });
            targets = res.targets || [];
            appendLog('client', `${name} removed from the pairing list`);
            render();
        } catch (err) {
            appendLog('client', `Failed to remove ${name}: ${err.message || err}`);
        }
    }

    // ── Upload Skill picker ──────────────────────────────────────────────
    // Lists this machine's own user-authored skills (skill_list already
    // excludes bundled ones — same rule the admin Skills panel relies on) so
    // one or more can be picked and pushed to a connected server. Each
    // picked skill's folder over there is fully overwritten to match the
    // local copy; skills not picked are left untouched.
    let skillUploadTarget = null;
    let skillUploadSkills = [];
    const skillUploadSelected = new Set();

    function showSkillUploadToast(msg, kind) {
        if (!dom.skillUploadToast) return;
        dom.skillUploadToast.textContent = msg;
        dom.skillUploadToast.classList.remove('hidden', 'error');
        if (kind === 'error') dom.skillUploadToast.classList.add('error');
        clearTimeout(showSkillUploadToast._tmr);
        showSkillUploadToast._tmr = setTimeout(
            () => dom.skillUploadToast.classList.add('hidden'), 3500);
    }

    async function openSkillUpload(target) {
        skillUploadTarget = target;
        skillUploadSelected.clear();
        if (!dom.skillUploadOverlay) return;
        dom.skillUploadOverlay.classList.remove('hidden');
        dom.skillUploadOverlay.setAttribute('aria-hidden', 'false');
        const name = target.name || target.host || target.target_id;
        if (dom.skillUploadTargetLabel) {
            dom.skillUploadTargetLabel.textContent = `Uploading to: ${name}`;
        }
        await refreshSkillUploadGrid();
    }

    function closeSkillUpload() {
        if (!dom.skillUploadOverlay) return;
        dom.skillUploadOverlay.classList.add('hidden');
        dom.skillUploadOverlay.setAttribute('aria-hidden', 'true');
        skillUploadTarget = null;
    }

    async function refreshSkillUploadGrid() {
        try {
            const res = await call('skill_list', {});
            skillUploadSkills = res.skills || [];
            renderSkillUploadGrid();
        } catch (err) {
            showSkillUploadToast(`Failed to load skills: ${err.message || err}`, 'error');
        }
    }

    function renderSkillUploadGrid() {
        if (!dom.skillUploadGrid) return;
        dom.skillUploadGrid.innerHTML = '';
        if (dom.skillUploadCount) {
            dom.skillUploadCount.textContent = skillUploadSkills.length
                ? `${skillUploadSkills.length} available`
                : '';
        }
        if (!skillUploadSkills.length) {
            const empty = document.createElement('div');
            empty.className = 'muted skills-empty';
            empty.textContent = '(no skills to upload — create one first)';
            dom.skillUploadGrid.appendChild(empty);
            return;
        }
        for (const s of skillUploadSkills) {
            const card = document.createElement('div');
            card.className = 'skill-card';

            const head = document.createElement('div');
            head.className = 'skill-card-head';

            const cbLabel = document.createElement('label');
            cbLabel.className = 'skill-upload-pick';
            const cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.checked = skillUploadSelected.has(s.name);
            cb.addEventListener('change', () => {
                if (cb.checked) skillUploadSelected.add(s.name);
                else skillUploadSelected.delete(s.name);
            });
            const name = document.createElement('span');
            name.className = 'skill-card-name';
            name.textContent = s.name;
            cbLabel.appendChild(cb);
            cbLabel.appendChild(name);
            head.appendChild(cbLabel);
            card.appendChild(head);

            const desc = document.createElement('div');
            desc.className = 'skill-card-desc';
            desc.textContent = s.description || '(no description)';
            card.appendChild(desc);

            dom.skillUploadGrid.appendChild(card);
        }
    }

    async function submitSkillUpload() {
        if (!skillUploadTarget) return;
        const names = Array.from(skillUploadSelected);
        if (!names.length) {
            showSkillUploadToast('Select at least one skill', 'error');
            return;
        }
        const targetName = skillUploadTarget.name || skillUploadTarget.host
                          || skillUploadTarget.target_id;
        appendLog('client', `Uploading ${names.length} skill(s) to ${targetName}…`);
        try {
            const res = await call('remote_push_skills', {
                target_id: skillUploadTarget.target_id,
                names,
            });
            const results = res.results || [];
            for (const r of results) {
                appendLog('client', r.ok
                    ? `  ✓ ${r.name}`
                    : `  ✗ ${r.name}: ${r.error || 'failed'}`);
            }
            const failed = results.filter((r) => !r.ok).length;
            showSkillUploadToast(
                failed ? `${failed} of ${results.length} failed — see Log` : 'Upload complete',
                failed ? 'error' : undefined);
            if (!failed) closeSkillUpload();
        } catch (err) {
            appendLog('client', `Upload to ${targetName} failed: ${err.message || err}`);
            showSkillUploadToast(`Upload failed: ${err.message || err}`, 'error');
        }
    }

    async function exitClient() {
        const connected = targets.filter(
            (t) => (t.state || (t.connected ? 'connected' : 'offline')) === 'connected');
        // No confirmation: leaving client mode is a LOCAL act now. It used to
        // release every connected machine — destroying their sessions and deleting
        // their pairings — which made "stop being a client" an instruction to
        // every server in the list. A client leaving is not something a server
        // needs to act on. Destroying anything is per-machine and asks first.
        appendLog('client',
                  connected.length
                      ? `Disconnecting from ${connected.length} machine${connected.length === 1 ? '' : 's'}`
                        + ' — their sessions keep running…'
                      : 'Leaving client mode…');
        try {
            const res = await call('connect_exit_client', {});
            targets = res.targets || [];
            // Exit = truly leaving client role. Clear log and go back.
            clearLog('client');
            goto('role');
        } catch (err) {
            appendLog('client', `Exit Client failed: ${err.message || err}`);
        }
    }

    // ── Session management (client side) ────────────────────────────────

    async function newRemoteSession(target) {
        // Delegate to remote-control.js which owns the tab-creation +
        // remote_bind pipeline. Falls back with a friendly log line if
        // HandQRemote isn't loaded (an install-order bug, shouldn't happen).
        if (!window.HandQRemote || !window.HandQRemote.newSession) {
            appendLog('client',
                'HandQRemote.newSession not found — please restart HandQ and try again.');
            return;
        }
        // Connect on demand: creating a session is a reason to connect, so an
        // offline card shouldn't dead-end here. ensure_client is idempotent, so
        // this is a no-op when already connected.
        target = await ensureConnected(target);
        if (!target) return;   // connect failed; ensureConnected already logged it
        try {
            const sid = await window.HandQRemote.newSession(target);
            appendLog('client',
                `Created session on ${target.name || target.host} (tab=${sid})`);
            // Close the panel so the user can start typing in the new tab.
            close();
            await refresh();
        } catch (err) {
            appendLog('client', `Failed to create session: ${err.message || err}`);
        }
    }

    async function ensureConnected(target) {
        // Returns the freshest target record once connected, or null on failure.
        // The record is re-read from `targets` after the connect because
        // remote_connect refreshes server_name/platform/state, which
        // newSession/focusOrMount downstream care about.
        const state = target.state || (target.connected ? 'connected' : 'offline');
        if (state === 'connected') return target;
        if (state === 'connecting') {
            appendLog('client',
                `${target.name || target.host} is still connecting — try again in a moment`);
            return null;
        }
        await connectTarget(target);
        const fresh = targets.find((t) => t.target_id === target.target_id);
        const freshState = fresh
            ? (fresh.state || (fresh.connected ? 'connected' : 'offline'))
            : 'offline';
        if (freshState !== 'connected') {
            appendLog('client',
                `${target.name || target.host} did not connect — cannot continue`);
            return null;
        }
        return fresh;
    }

    async function openRemoteSession(target, session) {
        // Session chip ▶: focus the tab already bound to this rc-xxx if one
        // exists (see remote-control.js), otherwise mount a new tab that
        // attaches with since_seq so the transcript arrives replayed.
        if (!window.HandQRemote || !window.HandQRemote.focusOrMount) {
            appendLog('client',
                'HandQRemote.focusOrMount not found — please restart HandQ and try again.');
            return;
        }
        // Opening a chip is also a reason to connect. Only meaningful for a
        // controllable session (▶ is disabled otherwise), and only worth trying
        // when a connect could actually help.
        target = await ensureConnected(target);
        if (!target) return;
        try {
            await window.HandQRemote.focusOrMount(target, session);
            close();
        } catch (err) {
            appendLog('client', `Failed to open session: ${err.message || err}`);
        }
    }

    async function closeRemoteSession(target, session) {
        const sid = session.session_id || session.id;
        // The one action that really ends a remote session — everything else
        // (closing the tab, closing HandQ, losing the network) leaves it running.
        // So it asks, and says which of the two it is doing.
        const ok = await confirm(
            'Destroy this remote session?',
            `Session "${session.title || sid}" on ${target.name || target.host} is`
            + `${session.alive === true ? ' still alive' : ' possibly still alive'}.\n\n`
            + '"Confirm" makes that machine end it completely: the agent stops, its working directory stays on the remote side.\n'
            + 'This cannot be undone.\n\n'
            + 'Just want to collapse the tab? Just close the tab — the session keeps running,'
            + ' and you can reopen it from here later.');
        if (!ok) return;
        appendLog('client', `Destroying remote session ${sid}…`);
        try {
            await call('remote_close_session', {
                target_id: target.target_id,
                remote_session_id: sid,
            });
            appendLog('client', `Session ${sid} destroyed`);
            await refresh();
        } catch (err) {
            // The chip is still there on purpose: an unconfirmed close leaves the
            // record alone (see hub.close_remote_session_by_id) so the operator
            // can retry rather than losing the only handle on a live session.
            appendLog('client', `Failed to destroy session: ${err.message || err}`);
            await refresh();
        }
    }

    async function forceCloseRemoteSession(target, session) {
        const sid = session.session_id || session.id;
        // This chip has no held capability — it belongs to another
        // controller, or its record was lost — so there is no way to open,
        // inspect, or normally close it. Force-close uses only the auth
        // token this controller already holds for the target, which is
        // enough to end the session but not to see anything about it.
        const ok = await confirm(
            'Force-terminate this session?',
            `Session "${session.title || sid}" on ${target.name || target.host} — `
            + 'this machine holds no credential for it (it may have been created by another controller, or the pairing record was lost).\n\n'
            + '"Confirm" only makes the remote side immediately terminate this session\'s agent process — you will not see its'
            + ' conversation content or work history, and it cannot be recovered or reopened.\n\n'
            + 'Use this only to clean up a zombie session that\'s occupying the session limit and that no controller can'
            + ' close normally. If you suspect someone else is actually using this session, confirm that first.');
        if (!ok) return;
        appendLog('client', `Force-terminating remote session ${sid}…`);
        try {
            await call('remote_close_session', {
                target_id: target.target_id,
                remote_session_id: sid,
                force: true,
            });
            appendLog('client', `Session ${sid} force-terminated`);
            await refresh();
        } catch (err) {
            appendLog('client', `Force-terminate failed: ${err.message || err}`);
        }
    }

    /**
     * Yes/no prompt. Delegates to HandQRemote's dialog so the Connect panel and
     * the pairing flows look the same, and falls back to window.confirm if that
     * module isn't loaded — a destructive action must never proceed just because
     * the pretty dialog was unavailable.
     */
    async function confirm(title, body) {
        if (window.HandQRemote && window.HandQRemote.confirmDialog) {
            return !!(await window.HandQRemote.confirmDialog(title, body));
        }
        try {
            return !!window.confirm(`${title}\n\n${body}`);
        } catch (_) {
            return false;
        }
    }

    // ── Add-server flows (delegate to HandQRemote's dialogs) ────────────

    async function addLinuxAuto() {
        // Reuses HandQRemote's Linux SSH auto-pair dialog (paste ssh_target,
        // keyring/password → deploy → daemon → CONNECT ME). Every log line
        // ends up here too rather than in a chat bubble (per design §10).
        if (!window.HandQRemote || !window.HandQRemote.addLinuxAuto) {
            appendLog('client',
                'HandQRemote.addLinuxAuto not found — please restart HandQ and try again.');
            return;
        }
        try {
            await window.HandQRemote.addLinuxAuto((line) => appendLog('client', line));
            await refresh();
        } catch (err) {
            appendLog('client', `Linux auto-pairing failed: ${err.message || err}`);
        }
    }

    async function addWindowsManual() {
        // Reuses HandQRemote's paste-pairing dialog.
        if (!window.HandQRemote || !window.HandQRemote.addManual) {
            appendLog('client',
                'HandQRemote.addManual not found — please restart HandQ and try again.');
            return;
        }
        try {
            await window.HandQRemote.addManual((line) => appendLog('client', line));
            await refresh();
        } catch (err) {
            appendLog('client', `Manual pairing failed: ${err.message || err}`);
        }
    }

    // ── Overlay open/close & init ───────────────────────────────────────

    function render() {
        if (page === 'server') renderServer();
        if (page === 'client') renderClient();
    }

    // Loading veil for the gap between the panel becoming visible and the
    // first remote_control_status reply landing — same pattern (and CSS
    // class) as Settings' ensureSettingsLoadingOverlay in renderer.js, so a
    // fresh boot's RPC round trip reads as "loading" instead of an empty
    // role/server/client page that looks broken.
    let connectLoadingEl = null;

    function ensureConnectLoadingOverlay() {
        if (connectLoadingEl) return connectLoadingEl;
        const card = dom.overlay && dom.overlay.querySelector('.connect-card');
        if (!card) return null;
        connectLoadingEl = document.createElement('div');
        connectLoadingEl.className = 'settings-loading-overlay hidden';
        const text = document.createElement('span');
        text.className = 'loading-text';
        text.textContent = 'Checking connection…';
        connectLoadingEl.appendChild(text);
        card.appendChild(connectLoadingEl);
        return connectLoadingEl;
    }

    function showConnectLoading() {
        const ov = ensureConnectLoadingOverlay();
        if (ov) ov.classList.remove('hidden');
    }

    function hideConnectLoading() {
        if (connectLoadingEl) connectLoadingEl.classList.add('hidden');
    }

    async function open() {
        if (!dom.overlay) return;
        dom.overlay.classList.remove('hidden');
        dom.overlay.setAttribute('aria-hidden', 'false');
        showConnectLoading();
        // Fetch current state FIRST, then route to the right page.
        try {
            await refresh();
        } finally {
            hideConnectLoading();
        }
        const active = resolveActivePage();
        goto(active || 'role');
    }

    function close() {
        if (!dom.overlay) return;
        dom.overlay.classList.add('hidden');
        dom.overlay.setAttribute('aria-hidden', 'true');
    }

    function escapeHtml(s) {
        if (s == null) return '';
        return String(s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function copyToClipboard(source) {
        const el = document.getElementById(source);
        if (!el) return;
        const text = el.textContent.trim();
        if (!text || text === '—') return;
        try {
            navigator.clipboard.writeText(text);
            appendLog(page === 'server' ? 'server' : 'client',
                      `Copied to clipboard (${text.length} chars)`);
        } catch (err) {
            log('WARN', 'connect-panel: clipboard write failed',
                { error: String(err) });
        }
    }

    function init() {
        if (mounted) return;
        mounted = true;
        dom.overlay = document.getElementById('overlay-connect');
        if (!dom.overlay) {
            log('WARN', 'connect-panel: #overlay-connect not found; disabled');
            return;
        }
        dom.title = document.getElementById('connect-title');
        dom.closeBtn = document.getElementById('connect-close');
        // Server dashboard refs.
        dom.serverPairing = document.getElementById('connect-server-pairing');
        dom.serverStatus = document.getElementById('connect-server-status');
        dom.serverSessions = document.getElementById('connect-server-sessions');
        dom.serverLog = document.getElementById('connect-server-log');
        // Client dashboard refs.
        dom.serverList = document.getElementById('connect-server-list');
        dom.clientLog = document.getElementById('connect-client-log');
        dom.clientExit = document.getElementById('connect-client-exit');
        // Upload Skill picker refs.
        dom.skillUploadOverlay = document.getElementById('overlay-skill-upload');
        dom.skillUploadTargetLabel = document.getElementById('skill-upload-target');
        dom.skillUploadToast = document.getElementById('skill-upload-toast');
        dom.skillUploadGrid = document.getElementById('skill-upload-grid');
        dom.skillUploadCount = document.getElementById('skill-upload-count');

        // Wire buttons.
        dom.closeBtn.addEventListener('click', close);
        document.getElementById('connect-role-server')
            .addEventListener('click', async () => { goto('server'); await startServer(); });
        document.getElementById('connect-role-client')
            .addEventListener('click', () => { goto('client'); refresh(); });
        document.getElementById('connect-server-exit')
            .addEventListener('click', stopServer);
        document.getElementById('connect-server-disconnect')
            .addEventListener('click', disconnectClient);
        document.getElementById('connect-client-exit')
            .addEventListener('click', exitClient);
        document.getElementById('connect-add-linux')
            .addEventListener('click', addLinuxAuto);
        document.getElementById('connect-add-windows')
            .addEventListener('click', addWindowsManual);
        // Log clear buttons
        const srvClear = document.getElementById('connect-server-log-clear');
        if (srvClear) srvClear.addEventListener('click', () => clearLog('server'));
        const cliClear = document.getElementById('connect-client-log-clear');
        if (cliClear) cliClear.addEventListener('click', () => clearLog('client'));
        // Copy buttons
        dom.overlay.querySelectorAll('.connect-copy').forEach((btn) => {
            btn.addEventListener('click', () => copyToClipboard(btn.dataset.copySource));
        });
        // Upload Skill picker buttons.
        const skUploadClose = document.getElementById('skill-upload-close');
        if (skUploadClose) skUploadClose.addEventListener('click', closeSkillUpload);
        const skUploadCancel = document.getElementById('skill-upload-cancel');
        if (skUploadCancel) skUploadCancel.addEventListener('click', closeSkillUpload);
        const skUploadRefresh = document.getElementById('skill-upload-refresh-btn');
        if (skUploadRefresh) skUploadRefresh.addEventListener('click', refreshSkillUploadGrid);
        const skUploadSubmit = document.getElementById('skill-upload-submit');
        if (skUploadSubmit) skUploadSubmit.addEventListener('click', submitSkillUpload);

        // Subscribe to bridge push events. Same channels remote-control.js
        // uses — we don't compete with it; both can update from the same
        // envelope. Also settle our own RPCs here.
        window.handq.onFinal((evt) => {
            settleRpc(evt, false);
        });
        window.handq.onError((evt) => { settleRpc(evt, true); });

        // Status envelopes carry remote_control_status / remote_serve_state /
        // connect_log — routed by handlePush.
        if (window.handq && window.handq.onStatus) {
            window.handq.onStatus((evt) => handlePush(evt));
        }

        // The island-menu trigger (#sc-remote) is wired by remote-control.js,
        // which now delegates to HandQConnect.open() when we're loaded — so
        // we don't attach a second listener here (it would fire twice).
    }

    function handlePush(evt) {
        if (!evt) return;
        // Status envelopes are shape {type: "status", kind: "<name>", ...}.
        if (evt.type !== 'status') return;
        const kind = evt.kind || '';
        if (kind === 'remote_control_status') {
            if (evt.serving !== undefined) serving = evt.serving;
            if (evt.targets !== undefined) targets = evt.targets;
            render();
        } else if (kind === 'remote_target_state') {
            // The hub broadcasts this on every connection-state change
            // (connecting / connected / disconnected / released, plus session
            // reconciliation). This panel used to ignore it entirely — only the
            // legacy remote-control.js overlay listened — so the client
            // dashboard never live-updated and a machine that connected in the
            // background stayed rendered as "Not connected" until the next full
            // refresh(). Now that connecting is on demand, this is what turns a
            // card from "Connecting…" into "Connected" without polling.
            if (Array.isArray(evt.targets)) targets = evt.targets;
            if (page === 'client') render();
        } else if (kind === 'remote_serve_state') {
            serveState = {
                serving: !!evt.serving,
                sessionCount: evt.session_count || 0,
                attachedCount: evt.attached_count || 0,
                port: evt.port || 0,
                error: evt.error || '',
            };
            if (page === 'server') render();
        } else if (kind === 'connect_log') {
            // v6 §10: bridge-side connection events land in the panel's Log
            // area, NOT chat bubbles. Route by the `role` field: "server"
            // events into the As Server dashboard's Log, "client" events
            // into the As Client dashboard's Log. Unknown routes to client
            // by default (the more common case).
            const role = (evt.role === 'server') ? 'server' : 'client';
            const src = evt.source ? `[${evt.source}] ` : '';
            const prefix = (evt.level === 'error') ? '⚠ '
                         : (evt.level === 'warn') ? '! '
                         : '';
            appendLog(role, `${prefix}${src}${evt.message || ''}`);
        }
    }

    // Wire the "sc-remote" island-menu button to open THIS panel instead of
    // the old #overlay-remote. renderer.js may also attach a handler that
    // opens the old overlay — the new panel wins by stopping propagation.
    // If a user still needs the old overlay it's reachable via
    // window.HandQRemote.open() from devtools during the transition.

    window.HandQConnect = {
        init,
        open,
        close,
        refresh,
    };
}());
