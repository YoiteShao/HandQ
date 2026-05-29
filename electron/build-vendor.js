'use strict';

const esbuild = require('esbuild');
const fs = require('fs');
const path = require('path');

const vendorDir = path.join(__dirname, 'renderer', 'vendor');
fs.mkdirSync(vendorDir, { recursive: true });

// Mirror <repo>/logo.png into electron/ so main.js can resolve it via
// path.join(__dirname, 'logo.png') in both dev and packaged builds. The
// repo root file is the single source of truth; this copy is a derived
// artifact, refreshed on every prestart so updates don't drift.
const logoSrc = path.join(__dirname, '..', 'logo.png');
const logoDst = path.join(__dirname, 'logo.png');
if (fs.existsSync(logoSrc)) {
    try {
        fs.copyFileSync(logoSrc, logoDst);
        console.log('logo.png mirrored: ' + logoSrc + ' -> ' + logoDst);
    } catch (err) {
        console.warn('logo.png mirror failed (non-fatal): ' + err.message);
    }
}

esbuild.buildSync({
    stdin: {
        contents: `
            import { Terminal } from '@xterm/xterm';
            import { FitAddon } from '@xterm/addon-fit';
            window.XTermLib = { Terminal, FitAddon };
        `,
        resolveDir: __dirname,
    },
    bundle: true,
    format: 'iife',
    outfile: path.join(vendorDir, 'xterm.bundle.js'),
    platform: 'browser',
    target: ['chrome120'],
});

const xtermCssSrc = path.join(__dirname, 'node_modules', '@xterm', 'xterm', 'css', 'xterm.css');
if (fs.existsSync(xtermCssSrc)) {
    fs.copyFileSync(xtermCssSrc, path.join(vendorDir, 'xterm.css'));
}

console.log('vendor bundle built: renderer/vendor/xterm.bundle.js + xterm.css');
