/*
 * remote-control.js — the "control another machine's HandQ" surface.
 *
 * Deliberately thin. A remote session is an ORDINARY session tab: the bridge
 * puts a RemoteSessionBridge in the slot a FlowControllerV2 would occupy and
 * replays the remote machine's UI events onto the local _StdioUI, so every
 * envelope the chat pane already knows how to draw arrives through the code
 * that already draws it. Nothing here touches bubbles, confirmations, the task
 * plan, or the activity sidebar — all of that works because it is the same path
 * a local session uses.
 *
 * So this module only owns what is genuinely new:
 *   1. the pairing dialog (paste one handq:// line)
 *   2. the paired-machine list, and "new session on that machine"
 *   3. this machine's own control address, for the operator to copy out
 *   4. a per-card connection badge, so "disconnected" reads as "reconnecting,
 *      the remote agent is still working" rather than as a failure
 *
 * Modal idiom follows admin-panel.js's programmatic Promise dialog rather than
 * window.confirm/prompt: on Windows an OS-level modal steals focus from the
 * Chromium renderer and the text-input/IME focus state does not reliably come
 * back, which leaves unrelated textareas unclickable (see the comment at
 * admin-panel.js:81-88).
 */
(function () {
    'use strict';

    const RPC_TIMEOUT_MS = 45000;
    // Linux auto-pairing runs discover → deploy a tarball → start the daemon →
    // poll for the pid file, and may wait on a password prompt in a chat tab.
    // On a cold host that is minutes, not seconds.
    const LINUX_PAIR_TIMEOUT_MS = 600000;

    const pending = new Map();
    let nextRpcId = 1;

    // Last payload from remote_control_status, so a re-render doesn't need a
    // round trip.
    let serving = null;
    let targets = [];
    // Serve-role state for the titlebar indicator, kept fresh by
    // remote_serve_state broadcasts. serving=false means the indicator hides.
    let serveState = { serving: false, sessionCount: 0, attachedCount: 0,
                       port: 0, error: '' };
    // Set while a remote operator's agent is driving THIS machine's physical
    // desktop, from the unstamped served_desktop_takeover_* broadcasts. Kept
    // separate from serveState because it is a different and much louder claim:
    // serving means "someone may drive me", this means "someone is moving my
    // cursor right now". The fullscreen overlay is main.js's job; this only
    // makes the titlebar say so too, so the warning survives the overlay's
    // idle-hide grace (the overlay hides between desktop actions while the
    // takeover is still armed).
    let servedTakeover = null;   // {sessionId, controllerName, title, reason}
    // local session id -> {targetId, endpoint, serverName, state}
    const remoteSessions = new Map();

    const dom = {};

    function log(level, msg, extra) {
        try {
            if (window.__handqLog) window.__handqLog(level, msg, extra);
        } catch (_) { /* ignore */ }
    }

    // ── RPC ──────────────────────────────────────────────────────────────────
    // Same shape as admin-panel.js's rpc(): the bridge answers every
    // remote_* type with a `final` carrying {ok, ...}, so onFinal settles the
    // promise. Envelope fields are merged LAST so a caller's payload can never
    // shadow type/id — clobbering id strands the response forever.

    function rpc(type, payload, timeoutMs) {
        const id = `rc-${type}-${nextRpcId++}-${Date.now()}`;
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

    // A bridge-side failure comes back as {ok:false, error} in a `final`, not as
    // an error envelope — that keeps the structured detail, which an error
    // envelope would flatten into a string. Unwrap it into a rejection here so
    // callers can just try/catch.
    async function call(type, payload, timeoutMs) {
        const result = await rpc(type, payload, timeoutMs);
        if (result && result.ok === false) {
            const err = new Error(result.error || `${type} failed`);
            // Carried through so a caller can distinguish "remote has a task
            // running, refusing auto-restart" from an ordinary failure and
            // offer a specific "force restart" retry instead of a dead-end
            // error message.
            if (result.busy) err.busy = true;
            throw err;
        }
        return result || {};
    }

    // The active chat tab's id, for panel-initiated operations that may need to
    // prompt the user mid-flight. Only stamp this on requests that actually need
    // a prompt route — a session_id also tells the bridge which session an
    // operation belongs to, so stamping it blindly would misattribute work.
    function activeSessionId() {
        try {
            if (window.HandQRenderer && window.HandQRenderer.currentSid) {
                return window.HandQRenderer.currentSid() || '';
            }
        } catch (_) { /* ignore */ }
        return '';
    }

    // ── Generic prompt dialog ────────────────────────────────────────────────

    let dialogEl = null;
    let dialogResolve = null;

    function buildDialog() {
        if (dialogEl) return dialogEl;
        const wrap = document.createElement('div');
        wrap.className = 'overlay hidden';
        wrap.id = 'overlay-remote-pair';
        wrap.style.zIndex = '2000';

        const card = document.createElement('div');
        card.className = 'overlay-card rc-dialog-card';
        card.setAttribute('role', 'dialog');
        card.setAttribute('aria-modal', 'true');

        const title = document.createElement('div');
        title.className = 'rc-dialog-title';
        card.appendChild(title);

        const body = document.createElement('div');
        body.className = 'rc-dialog-body';
        card.appendChild(body);

        const input = document.createElement('textarea');
        input.className = 'rc-dialog-input';
        input.rows = 3;
        input.spellcheck = false;
        input.placeholder = 'handq://10.0.0.5:55079/…';
        card.appendChild(input);

        const nameInput = document.createElement('input');
        nameInput.className = 'rc-dialog-name';
        nameInput.type = 'text';
        nameInput.placeholder = 'Display name (optional, defaults to the remote hostname)';
        card.appendChild(nameInput);

        const status = document.createElement('div');
        status.className = 'rc-dialog-status';
        card.appendChild(status);

        const actions = document.createElement('div');
        actions.className = 'scc-actions rc-dialog-actions';
        const cancelBtn = document.createElement('button');
        cancelBtn.type = 'button';
        cancelBtn.textContent = 'Cancel';
        const testBtn = document.createElement('button');
        testBtn.type = 'button';
        testBtn.textContent = 'Test Connection';
        const okBtn = document.createElement('button');
        okBtn.type = 'button';
        okBtn.className = 'primary';
        okBtn.textContent = 'Pair';
        actions.appendChild(cancelBtn);
        actions.appendChild(testBtn);
        actions.appendChild(okBtn);
        card.appendChild(actions);

        wrap.appendChild(card);
        document.body.appendChild(wrap);

        function settle(value) {
            wrap.classList.add('hidden');
            const resolve = dialogResolve;
            dialogResolve = null;
            if (resolve) resolve(value);
        }

        cancelBtn.addEventListener('click', () => settle(null));
        wrap.addEventListener('mousedown', (ev) => {
            if (ev.target === wrap) settle(null);
        });

        // Probe before committing. Worth its own button: a mistyped or stale
        // address otherwise fails much later, at first use, with the failure
        // detached from the action that caused it.
        testBtn.addEventListener('click', async () => {
            const pairing = input.value.trim();
            if (!pairing) { status.textContent = 'Paste the control address first'; return; }
            status.className = 'rc-dialog-status';
            status.textContent = 'Connecting…';
            testBtn.disabled = true;
            try {
                const res = await call('remote_probe', { pairing });
                status.className = 'rc-dialog-status ok';
                const count = (res.sessions || []).length;
                status.textContent = `Connected: ${res.server_name || '?'}`
                    + ` (${res.platform || '?'})`
                    + (count ? ` — remote already has ${count} session(s)` : '');
                if (!nameInput.value.trim() && res.server_name) {
                    nameInput.value = res.server_name;
                }
            } catch (err) {
                status.className = 'rc-dialog-status err';
                status.textContent = String(err.message || err);
            } finally {
                testBtn.disabled = false;
            }
        });

        okBtn.addEventListener('click', () => {
            const pairing = input.value.trim();
            if (!pairing) { status.textContent = 'Paste the control address first'; return; }
            settle({ pairing, name: nameInput.value.trim() });
        });

        document.addEventListener('keydown', (e) => {
            if (!dialogEl || dialogEl.wrap.classList.contains('hidden')) return;
            if (e.key === 'Escape') { e.preventDefault(); settle(null); }
        });

        dialogEl = { wrap, title, body, input, nameInput, status, testBtn, mode: 'pairing' };
        return dialogEl;
    }

    function openPairDialog() {
        const dlg = buildDialog();
        dlg.title.textContent = 'Pair a remote machine';
        dlg.body.textContent =
            'On the target machine, open Remote machines and copy its "This machine\'s control address" line, then paste it below.'
            + ' That address carries a one-time token — share it only through a trusted channel.';
        dlg.input.value = '';
        dlg.input.placeholder = 'handq://10.0.0.5:55079/…';
        dlg.nameInput.value = '';
        dlg.status.textContent = '';
        dlg.status.className = 'rc-dialog-status';
        dlg.testBtn.classList.remove('hidden');
        dlg.mode = 'pairing';
        dlg.wrap.classList.remove('hidden');
        return new Promise((resolve) => {
            dialogResolve = resolve;
            try { dlg.input.focus(); } catch (_) { /* ignore */ }
        });
    }

    /**
     * Linux variant: no address to paste. HandQ already has an authenticated way
     * in — the SSH channel it uses to install and start handq_linux — so the
     * address is fetched instead of copied. All the operator supplies is
     * user@host.
     */
    function openLinuxDialog() {
        const dlg = buildDialog();
        dlg.title.textContent = 'Auto-pair a Linux machine';
        dlg.body.textContent =
            'Enter user@host. HandQ will use SSH to confirm/install handq_linux and start the daemon,'
            + ' then read the direct-connect address it publishes — every interaction after that goes'
            + ' directly, no more SSH.'
            + ' Requires remote_control.serve to be true in that machine\'s handq_config.yaml.';
        dlg.input.value = '';
        dlg.input.placeholder = 'user@hostname';
        dlg.nameInput.value = '';
        dlg.status.textContent = '';
        dlg.status.className = 'rc-dialog-status';
        // No probe: there is nothing to probe until the daemon is up, and
        // bringing it up IS the action.
        dlg.testBtn.classList.add('hidden');
        dlg.mode = 'linux';
        dlg.wrap.classList.remove('hidden');
        return new Promise((resolve) => {
            dialogResolve = resolve;
            try { dlg.input.focus(); } catch (_) { /* ignore */ }
        });
    }

    // ── Confirm dialog ───────────────────────────────────────────────────────
    // Same reason as the pairing dialog: window.confirm() is an OS-level modal
    // that on Windows does not reliably hand text-input focus back to the
    // renderer, leaving unrelated textareas unclickable.

    let confirmEl = null;
    let confirmResolve = null;

    function confirmDialog(question, detail) {
        if (!confirmEl) {
            const wrap = document.createElement('div');
            wrap.className = 'overlay hidden';
            wrap.style.zIndex = '2100';
            const card = document.createElement('div');
            card.className = 'overlay-card rc-dialog-card';
            card.setAttribute('role', 'alertdialog');
            card.setAttribute('aria-modal', 'true');
            const title = el('div', 'rc-dialog-title');
            const body = el('div', 'rc-dialog-body');
            card.appendChild(title);
            card.appendChild(body);
            const actions = el('div', 'scc-actions rc-dialog-actions');
            const cancel = el('button', '', 'Cancel');
            cancel.type = 'button';
            const ok = el('button', 'primary', 'Confirm');
            ok.type = 'button';
            actions.appendChild(cancel);
            actions.appendChild(ok);
            card.appendChild(actions);
            wrap.appendChild(card);
            document.body.appendChild(wrap);

            function settle(value) {
                wrap.classList.add('hidden');
                const resolve = confirmResolve;
                confirmResolve = null;
                if (resolve) resolve(value);
            }
            cancel.addEventListener('click', () => settle(false));
            ok.addEventListener('click', () => settle(true));
            wrap.addEventListener('mousedown', (ev) => {
                if (ev.target === wrap) settle(false);
            });
            document.addEventListener('keydown', (e) => {
                if (!confirmEl || confirmEl.wrap.classList.contains('hidden')) return;
                if (e.key === 'Escape') { e.preventDefault(); settle(false); }
            });
            confirmEl = { wrap, title, body, ok };
        }
        confirmEl.title.textContent = question;
        confirmEl.body.textContent = detail || '';
        confirmEl.wrap.classList.remove('hidden');
        return new Promise((resolve) => {
            confirmResolve = resolve;
            try { confirmEl.ok.focus(); } catch (_) { /* ignore */ }
        });
    }

    // ── Rendering ────────────────────────────────────────────────────────────
    function el(tag, cls, text) {
        const node = document.createElement(tag);
        if (cls) node.className = cls;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    function renderTargets() {
        const host = dom.targetList;
        if (!host) return;
        host.innerHTML = '';
        host.setAttribute('data-empty', targets.length ? 'false' : 'true');
        if (!targets.length) {
            host.appendChild(el('div', 'rc-empty',
                'No machines paired yet. Click "+ Pair New Machine" to start.'));
            return;
        }

        for (const target of targets) {
            const row = el('div', 'rc-target');
            row.setAttribute('data-target', target.target_id);

            const dot = el('span', 'rc-dot' + (target.connected ? ' on' : ''));
            row.appendChild(dot);

            const info = el('div', 'rc-target-info');
            const nameLine = el('div', 'rc-target-name',
                target.name || target.host);
            if (target.platform) {
                nameLine.appendChild(el('span', 'rc-target-os',
                    target.platform === 'win32' ? 'Windows' : target.platform));
            }
            info.appendChild(nameLine);
            info.appendChild(el('div', 'rc-target-endpoint',
                `${target.host}:${target.port}`
                + (target.connected
                    ? (target.server_name ? ` · Connected to ${target.server_name}` : ' · Connected')
                    : ' · Not connected')));

            // Sessions still alive on that machine that this controller has
            // driven before. Re-adopting one is the "come back tomorrow and pick
            // up where you left off" path; the × terminates it on the served side.
            const known = target.sessions || [];
            if (known.length) {
                const list = el('div', 'rc-target-sessions');
                for (const session of known) {
                    const wrap = el('span', 'rc-session-chip-wrap');
                    const chip = el('button', 'rc-session-chip');
                    chip.type = 'button';
                    chip.textContent = session.title || session.session_id;
                    chip.title = `Re-adopt remote session ${session.session_id}`;
                    chip.addEventListener('click', () => {
                        adoptSession(target, session).catch(showError);
                    });
                    const kill = el('button', 'rc-session-kill', '×');
                    kill.type = 'button';
                    kill.title = 'End this remote session (actually stops the session on the remote machine)';
                    kill.addEventListener('click', (e) => {
                        e.stopPropagation();
                        terminateRemote(target, session).catch(showError);
                    });
                    wrap.appendChild(chip);
                    wrap.appendChild(kill);
                    list.appendChild(wrap);
                }
                info.appendChild(list);
            }
            row.appendChild(info);

            const actions = el('div', 'rc-target-actions');
            const newBtn = el('button', 'primary', 'New Session');
            newBtn.type = 'button';
            newBtn.addEventListener('click', () => {
                openRemoteSession(target).catch(showError);
            });
            actions.appendChild(newBtn);

            const forgetBtn = el('button', '', 'Unpair');
            forgetBtn.type = 'button';
            forgetBtn.addEventListener('click', () => {
                forgetTarget(target).catch(showError);
            });
            actions.appendChild(forgetBtn);
            row.appendChild(actions);

            host.appendChild(row);
        }
    }

    function renderServing() {
        const stateEl = dom.serveState;
        const bodyEl = dom.serveBody;
        if (!stateEl || !bodyEl) return;
        bodyEl.innerHTML = '';

        if (!serving || !serving.serving) {
            stateEl.textContent = 'Off';
            stateEl.className = 'remote-serve-state off';
            const help = el('p', 'help-text');
            help.textContent = serving && serving.error
                ? `Failed to start listener: ${serving.error}`
                : 'This machine does not currently accept remote control. Enable'
                  + ' "Allow this machine to be remotely controlled" in Settings → Remote control,'
                  + ' then restart HandQ.';
            bodyEl.appendChild(help);
            return;
        }

        stateEl.textContent = `Listening on :${serving.port}`;
        stateEl.className = 'remote-serve-state on';

        const help = el('p', 'help-text');
        help.textContent = 'Copy the line below to the controller. It carries this machine\'s access token — '
            + 'this channel has no TLS yet, so only share and use it on a trusted internal network.';
        bodyEl.appendChild(help);

        const row = el('div', 'rc-pairing-row');
        const code = el('code', 'rc-pairing');
        code.textContent = serving.pairing || '';
        row.appendChild(code);

        const copyBtn = el('button', 'primary', 'Copy');
        copyBtn.type = 'button';
        copyBtn.addEventListener('click', async () => {
            try {
                await navigator.clipboard.writeText(serving.pairing || '');
                copyBtn.textContent = 'Copied';
                setTimeout(() => { copyBtn.textContent = 'Copy'; }, 1500);
            } catch (_) {
                // Clipboard can be denied; selecting the text is the fallback
                // that always works.
                const range = document.createRange();
                range.selectNodeContents(code);
                const sel = window.getSelection();
                sel.removeAllRanges();
                sel.addRange(range);
                copyBtn.textContent = 'Selected — press Ctrl+C';
                setTimeout(() => { copyBtn.textContent = 'Copy'; }, 2500);
            }
        });
        row.appendChild(copyBtn);
        bodyEl.appendChild(row);

        // A multi-homed box (VPN + LAN, common in the target machine pool) has
        // several addresses and only some are reachable from the controller.
        // Showing them all beats making the operator guess.
        const endpoints = serving.endpoints || [];
        if (endpoints.length > 1) {
            const alt = el('div', 'rc-alt-endpoints');
            alt.appendChild(el('span', 'rc-alt-label', 'Other addresses on this machine:'));
            alt.appendChild(el('span', '', endpoints.slice(1).join('  ')));
            bodyEl.appendChild(alt);
        }

        const live = serving.sessions || [];
        const sessLine = el('div', 'rc-serve-sessions');
        sessLine.textContent = live.length
            ? `${live.length} session(s) currently driven remotely`
              + ` (${live.filter((s) => s.attached).length} connected)`
            : 'No sessions currently driven remotely';
        bodyEl.appendChild(sessLine);
    }

    function showError(err) {
        const message = String((err && err.message) || err);
        log('ERROR', 'remote_control error', { error: message });
        // v6: errors go to the Connect panel log, not a chat bubble.
        if (_linuxPairSink) {
            _linuxPairSink(`ERROR: ${message}`);
        }
        if (dom.serveState) {
            const status = dialogEl && dialogEl.status;
            if (status && dialogEl.wrap && !dialogEl.wrap.classList.contains('hidden')) {
                status.className = 'rc-dialog-status err';
                status.textContent = message;
            }
        }
    }

    // ── Actions ──────────────────────────────────────────────────────────────

    async function refresh() {
        const res = await call('remote_control_status', {});
        serving = res.serving || null;
        targets = res.targets || [];
        // Keep the titlebar indicator in sync whenever we pull full status
        // (the periodic broadcasts handle the between-times updates).
        if (serving) {
            serveState = {
                serving: !!serving.serving,
                sessionCount: serving.session_count || (serving.sessions || []).length,
                attachedCount: serving.attached_count
                    || (serving.sessions || []).filter((s) => s.attached).length,
                port: serving.port || 0,
                error: serving.error || '',
            };
            renderServeIndicator();
        }
        renderTargets();
        renderServing();
    }

    // ── Serve-role titlebar indicator ───────────────────────────────────────

    function renderServeIndicator() {
        const btn = dom.serveIndicator;
        if (!btn) return;
        if (!serveState.serving) {
            // Not serving → the indicator isn't shown at all. A machine that
            // isn't accepting remote control has nothing to indicate.
            btn.classList.add('hidden');
            return;
        }
        btn.classList.remove('hidden');
        const n = serveState.sessionCount;
        btn.classList.toggle('active', n > 0);   // highlighted while driven
        // Distinct from 'active': a remote agent is driving the physical mouse
        // and keyboard right now, not merely holding a session.
        btn.classList.toggle('driving-desktop', !!servedTakeover);
        if (dom.serveIndicatorCount) {
            dom.serveIndicatorCount.textContent = n > 0 ? String(n) : '';
        }
        if (servedTakeover) {
            const who = servedTakeover.controllerName || 'A remote controller';
            btn.title =
                `${who} is controlling this machine's mouse and keyboard right now.`
                + ' Press Ctrl+Shift+C to revoke desktop control.';
            return;
        }
        btn.title = n > 0
            ? `This machine is being remotely controlled (${n} remote session(s), ${serveState.attachedCount} connected)`
            : `This machine allows remote control, listening on :${serveState.port} (no one connected)`;
    }

    // Serving is no longer toggled from here. There used to be a setServe()
    // that flipped `remote_control.serve` in config and started/stopped the
    // listener, reachable from both the titlebar ⇄ indicator and a settings
    // checkbox — two surfaces for a decision the Connect panel's "As Server" /
    // "Exit Server" buttons already own, and the config half made HandQ listen
    // at boot with a token nobody had been shown. The indicator now just opens
    // the Connect panel, where the state and the buttons live together.

    async function addTarget() {
        const answer = await openPairDialog();
        if (!answer) return;
        const res = await call('remote_pair', answer);
        targets = res.targets || targets;
        renderTargets();
        announcePaired(res.target);
    }

    async function addLinuxTarget() {
        const answer = await openLinuxDialog();
        if (!answer) return;
        await runLinuxPair(answer, false);
    }

    /**
     * Runs the SSH auto-pair flow, and on a "daemon busy" refusal offers an
     * explicit confirm-then-retry with force:true rather than just showing an
     * error. The refusal itself (LinuxDaemonBusyError, see
     * linux_bootstrap.py's _require_idle_or_forced) means the remote daemon has
     * a task running and restarting it would interrupt that — this dialog is
     * the one place a human gets to decide that's acceptable, instead of the
     * bootstrap code silently deciding for them.
     */
    async function runLinuxPair(answer, force) {
        // v6: status lines go to the Connect panel's log area, not a chat
        // bubble. The sink is set by addLinuxAuto's caller; fall through to
        // nothing if not provided (old callers).
        if (_linuxPairSink) _linuxPairSink(
            `SSH → ${answer.pairing}: bootstrapping… (first connection may prompt for a password)`);
        try {
            const res = await call('remote_pair_linux', {
                ssh_target: answer.pairing,
                name: answer.name,
                force: !!force,
                // A host with no key trust and nothing in the keyring needs a
                // password, and the bridge can only route that prompt if it
                // knows which session to attach it to.
                session_id: activeSessionId(),
            }, LINUX_PAIR_TIMEOUT_MS);
            targets = res.targets || targets;
            renderTargets();
            announcePaired(res.target);
        } catch (err) {
            if (err.busy) {
                const proceed = await confirmDialog(
                    'The remote machine has a task running',
                    `${err.message}\n\n"Confirm" force-restarts the remote daemon and interrupts the current task;`
                    + ' "Cancel" makes no changes — you can try again later.');
                if (proceed) {
                    await runLinuxPair(answer, true);
                    return;
                }
                return;
            }
            throw err;
        }
    }

    // Temporary sink pointer set by addLinuxAuto — cleared after the call.
    let _linuxPairSink = null;

    function announcePaired(target) {
        const t = target || {};
        const msg = `Paired remote machine ${t.name || ''} (${t.host}:${t.port})`;
        // v6: announce to Connect panel log, NOT a chat bubble.
        if (_linuxPairSink) _linuxPairSink(msg);
    }

    async function forgetTarget(target) {
        const ok = await confirmDialog(
            `Unpair ${target.name || target.host}?`,
            'This machine will forget its address and token. Any session running on the other side is unaffected,'
            + ' but controlling it again means pairing from scratch.');
        if (!ok) return;
        const res = await call('remote_forget', { target_id: target.target_id });
        targets = res.targets || [];
        renderTargets();
    }

    /**
     * Mint a local tab, bind it to a machine, and let the user type.
     *
     * Order matters: remote_bind must land BEFORE the tab's first `request`, or
     * _ensure_any_flow builds an ordinary local FlowControllerV2 for the sid and
     * the tab is permanently local. Binding is awaited for that reason.
     */
    async function openRemoteSession(target, opts) {
        const label = target.name || target.host;
        const sid = window.HandQRenderer.createSession({
            name: `⇄ ${label}`,
        });
        try {
            await call('remote_bind', Object.assign({
                session_id: sid,
                target_id: target.target_id,
            }, opts || {}));
        } catch (err) {
            window.HandQRenderer.markRemoteSessionState(sid, 'failed', String(err.message || err));
            // v6 fix: if the bind fails, CLOSE the tab we just created so it
            // doesn't sit there as an empty local-session shell that confuses
            // the user. The error is reported via the Connect panel's log.
            if (window.HandQRenderer.closeSession) {
                window.HandQRenderer.closeSession(sid);
            }
            throw err;
        }
        remoteSessions.set(sid, {
            targetId: target.target_id,
            endpoint: `${target.host}:${target.port}`,
            serverName: target.server_name || '',
            state: 'pending',
            // Remembered so the Connect panel's focusOrMount can find an
            // existing tab bound to a given rc-xxx and switch to it instead
            // of mounting a duplicate. Left blank for a "fresh" open (no
            // remote_session_id yet) — the bridge fills it in once the
            // remote responds with session_opened.
            remoteSessionId: (opts && opts.remote_session_id) || '',
        });
        window.HandQRenderer.markRemoteSessionState(sid, 'pending', label);
        closeOverlay();
        // v6: also close the Connect panel if it's open
        if (window.HandQConnect && window.HandQConnect.close) {
            window.HandQConnect.close();
        }
        return sid;
    }

    function adoptSession(target, session) {
        // No since_seq. A re-adopt mints a BLANK tab, so it needs the whole
        // retained transcript, not the tail after wherever some earlier tab
        // left off — the bridge asks for a full replay (see
        // RemoteSessionBridge.start). Sending the chip's stored seq here is
        // what used to produce a tab that was correctly attached to a live
        // remote session yet showed no chat, no ACTIVITY and no FILES.
        return openRemoteSession(target, {
            remote_session_id: session.session_id,
        });
    }

    /**
     * End a remote session that has no open local tab (a session chip in the
     * panel). Distinct from closing a tab (which only detaches) and from
     * adopting the chip (which opens a new tab): this one really stops the
     * agent on the serving side.
     */
    async function terminateRemote(target, session) {
        const ok = await confirmDialog(
            'End this remote session?',
            `Session "${session.title || session.session_id}" is still alive on ${target.name || target.host};`
            + ' "Confirm" tells that machine to end it completely (the remote agent stops, its working directory stays).'
            + ' "Cancel" does nothing.');
        if (!ok) return;
        const res = await call('remote_close_session', {
            target_id: target.target_id,
            remote_session_id: session.session_id,
        });
        if (res.targets) targets = res.targets;
        renderTargets();
    }

    // ── Overlay wiring ───────────────────────────────────────────────────────

    function openOverlay() {
        // v6: if the new Connect panel is loaded, defer to it. The old
        // #overlay-remote surface is kept only so this module's
        // isRemote/terminate/setServe hooks (used by tab-close paths) keep
        // working without a wider refactor; opening it directly is a
        // transition-only escape hatch reachable through devtools.
        if (window.HandQConnect && window.HandQConnect.open) {
            window.HandQConnect.open();
            return;
        }
        if (!dom.overlay) return;
        dom.overlay.classList.remove('hidden');
        dom.overlay.setAttribute('aria-hidden', 'false');
        refresh().catch(showError);
    }

    function closeOverlay() {
        if (!dom.overlay) return;
        dom.overlay.classList.add('hidden');
        dom.overlay.setAttribute('aria-hidden', 'true');
    }

    // ── Status envelopes from the bridge ─────────────────────────────────────

    function onStatus(evt) {
        if (!evt || !evt.kind) return;
        const kind = evt.kind;

        if (kind === 'remote_target_state') {
            targets = evt.targets || targets;
            if (dom.overlay && !dom.overlay.classList.contains('hidden')) {
                renderTargets();
            }
            return;
        }

        if (kind === 'remote_serve_state') {
            // Serve-role changed (started/stopped, or a driven session
            // appeared/disappeared). Drives the titlebar indicator; no
            // session_id, it's machine-level.
            serveState = {
                serving: !!evt.serving,
                sessionCount: evt.session_count || 0,
                attachedCount: evt.attached_count || 0,
                port: evt.port || 0,
                error: evt.error || '',
            };
            renderServeIndicator();
            return;
        }

        if (kind === 'served_desktop_takeover_started') {
            // Machine-level and deliberately unstamped — the id is under
            // served_session_id, not session_id, so this envelope cannot make
            // the renderer mount a tab for a served session (see
            // stdio_bridge._ServedDesktopNotifier).
            servedTakeover = {
                sessionId: evt.served_session_id || '',
                controllerName: evt.controller_name || '',
                title: evt.title || '',
                reason: evt.reason || '',
            };
            renderServeIndicator();
            return;
        }

        if (kind === 'served_desktop_takeover_ended') {
            servedTakeover = null;
            renderServeIndicator();
            return;
        }

        const sid = evt.session_id;
        if (!sid) return;

        switch (kind) {
        case 'remote_session_opened':
            remoteSessions.set(sid, Object.assign(
                remoteSessions.get(sid) || {},
                { remoteSessionId: evt.remote_session_id, state: 'connected' }));
            window.HandQRenderer.markRemoteSessionState(
                sid, 'connected', evt.server_name || evt.endpoint || '');
            break;
        case 'remote_attached':
        case 'remote_connected':
            window.HandQRenderer.markRemoteSessionState(
                sid, 'connected', evt.server_name || evt.endpoint || '');
            break;
        case 'remote_disconnected':
            // NOT a failure: the remote agent keeps working and the client is
            // already retrying. The badge says "reconnecting" so the operator
            // doesn't assume the task died.
            window.HandQRenderer.markRemoteSessionState(
                sid, 'reconnecting', evt.detail || '');
            break;
        case 'remote_superseded':
            window.HandQRenderer.markRemoteSessionState(sid, 'superseded', '');
            break;
        case 'remote_session_closed':
            window.HandQRenderer.markRemoteSessionState(
                sid, 'closed', evt.reason || '');
            remoteSessions.delete(sid);
            break;
        default:
            break;
        }
    }

    // isRemote(sid) / terminate(sid) used to live here as "tab-close hooks".
    // They had no callers anywhere in electron/renderer: the tab-close decision
    // is made on the bridge side, where the truth is — renderer.js's
    // closeSession() sends close_session unconditionally and _do_close_session
    // asks hub.is_remote(sid) whether to detach or destroy locally. Removed
    // rather than left as a second, unreachable answer to the same question.

    function init() {
        dom.overlay = document.getElementById('overlay-remote');
        dom.targetList = document.getElementById('remote-target-list');
        dom.serveState = document.getElementById('remote-serve-state');
        dom.serveBody = document.getElementById('remote-serve-body');
        dom.serveIndicator = document.getElementById('serve-indicator');
        dom.serveIndicatorCount = document.getElementById('serve-indicator-count');
        const trigger = document.getElementById('sc-remote');
        const closeBtn = document.getElementById('remote-close');
        const addBtn = document.getElementById('remote-add-btn');
        const addLinuxBtn = document.getElementById('remote-add-linux-btn');

        if (trigger) trigger.addEventListener('click', openOverlay);
        if (closeBtn) closeBtn.addEventListener('click', closeOverlay);
        // The titlebar ⇄ indicator opens the same Remote machines panel — from
        // there the operator sees exactly what's connected and can turn serving
        // off. Clicking the indicator IS the "I noticed I'm being controlled"
        // affordance.
        if (dom.serveIndicator) {
            dom.serveIndicator.addEventListener('click', openOverlay);
        }
        if (addBtn) {
            addBtn.addEventListener('click', () => { addTarget().catch(showError); });
        }
        if (addLinuxBtn) {
            addLinuxBtn.addEventListener('click', () => {
                addLinuxTarget().catch(showError);
            });
        }
        if (dom.overlay) {
            dom.overlay.addEventListener('click', (e) => {
                if (e.target === dom.overlay) closeOverlay();
            });
        }
        window.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && dom.overlay
                && !dom.overlay.classList.contains('hidden')) {
                closeOverlay();
            }
        });

        // Additive fan-out (preload.js pushes onto arrays), so registering here
        // is independent of renderer.js's own listeners. Ids are namespaced
        // `rc-*` and every other listener ignores what it doesn't own.
        window.handq.onStatus(onStatus);
        window.handq.onFinal((evt) => { settleRpc(evt, false); });
        window.handq.onError((evt) => { settleRpc(evt, true); });
    }

    window.HandQRemote = {
        init,
        open: openOverlay,
        // Escape hatch: open the OLD #overlay-remote directly, bypassing the
        // v6-delegation in openOverlay. Only for tests that specifically
        // exercise the old panel's DOM (ui_headless_check.py). Real users go
        // through the island-menu trigger, which lands them in the new
        // Connect panel via openOverlay's delegation.
        legacyOpen() {
            if (!dom.overlay) return;
            dom.overlay.classList.remove('hidden');
            dom.overlay.setAttribute('aria-hidden', 'false');
            refresh().catch(showError);
        },
        refresh,
        // Shared with the Connect panel so both surfaces ask the same way. Every
        // irreversible Connect action (release a server, exit client, destroy a
        // session) goes through this — see connect-panel.js's confirm().
        confirmDialog,
        // v6 fix: renderer.js's closeSession(sid) must call this so
        // `remoteSessions` (this module's own bookkeeping of "which local
        // tab is bound to which rc-xxx") drops the entry for a tab that no
        // longer exists. Without it, the Connect panel's session chip ▶
        // (focusOrMount) finds a STALE entry pointing at a dead sid, calls
        // switchSession(deadSid) — which silently no-ops because
        // sessions.get(deadSid) is undefined — and the user sees nothing
        // happen (or stays on whatever tab was already active), looking
        // exactly like "re-adopt opened the wrong/local session". The bridge
        // side (RemoteSessionBridge.destroy / hub.release_bridge) already
        // detaches correctly; this was purely a stale-cache bug in the
        // renderer's OWN "is there already a tab for this rc-xxx" index.
        notifyLocalTabClosed(sid) {
            remoteSessions.delete(sid);
        },
        // v6 Connect panel entry points. `addManual` prompts for a
        // handq:// pairing string; `addLinuxAuto` prompts for an SSH target
        // and runs the discover/deploy/daemon pipeline. Both accept an
        // optional log-sink so the new Connect panel can pipe status lines
        // into its Log area instead of a chat bubble (design doc §10).
        async addManual(sink) {
            const answer = await openPairDialog();
            if (!answer) return null;
            if (sink) sink(`Pairing ${answer.name || answer.pairing || '…'} …`);
            _linuxPairSink = sink || null;
            try {
                const res = await call('remote_pair', answer);
                targets = res.targets || [];
                announcePaired(res.target);
                return res.target || null;
            } finally {
                _linuxPairSink = null;
            }
        },
        async addLinuxAuto(sink) {
            const answer = await openLinuxDialog();
            if (!answer) return null;
            _linuxPairSink = sink || null;
            try {
                await runLinuxPair(answer, false);
            } finally {
                _linuxPairSink = null;
            }
            return null;
        },
        // v6 Connect panel entry: create a fresh remote session on a target.
        // Wraps openRemoteSession so the new panel doesn't need to reach into
        // this module's DOM state — it just calls this with the target dict
        // and gets a mounted local tab back. Never closes the overlay.
        async newSession(target) {
            return openRemoteSession(target, {});
        },
        // v6 Connect panel entry: adopt an existing rc-* session as a local
        // tab (session chip ▶). If a tab is already bound to this
        // remote_session_id, focus it instead of mounting a duplicate — that
        // matches the "▶" semantic on the panel.
        async focusOrMount(target, session) {
            const remoteSid = session.session_id || session.id;
            if (!remoteSid) return null;
            // Look for a live local tab already bound to this remote id.
            // Defensive check on top of the notifyLocalTabClosed fix: even if
            // some other path leaves a stale remoteSessions entry, don't trust
            // it blindly — confirm the renderer still actually has that tab
            // before switching to it. A miss here falls through to
            // adoptSession, which is always correct (worst case: a harmless
            // extra tab bind), whereas trusting a dead sid produces the
            // "nothing happens" bug this comment is next to.
            for (const [localSid, info] of remoteSessions.entries()) {
                if (info.remoteSessionId === remoteSid) {
                    const stillExists = window.HandQRenderer
                        && window.HandQRenderer.hasSession
                        && window.HandQRenderer.hasSession(localSid);
                    if (stillExists) {
                        window.HandQRenderer.switchSession(localSid);
                        return localSid;
                    }
                    // Stale — drop it and fall through to re-adopt.
                    remoteSessions.delete(localSid);
                    break;
                }
            }
            return adoptSession(target, session);
        },
    };
}());
