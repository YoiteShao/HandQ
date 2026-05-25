'use strict';

const esbuild = require('esbuild');
const fs = require('fs');
const path = require('path');

const vendorDir = path.join(__dirname, 'renderer', 'vendor');
fs.mkdirSync(vendorDir, { recursive: true });

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
