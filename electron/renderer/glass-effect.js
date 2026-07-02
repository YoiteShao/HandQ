// HandQ Liquid Glass — Edge Refraction (liquidGL-style bevel).
// desktopCapturer stream + WebGL2 fragment shader focused on the edge band:
// broad refraction + sharp spike at boundary + chromatic aberration.
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
// - chromatic aberration (R/G/B at different refraction scales)
// - clean rounded-rect masking (sdRoundBox used for inShape only)
const FRAG = `#version 300 es
precision mediump float;

uniform sampler2D u_tex;
uniform vec2 u_res;        // canvas resolution
uniform vec4 u_crop;       // xy=offset, zw=size (normalized screen coords)
uniform float u_radius;    // corner radius in pixels
uniform float u_refraction;   // broad refraction strength
uniform float u_bevelDepth;   // sharp bevel spike strength
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
    float offsetAmt = edge * u_refraction + pow(edge, 10.0) * u_bevelDepth;
    offsetAmt *= centreBlend;

    // Displacement direction: outward from center
    vec2 dir = normalize(p + 0.001);
    vec2 offset = dir * offsetAmt;

    // Map to texture UV space
    vec2 baseUV = u_crop.xy + vec2(v_uv.x, 1.0 - v_uv.y) * u_crop.zw;

    // Chromatic aberration: each channel refracted differently
    vec2 off_r = offset * 1.4 * u_crop.zw;
    vec2 off_g = offset * 1.0 * u_crop.zw;
    vec2 off_b = offset * 0.5 * u_crop.zw;

    float r = texture(u_tex, baseUV + off_r).r;
    float g = texture(u_tex, baseUV + off_g).g;
    float b = texture(u_tex, baseUV + off_b).b;
    vec3 color = vec3(r, g, b);

    // Saturation boost at edges (makes chromatic split more vivid)
    float luma = dot(color, vec3(0.299, 0.587, 0.114));
    color = mix(vec3(luma), color, 1.0 + edge * 1.2);

    // Alpha: smooth fade, no hard white border
    float alpha = pow(edge, 1.5) * 0.7 * inShape;

    o = vec4(color * alpha, alpha);
}`;

// --- Constants ---
const CAPTURE_MAX_DIM = 1280;
const FRAME_INTERVAL_NORMAL = 33;   // ~30fps when idle
const FRAME_INTERVAL_DRAG = 100;    // ~10fps during active window drag/resize
const DRAG_DETECT_MS = 200;         // bounds updates within this window = "dragging"

// Shader parameters (tuned for window-level glass).
// LR / Top / Bottom bevel bands are independent — the top is widened so the
// title bar's blank strip carries a more prominent light/shadow band, while
// the bottom stays narrower to keep the content-hugging edge tight. Corners
// take max(LR, Top/Bottom), so the wider band dominates the corner arc.
const REFRACTION = 0.0;          // broad displacement (stronger)
const BEVEL_DEPTH = 0.01;        // sharp spike at edge (more intense)
const BEVEL_WIDTH_LR = 0.07;     // left/right edges — wider band
const BEVEL_WIDTH_TOP = 0.05;    // top edge — widened to fill the title bar area
const BEVEL_WIDTH_BOTTOM = 0.02; // bottom edge — narrow band

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
    let lastBoundsChangeTime = 0;

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

    if (window.glassCapture.onBoundsChanged) {
        window.glassCapture.onBoundsChanged((b) => {
            if (!b) return;
            bounds = b;
            lastBoundsChangeTime = performance.now();
            if (b.displayId !== currentDisplayId) switchDisplay(b);
        });
    }

    function resize() {
        const dpr = window.devicePixelRatio || 1;
        canvas.width = Math.round(canvas.clientWidth * dpr);
        canvas.height = Math.round(canvas.clientHeight * dpr);
        gl.viewport(0, 0, canvas.width, canvas.height);
    }
    resize();
    window.addEventListener('resize', resize);

    let lastFrameTime = 0;

    function frame(now) {
        requestAnimationFrame(frame);
        // Adaptive rate: during a burst of bounds updates (window drag/resize),
        // drop to 10fps and reuse the last video texture. DWM composition of a
        // transparent window is the dominant cost on Windows drag; cutting the
        // shader's per-frame work leaves headroom for the drag itself. Skipping
        // texImage2D also saves ~1-5ms of GPU upload per frame — the refraction
        // shows slightly stale desktop content during drag, but motion masks it.
        const isDragging = (now - lastBoundsChangeTime) < DRAG_DETECT_MS;
        const interval = isDragging ? FRAME_INTERVAL_DRAG : FRAME_INTERVAL_NORMAL;
        if (now - lastFrameTime < interval) return;
        lastFrameTime = now;
        if (!bounds) return;

        // Clear the whole canvas once (cheap, and needed so the interior stays
        // transparent between frames). Scissor is off here so clear covers all
        // pixels — the 4 strip draws below only touch the edge band.
        gl.disable(gl.SCISSOR_TEST);
        gl.clearColor(0, 0, 0, 0);
        gl.clear(gl.COLOR_BUFFER_BIT);

        gl.bindTexture(gl.TEXTURE_2D, tex);
        if (!isDragging) {
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

    frame();
    console.log('[glass] active @30fps, edge-strip scissor, cap:', capW + 'x' + capH);
}

// --- Boot ---
function init() {
    initGlass().catch((e) => console.warn('[glass]', e.message));
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
} else {
    init();
}

})();
