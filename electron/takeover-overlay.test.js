// Unit tests for the desktop takeover overlay controller.
//
// Runner: Node's built-in test module (no extra deps).
//   node --test electron/takeover-overlay.test.js
//
// Regression focus: the revoke hotkey envelope MUST carry the session_id that
// was supplied on the desktop_takeover_started event. Without it the bridge's
// _resolve_session_id rejects the user_input with "user_input: missing
// session_id" and the takeover is never actually revoked. See the bug fixed
// alongside these tests.

'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const { createTakeoverOverlay } = require('./takeover-overlay');

// --- fakes ------------------------------------------------------------------

// Minimal stand-in for an Electron BrowserWindow. Records the calls the
// controller makes and lets a test flip the destroyed flag.
class FakeWindow {
    constructor(opts) {
        this.opts = opts;
        this.destroyed = false;
        this.listeners = {};
        this.ignoreMouse = null;
        this.alwaysOnTopLevel = null;
        this.loadedFile = null;
    }
    isDestroyed() { return this.destroyed; }
    setIgnoreMouseEvents(ignore, opts) { this.ignoreMouse = { ignore, opts }; }
    setAlwaysOnTop(_flag, level) { this.alwaysOnTopLevel = level; }
    loadFile(p) { this.loadedFile = p; return Promise.resolve(); }
    on(event, cb) { this.listeners[event] = cb; }
    destroy() { this.destroyed = true; if (this.listeners.closed) this.listeners.closed(); }
}

// Fake globalShortcut that captures the registered revoke callback so tests
// can "press" the hotkey by invoking it directly.
function makeFakeShortcut(opts = {}) {
    const state = { registered: new Map(), registerCalls: 0, unregisterCalls: 0 };
    return {
        state,
        register(accel, cb) {
            state.registerCalls += 1;
            if (opts.registerReturns === false) return false;
            if (opts.registerThrows) throw new Error('register boom');
            state.registered.set(accel, cb);
            return true;
        },
        isRegistered(accel) { return state.registered.has(accel); },
        unregister(accel) { state.unregisterCalls += 1; state.registered.delete(accel); },
        // Test helper: simulate the user pressing the accelerator.
        fire(accel) {
            const cb = state.registered.get(accel);
            if (!cb) throw new Error('accelerator not registered: ' + accel);
            cb();
        },
    };
}

// Builds a controller wired to fakes, returning the controller plus captured
// state (created windows, the shortcut fake, and every bridge envelope sent).
function harness(overrides = {}) {
    const windows = [];
    const sent = [];
    const BrowserWindow = overrides.BrowserWindow || function (opts) {
        const w = new FakeWindow(opts);
        windows.push(w);
        return w;
    };
    const globalShortcut = overrides.globalShortcut || makeFakeShortcut();
    const writeToBridge = (obj) => { sent.push(obj); };
    const controller = createTakeoverOverlay({
        BrowserWindow,
        globalShortcut,
        writeToBridge,
        logLine: () => {},
        overlayHtmlPath: 'C:/fake/overlay.html',
        ...overrides.controller,
    });
    return { controller, windows, sent, globalShortcut };
}

// --- tests ------------------------------------------------------------------

test('revoke envelope carries the session_id from the started event', () => {
    const { controller, sent, globalShortcut } = harness();

    controller.show('sess-abc123');
    globalShortcut.fire(controller.accelerator);

    assert.equal(sent.length, 1);
    assert.deepEqual(sent[0], {
        type: 'user_input',
        kind: 'desktop_takeover_revoked',
        session_id: 'sess-abc123',
    });
});

test('revoke session_id is never dropped (regression: missing session_id)', () => {
    const { controller, sent, globalShortcut } = harness();

    controller.show('sid-999');
    globalShortcut.fire(controller.accelerator);

    // The exact failure this guards: an envelope with no/undefined session_id
    // would be rejected by the bridge's _resolve_session_id.
    assert.ok('session_id' in sent[0], 'envelope must include session_id key');
    assert.equal(sent[0].session_id, 'sid-999');
    assert.notEqual(sent[0].session_id, undefined);
});

test('each takeover binds its own session_id (hide/show cycle)', () => {
    const { controller, sent, globalShortcut } = harness();

    controller.show('first-session');
    globalShortcut.fire(controller.accelerator);
    controller.hide();

    controller.show('second-session');
    globalShortcut.fire(controller.accelerator);

    assert.equal(sent.length, 2);
    assert.equal(sent[0].session_id, 'first-session');
    assert.equal(sent[1].session_id, 'second-session');
});

test('duplicate started event is idempotent — no second window, sid unchanged', () => {
    const { controller, windows, sent, globalShortcut } = harness();

    controller.show('sid-1');
    // A duplicate desktop_takeover_started arrives before any hide.
    controller.show('sid-2-should-be-ignored');

    assert.equal(windows.length, 1, 'must not create a second overlay window');
    globalShortcut.fire(controller.accelerator);
    // The first registration wins; the second show() is a no-op so the hotkey
    // callback still references the original session_id.
    assert.equal(sent[0].session_id, 'sid-1');
});

test('show() configures the overlay window as a passthrough fullscreen layer', () => {
    const { controller, windows } = harness();

    controller.show('sid');
    const w = windows[0];

    assert.equal(w.opts.transparent, true);
    assert.equal(w.opts.fullscreen, true);
    assert.equal(w.opts.frame, false);
    assert.deepEqual(w.ignoreMouse, { ignore: true, opts: { forward: true } });
    assert.equal(w.alwaysOnTopLevel, 'screen-saver');
    assert.equal(w.loadedFile, 'C:/fake/overlay.html');
    assert.ok(controller.isVisible());
});

test('hide() destroys the window and frees the accelerator', () => {
    const { controller, windows, globalShortcut } = harness();

    controller.show('sid');
    assert.ok(globalShortcut.isRegistered(controller.accelerator));

    controller.hide();

    assert.ok(windows[0].isDestroyed());
    assert.equal(globalShortcut.isRegistered(controller.accelerator), false);
    assert.equal(controller.isVisible(), false);
});

test('window closed by the OS resets state so the next show() recreates it', () => {
    const { controller, windows } = harness();

    controller.show('sid-1');
    // Simulate the window being closed externally (fires the 'closed' handler).
    windows[0].listeners.closed();
    assert.equal(controller.isVisible(), false);

    controller.show('sid-2');
    assert.equal(windows.length, 2, 'a fresh window is created after external close');
});

test('failed hotkey registration still shows the overlay (no throw)', () => {
    const globalShortcut = makeFakeShortcut({ registerReturns: false });
    const { controller } = harness({ globalShortcut });

    assert.doesNotThrow(() => controller.show('sid'));
    assert.ok(controller.isVisible(),
        'overlay must remain visible even if the hotkey could not be bound');
});

test('BrowserWindow construction failure leaves no dangling overlay', () => {
    const BrowserWindow = function () { throw new Error('no display'); };
    const { controller, sent } = harness({ BrowserWindow });

    assert.doesNotThrow(() => controller.show('sid'));
    assert.equal(controller.isVisible(), false);
    assert.equal(sent.length, 0);
});

test('hide() before any show() is a safe no-op', () => {
    const { controller } = harness();
    assert.doesNotThrow(() => controller.hide());
    assert.equal(controller.isVisible(), false);
});
