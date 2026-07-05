/* ============================================================
 * admin-panel.js — LTM admin overlay controller.
 *
 * Activated by typing /memory (any case) in the composer. The
 * composer-submit handler in renderer.js peeks at the leading
 * token and, if it matches, calls window.adminPanel.open() and
 * suppresses the bridge dispatch.
 *
 * All operations talk to the bridge via the existing IPC layer
 * (handq.sendRequest + handq.onFinal / handq.onError). We wrap
 * those callbacks into a tiny request/response RPC so each tab
 * can `await ipcCall(type, params)` and render results.
 *
 * Conscious choices:
 *   - Single overlay, single bridge instance — never spawn a
 *     second window. Multiple bridges → multiple DreamWorkers
 *     contending on memory.db, which is the bug we're avoiding.
 *   - Inline edit is intentionally NOT exposed; the only way to
 *     "fix" an entry is archive + let the next session
 *     re-trigger triage.
 *   - Hard delete is intentionally NOT exposed; archive is the
 *     soft-delete path, version history preserved.
 * ============================================================ */
(function () {
    'use strict';

    // ── Tiny RPC layer ──────────────────────────────────────────
    // The bridge replies with a `final` envelope carrying the
    // same `id` we sent. We multiplex: one map of pending Promise
    // resolvers keyed by id; onFinal / onError look it up.
    const pending = new Map();          // id → {resolve, reject}
    let nextId = 1;

    function rpc(type, payload) {
        const id = `admin-${type}-${nextId++}-${Date.now()}`;
        return new Promise((resolve, reject) => {
            pending.set(id, { resolve, reject });
            // 30s wall-clock timeout so a wedged bridge never leaves
            // the UI hanging silently.
            const tmr = setTimeout(() => {
                if (pending.has(id)) {
                    pending.delete(id);
                    reject(new Error(`${type} timed out after 30s`));
                }
            }, 30000);
            pending.get(id)._tmr = tmr;
            try {
                // Merge envelope fields LAST so a caller's payload can never
                // shadow `type`/`id` — clobbering `id` strands the response
                // (bridge replies under the wrong correlation key, RPC times out).
                window.handq.sendRequest(Object.assign({}, payload || {}, { type, id }));
            } catch (err) {
                clearTimeout(tmr);
                pending.delete(id);
                reject(err);
            }
        });
    }

    function _settleRpc(evt, isError) {
        if (!evt || !evt.id) return false;
        const p = pending.get(evt.id);
        if (!p) return false;
        pending.delete(evt.id);
        if (p._tmr) clearTimeout(p._tmr);
        if (isError) {
            p.reject(new Error(evt.message || 'bridge error'));
        } else {
            p.resolve(evt.result);
        }
        return true;
    }

    // ── DOM ─────────────────────────────────────────────────────
    const overlay = document.getElementById('overlay-admin');
    const closeBtn = document.getElementById('admin-close');
    const tabs = Array.from(document.querySelectorAll('.admin-tab'));
    const panes = Array.from(document.querySelectorAll('.admin-pane'));
    const toast = document.getElementById('admin-toast');

    function showToast(msg, kind) {
        toast.textContent = msg;
        toast.classList.remove('hidden', 'error');
        if (kind === 'error') toast.classList.add('error');
        clearTimeout(showToast._tmr);
        showToast._tmr = setTimeout(() => toast.classList.add('hidden'), 3500);
    }

    // ── Tab switching ───────────────────────────────────────────
    function activateTab(name) {
        tabs.forEach((t) => {
            const on = t.dataset.adminTab === name;
            t.classList.toggle('active', on);
            t.setAttribute('aria-selected', on ? 'true' : 'false');
        });
        panes.forEach((p) => {
            p.classList.toggle('hidden', p.dataset.adminPane !== name);
        });
        // First-load on each tab.
        if (name === 'memory') refreshMemory();
        if (name === 'knowledge') refreshKnowledge();
        if (name === 'stats') refreshStats();
        if (name === 'activity') refreshActivity();
    }

    tabs.forEach((t) => t.addEventListener('click', () => activateTab(t.dataset.adminTab)));

    // ── Open / close ────────────────────────────────────────────
    function open() {
        overlay.classList.remove('hidden');
        overlay.setAttribute('aria-hidden', 'false');
        activateTab('memory');
    }
    function close() {
        overlay.classList.add('hidden');
        overlay.setAttribute('aria-hidden', 'true');
    }
    closeBtn.addEventListener('click', close);
    document.addEventListener('keydown', (e) => {
        if (!overlay.classList.contains('hidden') && e.key === 'Escape') close();
    });

    // ── Memory tab ──────────────────────────────────────────────
    const memDim = document.getElementById('admin-mem-dim');
    const memRefresh = document.getElementById('admin-mem-refresh');
    const memList = document.getElementById('admin-mem-list');
    const memCount = document.getElementById('admin-mem-count');

    async function refreshMemory() {
        const dim = memDim.value || null;
        const params = { limit: 100 };
        if (dim) params.dimension = dim;
        try {
            const r = await rpc('ltm_list_memory', params);
            renderEntryList(memList, r.entries || [], 'memory');
            memCount.textContent = `${(r.entries || []).length} entries`;
        } catch (err) {
            showToast('list memory failed: ' + err.message, 'error');
        }
    }
    memRefresh.addEventListener('click', refreshMemory);
    memDim.addEventListener('change', refreshMemory);

    // ── Knowledge tab ───────────────────────────────────────────
    const knCat = document.getElementById('admin-kn-cat');
    const knRefresh = document.getElementById('admin-kn-refresh');
    const knList = document.getElementById('admin-kn-list');
    const knCount = document.getElementById('admin-kn-count');

    async function refreshKnowledge() {
        const cat = knCat.value || null;
        const params = { limit: 100 };
        if (cat) params.category = cat;
        try {
            const r = await rpc('ltm_list_knowledge', params);
            renderEntryList(knList, r.entries || [], 'knowledge');
            knCount.textContent = `${(r.entries || []).length} entries`;
        } catch (err) {
            showToast('list knowledge failed: ' + err.message, 'error');
        }
    }
    knRefresh.addEventListener('click', refreshKnowledge);
    knCat.addEventListener('change', refreshKnowledge);

    function renderEntryList(host, entries, kind) {
        host.innerHTML = '';
        if (!entries.length) {
            const li = document.createElement('li');
            li.textContent = '(no entries)';
            li.style.opacity = '0.5';
            host.appendChild(li);
            return;
        }
        for (const e of entries) {
            const li = document.createElement('li');

            const main = document.createElement('div');
            const summary = document.createElement('div');
            summary.className = 'admin-entry-summary';
            summary.textContent = e.summary || '(no summary)';
            main.appendChild(summary);

            const meta = document.createElement('div');
            meta.className = 'admin-entry-meta';
            const facet = e.dimension || e.category || '?';
            const ts = e.updated_at
                ? new Date(e.updated_at * 1000).toISOString().slice(0, 19).replace('T', ' ')
                : '';
            meta.textContent = `${facet} · v${e.version} · src=${e.source || '?'} · ${ts}`;
            main.appendChild(meta);

            if (e.content) {
                const content = document.createElement('pre');
                content.className = 'admin-entry-content';
                content.textContent = e.content;
                main.appendChild(content);
            }
            li.appendChild(main);

            const actions = document.createElement('div');
            actions.className = 'admin-entry-actions';
            const archiveBtn = document.createElement('button');
            archiveBtn.type = 'button';
            archiveBtn.textContent = 'Archive';
            archiveBtn.addEventListener('click', () => archiveEntry(e.id, kind));
            actions.appendChild(archiveBtn);

            li.appendChild(actions);
            host.appendChild(li);
        }
    }

    async function archiveEntry(entryId, kind) {
        if (!confirm(`Archive this ${kind} entry?\nIt will stop being injected; version history is kept.`)) {
            return;
        }
        try {
            await rpc('ltm_archive', { entry_id: entryId, kind: kind, reason: 'user_request' });
            showToast('Archived.');
            if (kind === 'memory') refreshMemory(); else refreshKnowledge();
        } catch (err) {
            showToast('archive failed: ' + err.message, 'error');
        }
    }

    // ── Stats tab ───────────────────────────────────────────────
    const statsRefresh = document.getElementById('admin-stats-refresh');
    const statsOutput = document.getElementById('admin-stats-output');

    async function refreshStats() {
        try {
            const r = await rpc('ltm_stats', {});
            statsOutput.textContent = JSON.stringify(r, null, 2);
        } catch (err) {
            showToast('stats failed: ' + err.message, 'error');
        }
    }
    statsRefresh.addEventListener('click', refreshStats);

    // ── Schedules overlay (separate /schedules command) ─────────
    //
    // Functionally independent from /memory — the scheduler doesn't
    // read or write memory.db. We host its UI in its own overlay so
    // users who only need recurring-prompt management don't have to
    // see (or know about) the LTM admin surface, and vice versa.
    // RPC infrastructure is shared via the closure above.
    const schedOverlay = document.getElementById('overlay-schedules');
    const schedCloseBtn = document.getElementById('sched-close');
    const schedToast = document.getElementById('sched-toast');
    const schedList = document.getElementById('sched-list');
    const schedDetailEmpty = document.getElementById('sched-detail-empty');
    const schedDetail = document.getElementById('sched-detail');
    const schedDetailName = document.getElementById('sched-detail-name');
    const schedDetailSchedule = document.getElementById('sched-detail-schedule');
    const schedDetailStatus = document.getElementById('sched-detail-status');
    const schedDetailNext = document.getElementById('sched-detail-next');
    const schedDetailLast = document.getElementById('sched-detail-last');
    const schedDetailRuns = document.getElementById('sched-detail-runs');
    const schedDetailFailures = document.getElementById('sched-detail-failures');
    const schedDetailError = document.getElementById('sched-detail-error');
    const schedDetailPrompt = document.getElementById('sched-detail-prompt');
    const schedDetailRun = document.getElementById('sched-detail-run');
    const schedDetailToggle = document.getElementById('sched-detail-toggle');
    const schedCreateBtn = document.getElementById('sched-create-btn');
    const schedDeleteBtn = document.getElementById('sched-delete-btn');

    // Modal: separate overlay so clicking the FAB layers a small dialog
    // over the schedules panel, and Cancel/OK only closes the modal —
    // the schedules panel stays open underneath.
    const schedFormOverlay = document.getElementById('overlay-sched-form');
    const schedFormCloseBtn = document.getElementById('sched-form-close');
    const schedFormInner = document.getElementById('sched-form-inner');
    const schedName = document.getElementById('sched-name');
    const schedPrompt = document.getElementById('sched-prompt');
    const schedCreate = document.getElementById('sched-create');
    const schedCancel = document.getElementById('sched-cancel');

    // Selected task state. The list re-renders on every refresh; this
    // id lets us re-bind the detail pane to the same task across
    // refreshes (e.g. status updates from a background fire).
    let selectedTaskId = null;
    let allTasks = [];                             // last cron_list snapshot
    let schedPollTimer = null;                     // 5s background polling

    function showSchedToast(msg, kind) {
        schedToast.textContent = msg;
        schedToast.classList.remove('hidden', 'error');
        if (kind === 'error') schedToast.classList.add('error');
        clearTimeout(showSchedToast._tmr);
        showSchedToast._tmr = setTimeout(
            () => schedToast.classList.add('hidden'), 3500,
        );
    }

    function openSchedules() {
        schedOverlay.classList.remove('hidden');
        schedOverlay.setAttribute('aria-hidden', 'false');
        refreshSchedules();
        // Background poll while the panel is open so users see status
        // changes (PENDING / RUNNING / OK) without manual refresh.
        if (schedPollTimer) clearInterval(schedPollTimer);
        schedPollTimer = setInterval(refreshSchedules, 5000);
    }
    function closeSchedules() {
        schedOverlay.classList.add('hidden');
        schedOverlay.setAttribute('aria-hidden', 'true');
        if (schedPollTimer) {
            clearInterval(schedPollTimer);
            schedPollTimer = null;
        }
        // Also close the form modal if it happens to be open — keeps
        // state coherent so reopening the panel doesn't surprise the
        // user with a stray form.
        closeSchedForm();
    }
    schedCloseBtn.addEventListener('click', closeSchedules);
    document.addEventListener('keydown', (e) => {
        if (!schedOverlay.classList.contains('hidden') && e.key === 'Escape') {
            // If the form modal is open, Esc closes only that.
            if (!schedFormOverlay.classList.contains('hidden')) {
                closeSchedForm();
            } else {
                closeSchedules();
            }
        }
    });

    async function refreshSchedules() {
        try {
            const r = await rpc('cron_list', {});
            allTasks = r.tasks || [];
            renderScheduleList(allTasks);
            renderSelectedDetail();
        } catch (err) {
            // Don't toast every poll failure — only if the panel is
            // active and the user might be waiting for an action.
            if (!schedPollTimer) {
                showSchedToast('cron list failed: ' + err.message, 'error');
            }
        }
    }

    function mapSchedStatus(status) {
        switch (status) {
            case 'pending':   return { label: 'queued (waiting)', color: '#c8a200' };
            case 'cancelled': return { label: 'cancelled by user', color: '#888' };
            case 'running':   return { label: 'running', color: '#0a84ff' };
            case 'ok':        return { label: 'ok', color: '#2e7d3a' };
            case 'failed':    return { label: 'failed', color: '#c82828' };
            default:          return { label: status || 'idle', color: '' };
        }
    }

    function fmtTime(epochSec) {
        if (!epochSec) return '—';
        return new Date(epochSec * 1000)
            .toISOString().slice(0, 19).replace('T', ' ');
    }

    // Schedule strings starting with "once" are one-shot. After they
    // fire the store flips `enabled` to false — but "(disabled)" reads
    // like the user disabled it, while really the task ran and is done.
    // Render "(completed)" / "(failed)" instead so users aren't
    // confused about whether to re-enable.
    function isOneShotSchedule(schedule) {
        return typeof schedule === 'string'
            && schedule.trim().toLowerCase().startsWith('once');
    }
    function nameSuffix(t) {
        if (t.enabled) return '';
        if (isOneShotSchedule(t.schedule)) {
            if (t.last_status === 'ok') return ' (completed)';
            if (t.last_status === 'failed') return ' (failed)';
        }
        return ' (disabled)';
    }

    function renderScheduleList(tasks) {
        schedList.innerHTML = '';
        if (!tasks.length) {
            const li = document.createElement('li');
            li.textContent = '(no scheduled tasks — click + to create one)';
            li.className = 'muted';
            li.style.padding = '12px';
            li.style.textAlign = 'center';
            schedList.appendChild(li);
            return;
        }
        for (const t of tasks) {
            const li = document.createElement('li');
            li.className = 'sched-list-item';
            if (t.id === selectedTaskId) li.classList.add('selected');
            // Compact one-row layout: name • schedule • status badge.
            const head = document.createElement('div');
            head.className = 'sched-list-row';
            const nameSpan = document.createElement('span');
            nameSpan.className = 'sched-list-name';
            nameSpan.textContent = t.name + nameSuffix(t);
            const scheduleSpan = document.createElement('span');
            scheduleSpan.className = 'sched-list-schedule';
            scheduleSpan.textContent = t.schedule;
            const statusInfo = mapSchedStatus(t.last_status);
            const statusSpan = document.createElement('span');
            statusSpan.className = 'sched-list-status';
            statusSpan.textContent = statusInfo.label;
            if (statusInfo.color) statusSpan.style.color = statusInfo.color;
            head.appendChild(nameSpan);
            head.appendChild(scheduleSpan);
            head.appendChild(statusSpan);
            li.appendChild(head);
            li.addEventListener('click', () => selectTask(t.id));
            schedList.appendChild(li);
        }
    }

    function selectTask(id) {
        selectedTaskId = id;
        renderScheduleList(allTasks);
        renderSelectedDetail();
    }

    function renderSelectedDetail() {
        const t = allTasks.find(x => x.id === selectedTaskId);
        if (!t) {
            selectedTaskId = null;
            schedDetail.classList.add('hidden');
            schedDetailEmpty.classList.remove('hidden');
            schedDeleteBtn.disabled = true;
            return;
        }
        schedDeleteBtn.disabled = false;
        schedDetailEmpty.classList.add('hidden');
        schedDetail.classList.remove('hidden');
        schedDetailName.textContent = t.name + nameSuffix(t);
        schedDetailSchedule.textContent = t.schedule;
        const statusInfo = mapSchedStatus(t.last_status);
        schedDetailStatus.textContent = statusInfo.label;
        schedDetailStatus.style.color = statusInfo.color || '';
        schedDetailNext.textContent = fmtTime(t.next_run_at);
        schedDetailLast.textContent = fmtTime(t.last_run_at);
        schedDetailRuns.textContent = String(t.run_count);
        schedDetailFailures.textContent = String(t.failure_count);
        const errRows = document.querySelectorAll('.sched-detail-error-row');
        if (t.last_error) {
            schedDetailError.textContent = t.last_error;
            errRows.forEach(el => el.classList.remove('hidden'));
        } else {
            errRows.forEach(el => el.classList.add('hidden'));
        }
        schedDetailPrompt.textContent = t.prompt;
        schedDetailToggle.textContent = t.enabled ? 'Disable' : 'Enable';
    }

    schedDetailRun.addEventListener('click', () => {
        if (selectedTaskId) runSchedNow(selectedTaskId);
    });
    schedDetailToggle.addEventListener('click', () => {
        const t = allTasks.find(x => x.id === selectedTaskId);
        if (!t) return;
        setSchedEnabled(t.id, !t.enabled);
    });
    schedDeleteBtn.addEventListener('click', () => {
        if (!selectedTaskId) return;
        deleteSched(selectedTaskId);
    });

    // Action buttons stay enabled while the RPC is in flight by
    // default — but the bridge can take a few hundred ms (and longer
    // if it's mid-flow), during which the row visibly didn't change.
    // We use this helper to grey out the trio so the user sees that
    // their click registered and isn't tempted to click again.
    function setSchedActionsBusy(busy) {
        const disabled = !!busy;
        schedDetailRun.disabled = disabled;
        schedDetailToggle.disabled = disabled;
        // Delete button is also disabled-when-no-selection; only flip
        // it if a selection exists, so we don't accidentally enable it
        // during a busy state with no selected task.
        if (selectedTaskId) schedDeleteBtn.disabled = disabled;
    }

    async function runSchedNow(id) {
        setSchedActionsBusy(true);
        // Optimistic: flip the local row to running so the user sees
        // their click land before the next cron_list refresh.
        const local = allTasks.find(x => x.id === id);
        if (local) {
            local.last_status = 'running';
            renderScheduleList(allTasks);
            renderSelectedDetail();
        }
        try {
            await rpc('cron_run_now', { task_id: id });
            showSchedToast('triggered');
        } catch (err) {
            showSchedToast('run failed: ' + err.message, 'error');
        } finally {
            setSchedActionsBusy(false);
            // Pull truth — restores correct state if the optimistic
            // flip was wrong (e.g. bridge was busy and refused).
            refreshSchedules();
        }
    }
    async function setSchedEnabled(id, enabled) {
        setSchedActionsBusy(true);
        // Optimistic flip so the toggle button label and "(disabled)"
        // suffix update immediately, not after the round-trip.
        const local = allTasks.find(x => x.id === id);
        const previousEnabled = local ? local.enabled : null;
        if (local) {
            local.enabled = enabled;
            renderScheduleList(allTasks);
            renderSelectedDetail();
        }
        try {
            await rpc('cron_set_enabled', { task_id: id, enabled });
            showSchedToast(enabled ? 'enabled' : 'disabled');
        } catch (err) {
            // Rollback the optimistic flip — refreshSchedules below
            // will overwrite anyway, but rolling back synchronously
            // keeps the UI consistent if the refresh is slow too.
            if (local && previousEnabled !== null) {
                local.enabled = previousEnabled;
                renderScheduleList(allTasks);
                renderSelectedDetail();
            }
            showSchedToast('toggle failed: ' + err.message, 'error');
        } finally {
            setSchedActionsBusy(false);
            refreshSchedules();
        }
    }
    async function deleteSched(id) {
        if (!confirm('Delete this scheduled task?')) return;
        setSchedActionsBusy(true);
        // Optimistic remove. If the RPC fails, refreshSchedules below
        // re-pulls and the row reappears.
        const previousTasks = allTasks;
        allTasks = allTasks.filter(x => x.id !== id);
        if (id === selectedTaskId) selectedTaskId = null;
        renderScheduleList(allTasks);
        renderSelectedDetail();
        try {
            await rpc('cron_delete', { task_id: id });
            showSchedToast('deleted');
        } catch (err) {
            // Rollback so the row reappears immediately.
            allTasks = previousTasks;
            renderScheduleList(allTasks);
            renderSelectedDetail();
            showSchedToast('delete failed: ' + err.message, 'error');
        } finally {
            setSchedActionsBusy(false);
            refreshSchedules();
        }
    }

    // ── Create-task modal ──────────────────────────────────────────
    function openSchedForm() {
        schedName.value = '';
        schedPrompt.value = '';
        schedFormOverlay.classList.remove('hidden');
        schedFormOverlay.setAttribute('aria-hidden', 'false');
        schedName.focus();
    }
    function closeSchedForm() {
        schedFormOverlay.classList.add('hidden');
        schedFormOverlay.setAttribute('aria-hidden', 'true');
        // Restore submit button label in case it was left as "Inferring…"
        schedCreate.textContent = 'Create';
        schedCreate.disabled = false;
    }
    schedCreateBtn.addEventListener('click', openSchedForm);
    schedFormCloseBtn.addEventListener('click', closeSchedForm);
    schedCancel.addEventListener('click', closeSchedForm);

    // Form submit — hits the bridge. Server-side will use an LLM to
    // infer the schedule string from the prompt, so the UI doesn't
    // need a schedule field. Submit is async; show a "Inferring…"
    // label while we wait.
    async function submitSchedForm() {
        const name = schedName.value.trim();
        const prompt = schedPrompt.value.trim();
        if (!name || !prompt) {
            showSchedToast('name and prompt both required', 'error');
            return;
        }
        schedCreate.disabled = true;
        schedCreate.textContent = 'Inferring schedule…';
        try {
            const r = await rpc('cron_create', { name, prompt });
            if (r.ok) {
                closeSchedForm();
                const sched = (r.task && r.task.schedule) || '?';
                showSchedToast(`Created with schedule: ${sched}`);
                if (r.task && r.task.id) selectedTaskId = r.task.id;
                refreshSchedules();
            } else {
                showSchedToast(
                    'create failed: ' + (r.error || 'unknown'), 'error',
                );
            }
        } catch (err) {
            showSchedToast('create failed: ' + err.message, 'error');
        } finally {
            schedCreate.textContent = 'Create';
            schedCreate.disabled = false;
        }
    }
    schedFormInner.addEventListener('submit', (e) => {
        e.preventDefault();
        submitSchedForm();
    });

    // ── Activity tab ────────────────────────────────────────────
    const actRefresh = document.getElementById('admin-act-refresh');
    const actPause = document.getElementById('admin-act-pause');
    const actResume = document.getElementById('admin-act-resume');
    const actOutput = document.getElementById('admin-act-output');

    async function refreshActivity() { _act('personality_status'); }
    actRefresh.addEventListener('click', refreshActivity);
    actPause.addEventListener('click', () => _act('personality_pause'));
    actResume.addEventListener('click', () => _act('personality_resume'));

    async function _act(type) {
        try {
            const r = await rpc(type, {});
            actOutput.textContent = JSON.stringify(r, null, 2);
        } catch (err) {
            showToast(type + ' failed: ' + err.message, 'error');
        }
    }

    // ── Tools tab ───────────────────────────────────────────────
    const rememberText = document.getElementById('admin-remember-text');
    const rememberBtn = document.getElementById('admin-remember-btn');

    rememberBtn.addEventListener('click', async () => {
        const text = rememberText.value.trim();
        if (!text) { showToast('text required', 'error'); return; }
        try {
            const r = await rpc('ltm_remember', { text });
            if (r.ok) {
                showToast('Submitted. Triage will run on next dream cycle (≤ 60s).');
                rememberText.value = '';
            } else {
                showToast('submit failed: ' + (r.error || ''), 'error');
            }
        } catch (err) {
            showToast('submit failed: ' + err.message, 'error');
        }
    });

    // ── Skills overlay (separate menu entry) ────────────────────
    //
    // The single hub for the skill lifecycle. Lists installed skills as
    // cards — enable/disable, edit, delete — including auto-generated ones
    // (direct-written disabled by triage). "Import" pulls in an external
    // SKILL.md via a native file dialog. There is no approval queue. RPC
    // layer is shared via the closure above.
    const skOverlay = document.getElementById('overlay-skills');
    const skCloseBtn = document.getElementById('skills-close');
    const skToast = document.getElementById('skills-toast');
    const skGrid = document.getElementById('skills-grid');
    const skCount = document.getElementById('skills-count');
    const skCreateBtn = document.getElementById('skills-create-btn');
    const skImportBtn = document.getElementById('skills-import-btn');
    const skRefreshBtn = document.getElementById('skills-refresh-btn');
    // Create / edit modal.
    const skFormOverlay = document.getElementById('overlay-skill-form');
    const skFormCloseBtn = document.getElementById('skill-form-close');
    const skFormInner = document.getElementById('skill-form-inner');
    const skFormTitle = document.getElementById('skill-form-title');
    const skFormName = document.getElementById('skill-form-name');
    const skFormDesc = document.getElementById('skill-form-desc');
    const skFormBody = document.getElementById('skill-form-body');
    const skFormStanding = document.getElementById('skill-form-standing');
    const skFormCancel = document.getElementById('skill-form-cancel');
    const skFormSave = document.getElementById('skill-form-save');

    let allSkills = [];
    // null → create mode; a name string → editing that skill.
    let skEditingName = null;

    function showSkillToast(msg, kind) {
        skToast.textContent = msg;
        skToast.classList.remove('hidden', 'error');
        if (kind === 'error') skToast.classList.add('error');
        clearTimeout(showSkillToast._tmr);
        showSkillToast._tmr = setTimeout(() => skToast.classList.add('hidden'), 3500);
    }

    function openSkills() {
        skOverlay.classList.remove('hidden');
        skOverlay.setAttribute('aria-hidden', 'false');
        refreshSkillPanel();
    }
    function closeSkills() {
        skOverlay.classList.add('hidden');
        skOverlay.setAttribute('aria-hidden', 'true');
        closeSkillForm();
    }
    skCloseBtn.addEventListener('click', closeSkills);
    skRefreshBtn.addEventListener('click', refreshSkillPanel);
    document.addEventListener('keydown', (e) => {
        if (!skOverlay.classList.contains('hidden') && e.key === 'Escape') {
            if (!skFormOverlay.classList.contains('hidden')) {
                closeSkillForm();
            } else {
                closeSkills();
            }
        }
    });

    async function refreshSkillPanel() {
        try {
            const r = await rpc('skill_list', {});
            allSkills = (r && r.skills) || [];
            renderSkillCards(allSkills);
        } catch (err) {
            showSkillToast('load skills failed: ' + err.message, 'error');
        }
    }

    function renderSkillCards(skills) {
        skGrid.innerHTML = '';
        skCount.textContent = skills.length
            ? `${skills.length} installed`
            : '';
        if (!skills.length) {
            const empty = document.createElement('div');
            empty.className = 'muted skills-empty';
            empty.textContent = '(no skills installed — click + to create one)';
            skGrid.appendChild(empty);
            return;
        }
        for (const s of skills) {
            const card = document.createElement('div');
            card.className = 'skill-card';
            if (!s.enabled) card.classList.add('disabled');
            if (s.standing) card.classList.add('standing');

            const head = document.createElement('div');
            head.className = 'skill-card-head';
            const name = document.createElement('span');
            name.className = 'skill-card-name';
            name.textContent = s.name;
            head.appendChild(name);

            if (s.standing) {
                const badge = document.createElement('span');
                badge.className = 'skill-card-badge';
                badge.textContent = 'always on';
                head.appendChild(badge);
            }

            // Auto-origin badge: surfaces skills the miner minted from a
            // recurring task (disabled until the user reviews + enables them).
            // Editing such a skill in the panel claims ownership (origin→user),
            // after which the miner will never overwrite it.
            if (s.origin === 'auto') {
                const abadge = document.createElement('span');
                abadge.className = 'skill-card-badge auto';
                abadge.textContent = s.enabled ? 'auto' : 'auto · review';
                abadge.title = 'Auto-generated by the skill miner from a recurring '
                    + 'task. Review it, then flip Enabled to activate. Editing it '
                    + 'here claims ownership so it won’t be overwritten later.';
                head.appendChild(abadge);
            }

            const toggle = document.createElement('label');
            toggle.className = 'skill-toggle';
            const cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.checked = !!s.enabled;
            cb.addEventListener('change', () => toggleSkill(s.name, cb.checked, cb));
            const tlabel = document.createElement('span');
            tlabel.textContent = 'Enabled';
            toggle.appendChild(cb);
            toggle.appendChild(tlabel);
            head.appendChild(toggle);

            const standingToggle = document.createElement('label');
            standingToggle.className = 'skill-toggle';
            const scb = document.createElement('input');
            scb.type = 'checkbox';
            scb.checked = !!s.standing;
            scb.addEventListener('change', () => toggleStanding(s.name, scb.checked, scb));
            const slabel = document.createElement('span');
            slabel.textContent = 'Standing';
            standingToggle.appendChild(scb);
            standingToggle.appendChild(slabel);
            head.appendChild(standingToggle);
            card.appendChild(head);

            const desc = document.createElement('div');
            desc.className = 'skill-card-desc';
            desc.textContent = s.description || '(no description)';
            card.appendChild(desc);

            const problems = Array.isArray(s.problems) ? s.problems : [];
            for (const p of problems) {
                const warn = document.createElement('div');
                warn.className = 'skill-card-problem';
                warn.textContent = '⚠ ' + p;
                card.appendChild(warn);
            }

            const actions = document.createElement('div');
            actions.className = 'skill-card-actions';
            const editBtn = document.createElement('button');
            editBtn.type = 'button';
            editBtn.textContent = 'Edit';
            editBtn.addEventListener('click', () => openSkillForm(s.name));
            const delBtn = document.createElement('button');
            delBtn.type = 'button';
            delBtn.className = 'danger';
            delBtn.textContent = 'Delete';
            delBtn.addEventListener('click', () => deleteSkillEntry(s.name));
            actions.appendChild(editBtn);
            actions.appendChild(delBtn);
            card.appendChild(actions);

            skGrid.appendChild(card);
        }
    }

    async function toggleSkill(name, enabled, cb) {
        try {
            const r = await rpc('skill_set_enabled', { name, enabled });
            if (!r || !r.ok) throw new Error((r && (r.reason || r.error)) || 'unknown');
            showSkillToast(enabled ? `${name} enabled` : `${name} disabled`);
            const local = allSkills.find(x => x.name === name);
            if (local) {
                // "standing implies enabled": disabling a standing skill clears
                // standing backend-side. Mirror the authoritative result so
                // both checkboxes stay consistent.
                local.enabled = r.enabled;
                local.standing = r.standing;
            }
            renderSkillCards(allSkills);
        } catch (err) {
            if (cb) cb.checked = !enabled;   // roll back the optimistic flip
            showSkillToast('toggle failed: ' + err.message, 'error');
        }
    }

    async function toggleStanding(name, standing, cb) {
        try {
            const r = await rpc('skill_set_standing', { name, standing });
            if (!r || !r.ok) throw new Error((r && (r.reason || r.error)) || 'unknown');
            showSkillToast(standing ? `${name} set standing` : `${name} standing off`);
            const local = allSkills.find(x => x.name === name);
            if (local) {
                // "standing implies enabled": turning standing ON force-enables
                // the skill backend-side. Mirror the authoritative result so the
                // Enabled checkbox flips on too — the standing+disabled combo is
                // simply not representable.
                local.standing = r.standing;
                local.enabled = r.enabled;
            }
            renderSkillCards(allSkills);   // reflect the enforced enabled+standing
        } catch (err) {
            if (cb) cb.checked = !standing;   // roll back the optimistic flip
            showSkillToast('standing toggle failed: ' + err.message, 'error');
        }
    }

    async function deleteSkillEntry(name) {
        if (!confirm(`Delete skill "${name}"?\nIts SKILL.md directory is removed from disk.`)) {
            return;
        }
        try {
            const r = await rpc('skill_delete', { name });
            if (!r || !r.ok) throw new Error((r && (r.reason || r.error)) || 'unknown');
            showSkillToast(`${name} deleted`);
            refreshSkillPanel();
        } catch (err) {
            showSkillToast('delete failed: ' + err.message, 'error');
        }
    }

    function openSkillForm(name) {
        skEditingName = name || null;
        if (skEditingName) {
            const s = allSkills.find(x => x.name === skEditingName) || {};
            skFormTitle.textContent = 'Edit skill';
            skFormName.value = s.name || '';
            skFormDesc.value = s.description || '';
            skFormBody.value = s.body || '';
            if (skFormStanding) skFormStanding.checked = !!s.standing;
        } else {
            skFormTitle.textContent = 'New skill';
            skFormName.value = '';
            skFormDesc.value = '';
            skFormBody.value = '';
            if (skFormStanding) skFormStanding.checked = false;
        }
        skFormOverlay.classList.remove('hidden');
        skFormOverlay.setAttribute('aria-hidden', 'false');
        skFormName.focus();
    }
    function closeSkillForm() {
        skFormOverlay.classList.add('hidden');
        skFormOverlay.setAttribute('aria-hidden', 'true');
        skFormSave.textContent = 'Save';
        skFormSave.disabled = false;
    }
    skCreateBtn.addEventListener('click', () => openSkillForm(null));
    skFormCloseBtn.addEventListener('click', closeSkillForm);
    skFormCancel.addEventListener('click', closeSkillForm);

    async function submitSkillForm() {
        const name = skFormName.value.trim();
        const description = skFormDesc.value.trim();
        const body = skFormBody.value;
        const standing = !!(skFormStanding && skFormStanding.checked);
        if (!name || !description) {
            showSkillToast('name and description both required', 'error');
            return;
        }
        skFormSave.disabled = true;
        skFormSave.textContent = 'Saving…';
        try {
            let r;
            if (skEditingName) {
                r = await rpc('skill_update', {
                    name: skEditingName, new_name: name, description, body, standing,
                });
            } else {
                r = await rpc('skill_create', { name, description, body, standing });
            }
            if (!r || !r.ok) throw new Error((r && (r.reason || r.error)) || 'unknown');
            showSkillToast(skEditingName ? `${name} updated` : `${name} created`);
            closeSkillForm();
            refreshSkillPanel();
        } catch (err) {
            showSkillToast('save failed: ' + err.message, 'error');
        } finally {
            skFormSave.textContent = 'Save';
            skFormSave.disabled = false;
        }
    }
    skFormInner.addEventListener('submit', (e) => {
        e.preventDefault();
        submitSkillForm();
    });

    async function importSkillFromFile() {
        if (!window.handqDialog || !window.handqDialog.pickSkillFile) {
            showSkillToast('file dialog unavailable', 'error');
            return;
        }
        let picked;
        try {
            picked = await window.handqDialog.pickSkillFile();
        } catch (err) {
            showSkillToast('import failed: ' + err.message, 'error');
            return;
        }
        if (!picked || picked.canceled || !picked.path) return;
        try {
            const r = await rpc('skill_import', { path: picked.path });
            if (!r || !r.ok) throw new Error((r && (r.reason || r.error)) || 'unknown');
            showSkillToast(`${r.name} imported`);
            refreshSkillPanel();
        } catch (err) {
            showSkillToast('import failed: ' + err.message, 'error');
        }
    }
    skImportBtn.addEventListener('click', importSkillFromFile);

    // ── Bridge response routing ─────────────────────────────────
    // ``handq.onFinal`` / ``onError`` are fan-out subscribe APIs
    // (preload.js pushes onto a listener array), so we simply
    // register an extra listener that consumes envelopes whose id
    // begins with our ``admin-`` prefix. Other listeners (e.g. the
    // main renderer.js handler) still see those envelopes too,
    // but they ignore unknown ids.
    window.handq.onFinal((evt) => { _settleRpc(evt, false); });
    window.handq.onError((evt) => { _settleRpc(evt, true); });

    // ── Public surface ──────────────────────────────────────────
    window.adminPanel = {
        open,
        close,
        rpc,                              // exposed for ad-hoc debugging
        isOpen: () => !overlay.classList.contains('hidden'),
    };
    window.schedulePanel = {
        open: openSchedules,
        close: closeSchedules,
        isOpen: () => !schedOverlay.classList.contains('hidden'),
    };
    window.skillPanel = {
        open: openSkills,
        close: closeSkills,
        isOpen: () => !skOverlay.classList.contains('hidden'),
    };
})();
