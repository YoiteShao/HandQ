// HandQ Liquid Glass — Edge Refraction (liquidGL-style bevel).
// desktopCapturer stream + WebGL2 fragment shader focused on the edge band:
// broad refraction + sharp spike at boundary + multi-wavelength dispersion.
// The dispersion is a soft spectral halo (each wavelength refracts by a
// slightly different amount along the same direction), NOT a static tint or
// a wide per-channel fringe — that keeps it reading as real glass color
// dispersion rather than a rainbow stripe or a printed decal.
//
// Rasterization is limited to a 4-strip "picture frame" via gl.SCISSOR_TEST —
// interior pixels aren't visited at all, since the shader would discard them
// anyway. Window bounds are pushed from main.js on move/resize instead of
// polled per frame; this removes the IPC roundtrip that made drag feel laggy.

'use strict';

(function () {

const VERT = `#version 300 es
in vec2 a_pos;
out vec2 v_uv;
void main() {
    v_uv = a_pos * 0.5 + 0.5;
    gl_Position = vec4(a_pos, 0.0, 1.0);
}`;

// liquidGL-inspired fragment shader:
// - axis-split edgeFactor (LR + TB bands combined via max)
// - two-part refraction: edge*refraction + pow(edge,10)*bevelDepth
// - centreBlend suppression (no refraction near center)
// - clean rounded-rect masking (sdRoundBox used for inShape only)
const FRAG = `#version 300 es
precision mediump float;

uniform sampler2D u_tex;
uniform vec2 u_res;        // canvas resolution
uniform vec4 u_crop;       // xy=offset, zw=size (normalized screen coords)
uniform float u_radius;    // corner radius in pixels
uniform float u_refraction;   // broad refraction strength
uniform float u_bevelDepth;   // sharp bevel spike strength
uniform float u_dispersion;   // per-wavelength refraction spread (fraction of displacement)
uniform float u_bevelWidthLR;     // bevel band width for left/right edges (fraction of min dimension)
uniform float u_bevelWidthTop;    // bevel band width for the top edge (fraction of min dimension)
uniform float u_bevelWidthBottom; // bevel band width for the bottom edge (fraction of min dimension)

in vec2 v_uv;
out vec4 o;

// Correct SIGNED distance to rounded box (negative inside, positive outside)
float sdRoundBox(vec2 p, vec2 b, float r) {
    vec2 q = abs(p) - b + r;
    return min(max(q.x, q.y), 0.0) + length(max(q, 0.0)) - r;
}

float edgeFactor(vec2 uv) {
    vec2 p_px = (uv - 0.5) * u_res;
    // Inward distance from each edge (positive inside). WebGL clip space is
    // bottom-origin, so p_px.y > 0 corresponds to the upper half of the canvas
    // — distTop shrinks as we approach uv.y = 1, distBottom as uv.y = 0.
    float distLR = 0.5 * u_res.x - abs(p_px.x);
    float distTop = 0.5 * u_res.y - p_px.y;
    float distBottom = 0.5 * u_res.y + p_px.y;
    float minDim = min(u_res.x, u_res.y);
    float bevelLRpx = u_bevelWidthLR * minDim;
    float bevelTopPx = u_bevelWidthTop * minDim;
    float bevelBottomPx = u_bevelWidthBottom * minDim;
    // 1.0 at the corresponding edge, 0.0 beyond the bevel band.
    float edgeLR = 1.0 - smoothstep(0.0, bevelLRpx, distLR);
    float edgeTop = 1.0 - smoothstep(0.0, bevelTopPx, distTop);
    float edgeBottom = 1.0 - smoothstep(0.0, bevelBottomPx, distBottom);
    return max(edgeLR, max(edgeTop, edgeBottom));
}

void main() {
    // Inside/outside mask
    vec2 p_px = (v_uv - 0.5) * u_res;
    vec2 b_px = 0.5 * u_res;
    float sd = sdRoundBox(p_px, b_px, u_radius);
    float inShape = 1.0 - smoothstep(-1.0, 1.0, sd);
    if (inShape < 0.01) { o = vec4(0.0); return; }

    // Edge factor (0 at center, 1 at boundary)
    float edge = edgeFactor(v_uv);

    // Early out for fully-interior pixels (no refraction needed)
    if (edge < 0.001) { o = vec4(0.0); return; }

    // Centre blend: suppress refraction near absolute center
    vec2 p = v_uv - 0.5;
    p.x *= u_res.x / u_res.y;
    float centreBlend = smoothstep(0.12, 0.42, length(p));

    // liquidGL dual refraction formula:
    // broad linear + sharp exponential spike at the very edge
    float offsetAmt = edge * u_refraction + pow(edge, 6.0) * u_bevelDepth;
    offsetAmt *= centreBlend;

    // Displacement direction: outward from center. The rim samples the
    // desktop behind the window edge and pulls it further out, so straight
    // features behind the window visibly bow/shift as they cross the band —
    // the "bending light through a thick glass edge" cue.
    vec2 dir = normalize(p + 0.001);
    vec2 offset = dir * offsetAmt;

    // Map to texture UV space
    vec2 baseUV = u_crop.xy + vec2(v_uv.x, 1.0 - v_uv.y) * u_crop.zw;

    // Multi-wavelength dispersion done the physically-correct way: instead of
    // sampling one bent point and then adding a separate sideways per-channel
    // fringe (which reads as three discrete rainbow stripes once it's wide
    // enough to see), each wavelength REFRACTS BY A SLIGHTLY DIFFERENT AMOUNT
    // along the SAME direction — exactly how a real glass edge's refractive
    // index varies with wavelength. The spread is a small fraction (~±9%) of
    // the displacement itself, so the colored copies stay overlapped into a
    // soft spectral halo whose width scales WITH the bend (never separating
    // into bands), and it's gated by u_dispersion * a smooth edge ramp so the
    // color only blooms in the peak of the bevel, not across the whole band.
    float disp = u_dispersion * smoothstep(0.15, 1.0, edge);
    vec2 offR = offset * (1.0 + disp);
    vec2 offG = offset;
    vec2 offB = offset * (1.0 - disp);
    // Crisp single-tap per channel — the whole point of edge refraction is
    // seeing the desktop behind the window bent sharply and split into a
    // spectral halo. An earlier revision box-blurred these samples to stop a
    // row of small text under the band from tearing into colored lines, but
    // blurring the samples destroys exactly the sharpness that reads as
    // "glass" — the bend and the dispersion both smear into a dull haze. Keep
    // the taps crisp; the text-tearing case is handled instead by keeping the
    // band from reaching too far in (BEVEL_WIDTH_TOP) rather than by blurring.
    float r = texture(u_tex, baseUV + offR * u_crop.zw).r;
    float g = texture(u_tex, baseUV + offG * u_crop.zw).g;
    float b = texture(u_tex, baseUV + offB * u_crop.zw).b;
    vec3 color = vec3(r, g, b);

    // Gentle saturation lift in the peak so the spectral separation reads as
    // a luminous glass halo rather than a faint tint. Small — the dispersion
    // above already provides the hue; this just keeps it from washing out.
    float luma = dot(color, vec3(0.299, 0.587, 0.114));
    color = mix(vec3(luma), color, 1.0 + smoothstep(0.15, 1.0, edge) * 0.45);

    // Alpha: linear-ish falloff with a high edge peak so the refracted rim
    // clearly overrides the un-refracted desktop showing through the
    // transparent window — a low peak made the displaced copy blend back
    // into the real content behind and the bend became invisible.
    float alpha = pow(edge, 1.1) * 0.95 * inShape;

    o = vec4(color * alpha, alpha);
}`;

// --- Constants ---
// Capture the desktop at LOW resolution on purpose. First-principles: a
// frosted-glass edge refracts a BLURRED (low-frequency) copy of what's behind
// it, and a low-frequency image loses nothing when stored at low resolution
// (Nyquist). So instead of capturing 1280px and then spending GPU to blur it,
// we capture small and let the sampler's built-in bilinear upscale BE the
// blur — free. Three wins at once from this single number:
//   1. Upload cost (texImage2D, the per-frame bottleneck) drops ~11x vs 1280.
//   2. The refraction can no longer tear fine text into colored lines, because
//      there's no high-frequency detail left in the source to tear.
//   3. The soft, low-detail look is what real frosted glass should show.
// 384 keeps just enough structure that the bent content still reads as "the
// window/colors behind, bending" rather than a flat color smear.
const CAPTURE_MAX_DIM = 384;
// Event-driven rendering (see the render section below): we no longer spin a
// fixed-rate rAF loop. The only clock left is a throttle on how often a
// *changing* desktop is allowed to trigger a redraw — a blurred edge doesn't
// need more than a few updates a second, and this stops a busy background
// (e.g. a video playing behind the window) from dragging us back to 30fps.
// When the desktop is static and the window is still, nothing ticks at all.
const DESKTOP_REFRESH_MS = 200;     // max ~5fps desktop-content refresh when busy

// Shader parameters (tuned for window-level glass).
// LR / Top / Bottom bevel bands are independent — the top is widened so the
// title bar's blank strip carries a more prominent bend, while the bottom
// stays narrower to keep the content-hugging edge tight. Corners take
// max(LR, Top/Bottom), so the wider band dominates the corner arc.
//
// CRITICAL relationship: peak displacement must stay a fraction OF the bevel
// band width, not exceed it. Displacement in screen px ≈ (REFRACTION +
// BEVEL_DEPTH) × window_width; band width in px ≈ BEVEL_WIDTH_* × min(window
// dimensions). The previous 0.055/0.070 (sum 0.125 ≈ 105px on an 840px
// window) dwarfed the ~50px band, so the rim sampled desktop content far
// outside the edge — visually uncorrelated with the boundary, which read as
// "no bend / random smear" rather than refraction. Keeping the peak at
// ~70% of the band width makes the displaced content come from just past
// the window edge, so straight features behind the window smoothly bow as
// they cross the band — a real, legible bend.
const REFRACTION = 0.020;        // broad displacement (linear across the band)
const BEVEL_DEPTH = 0.028;       // extra displacement concentrated near the edge
const DISPERSION = 0.11;         // per-wavelength refraction spread (±11% of the
                                  // displacement). Small enough that the R/G/B
                                  // copies stay overlapped into a soft spectral
                                  // halo that scales with the bend, instead of
                                  // separating into discrete rainbow bands.
const BEVEL_WIDTH_LR = 0.10;     // left/right edges
const BEVEL_WIDTH_TOP = 0.06;    // top edge — a middle ground: 0.08 reached far
                                  // enough in to sit over dense nav-bar text and
                                  // tear it; 0.05 barely refracted at all. 0.06
                                  // keeps a clearly-visible refracting top rim
                                  // while pulling the deepest refraction back
                                  // toward the very edge where less text sits.
const BEVEL_WIDTH_BOTTOM = 0.04; // bottom edge — narrow band

async function initGlass() {
    if (!window.glassCapture) return;

    const canvas = document.createElement('canvas');
    canvas.id = 'glass-canvas';
    canvas.style.cssText =
        'position:fixed;inset:0;width:100%;height:100%;z-index:-1;pointer-events:none;border-radius:30px;';
    document.body.prepend(canvas);

    const gl = canvas.getContext('webgl2', { alpha: true, premultipliedAlpha: true });
    if (!gl) { canvas.remove(); return; }

    function makeShader(type, src) {
        const s = gl.createShader(type);
        gl.shaderSource(s, src);
        gl.compileShader(s);
        if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
            console.error('[glass]', gl.getShaderInfoLog(s));
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
        console.error('[glass]', gl.getProgramInfoLog(prog));
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
        refraction: gl.getUniformLocation(prog, 'u_refraction'),
        bevelDepth: gl.getUniformLocation(prog, 'u_bevelDepth'),
        dispersion: gl.getUniformLocation(prog, 'u_dispersion'),
        bevelWidthLR: gl.getUniformLocation(prog, 'u_bevelWidthLR'),
        bevelWidthTop: gl.getUniformLocation(prog, 'u_bevelWidthTop'),
        bevelWidthBottom: gl.getUniformLocation(prog, 'u_bevelWidthBottom'),
    };

    const tex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);

    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);

    // --- Capture ---
    const screenInfo = await window.glassCapture.getScreenSource();
    if (!screenInfo || !screenInfo.sourceId) { canvas.remove(); return; }

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
    } catch (_) {
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
            canvas.remove();
            return;
        }
    }

    const video = document.createElement('video');
    video.srcObject = stream;
    video.muted = true;
    await video.play();

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
    // When both are static the GPU does almost nothing — the previous frame is
    // already correct, so re-drawing it would be identical work for no visible
    // change. This is the whole point of the optimization: near-zero cost while
    // the window sits idle (the common case for an all-day app).

    function renderOnce(uploadTexture) {
        if (!bounds) return;

        // Clear the whole canvas (needed so the interior stays transparent).
        // Scissor is off here so clear covers all pixels — the 4 strip draws
        // below only touch the edge band.
        gl.disable(gl.SCISSOR_TEST);
        gl.clearColor(0, 0, 0, 0);
        gl.clear(gl.COLOR_BUFFER_BIT);

        gl.bindTexture(gl.TEXTURE_2D, tex);
        if (uploadTexture) {
            // Upload the current (low-res) desktop frame. Skipped when only the
            // window moved — the desktop pixels are unchanged, just where our
            // edge samples them, so the cached texture is still correct.
            gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, video);
        }

        gl.uniform1i(loc.tex, 0);
        gl.uniform2f(loc.res, canvas.width, canvas.height);
        gl.uniform4f(loc.crop,
            bounds.x / bounds.displayWidth,
            bounds.y / bounds.displayHeight,
            bounds.width / bounds.displayWidth,
            bounds.height / bounds.displayHeight);
        gl.uniform1f(loc.radius, 30.0 * (window.devicePixelRatio || 1));
        gl.uniform1f(loc.refraction, REFRACTION);
        gl.uniform1f(loc.bevelDepth, BEVEL_DEPTH);
        gl.uniform1f(loc.dispersion, DISPERSION);
        gl.uniform1f(loc.bevelWidthLR, BEVEL_WIDTH_LR);
        gl.uniform1f(loc.bevelWidthTop, BEVEL_WIDTH_TOP);
        gl.uniform1f(loc.bevelWidthBottom, BEVEL_WIDTH_BOTTOM);

        // Rasterize only the 4 edge strips. The shader would discard interior
        // pixels via `edge < 0.001` anyway, but the rasterizer still visits
        // every one — SCISSOR_TEST prevents that. `+ 4` gives a tiny margin so
        // the shader's alpha fade never gets clipped mid-transition.
        // gl.scissor uses bottom-left origin.
        const minDim = Math.min(canvas.width, canvas.height);
        const bevelTopPx = Math.ceil(BEVEL_WIDTH_TOP * minDim) + 4;
        const bevelBottomPx = Math.ceil(BEVEL_WIDTH_BOTTOM * minDim) + 4;
        const bevelLRpx = Math.ceil(BEVEL_WIDTH_LR * minDim) + 4;
        const midH = Math.max(0, canvas.height - bevelTopPx - bevelBottomPx);
        gl.enable(gl.SCISSOR_TEST);

        gl.scissor(0, canvas.height - bevelTopPx, canvas.width, bevelTopPx); // top
        gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
        gl.scissor(0, 0, canvas.width, bevelBottomPx);                       // bottom
        gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
        gl.scissor(0, bevelBottomPx, bevelLRpx, midH);                       // left (mid section only)
        gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
        gl.scissor(canvas.width - bevelLRpx, bevelBottomPx, bevelLRpx, midH);// right (mid section only)
        gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);

        gl.disable(gl.SCISSOR_TEST);
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
    console.log('[glass] active (event-driven + ' + DESKTOP_REFRESH_MS + 'ms refresh), cap:', capW + 'x' + capH);
}

// --- Boot ---
function init() {
    // Log the full error (with stack), not just .message — a silent init
    // failure here means the whole effect never renders, and the stack is what
    // pinpoints where. (A temporal-dead-zone ReferenceError from mis-ordered
    // `let`s vs. an early call site is exactly the kind of bug that hid here.)
    initGlass().catch((e) => console.warn('[glass] init failed:', e));
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
} else {
    init();
}

})();
