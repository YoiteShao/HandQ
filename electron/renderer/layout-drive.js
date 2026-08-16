// ============================================================================
// layout-drive.js — hold the centre region's width constant while a side
// panel opens or closes.
//
// THE PROBLEM
//
// Toggling either side panel (the left stage rail, the right session sidebar)
// starts two animations at once:
//
//   1. main.js grows or shrinks the OS window by that panel's width, through
//      `setBounds(..., true)` — a Windows DWM animation of roughly 180ms
//      whose easing curve is neither exposed to us nor ours to change.
//   2. the panel itself changes width.
//
// `.main` lays out as
//
//       rail (+margin) | chat-region (flex:1) | sidebar (+margin)
//
// so chat_width = window_width - rail - sidebar - margins. chat-region is the
// flex:1 member, which means it absorbs the DIFFERENCE between those two
// animations on every single frame. Give the panel its own clock — a CSS
// transition, however carefully its duration and easing are matched to DWM's
// — and that residual is non-zero for the whole toggle: the centre card
// visibly bulges or pinches, then settles back to the exact width it started
// at. Measured before this module existed: about 113px of bulge opening a
// 512px sidebar, about 30px for the 160px rail. Both read to the user as "the
// centre card janks for no reason", which is fair — nothing about a
// side-panel toggle should move it at all.
//
// Matching the curves cannot fix this, and both panels had already tried.
// styles.css's rail rule carried a comment admitting its 200ms spring-soft
// only got "close enough that the chat card only drifts a few pixels"; the
// sidebar had been retuned several times to the same end. The durations
// differ, DWM's easing is not ours to set, and even an exact match would hold
// for only one window size at one refresh rate.
//
// THE FIX
//
// Delete the panel's independent clock. Once per frame, ask "how much room
// has the window ACTUALLY made so far?" and make that the panel's width. The
// window's own animation becomes the single clock, and the centre region's
// width is invariant by construction instead of by tuning — there is no
// second curve left to match. A panel driven this way needs no transition of
// its own, and gets its width from a custom property so a JS-owned value can
// outrank the `width: ... !important` rules that both panels use for their
// collapsed states.
//
// USAGE
//
//   HandQLayoutDrive.start({
//       el:      panelElement,
//       prop:    '--ss-driven-w',   // custom property the CSS reads
//       cls:     'ss-driving',      // class scoping that CSS rule
//       startW:  0,                 // panel width before the toggle
//       finalW:  512,               // panel width after it
//       solve:   () => ...,         // raw candidate width for RIGHT NOW
//       onSettle: () => ...,        // optional; runs after the class is gone
//   });
//
// `solve` returns the width the panel could have at this instant while the
// protected region keeps its pre-toggle width. It does not need to account
// for every box in the row: whatever constant it misses is measured once at
// start-up and added back on every frame, so frame 0 always reproduces
// `startW` exactly.
// ============================================================================

window.HandQLayoutDrive = (function () {
    'use strict';

    // A drive ends when the window has stopped changing width. Require a few
    // identical frames so a mid-animation plateau (DWM briefly holding a size
    // under load) doesn't end it early, and cap the whole thing so a resize
    // that never arrives — setBounds is a no-op when the window is already
    // against the work-area edge — still releases the driven state instead of
    // spinning forever.
    const STABLE_FRAMES = 3;
    const MIN_MS = 120;
    const MAX_MS = 700;

    // One drive per element. Starting a second on the same element cancels
    // the first, so a rapid open→close→open lands on the newest intent
    // instead of leaving two rAF loops fighting over one custom property.
    const active = new Map();

    function cancel(el) {
        const d = active.get(el);
        if (!d) return;
        if (d.rafId) cancelAnimationFrame(d.rafId);
        active.delete(el);
        el.classList.remove(d.cls);
        el.style.removeProperty(d.prop);
    }

    function isActive(el) {
        return active.has(el);
    }

    function start(opts) {
        const el = opts && opts.el;
        if (!el) return;
        cancel(el);

        const startW = Number(opts.startW) || 0;
        const finalW = Number(opts.finalW) || 0;
        const settle = () => {
            cancel(el);
            if (typeof opts.onSettle === 'function') opts.onSettle();
        };

        // Reduced motion: no drive, no intermediate frames. The caller's
        // onSettle applies the final width, which is the whole animation.
        if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
            settle();
            return;
        }

        const d = {
            cls: opts.cls,
            prop: opts.prop,
            solve: opts.solve,
            // Clamp bounds are just the two endpoints, whichever order they
            // come in — the same interval serves an open and a close.
            lo: Math.min(startW, finalW),
            hi: Math.max(startW, finalW),
            // Self-calibration: whatever `solve` fails to account for is a
            // constant for the duration of one toggle, so measuring it once
            // here lets frame 0 reproduce startW exactly.
            bias: startW - Number(opts.solve()),
            lastW: -1,
            stable: 0,
            startTs: performance.now(),
            rafId: 0,
            settle: settle,
        };
        active.set(el, d);

        el.style.setProperty(d.prop, Math.round(startW) + 'px');
        el.classList.add(d.cls);
        d.rafId = requestAnimationFrame(() => tick(el));
    }

    function tick(el) {
        const d = active.get(el);
        if (!d) return;
        d.rafId = 0;

        // window.innerWidth is the stability signal rather than a measured
        // element box: it is what the OS resize actually moves, it needs no
        // layout flush, and it is already in sync with the frame we're in.
        const winW = window.innerWidth;
        const raw = Number(d.solve());
        const next = Math.max(d.lo, Math.min(d.hi, raw + d.bias));
        el.style.setProperty(d.prop, Math.round(next) + 'px');

        if (Math.abs(winW - d.lastW) < 0.5) d.stable++;
        else d.stable = 0;
        d.lastW = winW;

        const elapsed = performance.now() - d.startTs;
        if ((d.stable >= STABLE_FRAMES && elapsed > MIN_MS) || elapsed > MAX_MS) {
            d.settle();
            return;
        }
        d.rafId = requestAnimationFrame(() => tick(el));
    }

    return { start: start, cancel: cancel, isActive: isActive };
})();
