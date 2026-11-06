// Turn the SVG captures from scripts/screenshots.py into the README images.
//
//   node scripts/render.mjs build/shots docs
//
// Writes docs/screenshots/*.png, docs/screenshots/demo.gif and
// docs/social-preview.png. Needs Playwright with Chromium, JetBrains Mono
// installed locally, and ffmpeg for the GIF.
import { createRequire } from 'node:module';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

// require() honours NODE_PATH, so a global Playwright install works too.
const { chromium } = createRequire(import.meta.url)('playwright');

const [src = 'build/shots', docs = 'docs'] = process.argv.slice(2);
const shotsOut = path.join(docs, 'screenshots');
fs.mkdirSync(shotsOut, { recursive: true });

const FONT = "'JetBrains Mono', 'DejaVu Sans Mono', monospace";

// Textual's SVG asks for Fira Code from a CDN; use a local monospace face instead.
function fixFonts(svg) {
  return svg
    .replace(/@font-face\s*{[^}]*}/g, '')
    .replace(/font-family: Fira Code, monospace;/g, `font-family: ${FONT};`);
}

const page = (body, w, extraCss = '') => `<!doctype html><html><head><meta charset="utf-8"><style>
  html,body{margin:0;background:#0d1117}
  .stage{width:${w}px;box-sizing:border-box;padding:44px 52px;
    background:radial-gradient(ellipse at 70% 40%, #1d3a4a 0%, #121a24 45%, #0d1117 80%);}
  .stage svg{display:block;width:100%;height:auto;filter:drop-shadow(0 18px 40px rgba(0,0,0,.55));border-radius:10px}
  ${extraCss}
</style></head><body>${body}</body></html>`;

async function renderSvg(p, file, out, width) {
  const svg = fixFonts(fs.readFileSync(file, 'utf8'));
  await p.setContent(page(`<div class="stage">${svg}</div>`, width), { waitUntil: 'load' });
  await p.evaluate(() => document.fonts.ready);
  await p.locator('.stage').screenshot({ path: out });
}

const browser = await chromium.launch();
const p = await browser.newPage({ viewport: { width: 1800, height: 1200 }, deviceScaleFactor: 1 });
await p.route(/^https?:/, (r) => r.abort());

// Stills, wide enough to stay crisp on high-density screens at GitHub's column width.
for (const f of fs.readdirSync(src).filter((n) => n.endsWith('.svg'))) {
  const out = path.join(shotsOut, f.replace(/\.svg$/, '.png'));
  await renderSvg(p, path.join(src, f), out, 1760);
  console.log('wrote', out);
}

// Demo GIF frames.
const framesDir = path.join(src, 'frames');
const timeline = fs.existsSync(path.join(framesDir, 'timeline.txt'))
  ? fs.readFileSync(path.join(framesDir, 'timeline.txt'), 'utf8').trim().split('\n').map((l) => l.split(' '))
  : [];
if (timeline.length) {
  const tmp = fs.mkdtempSync(path.join(src, 'png-'));
  const concat = [];
  for (const [name, secs] of timeline) {
    const png = path.join(tmp, `${name}.png`);
    await renderSvg(p, path.join(framesDir, `${name}.svg`), png, 1200);
    concat.push(`file '${path.resolve(png)}'`, `duration ${secs}`);
  }
  concat.push(`file '${path.resolve(path.join(tmp, timeline.at(-1)[0] + '.png'))}'`);
  const list = path.join(tmp, 'list.txt');
  fs.writeFileSync(list, concat.join('\n') + '\n');
  const gif = path.join(shotsOut, 'demo.gif');
  execFileSync('ffmpeg', ['-y', '-loglevel', 'error', '-f', 'concat', '-safe', '0', '-i', list,
    '-vf', 'split[a][b];[a]palettegen=max_colors=128:stats_mode=full[p];[b][p]paletteuse=dither=none',
    '-loop', '0', gif]);
  console.log('wrote', gif, `(${timeline.length} frames)`);
}

// Social preview card (1280x640), the image GitHub shows when the repo is shared.
const logo = fs.readFileSync('assets/awsnav.svg', 'utf8').replace(/<\?xml[^>]*>/, '');
const heroSvg = fixFonts(fs.readFileSync(path.join(src, 'hero.svg'), 'utf8'));
await p.setViewportSize({ width: 1280, height: 640 });
await p.setContent(`<!doctype html><html><head><meta charset="utf-8"><style>
  html,body{margin:0}
  .card{width:1280px;height:640px;box-sizing:border-box;position:relative;overflow:hidden;
    background:radial-gradient(ellipse at 72% 45%, #1d3a4a 0%, #131b25 42%, #0d1117 78%);
    font-family:Inter, 'DejaVu Sans', sans-serif;color:#e6edf3}
  .left{position:absolute;left:84px;top:96px;width:430px}
  .logo svg{width:96px;height:96px}
  h1{font-family:${FONT};font-weight:800;font-size:66px;letter-spacing:-1px;margin:26px 0 6px}
  .tag{font-size:31px;font-weight:700;line-height:1.18;color:#e6edf3;text-wrap:balance}
  .sub{font-size:21px;color:#9aa7b4;margin-top:16px;line-height:1.4}
  .repo{position:absolute;left:84px;bottom:70px;font-family:${FONT};font-size:24px;color:#7fd4e6}
  .shot{position:absolute;left:560px;top:118px;width:860px}
  .shot svg{display:block;width:100%;height:auto;filter:drop-shadow(0 24px 50px rgba(0,0,0,.6))}
</style></head><body><div class="card">
  <div class="left">
    <div class="logo">${logo}</div>
    <h1>awsnav</h1>
    <div class="tag">Every AWS service in your terminal</div>
    <div class="sub">EC2 to Bedrock. Every region scanned. Read-only by default.</div>
  </div>
  <div class="shot">${heroSvg}</div>
  <div class="repo">redasgard/awsnav</div>
</div></body></html>`, { waitUntil: 'load' });
await p.evaluate(() => document.fonts.ready);
await p.locator('.card').screenshot({ path: path.join(docs, 'social-preview.png') });
console.log('wrote', path.join(docs, 'social-preview.png'));
await browser.close();
