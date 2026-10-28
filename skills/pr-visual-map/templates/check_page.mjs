// Interaction check for a generated index.html, via the Chrome DevTools protocol (Node 22+, no deps).
// Usage: node check_page.mjs <index.html> <blockId> [groupId]
// Checks: no JS errors, no network requests besides the page, click selects, evidence chip jumps to the
// line, "Mark reviewed" persists across reload, "Copy question" succeeds, Esc goes up, group drill-down.
import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const [page, blockId, groupId] = process.argv.slice(2);
if (!page || !blockId) { console.error('usage: node check_page.mjs <index.html> <blockId> [groupId]'); process.exit(2); }
const url = 'file://' + resolve(page);
const CHROME = process.env.CHROME || ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/Applications/Chromium.app/Contents/MacOS/Chromium', '/usr/bin/google-chrome', '/usr/bin/google-chrome-stable',
  '/usr/bin/chromium', '/usr/bin/chromium-browser'].find(p => existsSync(p));
if (!CHROME) { console.error('Chrome or Chromium not found; set CHROME=/path/to/chrome'); process.exit(2); }
const port = 9300 + Math.floor(Math.random() * 500);
const prof = mkdtempSync(join(tmpdir(), 'prmap-chrome-'));
const chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', `--remote-debugging-port=${port}`, `--user-data-dir=${prof}`,
  '--window-size=1440,900', 'about:blank'], { stdio: 'ignore' });
const sleep = ms => new Promise(r => setTimeout(r, ms));
const results = [];
const check = (name, ok, detail = '') => results.push({ name, ok: !!ok, detail });

let ws, id = 0;
const pending = new Map(), errors = [], requests = [];
const send = (method, params = {}) => new Promise(res => { const i = ++id; pending.set(i, res); ws.send(JSON.stringify({ id: i, method, params })); });
const ev = async expr => (await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true, userGesture: true })).result?.result?.value;

try {
  let target;
  for (let i = 0; i < 50 && !target; i++) {
    await sleep(150);
    try { target = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find(t => t.type === 'page'); } catch { /* not up yet */ }
  }
  ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise(r => ws.addEventListener('open', r));
  ws.addEventListener('message', m => {
    const d = JSON.parse(m.data);
    if (d.id && pending.has(d.id)) { pending.get(d.id)(d); pending.delete(d.id); }
    if (d.method === 'Runtime.exceptionThrown') errors.push(d.params.exceptionDetails.exception?.description || d.params.exceptionDetails.text);
    if (d.method === 'Runtime.consoleAPICalled' && d.params.type === 'error') errors.push(d.params.args.map(a => a.value).join(' '));
    if (d.method === 'Network.requestWillBeSent') requests.push(d.params.request.url);
  });
  await send('Runtime.enable'); await send('Network.enable'); await send('Page.enable');
  await send('Emulation.setFocusEmulationEnabled', { enabled: true });
  await send('Browser.grantPermissions', { permissions: ['clipboardReadWrite', 'clipboardSanitizedWrite'] }).catch(() => {});
  await send('Page.navigate', { url }); await sleep(1200);

  check('page renders blocks', await ev(`document.querySelectorAll('#map .node').length`) > 0);
  await ev(`localStorage.clear()`);
  await ev(`document.querySelector('.node[data-id="${blockId}"]').dispatchEvent(new MouseEvent('click', {bubbles: true}))`);
  await sleep(200);
  const label = await ev(`document.querySelector('#panel h2').textContent`);
  check('click selects the block', label && label !== 'What this PR does', label);
  check('selected block has a halo', await ev(`!!document.querySelector('.node.sel[data-id="${blockId}"]')`));
  const evid = await ev(`(document.querySelector('#panel .ev') || {}).dataset?.ev`);
  await ev(`document.querySelector('#panel .ev').click()`); await sleep(400);
  const hit = await ev(`(() => { const h = document.querySelector('pre.diff span.hit, .hh.hit'); return h ? (h.dataset.ln || h.textContent.slice(0, 40)) : null; })()`);
  check('evidence chip opens the diff at the line', !!hit, `${evid} -> ${hit}`);
  await ev(`document.getElementById('rev').click()`); await sleep(150);
  check('mark reviewed updates the counter', /^reviewed 1\//.test(await ev(`document.getElementById('revcount').textContent`)));
  await ev(`document.getElementById('copy').click()`); await sleep(400);
  const copied = await ev(`document.getElementById('copy').textContent`);
  const clip = await ev(`navigator.clipboard.readText().catch(() => '')`);
  const fallback = !!(await ev(`document.querySelector('textarea.copy') && 1`));
  check('copy question for agent', copied === 'Copied ✓' || fallback,
    `${copied === 'Copied ✓' ? 'clipboard' : 'manual-copy fallback shown'}; clipboard ${clip ? clip.length + ' chars, starts: ' + JSON.stringify(clip.slice(0, 70)) : 'not readable'}`);
  await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 });
  await sleep(200);
  const back = await ev(`document.querySelector('#panel h2').textContent`);
  check('Esc goes back up', groupId ? true : back === 'What this PR does', back);
  await send('Page.reload'); await sleep(1200);
  check('reviewed state survives reload', /^reviewed 1\//.test(await ev(`document.getElementById('revcount').textContent`)));
  if (groupId) {
    await ev(`document.querySelector('.node[data-id="${groupId}"]').dispatchEvent(new MouseEvent('click', {bubbles: true}))`);
    await sleep(250);
    const crumb = await ev(`document.getElementById('crumbs').textContent`);
    const inner = await ev(`document.querySelectorAll('#map .node').length`);
    check('group click drills down', crumb.includes('›') && inner > 0, `${crumb.trim()} · ${inner} blocks`);
    await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 });
    await sleep(250);
    check('Esc leaves the group', !(await ev(`document.getElementById('crumbs').textContent`)).includes('›'));
  }
  const external = requests.filter(u => !u.startsWith('file://') && !u.startsWith('data:'));
  check('no network requests', external.length === 0, external.slice(0, 3).join(', '));
  check('no JS errors', errors.length === 0, errors.slice(0, 3).join(' | '));
} catch (e) {
  check('harness', false, String(e));
} finally {
  try { ws?.close(); } catch { /* ignore */ }
  chrome.kill();
  await sleep(300);
  rmSync(prof, { recursive: true, force: true });
}
for (const r of results) console.log(`${r.ok ? '✓' : '✗'} ${r.name}${r.detail ? ' — ' + r.detail : ''}`);
process.exit(results.every(r => r.ok) ? 0 : 1);
