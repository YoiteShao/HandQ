// HandQ Liquid Glass — unified edge-to-core refraction.
// desktopCapturer stream + WebGL2 fragment shader covering the WHOLE window:
// one continuous "bend" falloff (1 at the rounded-rect boundary, decaying to
// 0 over EDGE_THICKNESS px inward) drives displacement, chromatic dispersion,
// glow AND alpha together. Earlier revisions split the rim (crisp per-channel
// displacement via gl.SCISSOR_TEST strips) from the interior (fully
// transparent, undrawn) as two separate code paths — even with a smooth
// alpha ramp between them, switching what the shader DOES at the seam read
// as "edge is edge, center is center" rather than one continuous piece of
// glass. Validated in an isolated demo (see project chat) before porting
// here; a single sampling function whose inputs simply decay toward the
// center removes the seam entirely, at the cost of drawing the full quad
// instead of 4 edge strips (acceptable — render is still event-driven, not
// a fixed-rate loop, and the window is a few hundred px across).
//
// Displacement direction is the outward normal of the rounded-rect SDF,
// taken via screen-space derivatives (dFdx/dFdy of the signed distance
// itself) rather than a vector from the shape's center — a radial-from-
// center vector is only correct at the corners; along a flat edge the true
// outward direction is perpendicular to that edge. The derivative trick
// gives the right normal everywhere with no per-region case analysis.

'use strict';

(function () {

const VERT = `#version 300 es
in vec2 a_pos;
out vec2 v_uv;
void main() {
    v_uv = a_pos * 0.5 + 0.5;
    gl_Position = vec4(a_pos, 0.0, 1.0);
}`;

const FRAG = `#version 300 es
precision highp float;

uniform sampler2D u_tex;
uniform vec2 u_res;
uniform vec4 u_crop;            // xy=offset, zw=size (normalized screen coords)
uniform float u_radius;         // corner radius, px

uniform float u_edgeThickness;  // px — width of the bend band inward from the rim
uniform float u_refraction;     // px, peak displacement at the rim
uniform float u_dispersion;     // px, peak per-channel spectral spread at the rim
uniform float u_glowStrength;   // additive, color-tinted glow intensity at rim
uniform float u_edgeOpacity;    // white-tint mix strength at the rim (0 = clear glass)
uniform float u_coreOpacity;    // white-tint mix strength at the interior (frosted body)
uniform float u_glassAlpha;     // extra flat tint mix added on top (material color)
uniform vec3  u_tintColor;
uniform float u_frostiness;     // blur radius, px (0 = off)

in vec2 v_uv;
out vec4 o;

float sdRoundBox(vec2 p, vec2 b, float r) {
    vec2 q = abs(p) - b + r;
    return min(max(q.x, q.y), 0.0) + length(max(q, 0.0)) - r;
}

vec3 sampleBg(vec2 uv) {
    return texture(u_tex, u_crop.xy + uv * u_crop.zw).rgb;
}

vec3 sampleBgAA(vec2 uv, float radiusPx) {
    if (radiusPx < 0.4) return sampleBg(uv);
    vec2 texel = u_crop.zw / u_res;
    vec3 sum = vec3(0.0);
    float n = 0.0;
    for (int x = -2; x <= 2; x++) {
        for (int y = -2; y <= 2; y++) {
            vec2 off = vec2(float(x), float(y)) * texel * (radiusPx * 0.5);
            sum += sampleBg(uv + off);
            n += 1.0;
        }
    }
    return sum / n;
}

void main() {
    vec2 p_px = (v_uv - 0.5) * u_res;
    vec2 b_px = 0.5 * u_res;
    float sd = sdRoundBox(p_px, b_px, u_radius);
    float inShape = 1.0 - smoothstep(-1.0, 1.0, sd);
    if (inShape < 0.01) { o = vec4(0.0); return; }

    // Outward normal of the SDF field via screen-space derivatives — correct
    // along flat edges AND around the rounded corners alike.
    vec2 gradSd = vec2(dFdx(sd), dFdy(sd));
    vec2 normalDir = length(gradSd) > 1e-5 ? normalize(gradSd) : vec2(0.0);

    // Single continuous falloff: 1 at the boundary, eased down to 0 over
    // u_edgeThickness px moving inward. Everything below reads from "bend",
    // never branches into a separate interior code path.
    float distIn = -sd;
    float shape = 1.0 - smoothstep(0.0, u_edgeThickness, distIn);
    float bend = pow(shape, 1.4);

    vec2 baseUV = vec2(v_uv.x, 1.0 - v_uv.y);
    vec2 dispUV = normalDir * (bend * u_refraction) / u_res;
    // Dispersion is an INDEPENDENT pixel-scale offset (u_dispersion is now a
    // px quantity, not a fraction of the refraction displacement). Earlier
    // revision multiplied the already-small bend*refraction displacement by
    // (1 ± disp) — with disp capped at 0.4, the resulting per-channel
    // separation was a small fraction of an already-small number, which read
    // as "faint" no matter how the sliders were set. Reference art (Apple's
    // liquid glass) shows a wide, clearly-separated spectral fringe at the
    // rim — decoupling dispersion from refraction lets it reach that
    // magnitude independently.
    vec2 dispersionOffsetUV = normalDir * (bend * u_dispersion) / u_res;
    float blurPx = u_frostiness * mix(1.0, 0.4, bend);

    // Single shared tap when dispersion is off (the common case — confirmed
    // default is 0). u_dispersion is a uniform, so this branch is the same
    // for every pixel in the draw call (no per-pixel divergence cost) — it
    // just skips doing 3x the texture fetches across the WHOLE window body
    // for a per-channel offset that would evaluate to zero anyway.
    vec3 color;
    if (u_dispersion < 0.001) {
        color = sampleBgAA(baseUV + dispUV, blurPx);
    } else {
        color.r = sampleBgAA(baseUV + dispUV + dispersionOffsetUV, blurPx).r;
        color.g = sampleBgAA(baseUV + dispUV, blurPx).g;
        color.b = sampleBgAA(baseUV + dispUV - dispersionOffsetUV, blurPx).b;
    }

    // Saturation lift so the dispersion reads as a spectral halo, not a wash.
    float luma = dot(color, vec3(0.299, 0.587, 0.114));
    color = mix(vec3(luma), color, 1.0 + bend * 0.7);

    // Tint/frost — NOT alpha. This is the actual "transparent at the rim,
    // less transparent toward the center" cue the design calls for. Earlier
    // revision implemented that gradient by lowering the COMPOSITING alpha
    // itself (edgeOpacity down to ~0.05) — but this canvas sits over an
    // OS-transparent window showing the SAME real desktop underneath, so a
    // faint-alpha copy of a slightly-shifted image blends almost invisibly
    // back into its own undisplaced twin: the refraction/dispersion shift
    // was real but diluted into imperceptibility. Fix: composite at
    // near-full alpha everywhere inside the shape (so the bend actually
    // replaces what's there instead of faintly blending into it), and do
    // the transparent-to-frosted gradient via how much white tint gets
    // mixed in — near 0 at the rim (clear glass, dispersion fully visible),
    // higher toward center (frosted body).
    float tintMix = clamp(mix(u_coreOpacity, u_edgeOpacity, bend) + u_glassAlpha, 0.0, 1.0);
    color = mix(color, u_tintColor, tintMix);

    // Additive glow at the rim, tinted by the already-dispersed color so the
    // highlight itself carries a hint of the spectral fringe rather than
    // reading as a flat white line. Multiplicative-only (no flat vec3(...)
    // additive term) — an earlier revision added a flat brightening on top,
    // which pushes R/G/B up by an equal amount regardless of the underlying
    // hue, i.e. it desaturates toward literal white and reads as a solid
    // white outline rather than a soft colored highlight. pow(bend, 4.5)
    // (was 3.0) narrows the highlight to a thin rim line instead of a wide
    // band, per the same complaint (outer edge looked like a thick white
    // border, not a refined highlight).
    float glow = pow(bend, 4.5) * u_glowStrength;
    color += color * glow;

    // Compositing alpha: near-1 through the body of the shape, falling only
    // in the outer antialiasing ring (inShape) — this is what makes the bend
    // itself visible instead of alpha-diluted. inShape already handles the
    // true shape boundary independently of u_edgeThickness (the refraction
    // band width), so corners/edges still antialias cleanly.
    float alpha = inShape;

    o = vec4(color * alpha, alpha);
}`;

// --- Constants ---
// Capture resolution. The old design only ever showed this capture in thin
// edge strips (gl.SCISSOR_TEST bands) — at that scale a deliberately LOW-res
// capture (384px, relying on bilinear upscale as free blur) was invisible as
// blur and saved a lot of upload cost. Now the shader paints the ENTIRE
// window body with this same texture (STATE.coreOpacity blends it under a
// tint, not a discard), so a 384px source stretched ~2-3x across the full
// card reads as visibly mushy — text/icons behind the window turn to soup
// everywhere, not just at the rim. Raised to 1024 so the interior stays
// legible; frostiness (STATE.frostiness, default 0) is the correct knob for
// an intentional soft-frost look now, not an under-resolved capture.
const CAPTURE_MAX_DIM = 1024;
// Event-driven rendering (see the render section below): we no longer spin a
// fixed-rate rAF loop. The only clock left is a throttle on how often a
// *changing* desktop is allowed to trigger a redraw — a blurred edge doesn't
// need more than a few updates a second, and this stops a busy background
// (e.g. a video playing behind the window) from dragging us back to 30fps.
// When the desktop is static and the window is still, nothing ticks at all.
const DESKTOP_REFRESH_MS = 200;     // max ~5fps desktop-content refresh when busy

// Shader parameters — validated against an isolated demo app before porting
// here (see project chat for the layer-1/layer-2 validation walkthrough).
// Kept as a mutable object (not const numbers) so the Ctrl+Shift+G tuning
// panel below can adjust them live without touching shader/render code.
// Corner radius intentionally stays at HandQ's existing 30px (matches
// `.app { border-radius: 30px }` in styles.css) rather than the demo's own
// 14px default, which was only sized for its own smaller demo window.
const STATE = {
    edgeThickness: 45,   // px — width of the bend band inward from the rim
    refraction: 34,      // peak displacement magnitude at the rim
    dispersion: 0,       // px — peak per-channel spectral spread at the rim
                          // (independent of refraction — see shader comment)
    glowStrength: 0.5,   // additive, color-tinted glow intensity at the rim
    edgeOpacity: 0,      // white-tint mix strength at the rim (near-clear glass)
    coreOpacity: 0,      // white-tint mix strength at the interior (frosted body)
    glassAlpha: 0.0,     // tint mix strength (material color)
    frostiness: 8,     // extra blur radius, px (0 = off)
    radius: 30,          // corner radius, px — matches .app's CSS radius
};

async function initGlass() {
    if (!window.glassCapture) { _logToFile('WARN', 'window.glassCapture missing, aborting'); return; }

    const canvas = document.createElement('canvas');
    canvas.id = 'glass-canvas';
    canvas.style.cssText =
        'position:fixed;inset:0;width:100%;height:100%;z-index:-1;pointer-events:none;border-radius:30px;';
    document.body.prepend(canvas);

    const gl = canvas.getContext('webgl2', { alpha: true, premultipliedAlpha: false, preserveDrawingBuffer: true });
    if (!gl) { _logToFile('WARN', 'no webgl2 context, aborting'); canvas.remove(); return; }

    function makeShader(type, src) {
        const s = gl.createShader(type);
        gl.shaderSource(s, src);
        gl.compileShader(s);
        if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
            const log = gl.getShaderInfoLog(s);
            console.error('[glass]', log);
            _logToFile('ERROR', 'shader compile failed', { log });
            return null;
        }
        return s;
    }
    const vs = makeShader(gl.VERTEX_SHADER, VERT);
    const fs = makeShader(gl.FRAGMENT_SHADER, FRAG);
    if (!vs || !fs) { canvas.remove(); return; }

    const prog = gl.createProgram();
    gl.attachShader(prog, vs);
    gl.attachShader(prog, fs);
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
        const log = gl.getProgramInfoLog(prog);
        console.error('[glass]', log);
        _logToFile('ERROR', 'program link failed', { log });
        canvas.remove();
        return;
    }
    gl.useProgram(prog);

    const buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1, 1,-1, -1,1, 1,1]), gl.STATIC_DRAW);
    const aPos = gl.getAttribLocation(prog, 'a_pos');
    gl.enableVertexAttribArray(aPos);
    gl.vertexAttribPointer(aPos, 2, gl.FLOAT, false, 0, 0);

    const loc = {
        tex: gl.getUniformLocation(prog, 'u_tex'),
        res: gl.getUniformLocation(prog, 'u_res'),
        crop: gl.getUniformLocation(prog, 'u_crop'),
        radius: gl.getUniformLocation(prog, 'u_radius'),
        edgeThickness: gl.getUniformLocation(prog, 'u_edgeThickness'),
        refraction: gl.getUniformLocation(prog, 'u_refraction'),
        dispersion: gl.getUniformLocation(prog, 'u_dispersion'),
        glowStrength: gl.getUniformLocation(prog, 'u_glowStrength'),
        edgeOpacity: gl.getUniformLocation(prog, 'u_edgeOpacity'),
        coreOpacity: gl.getUniformLocation(prog, 'u_coreOpacity'),
        glassAlpha: gl.getUniformLocation(prog, 'u_glassAlpha'),
        tintColor: gl.getUniformLocation(prog, 'u_tintColor'),
        frostiness: gl.getUniformLocation(prog, 'u_frostiness'),
    };

    const tex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);

    gl.enable(gl.BLEND);
    gl.blendFuncSeparate(gl.ONE, gl.ONE_MINUS_SRC_ALPHA, gl.ONE, gl.ONE_MINUS_SRC_ALPHA);

    // --- Capture ---
    const screenInfo = await window.glassCapture.getScreenSource();
    if (!screenInfo || !screenInfo.sourceId) {
        _logToFile('WARN', 'getScreenSource returned no sourceId, aborting', { screenInfo });
        canvas.remove();
        return;
    }
    _logToFile('INFO', 'screen source acquired', {
        displayId: screenInfo.displayId,
        displayWidth: screenInfo.displayWidth,
        displayHeight: screenInfo.displayHeight,
    });

    const scale = screenInfo.scaleFactor || 1;
    const rawW = screenInfo.displayWidth * scale;
    const rawH = screenInfo.displayHeight * scale;
    const capScale = Math.min(1, CAPTURE_MAX_DIM / Math.max(rawW, rawH));
    const capW = Math.round(rawW * capScale);
    const capH = Math.round(rawH * capScale);

    let stream;
    try {
        stream = await navigator.mediaDevices.getUserMedia({
            audio: false,
            video: {
                mandatory: {
                    chromeMediaSource: 'desktop',
                    chromeMediaSourceId: screenInfo.sourceId,
                    maxWidth: capW,
                    maxHeight: capH,
                    maxFrameRate: 30,
                    cursor: 'never',
                },
            },
        });
    } catch (errA) {
        try {
            stream = await navigator.mediaDevices.getUserMedia({
                audio: false,
                video: {
                    mandatory: {
                        chromeMediaSource: 'desktop',
                        chromeMediaSourceId: screenInfo.sourceId,
                        maxWidth: capW,
                        maxHeight: capH,
                        maxFrameRate: 30,
                    },
                },
            });
        } catch (err) {
            console.warn('[glass] capture failed:', err.message);
            _logToFile('ERROR', 'getUserMedia failed (both attempts)', {
                firstError: errA && errA.message,
                secondError: err && err.message,
            });
            canvas.remove();
            return;
        }
    }
    _logToFile('INFO', 'getUserMedia stream acquired', { capW, capH });

    const video = document.createElement('video');
    video.srcObject = stream;
    video.muted = true;
    await video.play();
    _logToFile('INFO', 'video playing', {
        videoWidth: video.videoWidth,
        videoHeight: video.videoHeight,
    });

    // Initial bounds via one-shot fetch; from here on we listen for main-push
    // updates instead of polling. No per-frame IPC.
    let bounds = await window.glassCapture.getWindowBounds();
    let currentDisplayId = screenInfo.displayId;

    async function switchDisplay(newBounds) {
        if (!newBounds || newBounds.displayId === currentDisplayId) return;
        currentDisplayId = newBounds.displayId;
        const info = await window.glassCapture.getScreenSource();
        if (!info || !info.sourceId) return;
        stream.getTracks().forEach((t) => t.stop());
        try {
            const ns = await navigator.mediaDevices.getUserMedia({
                audio: false,
                video: {
                    mandatory: {
                        chromeMediaSource: 'desktop',
                        chromeMediaSourceId: info.sourceId,
                        maxWidth: capW,
                        maxHeight: capH,
                        maxFrameRate: 30,
                    },
                },
            });
            video.srcObject = ns;
            await video.play();
        } catch (_) {}
    }

    // Render-loop state. MUST be declared before resize() is called below —
    // resize() calls requestRender(), and although requestRender/renderOnce are
    // hoisted function declarations, these `let`s are NOT: reading them before
    // this line throws a temporal-dead-zone ReferenceError, which would abort
    // initGlass() (caught silently by the .catch at the bottom) and leave the
    // whole effect un-rendered.
    let pendingRAF = 0;      // rAF handle coalescing multiple requests into one draw
    let needUpload = false;  // does the next draw need a fresh desktop texture?

    if (window.glassCapture.onBoundsChanged) {
        window.glassCapture.onBoundsChanged((b) => {
            if (!b) return;
            bounds = b;
            if (b.displayId !== currentDisplayId) switchDisplay(b);
            // Window moved/resized → the edge sits over new desktop content, so
            // redraw. Reuse the cached texture (don't re-upload) since a drag is
            // a burst of these; the desktop itself hasn't changed, only where
            // our edge samples it. requestRender coalesces the burst to 1/frame.
            requestRender(false);
        });
    }

    function resize() {
        const dpr = window.devicePixelRatio || 1;
        canvas.width = Math.round(canvas.clientWidth * dpr);
        canvas.height = Math.round(canvas.clientHeight * dpr);
        gl.viewport(0, 0, canvas.width, canvas.height);
        requestRender(true);
    }
    resize();
    window.addEventListener('resize', resize);

    // --- Event-driven render ---
    // No fixed-rate loop. We draw a frame only when an INPUT changes:
    //   • the window moved/resized  → onBoundsChanged → requestRender(false)
    //   • the desktop behind us changed → refresh timer → requestRender(true)
    //   • a tuning-panel slider moved → requestRender(false)
    // When both are static the GPU does almost nothing — the previous frame is
    // already correct, so re-drawing it would be identical work for no visible
    // change. This is the whole point of the optimization: near-zero cost while
    // the window sits idle (the common case for an all-day app).

    function renderOnce(uploadTexture) {
        if (!bounds) return;

        gl.clearColor(0, 0, 0, 0);
        gl.clear(gl.COLOR_BUFFER_BIT);

        gl.bindTexture(gl.TEXTURE_2D, tex);
        if (uploadTexture) {
            // Upload the current (low-res) desktop frame. Skipped when only the
            // window moved — the desktop pixels are unchanged, just where our
            // edge samples them, so the cached texture is still correct.
            gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, video);
        }

        const dpr = window.devicePixelRatio || 1;
        gl.uniform1i(loc.tex, 0);
        gl.uniform2f(loc.res, canvas.width, canvas.height);
        gl.uniform4f(loc.crop,
            bounds.x / bounds.displayWidth,
            bounds.y / bounds.displayHeight,
            bounds.width / bounds.displayWidth,
            bounds.height / bounds.displayHeight);
        gl.uniform1f(loc.radius, STATE.radius * dpr);
        gl.uniform1f(loc.edgeThickness, STATE.edgeThickness * dpr);
        gl.uniform1f(loc.refraction, STATE.refraction * dpr * 0.6);
        gl.uniform1f(loc.dispersion, STATE.dispersion * dpr);
        gl.uniform1f(loc.glowStrength, STATE.glowStrength);
        gl.uniform1f(loc.edgeOpacity, STATE.edgeOpacity);
        gl.uniform1f(loc.coreOpacity, STATE.coreOpacity);
        gl.uniform1f(loc.glassAlpha, STATE.glassAlpha);
        gl.uniform3f(loc.tintColor, 0.98, 0.98, 1.0);
        gl.uniform1f(loc.frostiness, STATE.frostiness * dpr);

        // Full-quad draw — the shader now does real work across the whole
        // window (the interior composites a tinted/frosted backdrop, not a
        // discard), so there's no strip to scissor down to anymore.
        gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    }

    // Coalesce any number of requests within one frame into a single draw. If
    // ANY of them needed a fresh texture upload, the coalesced draw uploads.
    function requestRender(withUpload) {
        if (withUpload) needUpload = true;
        if (pendingRAF) return;
        pendingRAF = requestAnimationFrame(() => {
            pendingRAF = 0;
            const upload = needUpload;
            needUpload = false;
            renderOnce(upload);
        });
    }

    // Desktop-content refresh. NOTE: an earlier revision drove this with
    // video.requestVideoFrameCallback, but this <video> is created detached
    // (never added to the DOM) — it decodes frames (so texImage2D can pull the
    // current one) but is never *presented* by the compositor, and rVFC only
    // fires on presentation. So rVFC never fired, the texture stayed empty, and
    // the whole effect vanished. Use a plain low-rate timer instead: reliable,
    // and still a huge win over the old 30fps×1280px loop (~1/6 the frames,
    // each ~1/11 the upload). Window moves are handled separately and crisply
    // by onBoundsChanged above, so drag stays full-rate; this timer only keeps
    // the *content* behind the glass reasonably fresh.
    setInterval(() => requestRender(true), DESKTOP_REFRESH_MS);

    // First paint. The very first video frame may not have arrived yet, so also
    // schedule a couple of early uploads to fill the texture without waiting a
    // full DESKTOP_REFRESH_MS interval.
    requestRender(true);
    setTimeout(() => requestRender(true), 60);
    setTimeout(() => requestRender(true), 150);
    console.log('[glass] active (unified edge-to-core, event-driven + ' + DESKTOP_REFRESH_MS + 'ms refresh), cap:', capW + 'x' + capH);

    installTuningPanel(() => requestRender(false));

    // Exposed for the Playwright screenshot driver (and any future scripted
    // testing) to programmatically set params and force a redraw without
    // going through the slider DOM. __glassRedrawSync bypasses
    // requestAnimationFrame entirely — rAF can be throttled/skipped when the
    // window lacks OS focus (as happens when Playwright drives it headlessly),
    // which would otherwise make automated before/after comparisons silently
    // no-op.
    window.__glassState = STATE;
    window.__glassRedraw = () => requestRender(false);
    window.__glassRedrawSync = () => renderOnce(true);
}

// --- Ctrl+Shift+G tuning panel ---
// Dev-only live control over STATE (see above), mirroring the existing
// Ctrl+Shift+L debug-log-panel pattern in renderer.js. Sliders write directly
// into the same STATE object renderOnce() reads every frame, then trigger an
// immediate (non-uploading) redraw so changes are visible instantly against
// the real window instead of round-tripping through the standalone demo app.
function installTuningPanel(requestRedraw) {
    const FIELDS = [
        { key: 'edgeThickness', label: 'Edge thickness (px)', min: 4, max: 100, step: 1 },
        { key: 'refraction',    label: 'Refraction strength', min: 0, max: 100, step: 1 },
        { key: 'dispersion',    label: 'Dispersion (px)',       min: 0, max: 40,  step: 0.5 },
        { key: 'glowStrength',  label: 'Glow strength',        min: 0, max: 0.5, step: 0.01 },
        { key: 'edgeOpacity',   label: 'Edge opacity',         min: 0, max: 1,   step: 0.01 },
        { key: 'coreOpacity',   label: 'Core opacity',         min: 0, max: 1,   step: 0.01 },
        { key: 'glassAlpha',    label: 'Glass alpha (tint)',   min: 0, max: 1,   step: 0.01 },
        { key: 'frostiness',    label: 'Frostiness (blur px)', min: 0, max: 20,  step: 0.5 },
        { key: 'radius',        label: 'Corner radius (px)',   min: 0, max: 60,  step: 1 },
    ];

    let panelEl = null;
    let panelVisible = false;

    function buildPanel() {
        if (panelEl) return panelEl;
        panelEl = document.createElement('div');
        panelEl.id = 'glass-tuning-panel';
        panelEl.style.cssText =
            'position:fixed;top:12px;right:12px;width:260px;z-index:99999;' +
            'background:rgba(20,20,24,0.90);color:#fff;border-radius:10px;' +
            'padding:12px;font:11px/1.4 -apple-system,Segoe UI,sans-serif;' +
            'box-shadow:0 8px 32px rgba(0,0,0,0.4);display:none;';

        const title = document.createElement('div');
        title.textContent = 'Liquid glass tuning (Ctrl+Shift+G to hide)';
        title.style.cssText = 'font-weight:600;margin-bottom:8px;';
        panelEl.appendChild(title);

        FIELDS.forEach((f) => {
            const row = document.createElement('label');
            row.style.cssText = 'display:block;margin-top:8px;';
            const valueSpan = document.createElement('span');
            valueSpan.textContent = String(STATE[f.key]);
            row.appendChild(document.createTextNode(f.label + ' '));
            row.appendChild(valueSpan);

            const input = document.createElement('input');
            input.type = 'range';
            input.min = String(f.min);
            input.max = String(f.max);
            input.step = String(f.step);
            input.value = String(STATE[f.key]);
            input.style.cssText = 'display:block;width:100%;';
            input.addEventListener('input', () => {
                const v = parseFloat(input.value);
                STATE[f.key] = v;
                valueSpan.textContent = String(v);
                requestRedraw();
            });

            row.appendChild(input);
            panelEl.appendChild(row);
        });

        document.body.appendChild(panelEl);
        return panelEl;
    }

    window.addEventListener('keydown', (e) => {
        if (e.ctrlKey && e.shiftKey && (e.key === 'G' || e.key === 'g')) {
            e.preventDefault();
            buildPanel();
            panelVisible = !panelVisible;
            panelEl.style.display = panelVisible ? 'block' : 'none';
        }
    });
}

// --- Boot ---
function _logToFile(level, msg, extra) {
    // glass-effect.js runs before renderer.js's window.__handqLog wiring is
    // guaranteed ready in all load orders, so guard defensively. This routes
    // through the same forwarder renderer.js installs, landing in
    // handq-frontend.log — console.* alone is invisible once DevTools isn't
    // attached (packaged builds, or when nobody popped devtools this launch).
    try {
        if (typeof window.__handqLog === 'function') {
            window.__handqLog(level, '[glass] ' + msg, extra);
            return;
        }
    } catch (_) { /* ignore */ }
    try { console.log('[glass]', msg, extra); } catch (_) { /* ignore */ }
}

function init() {
    // Log the full error (with stack), not just .message — a silent init
    // failure here means the whole effect never renders, and the stack is what
    // pinpoints where. (A temporal-dead-zone ReferenceError from mis-ordered
    // `let`s vs. an early call site is exactly the kind of bug that hid here.)
    _logToFile('INFO', 'init starting');
    initGlass().catch((e) => {
        console.warn('[glass] init failed:', e);
        _logToFile('ERROR', 'init failed', { message: e && e.message, stack: e && e.stack });
    });
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
} else {
    init();
}

})();
