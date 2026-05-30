// HandQ update notifier.
//
// Notify-only model. We do NOT download or run the installer ourselves —
// we only point the user at the SMB share where new versions live. This
// is by design: the user closes HandQ via our quit button, then double-
// clicks the installer themselves, so the NSIS wizard is never racing
// the bridge / Electron exit and can never hit "file in use" on
// HandQ.exe / handq-bridge.exe.
//
// Publish workflow (developer): drop `HandQ Setup x.y.z.exe` into the
// share path. No version.json, no SHA file, no script. The installer
// filename IS the metadata; we read the highest semver in the folder.
//
// Update workflow (user): on next launch the dialog appears; clicking
// the primary button opens the share in Explorer (independent process)
// and quits HandQ via the existing `before-quit` shutdown chain.
//
// Share path resolution (highest priority first):
//   1. HANDQ_UPDATE_BASE environment variable     (testing / per-machine)
//   2. handq_config.yaml :: update.share_path     (user-editable)
//   3. DEFAULT_UPDATE_BASE                        (compiled-in fallback)
// Empty string (e.g. `share_path: ''`) disables update checks.

'use strict';

const fs = require('fs');
const fsp = fs.promises;
const os = require('os');
const path = require('path');
const { app, dialog, shell } = require('electron');

const DEFAULT_UPDATE_BASE = '\\\\wine\\APTAuto\\ADAS\\fengxuan\\HandQ';

// SMB readdir on a healthy LAN finishes in well under a second. 5 s is the
// budget we give a flaky / VPN-routed share before giving up. Failure is
// silent (logged only) — the user just doesn't see a dialog this launch.
const SCAN_TIMEOUT_MS = 5000;

// Matches electron-builder's default NSIS artifact name:
//   `${productName} Setup ${version}.${ext}`
// productName='HandQ' is set in package.json; semver is x.y.z without
// pre-release tags (we don't ship those today).
const INSTALLER_RE = /^HandQ Setup (\d+\.\d+\.\d+)\.exe$/i;

let _ranOnce = false;

function cmpVer(a, b) {
    const pa = a.split('.').map((n) => parseInt(n, 10) || 0);
    const pb = b.split('.').map((n) => parseInt(n, 10) || 0);
    const len = Math.max(pa.length, pb.length);
    for (let i = 0; i < len; i++) {
        const x = pa[i] || 0;
        const y = pb[i] || 0;
        if (x !== y) return x < y ? -1 : 1;
    }
    return 0;
}

// ── Config resolution ──────────────────────────────────────────────────────

function userConfigPath() {
    // Mirrors bridge_main._user_handq_root + _resolve_config_path on Windows.
    // We deliberately don't import a yaml parser — `update.share_path` is a
    // single scalar at known nesting; a 30-line state machine is enough.
    const home = process.env.USERPROFILE || os.homedir();
    return path.join(home, 'HandQ', 'handq_config.yaml');
}

function readShareFromYaml(yamlPath) {
    let text;
    try {
        text = fs.readFileSync(yamlPath, 'utf8');
    } catch {
        return null;
    }
    // Walk lines looking for a top-level `update:` block, then within it
    // for `  share_path: <value>`. We stop scanning the block at the first
    // line that starts at column 0 (next top-level key).
    let inUpdate = false;
    for (const raw of text.split(/\r?\n/)) {
        const line = raw.replace(/\s+$/, '');
        if (/^update:\s*(?:#.*)?$/.test(line)) { inUpdate = true; continue; }
        if (inUpdate && /^[A-Za-z_]/.test(raw)) {
            // Sibling top-level key — block ended without a hit.
            inUpdate = false;
        }
        if (!inUpdate) continue;
        const m = raw.match(
            /^\s+share_path:\s*(?:'([^']*)'|"([^"]*)"|([^#\n]*?))\s*(?:#.*)?$/,
        );
        if (m) {
            const v = (m[1] !== undefined ? m[1]
                    : m[2] !== undefined ? m[2]
                    : (m[3] || '')).trim();
            return v; // empty string is meaningful — disables updates
        }
    }
    return null;
}

function resolveShareBase(logLine) {
    const env = (process.env.HANDQ_UPDATE_BASE || '').trim();
    if (env) {
        logLine('UPDATER', 'share_path source: HANDQ_UPDATE_BASE env',
                { value: env });
        return env;
    }
    const fromYaml = readShareFromYaml(userConfigPath());
    if (fromYaml !== null) {
        logLine('UPDATER', 'share_path source: handq_config.yaml',
                { value: fromYaml || '<empty: updates disabled>' });
        return fromYaml; // may be '' to explicitly disable
    }
    logLine('UPDATER', 'share_path source: built-in default',
            { value: DEFAULT_UPDATE_BASE });
    return DEFAULT_UPDATE_BASE;
}

// ── Scan + dialog ──────────────────────────────────────────────────────────

async function scanLatestVersion(updateBase) {
    const entries = await Promise.race([
        fsp.readdir(updateBase),
        new Promise((_, reject) => setTimeout(
            () => reject(new Error(`scan timeout >${SCAN_TIMEOUT_MS}ms`)),
            SCAN_TIMEOUT_MS,
        )),
    ]);
    let best = null;
    for (const name of entries) {
        const m = INSTALLER_RE.exec(name);
        if (!m) continue;
        if (!best || cmpVer(m[1], best.version) > 0) {
            best = { version: m[1], filename: name };
        }
    }
    return best;
}

async function checkForUpdates({ logLine, mainWindow }) {
    if (_ranOnce) return;
    _ranOnce = true;

    const updateBase = resolveShareBase(logLine);
    if (!updateBase) {
        logLine('UPDATER', 'updates disabled (share_path is empty)');
        return;
    }

    const current = app.getVersion();
    let latest;
    try {
        latest = await scanLatestVersion(updateBase);
    } catch (e) {
        logLine('UPDATER', 'scan failed', {
            base: updateBase,
            err: e && e.message,
        });
        return;
    }
    if (!latest) {
        logLine('UPDATER', 'no installer found', { base: updateBase });
        return;
    }
    if (cmpVer(latest.version, current) <= 0) {
        logLine('UPDATER', 'up-to-date', {
            current,
            remote: latest.version,
        });
        return;
    }

    logLine('UPDATER', 'new version available', {
        current,
        remote: latest.version,
        installer: latest.filename,
    });

    let response;
    try {
        const r = await dialog.showMessageBox(mainWindow, {
            type: 'info',
            title: 'HandQ 更新',
            message: `HandQ ${latest.version} 已发布（当前 ${current}）`,
            detail:
                '点击"打开更新目录并退出"将退出当前 HandQ 并打开共享文件夹，'
                + '请将安装包复制到本机后双击运行。\n\n如果跳过，下次启动会再次提示。',
            buttons: ['打开更新目录并退出', '稍后'],
            defaultId: 0,
            cancelId: 1,
            noLink: true,
        });
        response = r.response;
    } catch (e) {
        logLine('UPDATER', 'showMessageBox failed', { err: e && e.message });
        return;
    }

    if (response !== 0) {
        logLine('UPDATER', 'user dismissed update dialog');
        return;
    }

    // Open Explorer first, then quit. shell.openPath spawns the system
    // file-manager as an independent process — once it's launched, our
    // own quit can't take it down with us.
    try {
        const errMsg = await shell.openPath(updateBase);
        if (errMsg) {
            logLine('UPDATER', 'openPath returned error', { err: errMsg });
        }
    } catch (e) {
        logLine('UPDATER', 'openPath threw', { err: e && e.message });
    }

    logLine('UPDATER', 'quitting for update', { remote: latest.version });
    // Triggers the existing `before-quit` handler in main.js (sends
    // {type:"shutdown"} to the bridge, 2 s grace, then app.exit(0)).
    app.quit();
}

module.exports = { checkForUpdates };
