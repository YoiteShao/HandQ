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
//   * On {type:"status", kind:"desktop_takeover_ended", ...} we hide the
//     overlay and free the hotkey.
//
// The revoke hotkey sends {type:"user_input", kind:"desktop_takeover_revoked",
// session_id}. The session_id is MANDATORY: the bridge's _resolve_session_id
// rejects any session-scoped user_input without one ("user_input: missing
// session_id") and the envelope would never reach the desktop_takeover_revoked
// handler. We capture it from the started event that triggered the overlay.

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

    function show(sessionId) {
        if (takeoverOverlay && !takeoverOverlay.isDestroyed()) {
            // Backend's _start_takeover is idempotent but a duplicate event
            // could still arrive on edge cases. Don't double-create.
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
                        { session_id: sessionId });
                // session_id is mandatory — see module header.
                writeToBridge({
                    type: 'user_input',
                    kind: 'desktop_takeover_revoked',
                    session_id: sessionId,
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

    function hide() {
        // Always free the shortcut even if the window object is already gone.
        try {
            if (globalShortcut.isRegistered(accelerator)) {
                globalShortcut.unregister(accelerator);
            }
        } catch (err) {
            logLine('OVERLAY', 'revoke hotkey unregister error',
                    { err: err && err.message });
        }
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
