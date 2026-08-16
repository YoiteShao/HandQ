// Desktop takeover overlay controller.
//
// Extracted from main.js so the session-id plumbing can be unit-tested without
// booting the whole Electron app. main.js owns a single instance of this
// controller; all Electron surfaces (BrowserWindow, globalShortcut) and the
// bridge writer / logger are injected so tests can supply fakes.
//
// IPC contract (see docs/desktop_tool.md §11):
//   * Backend emits {type:"status", kind:"desktop_takeover_started",
//     reason:"input_action", session_id:"..."} when the agent performs a
//     desktop input action. We show a fullscreen rainbow-border overlay and
//     register Ctrl+Shift+C as a process-wide revoke hotkey.
//   * On {type:"status", kind:"desktop_takeover_ended", session_id:"..."} we
//     hide the overlay and free the hotkey — but only if session_id matches
//     the session we're currently bound to (see "latest-wins" below).
//
// The revoke hotkey sends {type:"user_input", kind:"desktop_takeover_revoked",
// session_id}. The session_id is MANDATORY: the bridge's _resolve_session_id
// rejects any session-scoped user_input without one ("user_input: missing
// session_id") and the envelope would never reach the desktop_takeover_revoked
// handler.
//
// Latest-wins session binding: docs/desktop_tool.md §11.2 requires
// started/ended to be treated as level-triggered (latest-wins). Desktop
// input is a single OS-level exclusive resource, so at most one overlay
// window ever exists — but WHICH session's revoke the hotkey targets must
// track whichever session most recently sent `started`, even if that
// second `started` arrives while the first session's overlay/hotkey is
// still up (show() no-ops on the window but still rebinds the session). A
// bare `hide(sessionId)` for a session that is no longer the current one is
// a stale/superseded event and must NOT tear down the newer session's
// overlay — only an unqualified hide() (no sessionId — bridge exit, app
// quit) forces teardown unconditionally.

'use strict';

const TAKEOVER_REVOKE_ACCELERATOR = 'Control+Shift+C';

/**
 * Build a takeover-overlay controller bound to the given Electron surfaces.
 *
 * @param {object} deps
 * @param {Function} deps.BrowserWindow   Electron BrowserWindow constructor.
 * @param {object}   deps.globalShortcut  Electron globalShortcut module.
 * @param {Function} deps.writeToBridge   Sends a JSON envelope to the bridge.
 * @param {Function} [deps.logLine]       (component, msg, extra) logger.
 * @param {string}   deps.overlayHtmlPath Absolute path to overlay.html.
 * @param {string}   [deps.accelerator]   Revoke accelerator (test override).
 * @returns {{show: Function, hide: Function, accelerator: string,
 *            isVisible: Function}}
 */
function createTakeoverOverlay(deps) {
    const {
        BrowserWindow,
        globalShortcut,
        writeToBridge,
        overlayHtmlPath,
    } = deps;
    const logLine = deps.logLine || function () {};
    const accelerator = deps.accelerator || TAKEOVER_REVOKE_ACCELERATOR;

    // Module-level singleton in the original main.js — kept here as closure
    // state so main.js holds exactly one overlay window at a time.
    let takeoverOverlay = null;
    // Which session the visible overlay (and its revoke hotkey) currently
    // act on. Rebound on every show(), even when the window itself isn't
    // recreated — see the "latest-wins" note in the module header.
    let currentSessionId = null;

    function show(sessionId) {
        const rebind = currentSessionId !== sessionId;
        currentSessionId = sessionId;
        if (takeoverOverlay && !takeoverOverlay.isDestroyed()) {
            // Backend's _start_takeover is idempotent but a duplicate event
            // could still arrive on edge cases. Don't double-create the
            // window — but do log a rebind so a later hotkey-misroute is
            // traceable.
            if (rebind) {
                logLine('OVERLAY', 'takeover overlay rebound to new session',
                        { session_id: sessionId });
            }
            return;
        }
        logLine('OVERLAY', 'show takeover overlay', { session_id: sessionId });

        try {
            takeoverOverlay = new BrowserWindow({
                frame: false,
                transparent: true,
                alwaysOnTop: true,
                focusable: false,
                skipTaskbar: true,
                fullscreen: true,
                hasShadow: false,
                resizable: false,
                movable: false,
                minimizable: false,
                maximizable: false,
                closable: false,
                backgroundColor: '#00000000',
                // Overlay is purely presentational — no preload, no IPC.
                webPreferences: {
                    contextIsolation: true,
                    sandbox: true,
                    nodeIntegration: false,
                },
            });
        } catch (err) {
            logLine('OVERLAY', 'create BrowserWindow failed',
                    { err: err && err.message });
            takeoverOverlay = null;
            return;
        }

        // Forward mouse events so the agent's clicks reach the underlying app.
        try {
            takeoverOverlay.setIgnoreMouseEvents(true, { forward: true });
        } catch (err) {
            logLine('OVERLAY', 'setIgnoreMouseEvents failed',
                    { err: err && err.message });
        }
        // "screen-saver" beats most fullscreen apps; fall back silently if the
        // platform rejects the level.
        try {
            takeoverOverlay.setAlwaysOnTop(true, 'screen-saver');
        } catch (_) { /* ignore */ }

        Promise.resolve(takeoverOverlay.loadFile(overlayHtmlPath))
            .catch((err) => {
                logLine('OVERLAY', 'loadFile failed', { err: err && err.message });
            });

        // Hold off on setting visibleOnAllWorkspaces; default behaviour follows
        // the user's active virtual desktop, which is what we want.

        takeoverOverlay.on('closed', () => {
            takeoverOverlay = null;
        });

        // Register the revoke hotkey. Registration can fail if another app
        // already owns the combo — log it but continue showing the overlay so
        // the user still sees the indicator.
        try {
            const ok = globalShortcut.register(accelerator, () => {
                logLine('OVERLAY', 'revoke hotkey fired',
                        { session_id: currentSessionId });
                // session_id is mandatory — see module header. Read the
                // live binding, not the sessionId this closure captured at
                // registration time, so a later rebind (show() for a new
                // session while the window/hotkey stay up) is honoured.
                writeToBridge({
                    type: 'user_input',
                    kind: 'desktop_takeover_revoked',
                    session_id: currentSessionId,
                });
            });
            if (!ok) {
                logLine('OVERLAY', 'revoke hotkey register returned false',
                        { accelerator });
            }
        } catch (err) {
            logLine('OVERLAY', 'revoke hotkey register error',
                    { err: err && err.message });
        }
    }

    function hide(sessionId) {
        if (sessionId !== undefined && sessionId !== currentSessionId) {
            // Stale/superseded `ended` — a more recent show() has already
            // rebound the overlay to a different session. That session is
            // still using the overlay; ignore this one rather than tearing
            // it down out from under it.
            logLine('OVERLAY', 'ignoring stale hide for superseded session', {
                session_id: sessionId,
                current_session_id: currentSessionId,
            });
            return;
        }
        // Always free the shortcut even if the window object is already gone.
        try {
            if (globalShortcut.isRegistered(accelerator)) {
                globalShortcut.unregister(accelerator);
            }
        } catch (err) {
            logLine('OVERLAY', 'revoke hotkey unregister error',
                    { err: err && err.message });
        }
        currentSessionId = null;
        if (!takeoverOverlay || takeoverOverlay.isDestroyed()) {
            takeoverOverlay = null;
            return;
        }
        logLine('OVERLAY', 'hide takeover overlay');
        try {
            takeoverOverlay.destroy();
        } catch (err) {
            logLine('OVERLAY', 'destroy failed', { err: err && err.message });
        }
        takeoverOverlay = null;
    }

    function isVisible() {
        return !!(takeoverOverlay && !takeoverOverlay.isDestroyed());
    }

    return { show, hide, accelerator, isVisible };
}

module.exports = { createTakeoverOverlay, TAKEOVER_REVOKE_ACCELERATOR };
