// Browser smoke test of the main user flows. Needs a running server in development mode (mails go to its log).
// Usage: BASE_URL=http://localhost:8000/ SERVER_LOG=server.log ADMIN_EMAIL=.. ADMIN_PASSWORD=.. node tests/e2e/ui-smoke.js [chromium|firefox|webkit]
const pw = require('playwright'); const crypto = require('crypto'); const fs = require('fs');
const ENGINE = process.argv[2] || 'chromium', SP = process.env.SHOT_DIR || '.', FULL = !!process.env.SHOT_DIR;
const LOG = process.env.SERVER_LOG || 'server.log', ADMIN = process.env.ADMIN_EMAIL || 'admin@example.com', ADMIN_PW = process.env.ADMIN_PASSWORD || 'Admin-pass-2026';
const RUN = Date.now().toString(36); const URL = process.env.BASE_URL || 'http://localhost:8000/', SAMPLE = require('path').resolve(__dirname, '../../samples/good_label.png');
function totp(secret) {           // RFC 6238, for the test only
  const b32 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'; let bits = '';
  for (const c of secret.replace(/=+$/, '')) bits += b32.indexOf(c).toString(2).padStart(5, '0');
  const key = Buffer.from(bits.match(/.{8}/g).map(b => parseInt(b, 2)));
  const ctr = Buffer.alloc(8); ctr.writeBigUInt64BE(BigInt(Math.floor(Date.now() / 30000)));
  const h = crypto.createHmac('sha1', key).update(ctr).digest(); const o = h[19] & 15;
  return String(((h.readUInt32BE(o) & 0x7fffffff) % 1e6)).padStart(6, '0');
}
const lastMail = kind => { const m = [...fs.readFileSync(LOG, 'utf8').matchAll(new RegExp(`#/${kind}/([A-Za-z0-9_-]+)`, 'g'))]; return m.at(-1)[1]; };
(async () => {
  const b = await pw[ENGINE].launch(); const errors = []; const steps = [];
  const ctx = await b.newContext({ viewport: { width: 1440, height: 1000 } });
  const p = await ctx.newPage();
  p.on('pageerror', e => errors.push(String(e)));
  p.on('console', m => { if (m.type() === 'error' && !/401|Unauthorized/.test(m.text())) errors.push(m.text()); });
  const ok = s => { steps.push(s); console.log('ok:', s); };
  await p.goto(URL);
  await p.fill('#l-email', ADMIN); await p.fill('#l-pass', ADMIN_PW); await p.click('#login-form button');
  if (await p.isVisible('#terms-ok').catch(() => false) || await p.waitForSelector('#terms-ok', { timeout: 5000 }).catch(() => null)) await p.click('#terms-ok');
  await p.waitForSelector('#app-view:not(.hidden)'); ok('login');
  await p.setInputFiles('#file', SAMPLE); await p.fill('#product', 'Roasted Peanuts');
  await p.fill('#pw', '100'); await p.fill('#ph', '75'); await p.uncheck('#straighten');
  await p.click('#go'); await p.waitForSelector('.verdict', { timeout: 90000 }); ok('scan');
  await p.click('#del-scan'); await p.click('#del-scan'); await p.waitForSelector('text=deleted'); ok('delete scan');
  await p.click('nav button[data-tab=users]'); await p.waitForSelector('#org-kpis .kpi'); ok('company tab');
  await p.fill('#u-name', 'Priya Sharma'); await p.fill('#u-email', `priya-${ENGINE}-${RUN}@sunrise.in`); await p.selectOption('#u-role', 'reviewer');
  await p.click('#uform button'); await p.waitForSelector('#umsg input.copy'); ok('invite user');
  const invite = await p.inputValue('#umsg input.copy');
  await p.fill('#k-name', 'SAP ' + ENGINE); await p.click('#kform button'); await p.waitForSelector('#k-new input.copy'); ok('api key');
  const apikey = await p.inputValue('#k-new input.copy');
  await p.fill('#o-ret', '365'); await p.click('#oform button'); await p.waitForSelector('#omsg:has-text("Saved")'); ok('company settings');
  // webhook: a local receiver (the server must run with LABELCHECK_ALLOW_PRIVATE_FETCH=1 and scan workers)
  const got = []; const http = require('http');
  const recv = http.createServer((req, res) => { let d = ''; req.on('data', c => d += c); req.on('end', () => { got.push({ h: req.headers, body: d }); res.end('ok'); }); });
  await new Promise(r => recv.listen(0, '127.0.0.1', r));
  await p.fill('#wh-url', `http://127.0.0.1:${recv.address().port}/hook`); await p.fill('#wh-desc', 'ERP ' + ENGINE);
  await p.click('#whform button'); await p.waitForSelector('#wh-new input.copy'); ok('webhook added');
  const whsecret = await p.inputValue('#wh-new input.copy');
  await p.locator('#whlist [data-whtest]').last().click(); await p.waitForSelector('#wh-new:has-text("queued")');
  for (let i = 0; i < 40 && !got.length; i++) await new Promise(r => setTimeout(r, 500));
  if (!got.length) throw new Error('webhook not delivered');
  const [t, v1] = got[0].h['x-labelcheck-signature'].split(',').map(x => x.split('=')[1]);
  if (crypto.createHmac('sha256', whsecret).update(`${t}.${got[0].body}`).digest('hex') !== v1) throw new Error('bad webhook signature');
  ok('webhook delivered + signature valid');
  await p.locator('#whlist [data-whlog]').last().click(); await p.waitForSelector('#wh-deliveries .pill.s-pass'); ok('webhook delivery log');
  if (FULL) await p.screenshot({ path: `${SP}/e2e-company.png`, fullPage: true });
  await p.locator('#whlist [data-whdel]').last().click(); await p.locator('#whlist [data-whdel]').last().click(); recv.close();
  await p.click('nav button[data-tab=platform]'); await p.waitForSelector('#pf-list tr td');
  await p.fill('#pf-name', 'Alpina ' + ENGINE); await p.fill('#pf-aname', 'Rahul'); await p.fill('#pf-aemail', `rahul-${ENGINE}-${RUN}@alpina.in`);
  await p.click('#pf-form button'); await p.waitForSelector('#pf-msg input.copy'); ok('platform: new company');
  if (FULL) await p.screenshot({ path: `${SP}/e2e-platform.png`, fullPage: true });
  await p.click('nav button[data-tab=account]'); await p.click('#t-setup'); await p.waitForSelector('.qr svg'); ok('2fa QR shown');
  const secret = (await p.textContent('#t-step code')).trim();
  await p.fill('#t-new', totp(secret)); await p.click('#t-on'); await p.waitForSelector('text=Two-step login is');
  ok('2fa enabled');
  if (FULL) await p.screenshot({ path: `${SP}/e2e-account.png`, fullPage: true });
  // API key works from outside the browser session
  const r = await (await fetch(URL + 'api/scans', { headers: { 'X-API-Key': apikey } })).status; if (r !== 200) throw new Error('api key ' + r); ok('api key call');
  // invite accepted in a fresh browser
  const c2 = await b.newContext(); const q = await c2.newPage(); q.on('pageerror', e => errors.push(String(e)));
  await q.goto(invite); await q.waitForSelector('#setpw-view:not(.hidden)'); await q.waitForSelector('#setpw-info:has-text("Set a password")');
  await q.fill('#s-pass', 'Priya-pass-2026'); await q.fill('#s-pass2', 'Priya-pass-2026'); await q.click('#setpw-btn');
  await q.waitForSelector('#terms-ok'); await q.click('#terms-ok'); await q.waitForSelector('#app-view:not(.hidden)'); ok('invite accepted');
  // forgot password for the new user
  await q.click('nav button[data-tab=account]'); await q.click('#logout'); await q.waitForSelector('#login-view:not(.hidden)');
  await q.click('#forgot-link'); await q.fill('#f-email', `priya-${ENGINE}-${RUN}@sunrise.in`); await q.click('#forgot-form button');
  await q.waitForSelector('#f-msg:has-text("reset link")'); await q.waitForTimeout(300);
  await q.goto(URL + '#/reset/' + lastMail('reset')); await q.waitForSelector('#setpw-view:not(.hidden)');
  await q.fill('#s-pass', 'Priya-new-2026'); await q.fill('#s-pass2', 'Priya-new-2026'); await q.click('#setpw-btn');
  await q.waitForSelector('#l-msg:has-text("Password changed")'); ok('password reset');
  // admin login now asks for the 2FA code
  const c3 = await b.newContext(); const s = await c3.newPage();
  await s.goto(URL); await s.fill('#l-email', ADMIN); await s.fill('#l-pass', ADMIN_PW); await s.click('#login-form button');
  await s.waitForSelector('#l-totp-wrap:not(.hidden)'); ok('2fa asked at login');
  // mobile layout
  await q.setViewportSize({ width: 390, height: 844 });
  const overflow = await q.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
  // turn 2FA off again so the next browser run can set it up
  await p.fill('#t-pass', ADMIN_PW); await new Promise(r => setTimeout(r, 31000 - (Date.now() % 30000)));
  await p.fill('#t-code', totp(secret)); await p.click('#t-off'); await p.waitForSelector('#t-setup'); ok('2fa disabled');
  console.log(JSON.stringify({ ENGINE, steps: steps.length, last: steps.at(-1), errors, mobileOverflow: overflow }));
  await b.close();
})().catch(async e => { console.error('FAILED', ENGINE, e.message.split('\n').slice(0,3).join(' / ')); process.exit(1); });
