/**
 * session-sidebar.js — right-hand session detail panel (Stage-Manager
 * redesign). Stacked top to bottom inside one frosted panel:
 *   1. Plan bar     — collapsed summary of the coordinator's task-plan
 *      queue (+ the agent's own current sub-step, when present).
 *   2. Activity feed — flat chronological log of decisions/tool calls/
 *      results for the focused session (formerly rendered inline between
 *      chat bubbles; moved here so the chat pane stays pure conversation).
 *   3. File r/w record — a directory tree of every file the focused
 *      session has touched (read/edit), with per-file ↺ undo markers on
 *      edited files wired to the bridge's file_undo IPC.
 *
 * Real-time only — the panel has NO concept of past/future events. It
 * accumulates as events stream in from the bridge (kind=file_touch,
 * kind=task_plan, kind=agent_todo, plus activity entries pushed directly
 * by renderer.js's onStatus dispatcher).
 *
 * Public API (consumed by renderer.js):
 *   SessionSidebar.init()
 *   SessionSidebar.setActiveSession(sid, name)
 *   SessionSidebar.setSessionName(sid, name)
 *   SessionSidebar.setTaskPlan(sid, items)              // {item_id, instruction, status}[]
 *   SessionSidebar.setAgentTodo(sid, todos)             // {content, status}[]
 *   SessionSidebar.ingestFileTouch(sid, {path, touch, tool, item_id})
 *   SessionSidebar.pushActivity(sid, {label, content, time, iter, tool, pending}) -> entry
 *   SessionSidebar.updateActivityResult(sid, iter, tool, resultIcon, headLabel, resultContent)
 *   SessionSidebar.forceFinalizePendingActivity(sid)    // marks stuck "pending" entries interrupted
 *   SessionSidebar.clearActivity(sid)
 *   SessionSidebar.notifySessionClosed(sid)
 *   SessionSidebar.onUndoRequest(callback)              // (sid, itemId) => void
 */
(function () {
  'use strict';

  // ── Constants ─────────────────────────────────────────────────────────
  const TOUCH_RANK = { hit: 1, read: 2, edit: 3 };

  // Monotonic touch counter — stamped onto each file on every touch so the
  // Files tree can sort "most-recently-touched first" (LRU-style) without
  // relying on Date.now() (which ties when several files are touched in the
  // same millisecond). Global across sessions is fine: it's only ever used
  // for relative ordering within one session's file set.
  let _touchSeq = 0;

  // ── UI element handles (populated in init) ────────────────────────────
  const dom = {};
  const undoListeners = [];

  // ── Per-session state ─────────────────────────────────────────────────
  // sid -> {
  //   name: str,
  //   filesByPath: Map<path, {kind, visits, firstSeenTs, count, isNew, lastItemId, lastTouchSeq}>,
  //   filesByItem: Map<itemId, {order: string[], rows: Map<path, {kind, count, lastKind, isNew}>}>
  //   itemFirstSeen: Map<itemId, ts>,   // for item-block sort
  //   taskItems: Array<{id, instruction, status}>,
  //   agentTodoItems: Array<{content, status}>,
  //   activityItems: Array<{label, content, time, iter, tool, pending, resultContent}>,
  //   lastEvent: null | {path, touch, tool, itemId},
  //   selectedPath: string | null,
  //   collapsedDirs: Set<dirKey>,        // folders the user collapsed in the tree
  //   pendingUndoPath: string | null,    // file whose undo-confirm strip is open
  // }
  const bySid = new Map();
  let activeSid = null;
  // Sidebar visibility follows this state machine:
  //   * Default (fresh session, no activity) — collapsed, rail hidden of intent.
  //   * First file_touch or task_plan for the active session — auto-expand,
  //     UNLESS the user has manually collapsed it in this session (below).
  //   * User clicks the ‹ button while expanded — set userDismissed=true,
  //     stay collapsed even as more events arrive. A rail pulse hints new
  //     activity exists but the sidebar respects the user's choice.
  //   * User clicks the › button on the collapsed rail — clear userDismissed
  //     and expand again.
  let userDismissedCollapse = false;

  function _emptySessionState(name) {
    return {
      name: name || '',
      filesByPath: new Map(),
      filesByItem: new Map(),
      itemFirstSeen: new Map(),
      taskItems: [],
      itemStatus: new Map(),
      agentTodoItems: [],
      activityItems: [],
      lastEvent: null,
      selectedPath: null,
      // Directory-tree UI state (see _renderFilesList): set of collapsed
      // folder keys (default = all expanded, i.e. not in the set), and the
      // path whose per-file undo confirm strip is currently open (only one
      // at a time). Both survive re-renders since they live on the session.
      collapsedDirs: new Set(),
      pendingUndoPath: null,
    };
  }

  function _ensureState(sid) {
    if (!bySid.has(sid)) bySid.set(sid, _emptySessionState(''));
    return bySid.get(sid);
  }

  // ── Public API ────────────────────────────────────────────────────────
  window.SessionSidebar = {
    init: init,
    setActiveSession: function (sid, name) {
      const switching = sid && sid !== activeSid;
      activeSid = sid || null;
      if (sid) {
        const s = _ensureState(sid);
        if (name) s.name = name;
      }
      // Reset the "user closed the sidebar" flag on every session switch —
      // that gesture belongs to the session the user made it on, not to
      // every session opened afterward. Without this reset, once the user
      // collapses the sidebar in ANY session, EVERY subsequent session
      // inherits the "user said no" state and won't auto-expand on
      // activity — which reads as a broken auto-expand feature.
      if (switching) userDismissedCollapse = false;
      _renderName();
      _renderPlanBar();
      _renderActivityList();
      _renderFilesList();
      // Empty-session policy: an active session with NO files touched, NO
      // task plan, and NO activity log has nothing meaningful to show in
      // the sidebar — the empty "No activity yet" panel just wastes screen
      // real-estate on the compact 840px baseline window. So:
      //   * Switching to a session with content → try to auto-expand
      //     (respecting the user's manual-dismiss flag if set).
      //   * Switching to an empty session → auto-COLLAPSE the sidebar so
      //     the main stage claims the full width AND main.js can shrink
      //     the window back to the baseline. We do NOT touch
      //     userDismissedCollapse here: this collapse is reactive, not a
      //     user gesture. As soon as real activity arrives on this session,
      //     ingestFileTouch/pushActivity/recall's own _maybeAutoExpand
      //     call will bring the sidebar back automatically. And because
      //     _updateActivityBadge() gates .ss-reopen's data-visible on
      //     hasActivity, the floating reopen button also stays hidden
      //     while the session is empty — no orphan tab-strip artifact.
      const s = activeSid ? bySid.get(activeSid) : null;
      const hasActivity = !!(s && (s.filesByPath.size > 0
                                   || s.taskItems.length > 0
                                   || s.activityItems.length > 0));
      if (hasActivity) {
        _maybeAutoExpand();
      } else {
        _setCollapsed(true);
        _updateActivityBadge();
      }
    },
    setSessionName: function (sid, name) {
      if (!sid) return;
      const s = _ensureState(sid);
      s.name = name || '';
      if (sid === activeSid) _renderName();
    },
    setTaskPlan: function (sid, items) {
      if (!sid || !Array.isArray(items)) return;
      const s = _ensureState(sid);
      s.taskItems = items.map(it => ({
        id: it.item_id || it.id || '',
        instruction: it.instruction || '',
        status: it.status || 'pending',
      }));
      for (const it of s.taskItems) s.itemStatus.set(it.id, it.status);
      if (sid === activeSid) {
        _renderPlanBar();
        _renderFilesList();
        // A task plan appearing is a strong "session just started doing
        // something" signal — nudge the sidebar open if the user hasn't
        // explicitly said no.
        if (items.length > 0) _maybeAutoExpand();
      }
    },
    setAgentTodo: function (sid, todos) {
      if (!sid) return;
      const s = _ensureState(sid);
      s.agentTodoItems = Array.isArray(todos) ? todos.map(t => ({
        content: (t && t.content) || '',
        status: (t && t.status) || 'pending',
      })) : [];
      if (sid === activeSid) _renderPlanBar();
    },
    ingestFileTouch: function (sid, evt) {
      if (!sid || !evt || !evt.path || !evt.touch) return;
      _ingest(sid, evt);
      if (sid === activeSid) {
        _renderFilesList();
        _maybeAutoExpand();
      } else {
        // Event for another session — no visual update here, but next time
        // the user switches to that session we should nudge it open.
      }
    },
    // Appends one entry to sid's flat activity log and (if focused)
    // renders it. Returns the entry so callers (renderer.js) can later
    // pass it back through updateActivityResult by iter/tool matching, or
    // directly mutate the returned object to fold in a streamed result.
    // NOTE: callers still pass an `icon` field (legacy emoji glyphs
    // hardcoded in renderer.js) — it's accepted but deliberately dropped
    // here, never stored or rendered. Removed per explicit request: these
    // were decorative literals, not anything the backend sent.
    pushActivity: function (sid, entry) {
      if (!sid || !entry) return null;
      const s = _ensureState(sid);
      const record = {
        label: entry.label || '',
        content: entry.content == null ? '' : String(entry.content),
        time: entry.time || new Date().toLocaleTimeString([], { hour12: false }),
        iter: entry.iter != null ? String(entry.iter) : null,
        tool: entry.tool ? String(entry.tool) : null,
        pending: !!entry.pending,
        resultContent: null,
      };
      s.activityItems.push(record);
      if (sid === activeSid) {
        _renderActivityList();
        _maybeAutoExpand();
      }
      return record;
    },
    // Nudge the sidebar to auto-expand without pushing an activity entry.
    // For signals that shouldn't leave a row in the activity list — e.g.
    // recall_started, which has no matching recall_finished event, so a
    // pushed "recalling…" entry would linger forever with a misleading
    // placeholder as its content. This just wakes the panel up when the
    // agent starts doing something visible in the pill/chat; the FIRST
    // real activity entry (decision/tool/file) is what populates the list.
    nudgeExpand: function (sid) {
      if (!sid || sid !== activeSid) return;
      _maybeAutoExpand();
    },
    // Folds a tool's post-execution result into its matching pending entry
    // (newest-first scan by iter+tool, same matching rule the old chat-side
    // activity groups used) instead of appending a separate "done" line.
    updateActivityResult: function (sid, iter, tool, resultIcon, headLabel, resultContent) {
      if (!sid) return;
      const s = bySid.get(sid);
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
        window.SessionSidebar.pushActivity(sid, {
          label: (tool || 'tool') + ' done',
          content: resultContent || '',
        });
        return;
      }
      match.label = headLabel || match.tool || match.label;
      match.resultContent = resultContent == null ? '' : String(resultContent);
      match.pending = false;
      if (sid === activeSid) _renderActivityList();
    },
    // Defensive cleanup for activity entries left stuck "pending" (spinner
    // state) because the task ended abnormally (bridge crash, fatal error,
    // session closed) before the matching tool-result event arrived.
    forceFinalizePendingActivity: function (sid) {
      if (!sid) return;
      const s = bySid.get(sid);
      if (!s) return;
      for (const entry of s.activityItems) {
        if (!entry.pending) continue;
        entry.label = (entry.label || entry.tool || 'tool') + ' (interrupted)';
        entry.resultContent = '';
        entry.pending = false;
      }
      if (sid === activeSid) _renderActivityList();
    },
    clearActivity: function (sid) {
      if (!sid) return;
      const s = bySid.get(sid);
      if (!s) return;
      s.activityItems = [];
      if (sid === activeSid) _renderActivityList();
    },
    notifySessionClosed: function (sid) {
      if (!sid) return;
      bySid.delete(sid);
      if (sid === activeSid) {
        activeSid = null;
        _renderName();
        _renderPlanBar();
        _renderActivityList();
        _renderFilesList();
        _updateActivityBadge();
      }
    },
    onUndoRequest: function (cb) { if (typeof cb === 'function') undoListeners.push(cb); },

    // ── Debug helpers, exposed on window.SessionSidebar so you can
    //    diagnose UI plumbing from DevTools without needing the backend
    //    to actually fire an event. Meant for troubleshooting only.
    __debug: {
      /** Inject a fake file_touch event for the currently-active session.
       *  Bypasses the bridge entirely — the sidebar reacts exactly as
       *  it would to a real envelope. Useful for "is the flow broken at
       *  the Python end or at the JS end?" triage. */
      inject: function (payload) {
        if (!activeSid) {
          console.warn('[SessionSidebar] no active session; call'
            + ' setActiveSession(sid, name) first');
          return;
        }
        const evt = Object.assign({
          path: 'demo/hello.py',
          touch: 'read',
          tool: 'read',
          item_id: 'DEMO',
        }, payload || {});
        window.SessionSidebar.ingestFileTouch(activeSid, evt);
      },
      /** Dump per-session state to the console for inspection. */
      state: function (sid) {
        const s = bySid.get(sid || activeSid);
        if (!s) { console.log('(no state)'); return null; }
        return {
          name: s.name,
          files: [...s.filesByPath.entries()].map(([p, f]) =>
            ({ path: p, kind: f.kind, visits: f.visits })),
          byItem: [...s.filesByItem.entries()].map(([iid, b]) =>
            ({ item: iid, paths: b.order })),
          taskItems: s.taskItems,
          activityItems: s.activityItems,
          lastEvent: s.lastEvent,
        };
      },
    },
  };

  // ── Ingest one event: update state, no rendering ──────────────────────
  function _ingest(sid, evt) {
    const s = _ensureState(sid);
    const path = String(evt.path || '');
    const kind = String(evt.touch || '');
    const itemId = String(evt.item_id || '');
    if (!(kind in TOUCH_RANK)) return;

    let f = s.filesByPath.get(path);
    if (!f) {
      f = { kind: kind, visits: 0, firstSeenTs: Date.now(), count: 0, isNew: true, lastItemId: itemId || '', lastTouchSeq: 0 };
      s.filesByPath.set(path, f);
    } else {
      if (TOUCH_RANK[kind] > TOUCH_RANK[f.kind]) f.kind = kind;
      f.isNew = true;
    }
    f.visits += 1;
    f.count += 1;
    f.lastTouchSeq = ++_touchSeq;   // bump so the Files tree sorts this file to the front
    // Track the most recent task item that touched this file — the backend's
    // file_undo is per-item (rewind_item reverts every file captured under
    // one item_id), so the per-file ↺ marker in the tree undoes via THIS
    // item_id. It reverts every file that item touched, not just this one
    // file — the confirm strip spells that out before firing.
    if (itemId) f.lastItemId = itemId;

    // Group by item — kept for the per-file undo's "how many files does
    // this item's revert actually touch" count shown in the confirm strip.
    if (itemId) {
      let bucket = s.filesByItem.get(itemId);
      if (!bucket) {
        bucket = { order: [], rows: new Map() };
        s.filesByItem.set(itemId, bucket);
        s.itemFirstSeen.set(itemId, Date.now());
      }
      let row = bucket.rows.get(path);
      if (!row) {
        row = { kind: kind, count: 0, lastKind: kind, isNew: true };
        bucket.order.unshift(path);
        bucket.rows.set(path, row);
      } else {
        row.isNew = true;
      }
      row.count += 1;
      row.lastKind = kind;
      if (TOUCH_RANK[kind] > TOUCH_RANK[row.kind]) row.kind = kind;
    }

    s.lastEvent = { path: path, touch: kind, tool: String(evt.tool || ''), itemId: itemId };
  }

  // ── Initialization ────────────────────────────────────────────────────
  function init() {
    if (dom.host) return;    // idempotent
    const $ = id => document.getElementById(id);
    dom.host           = $('session-sidebar');
    if (!dom.host) return;   // sidebar isn't in the DOM (older layout)
    dom.name           = $('ss-session-name');
    dom.planBar        = $('ss-planbar');
    dom.activityList   = $('ss-activity-list');
    dom.activityCount  = $('ss-activity-count');
    dom.filesList      = $('ss-files-list');
    dom.filesCount     = $('ss-files-count');
    dom.widthHandle    = $('ss-width-handle');
    dom.collapse       = $('ss-collapse');
    dom.reopen         = $('ss-reopen');

    // Collapse toggle — flips the state machine. Clicking WHEN EXPANDED
    // marks it "user dismissed" so subsequent activity doesn't fight the
    // user; the floating reopen button is what brings it back later, and
    // clicking THAT clears the dismissed flag.
    dom.collapse.addEventListener('click', () => {
      userDismissedCollapse = true;
      _setCollapsed(true);
    });
    if (dom.reopen) {
      dom.reopen.addEventListener('click', () => {
        userDismissedCollapse = false;
        _setCollapsed(false);
      });
    }

    // Floor mirrors the CSS min-width contract on .session-sidebar
    // (max(200px, 45% of #chat-region's width)) — without reading it back
    // here too, a drag could pull the handle below the CSS floor and the
    // box would visually snap back up, fighting the user's drag gesture.
    _wireDrag(dom.widthHandle, 'ew', (dx, startVal) => {
      const floor = _sidebarMinWidth();
      const w = Math.max(floor, Math.min(480, startVal - dx));
      dom.host.style.width = w + 'px';
    }, () => dom.host.getBoundingClientRect().width);

    // Note: the old ResizeObserver on #chat-region that maintained a
    // --chat-region-w CSS variable + re-clamped the sidebar's inline width
    // was removed — it existed to feed the sidebar's ratio-based min-width
    // formula (max(200px, chat-region * 0.45)), which fought the "sidebar
    // is an additive delta on top of a fixed baseline" model introduced
    // with the layout-driven auto-resize. Sidebar's min-width is now a
    // fixed 220px (see styles.css), so no live-ratio bookkeeping is
    // needed. Drag handle still enforces the 220px floor via
    // _sidebarMinWidth below.

    _renderName();
    _renderPlanBar();
    _renderActivityList();
    _renderFilesList();
  }

  // Fixed 220px floor for the drag handle — matches .session-sidebar's
  // CSS min-width exactly. Previously computed from chat-region width to
  // preserve a ratio; that ratio-driven floor is gone (see the ResizeObserver
  // removal comment above).
  function _sidebarMinWidth() {
    return 220;
  }

  function _setCollapsed(collapsed) {
    // Guard against being called before SessionSidebar.init() has populated
    // dom.host — the boot sequence runs createSession() BEFORE init(), so
    // the very first setActiveSession → _setCollapsed(true) branch (added
    // for the empty-session auto-collapse) would otherwise throw on a
    // missing setAttribute target. Same guard shape as _maybeAutoExpand /
    // _updateActivityBadge below.
    if (!dom.host) return;
    const prev = dom.host.getAttribute('data-collapsed');
    const next = collapsed ? 'true' : 'false';
    dom.host.setAttribute('data-collapsed', next);
    _updateActivityBadge();
    // Notify the outer renderer's _updateLayout so the window can grow /
    // stay at baseline in step with sidebar visibility. Decoupled via a
    // DOM CustomEvent — no direct import needed. Only fires on ACTUAL state
    // changes so we don't spam the IPC channel when nothing moved.
    if (prev !== next) {
      try {
        window.dispatchEvent(new CustomEvent('session-sidebar-toggle', {
          detail: { collapsed: !!collapsed },
        }));
      } catch (_) { /* CustomEvent is universally supported; swallow if not */ }
    }
  }

  function _maybeAutoExpand() {
    // Auto-open the sidebar the moment the active session shows real
    // activity — unless the user explicitly collapsed it after that.
    // When the user IS holding it collapsed, flip the rail's activity
    // hint so they know something's happening in the box they closed.
    if (!dom.host) return;
    const collapsed = dom.host.getAttribute('data-collapsed') === 'true';
    if (collapsed && !userDismissedCollapse) {
      _setCollapsed(false);
    } else {
      _updateActivityBadge();
    }
  }

  function _updateActivityBadge() {
    if (!dom.host) return;
    const collapsed = dom.host.getAttribute('data-collapsed') === 'true';
    const s = activeSid ? bySid.get(activeSid) : null;
    const hasActivity = !!(s && (s.filesByPath.size > 0 || s.lastEvent || s.activityItems.length > 0));
    // The floating reopen button is the ONLY way the user can bring the
    // sidebar back after it's been collapsed (whether by their click or by
    // the "empty session → auto-collapse" branch in setActiveSession). So
    // it's visible whenever the sidebar is collapsed AND we have an active
    // session, regardless of whether that session has activity yet — an
    // empty session might get activity a second later, and hiding the
    // button until then leaves the user with no way in.
    //
    // The `.ss-reopen-dot` pulse ring inside the button separately keys
    // off hasActivity (via the `has-activity` class below) — that's the
    // "here's something you missed" hint, so the button itself is present
    // as a control but only pulses when there's genuinely new content.
    if (dom.reopen) {
      dom.reopen.setAttribute(
        'data-visible',
        collapsed && !!s ? 'true' : 'false',
      );
      dom.reopen.classList.toggle('has-activity', hasActivity);
    }
  }

  function _wireDrag(el, axis, onMove, getStartValue) {
    let active = null;
    el.addEventListener('pointerdown', (e) => {
      active = { startX: e.clientX, startY: e.clientY, startVal: getStartValue() };
      el.classList.add('dragging');
      el.setPointerCapture(e.pointerId);
    });
    el.addEventListener('pointermove', (e) => {
      if (!active) return;
      if (axis === 'ew') onMove(e.clientX - active.startX, active.startVal);
      else               onMove(e.clientY - active.startY, active.startVal);
    });
    const stop = (e) => {
      if (!active) return;
      active = null;
      el.classList.remove('dragging');
      try { el.releasePointerCapture(e.pointerId); } catch (_) {}
    };
    el.addEventListener('pointerup', stop);
    el.addEventListener('pointercancel', stop);
  }

  // ── HUD renderers ─────────────────────────────────────────────────────
  function _renderName() {
    if (!dom.name) return;
    const s = activeSid ? bySid.get(activeSid) : null;
    dom.name.textContent = s ? (s.name || activeSid.slice(0, 8)) : '';
  }

  const PLAN_GLYPH = {
    done: '✓', running: '▶', pending: '○', failed: '✗', interrupted: '⊗', skipped: '⊘',
  };
  const TODO_GLYPH = { completed: '✓', in_progress: '▶', pending: '☐' };

  // Full task-plan queue + (if present) the agent's own current sub-step.
  // Always fully expanded — no collapse/toggle — so the plan is readable
  // at a glance without an extra click.
  function _renderPlanBar() {
    if (!dom.planBar) return;
    const s = activeSid ? bySid.get(activeSid) : null;
    dom.planBar.innerHTML = '';
    if (!s || s.taskItems.length === 0) return;

    const items = s.taskItems;
    const doneCount = items.filter(it => it.status === 'done').length;
    const failedCount = items.filter(it => it.status === 'failed').length;

    const panel = document.createElement('div');
    panel.className = 'task-plan-panel';

    const header = document.createElement('div');
    header.className = 'task-plan-header';
    let summary = 'Plan · ' + doneCount + '/' + items.length + ' done';
    if (failedCount) summary += ' · ' + failedCount + ' failed';
    header.innerHTML = '<span class="tp-summary">' + _esc(summary) + '</span>';
    panel.appendChild(header);

    const list = document.createElement('div');
    list.className = 'task-plan-items';
    for (const it of items) {
      const row = document.createElement('div');
      row.className = 'task-plan-item tp-' + it.status;
      row.innerHTML =
        '<span class="tp-glyph">' + (PLAN_GLYPH[it.status] || '·') + '</span>' +
        '<span class="tp-text">' + _esc(it.instruction) + '</span>';
      list.appendChild(row);
    }
    panel.appendChild(list);
    dom.planBar.appendChild(panel);

    if (s.agentTodoItems.length > 0) {
      const todoPanel = document.createElement('div');
      todoPanel.className = 'agent-todo-panel';
      const todoDone = s.agentTodoItems.filter(t => t.status === 'completed').length;
      const todoHeader = document.createElement('div');
      todoHeader.className = 'agent-todo-header';
      todoHeader.innerHTML =
        '<span class="at-summary">Agent todo · ' + todoDone + '/' + s.agentTodoItems.length + '</span>';
      todoPanel.appendChild(todoHeader);
      const todoList = document.createElement('div');
      todoList.className = 'agent-todo-items';
      for (const t of s.agentTodoItems) {
        const row = document.createElement('div');
        row.className = 'agent-todo-item at-' + t.status;
        row.innerHTML =
          '<span class="at-glyph">' + (TODO_GLYPH[t.status] || '·') + '</span>' +
          '<span class="at-text">' + _esc(t.content) + '</span>';
        todoList.appendChild(row);
      }
      todoPanel.appendChild(todoList);
      dom.planBar.appendChild(todoPanel);
    }
  }

  const ACTIVITY_TRUNC = 2000;

  function _truncate(s, n) {
    if (!s) return '';
    return s.length > n ? s.slice(0, n - 1) + '…' : s;
  }

  function _isJsonString(s) {
    if (!s || typeof s !== 'string') return false;
    const trimmed = s.trim();
    if ((trimmed[0] === '{' && trimmed[trimmed.length - 1] === '}') ||
        (trimmed[0] === '[' && trimmed[trimmed.length - 1] === ']')) {
      try { JSON.parse(trimmed); return true; }
      catch (_) { return false; }
    }
    return false;
  }

  function _renderJsonValue(value) {
    if (value === null) { const s = document.createElement('span'); s.className = 'ai-json-null'; s.textContent = 'null'; return s; }
    if (typeof value === 'boolean') { const s = document.createElement('span'); s.className = 'ai-json-bool'; s.textContent = String(value); return s; }
    if (typeof value === 'number') { const s = document.createElement('span'); s.className = 'ai-json-num'; s.textContent = String(value); return s; }
    if (typeof value === 'string') {
      const display = value.length > 120 ? value.slice(0, 117) + '…' : value;
      const s = document.createElement('span'); s.className = 'ai-json-str'; s.textContent = '"' + display + '"';
      return s;
    }
    if (Array.isArray(value)) {
      if (value.length === 0) { const s = document.createElement('span'); s.className = 'ai-json-bracket'; s.textContent = '[]'; return s; }
      const ul = document.createElement('ul'); ul.className = 'ai-json-tree';
      for (let i = 0; i < value.length; i++) {
        const li = document.createElement('li'); li.className = 'ai-json-entry';
        const idx = document.createElement('span'); idx.className = 'ai-json-key'; idx.textContent = '[' + i + '] ';
        li.appendChild(idx);
        li.appendChild(_renderJsonValue(value[i]));
        ul.appendChild(li);
      }
      return ul;
    }
    if (typeof value === 'object') {
      const keys = Object.keys(value);
      if (keys.length === 0) { const s = document.createElement('span'); s.className = 'ai-json-bracket'; s.textContent = '{}'; return s; }
      const ul = document.createElement('ul'); ul.className = 'ai-json-tree';
      for (const k of keys) {
        const li = document.createElement('li'); li.className = 'ai-json-entry';
        const keySpan = document.createElement('span'); keySpan.className = 'ai-json-key'; keySpan.textContent = k + ': ';
        li.appendChild(keySpan);
        li.appendChild(_renderJsonValue(value[k]));
        ul.appendChild(li);
      }
      return ul;
    }
    return document.createTextNode(String(value));
  }

  function _renderJsonContent(jsonStr) {
    try { return _renderJsonValue(JSON.parse(jsonStr.trim())); }
    catch (_) { return document.createTextNode(jsonStr); }
  }

  // Flat chronological activity log — no per-turn grouping (there's no
  // "next chat bubble seals the group" boundary here, since this panel
  // isn't interleaved with chat bubbles; see the file header comment).
  // Rendered NEWEST-FIRST (storage order stays chronological — oldest to
  // newest — since updateActivityResult's pending-match scan walks
  // s.activityItems from the end expecting that order; only the render
  // pass here reverses for display).
  function _renderActivityList() {
    if (!dom.activityList) return;
    const s = activeSid ? bySid.get(activeSid) : null;
    dom.activityList.innerHTML = '';
    if (!s || s.activityItems.length === 0) {
      dom.activityList.innerHTML = '<div class="ss-activity-empty">No activity yet.</div>';
      if (dom.activityCount) dom.activityCount.textContent = '';
      return;
    }
    for (let idx = s.activityItems.length - 1; idx >= 0; idx--) {
      const entry = s.activityItems[idx];
      const item = document.createElement('div');
      item.className = 'ss-activity-item';
      const head = document.createElement('div');
      head.className = 'aa-head';
      head.innerHTML =
        '<span class="aa-label">' + _esc(entry.label) + '</span>' +
        '<span class="aa-time">' + _esc(entry.time) + '</span>';
      item.appendChild(head);
      if (entry.content) {
        const contentIsJson = _isJsonString(entry.content);
        const content = document.createElement('span');
        content.className = 'aa-content' + (contentIsJson ? ' ai-json' : '');
        if (contentIsJson) content.appendChild(_renderJsonContent(entry.content));
        else content.textContent = _truncate(entry.content, ACTIVITY_TRUNC);
        item.appendChild(content);
        if (!contentIsJson) item.title = entry.content;
      }
      if (entry.resultContent) {
        const resultIsJson = _isJsonString(entry.resultContent);
        const result = document.createElement('div');
        result.className = 'aa-result' + (resultIsJson ? ' ai-json' : '');
        if (resultIsJson) {
          result.appendChild(document.createTextNode('↳ '));
          result.appendChild(_renderJsonContent(entry.resultContent));
        } else {
          result.textContent = '↳ ' + _truncate(entry.resultContent, ACTIVITY_TRUNC);
        }
        item.appendChild(result);
      }
      item.addEventListener('click', () => {
        item.classList.toggle('expanded');
        const c = item.querySelector('.aa-content');
        if (c && !c.classList.contains('ai-json')) {
          c.textContent = item.classList.contains('expanded')
            ? entry.content
            : _truncate(entry.content, ACTIVITY_TRUNC);
        }
        const r = item.querySelector('.aa-result');
        if (r && !r.classList.contains('ai-json')) {
          r.textContent = '↳ ' + (item.classList.contains('expanded')
            ? entry.resultContent
            : _truncate(entry.resultContent, ACTIVITY_TRUNC));
        }
      });
      dom.activityList.appendChild(item);
    }
    if (dom.activityCount) dom.activityCount.textContent = String(s.activityItems.length);
  }

  // ── Files: a single directory tree of everything the active session
  // touched. No task grouping (files reflect the whole session, not one
  // task item — confirmed with the backend's per-session RewindStore).
  // Folders collapse/expand; each file leaf shows a kind dot + touch count,
  // and edited files carry a ↺ marker that opens an inline confirm strip
  // (undo is destructive + per-item on the backend, so it asks first).

  // Split a path into segments on either separator. Drive letters / UNC
  // roots stay attached to the first meaningful segment so we don't render
  // a lone "C:" folder node.
  function _pathSegments(path) {
    const norm = String(path).replace(/\\/g, '/').replace(/\/+$/, '');
    const parts = norm.split('/').filter(Boolean);
    return parts.length ? parts : [norm];
  }

  // Middle-truncate a single long name to fit a narrow column: keep the
  // head and the tail (extensions/trailing digits carry meaning), drop the
  // middle. Used per-segment, not on the whole path — the tree already
  // splits the path across rows, this only catches one very long segment.
  function _midTruncate(name, max) {
    if (name.length <= max) return name;
    const keep = max - 1;
    const head = Math.ceil(keep * 0.6);
    const tail = keep - head;
    return name.slice(0, head) + '…' + name.slice(name.length - tail);
  }

  // Build a nested tree: { name, key, dirs: Map<name,node>, files: [{path,f}],
  // recent } where `recent` is the max lastTouchSeq anywhere in the subtree,
  // used to sort most-recently-active folders/files to the top (LRU-style).
  function _buildFileTree(s) {
    const root = { dirs: new Map(), files: [], recent: 0 };
    // Insert in ascending lastTouchSeq so that, at each level, unshift-free
    // push keeps insertion order; the actual most-recent-first ordering is
    // applied at render time via _sortTreeNode (which also propagates each
    // subtree's max seq up). Insertion order here doesn't matter for
    // correctness, only that every file lands in the right node.
    for (const [path, f] of s.filesByPath) {
      const segs = _pathSegments(path);
      let node = root;
      let key = '';
      root.recent = Math.max(root.recent, f.lastTouchSeq || 0);
      // All but the last segment are directories.
      for (let i = 0; i < segs.length - 1; i++) {
        const seg = segs[i];
        key = key ? key + '/' + seg : seg;
        let child = node.dirs.get(seg);
        if (!child) {
          child = { name: seg, key: key, dirs: new Map(), files: [], recent: 0 };
          node.dirs.set(seg, child);
        }
        child.recent = Math.max(child.recent, f.lastTouchSeq || 0);
        node = child;
      }
      node.files.push({ path: path, name: segs[segs.length - 1], f: f });
    }
    return root;
  }

  function _renderFilesList() {
    if (!dom.filesList) return;
    const s = activeSid ? bySid.get(activeSid) : null;
    dom.filesList.innerHTML = '';
    if (!s || s.filesByPath.size === 0) {
      dom.filesList.innerHTML = '<div class="ss-files-empty">No file activity yet.<br>Waiting for the agent…</div>';
      if (dom.filesCount) dom.filesCount.textContent = '';
      return;
    }

    const tree = _buildFileTree(s);
    let editedCount = 0;
    for (const f of s.filesByPath.values()) if (f.kind === 'edit') editedCount++;

    // Recursively render a directory's children into `container` at `depth`.
    // Ordering at each level is LRU-style: whatever the agent touched most
    // recently floats to the top — folders sorted by the max touch-seq
    // anywhere inside them, files by their own touch-seq, both descending
    // (newest first), with a case-insensitive name tiebreaker so equal-seq
    // siblings don't jitter between renders. Folders still come before
    // files at each level. A folder with a single child folder and no files
    // is collapsed into "a/b/c" so deep lone chains don't waste rows.
    function renderDir(node, container, depth) {
      const dirs = [...node.dirs.values()].sort((a, b) =>
        (b.recent - a.recent) || a.name.toLowerCase().localeCompare(b.name.toLowerCase()));
      for (let dir of dirs) {
        // Collapse lone-child directory chains (a → b → c with no files at
        // the intermediate levels) into one "a/b/c" row.
        let label = dir.name;
        while (dir.files.length === 0 && dir.dirs.size === 1) {
          const only = [...dir.dirs.values()][0];
          label = label + '/' + only.name;
          dir = only;
        }
        const collapsed = s.collapsedDirs.has(dir.key);
        const row = document.createElement('div');
        row.className = 'ss-tree-dir';
        // Tighter indent step (8px per depth, base 2) plus a per-depth
        // vertical guide rail painted by CSS (see .ss-file-row rules).
        // The old 12px×depth + 6 base padded rows so far to the right at
        // depth 3+ that the file names looked stranded in whitespace.
        row.style.paddingLeft = (depth * 8 + 2) + 'px';
        row.style.setProperty('--tree-depth', String(depth));
        row.innerHTML =
          '<span class="ss-tree-twist">' + (collapsed ? '▸' : '▾') + '</span>' +
          '<span class="ss-tree-folder" title="' + _esc(dir.key) + '">' +
          _esc(_midTruncate(label, 34)) + '</span>';
        row.addEventListener('click', () => {
          if (s.collapsedDirs.has(dir.key)) s.collapsedDirs.delete(dir.key);
          else s.collapsedDirs.add(dir.key);
          _renderFilesList();
        });
        container.appendChild(row);
        if (!collapsed) renderDir(dir, container, depth + 1);
      }
      const files = node.files.slice().sort((a, b) =>
        ((b.f.lastTouchSeq || 0) - (a.f.lastTouchSeq || 0)) ||
        a.name.toLowerCase().localeCompare(b.name.toLowerCase()));
      for (const leaf of files) {
        container.appendChild(renderFileLeaf(leaf, depth));
      }
    }

    function renderFileLeaf(leaf, depth) {
      const f = leaf.f;
      const path = leaf.path;
      const wrap = document.createElement('div');
      wrap.className = 'ss-tree-leaf-wrap';
      const isCurrent = s.lastEvent && s.lastEvent.path === path;
      const el = document.createElement('div');
      el.className = 'ss-file-row ' + f.kind + (f.isNew ? ' new-flash' : '') +
                     (isCurrent || s.selectedPath === path ? ' active' : '');
      // Tighter indent — matches the folder rule. depth * 8 + 2 leaves the
      // kind-dot flush against the deepest guide rail without whitespace.
      el.style.paddingLeft = (depth * 8 + 2) + 'px';
      el.style.setProperty('--tree-depth', String(depth));
      const undoMark = f.kind === 'edit'
        ? '<button class="ss-file-undo" type="button" title="Undo edits from this file’s task" aria-label="Undo">↺</button>'
        : '';
      el.innerHTML =
        '<span class="ss-kind-dot ' + f.kind + '"></span>' +
        '<span class="fp" title="' + _esc(path) + '">' + _esc(_midTruncate(leaf.name, 30)) + '</span>' +
        '<span class="ct">' + f.count + '×</span>' +
        undoMark;
      el.addEventListener('click', (ev) => {
        // The ↺ button has its own handler (below); a click anywhere else
        // on the row just toggles selection. Double-click (see below)
        // reveals the file in the system file explorer via IPC — the
        // single-click stays a pure UI toggle so double-clicks don't leave
        // orphan selection state behind after the reveal fires.
        if (ev.target.closest('.ss-file-undo')) return;
        if (ev.detail >= 2) return;
        s.selectedPath = s.selectedPath === path ? null : path;
        s.pendingUndoPath = null;
        _renderFilesList();
      });
      el.addEventListener('dblclick', (ev) => {
        if (ev.target.closest('.ss-file-undo')) return;
        // Reveal in Explorer / Finder. Non-fatal if the bridge is absent
        // (older builds, sandboxed dev sessions) — just silently no-op.
        try {
          const api = window.handq;
          if (api && typeof api.revealFile === 'function') {
            api.revealFile(path);
          }
        } catch (_) { /* swallow */ }
      });
      el.title = _esc(path) + '  (double-click to reveal)';
      const undoBtn = el.querySelector('.ss-file-undo');
      if (undoBtn) {
        undoBtn.addEventListener('click', (ev) => {
          ev.stopPropagation();
          s.pendingUndoPath = s.pendingUndoPath === path ? null : path;
          _renderFilesList();
        });
      }
      f.isNew = false;
      wrap.appendChild(el);

      // Inline confirm strip — only for the file whose ↺ was clicked. Undo
      // is per-item on the backend: reverting THIS file's task reverts every
      // file that task touched, so the strip states the real blast radius.
      if (s.pendingUndoPath === path && f.kind === 'edit') {
        const iid = f.lastItemId;
        const bucket = iid ? s.filesByItem.get(iid) : null;
        let affected = 1;
        if (bucket) {
          affected = 0;
          for (const r of bucket.rows.values()) if (r.kind === 'edit') affected++;
          if (affected === 0) affected = 1;
        }
        const confirm = document.createElement('div');
        confirm.className = 'ss-undo-confirm';
        confirm.style.marginLeft = (depth * 8 + 2) + 'px';
        confirm.innerHTML =
          '<span class="uc-msg">Undo reverts ' + affected +
          ' file' + (affected > 1 ? 's' : '') + ' from this task.</span>' +
          '<button class="uc-yes" type="button">Undo</button>' +
          '<button class="uc-no" type="button">Cancel</button>';
        confirm.querySelector('.uc-yes').addEventListener('click', (ev) => {
          ev.stopPropagation();
          s.pendingUndoPath = null;
          for (const cb of undoListeners) {
            try { cb(activeSid, iid); } catch (_) {}
          }
          _renderFilesList();
        });
        confirm.querySelector('.uc-no').addEventListener('click', (ev) => {
          ev.stopPropagation();
          s.pendingUndoPath = null;
          _renderFilesList();
        });
        wrap.appendChild(confirm);
      }
      return wrap;
    }

    renderDir(tree, dom.filesList, 0);

    if (dom.filesCount) {
      const n = s.filesByPath.size;
      dom.filesCount.textContent = n + ' file' + (n !== 1 ? 's' : '') +
        (editedCount ? ' · ' + editedCount + ' edited' : '');
    }
  }

  function _esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

})();
