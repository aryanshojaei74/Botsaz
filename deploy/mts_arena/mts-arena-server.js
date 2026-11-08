'use strict';
// MTS Arena — وب‌سرور مینی‌اپ + سرور بازی آنلاین (بدون وابستگی خارجی؛ Node.js 18 به بالا)
// - index.html کنار همین فایل (یا در پوشه‌ی public) نمایش داده می‌شود.
// - بیلیارد: ارتباط با HTTP روی /api (بدون نیاز به WebSocket).
// - تخته‌نرد: ارتباط با WebSocket روی /ws.
// - هویت بازیکنان با initData تلگرام و توکن ربات (BOT_TOKEN) بررسی می‌شود.

const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

const PORT = Number(process.env.PORT) || 3000;
const BOT_TOKEN = process.env.BOT_TOKEN || process.env.TELEGRAM_BOT_TOKEN || '';
const PUBLIC_DIR = fs.existsSync(path.join(__dirname, 'public', 'index.html')) ? path.join(__dirname, 'public') : __dirname;
const MAX_MSG = 32 * 1024;
const MAX_BODY = 256 * 1024;
const AUTH_MAX_AGE_SEC = 24 * 3600;
const MAX_MULTI_PLAYERS = 8;
const POLL_WAIT_MS = 20000;          // درخواست poll تا ۲۰ ثانیه منتظر پیام می‌ماند
const SESSION_IDLE_MS = 90000;       // نشستی که ۹۰ ثانیه poll نشده، خارج می‌شود
const WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11';
const CSP = "default-src 'self'; script-src 'self' 'unsafe-inline' https://telegram.org; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com data:; img-src 'self' data: blob: https:; media-src 'self' blob:; connect-src 'self' wss: https:; frame-src 'self'; worker-src 'self' blob:; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors https://web.telegram.org https://*.telegram.org https://t.me";

if (!BOT_TOKEN) console.warn('[mts-arena] BOT_TOKEN تنظیم نشده است؛ بازی آنلاین کار نمی‌کند.');

const MIME = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8',
  '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.svg': 'image/svg+xml', '.json': 'application/json', '.txt': 'text/plain; charset=utf-8',
};

const conns = new Set();              // همه‌ی اتصال‌ها: WebSocket (Conn) و نشست HTTP (HttpSession)
const sessions = new Map();           // توکن HTTP -> نشست

// ---------- وب‌سرور ----------
function handleHttp(req, res) {
  let pathname;
  try { pathname = new URL(req.url, 'http://x').pathname; } catch (_) { res.writeHead(400); return res.end(); }
  if (pathname.startsWith('/api/')) return handleApi(req, res, pathname);
  if (pathname === '/healthz') {
    res.writeHead(200, { 'content-type': 'text/plain; charset=utf-8' });
    return res.end('MTS Arena relay: OK');
  }
  let rel;
  try { rel = decodeURIComponent(pathname).replace(/^\/+/, ''); } catch (_) { res.writeHead(400); return res.end(); }
  if (rel === '') rel = 'index.html';
  if (rel.split('/').some(p => p.startsWith('.'))) { res.writeHead(404); return res.end(); }
  const file = path.resolve(PUBLIC_DIR, rel);
  if (!file.startsWith(PUBLIC_DIR + path.sep) || file === __filename) { res.writeHead(404); return res.end(); }
  fs.readFile(file, (err, data) => {
    if (err) { res.writeHead(404, { 'content-type': 'text/plain; charset=utf-8' }); return res.end('Not found'); }
    res.writeHead(200, {
      'content-type': MIME[path.extname(file).toLowerCase()] || 'application/octet-stream',
      'x-content-type-options': 'nosniff',
      'referrer-policy': 'strict-origin-when-cross-origin',
      'permissions-policy': 'microphone=(self), camera=(), geolocation=(), payment=()',
      'cross-origin-opener-policy': 'same-origin-allow-popups',
      'content-security-policy': CSP,
    });
    res.end(data);
  });
}

function json(res, status, obj) {
  if (res.writableEnded) return;
  res.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' });
  res.end(JSON.stringify(obj));
}

function readBody(req, cb) {
  let size = 0, done = false;
  const chunks = [];
  req.on('data', c => {
    if (done) return;
    size += c.length;
    if (size > MAX_BODY) { done = true; cb(new Error('too-large')); req.destroy(); return; }
    chunks.push(c);
  });
  req.on('end', () => { if (!done) { done = true; cb(null, Buffer.concat(chunks).toString('utf8')); } });
  req.on('error', () => { if (!done) { done = true; cb(new Error('read-error')); } });
}

function handleApi(req, res, pathname) {
  if (req.method !== 'POST') return json(res, 405, { error: 'method' });
  readBody(req, (err, body) => {
    if (err) return json(res, 413, { error: 'too-large' });
    let data;
    try { data = body ? JSON.parse(body) : {}; } catch (_) { return json(res, 400, { error: 'bad-json' }); }
    if (!data || typeof data !== 'object') return json(res, 400, { error: 'bad-json' });
    if (pathname === '/api/hello') return apiHello(res, data);
    if (pathname === '/api/send') return apiSend(res, data);
    if (pathname === '/api/poll') return apiPoll(res, data);
    json(res, 404, { error: 'not-found' });
  });
}

function apiHello(res, data) {
  if (!BOT_TOKEN) return json(res, 503, { error: 'no-token' });
  const user = verifyInitData(String(data.initData || ''));
  if (!user) return json(res, 401, { error: 'unauthorized' });
  for (const o of [...conns]) {
    if (o.att && o.att.uid === user.uid) { leave(o); o.close(4000, 'replaced'); }
  }
  const token = crypto.randomBytes(24).toString('base64url');
  const s = new HttpSession(token, user);
  conns.add(s);
  sessions.set(token, s);
  json(res, 200, { t: 'hello-ok', id: user.uid, name: user.name, token });
}

function apiSend(res, data) {
  const s = sessions.get(String(data.token || ''));
  if (!s || !s.open) return json(res, 401, { error: 'session' });
  s.lastSeen = Date.now();
  const msgs = Array.isArray(data.msgs) ? data.msgs.slice(0, 100) : [];
  for (const m of msgs) {
    if (m && typeof m === 'object') onClientMessage(s, JSON.stringify(m));
  }
  json(res, 200, { ok: true });
}

function apiPoll(res, data) {
  const s = sessions.get(String(data.token || ''));
  if (!s || !s.open) return json(res, 401, { error: 'session' });
  s.lastSeen = Date.now();
  if (s.inbox.length) return json(res, 200, { msgs: s.inbox.splice(0).map(x => JSON.parse(x)) });
  if (s.waiter) { const w = s.waiter; s.waiter = null; clearTimeout(w.timer); w.done([]); }
  const waiter = { timer: null, done: msgs => json(res, 200, { msgs: msgs.map(x => JSON.parse(x)) }) };
  waiter.timer = setTimeout(() => {
    if (s.waiter === waiter) { s.waiter = null; waiter.done([]); }
  }, POLL_WAIT_MS);
  s.waiter = waiter;
  res.on('close', () => {
    if (s.waiter === waiter) { s.waiter = null; clearTimeout(waiter.timer); }
  });
}

// ---------- نشست HTTP (بیلیارد) ----------
class HttpSession {
  constructor(token, user) {
    this.token = token;
    this.http = true;
    this.open = true;
    this.att = { uid: user.uid, name: user.name, code: null, host: false, mode: null };
    this.inbox = [];
    this.waiter = null;
    this.lastSeen = Date.now();
  }
  send(text) {
    if (!this.open) return;
    this.inbox.push(text);
    if (this.inbox.length > 500) this.inbox.splice(0, this.inbox.length - 500);
    const w = this.waiter;
    if (w) { this.waiter = null; clearTimeout(w.timer); w.done(this.inbox.splice(0)); }
  }
  close() { dropSession(this); }
}

function dropSession(s) {
  if (!conns.has(s)) return;
  leave(s);
  conns.delete(s);
  s.open = false;
  sessions.delete(s.token);
  const w = s.waiter;
  if (w) { s.waiter = null; clearTimeout(w.timer); w.done([]); }
}

setInterval(() => {
  const now = Date.now();
  for (const s of [...sessions.values()]) {
    if (!s.waiter && now - s.lastSeen > SESSION_IDLE_MS) dropSession(s);
  }
}, 15000).unref();

// ---------- WebSocket (تخته‌نرد؛ فریم‌بندی RFC 6455) ----------
const server = http.createServer(handleHttp);

server.on('upgrade', (req, socket) => {
  let pathname = '';
  try { pathname = new URL(req.url, 'http://x').pathname; } catch (_) {}
  const key = req.headers['sec-websocket-key'];
  if (pathname !== '/ws' || !key || String(req.headers.upgrade || '').toLowerCase() !== 'websocket') {
    socket.write('HTTP/1.1 426 Upgrade Required\r\nConnection: close\r\n\r\n');
    return socket.destroy();
  }
  const accept = crypto.createHash('sha1').update(key + WS_GUID).digest('base64');
  socket.write('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + '\r\n\r\n');
  socket.setNoDelay(true);
  conns.add(new Conn(socket));
});

class Conn {
  constructor(socket) {
    this.socket = socket;
    this.open = true;
    this.att = null;
    this.buf = Buffer.alloc(0);
    this.frags = null;
    socket.on('data', d => { this.buf = Buffer.concat([this.buf, d]); while (this.parse()); });
    socket.on('close', () => this.onClose());
    socket.on('error', () => this.onClose());
  }
  send(text) { this.writeFrame(0x1, Buffer.from(text, 'utf8')); }
  writeFrame(op, payload) {
    if (!this.open) return;
    const len = payload.length;
    let header;
    if (len < 126) { header = Buffer.from([0x80 | op, len]); }
    else if (len < 65536) { header = Buffer.alloc(4); header[0] = 0x80 | op; header[1] = 126; header.writeUInt16BE(len, 2); }
    else { header = Buffer.alloc(10); header[0] = 0x80 | op; header[1] = 127; header.writeBigUInt64BE(BigInt(len), 2); }
    try { this.socket.write(Buffer.concat([header, payload])); } catch (_) {}
  }
  close(code, reason) {
    if (!this.open) return;
    const r = Buffer.from(reason || '', 'utf8');
    const p = Buffer.alloc(2 + r.length);
    p.writeUInt16BE(code, 0); r.copy(p, 2);
    this.writeFrame(0x8, p);
    this.open = false;
    try { this.socket.end(); } catch (_) {}
    this.onClose();
  }
  parse() {
    const b = this.buf;
    if (b.length < 2) return false;
    const fin = (b[0] & 0x80) !== 0;
    const op = b[0] & 0x0f;
    const masked = (b[1] & 0x80) !== 0;
    let len = b[1] & 0x7f;
    let off = 2;
    if (len === 126) { if (b.length < 4) return false; len = b.readUInt16BE(2); off = 4; }
    else if (len === 127) {
      if (b.length < 10) return false;
      const big = b.readBigUInt64BE(2);
      if (big > BigInt(MAX_MSG)) { this.close(1009, 'too big'); return false; }
      len = Number(big); off = 10;
    }
    if (len > MAX_MSG) { this.close(1009, 'too big'); return false; }
    const maskLen = masked ? 4 : 0;
    const need = off + maskLen + len;
    if (b.length < need) return false;
    let payload = b.subarray(off + maskLen, need);
    if (masked) {
      const mask = b.subarray(off, off + 4);
      payload = Buffer.from(payload);
      for (let i = 0; i < payload.length; i++) payload[i] ^= mask[i & 3];
    }
    this.buf = b.subarray(need);

    if (op === 0x8) { this.close(1000, ''); return false; }
    if (op === 0x9) { this.writeFrame(0xA, payload); return true; }
    if (op === 0xA) return true;

    let msgOp = op;
    let data = payload;
    if (op === 0x1 || op === 0x2) {
      if (!fin) { this.frags = { op, parts: [payload], size: payload.length }; return true; }
    } else if (op === 0x0) {
      if (!this.frags) return true;
      this.frags.parts.push(payload);
      this.frags.size += payload.length;
      if (this.frags.size > MAX_MSG) { this.frags = null; this.close(1009, 'too big'); return false; }
      if (!fin) return true;
      msgOp = this.frags.op;
      data = Buffer.concat(this.frags.parts);
      this.frags = null;
    } else {
      return true;
    }
    if (msgOp === 0x1) onClientMessage(this, data.toString('utf8'));
    return true;
  }
  onClose() {
    if (!conns.has(this)) return;
    this.open = false;
    conns.delete(this);
    leave(this);
  }
}

// ---------- منطق اتاق‌ها (برای WebSocket و HTTP یکسان است) ----------
function onClientMessage(ws, raw) {
  if (raw.length > MAX_MSG) return;
  let m;
  try { m = JSON.parse(raw); } catch (_) { return; }
  if (!m || typeof m !== 'object') return;
  if (m.t === 'hello') { if (!ws.http) hello(ws, m.initData); return; }
  if (!ws.att || !ws.att.uid) return;
  switch (m.t) {
    case 'create': return create(ws, m);
    case 'join': return join(ws, String(m.code || ''));
    case 'relay': return relay(ws, m);
    case 'leave': return leave(ws);
  }
}

function sendJSON(ws, obj) { ws.send(JSON.stringify(obj)); }

function members(code) {
  return [...conns].filter(c => c.open && c.att && c.att.code === code);
}

function hello(ws, initData) {
  const user = verifyInitData(String(initData || ''));
  if (!user) return ws.close(4401, 'unauthorized');
  for (const o of [...conns]) {
    if (o !== ws && o.att && o.att.uid === user.uid) { leave(o); o.close(4000, 'replaced'); }
  }
  ws.att = { uid: user.uid, name: user.name, code: null, host: false, mode: null };
  sendJSON(ws, { t: 'hello-ok', id: user.uid, name: user.name });
}

function create(ws, m) {
  if (ws.att.code) return sendJSON(ws, { t: 'error', error: 'busy' });
  const multi = m.mode === 'multi';            // بیلیارد: کد ۴ رقمی و تا ۸ نفر
  const lo = multi ? 1000 : 100000;            // تخته‌نرد: کد ۶ رقمی و ۲ نفر
  const span = multi ? 9000 : 900000;
  let code;
  do { code = String(lo + crypto.randomInt(span)); } while (members(code).length > 0);
  ws.att = { ...ws.att, code, host: true, mode: multi ? 'multi' : 'pair' };
  sendJSON(ws, { t: 'room', code, host: ws.att.uid, players: [{ id: ws.att.uid, name: ws.att.name }] });
}

function join(ws, rawCode) {
  if (ws.att.code) return sendJSON(ws, { t: 'error', error: 'busy' });
  const code = normalizeDigits(rawCode).replace(/\D/g, '');
  if (code.length !== 4 && code.length !== 6) return sendJSON(ws, { t: 'error', error: 'not-found' });
  const room = members(code);
  if (room.length === 0) return sendJSON(ws, { t: 'error', error: 'not-found' });
  const host = room.find(c => c.att.host);
  if (!host) return sendJSON(ws, { t: 'error', error: 'not-found' });
  const ha = host.att;
  if (ha.uid === ws.att.uid) return sendJSON(ws, { t: 'error', error: 'busy' });
  const multi = ha.mode === 'multi';
  if (multi ? room.length >= MAX_MULTI_PLAYERS : room.length >= 2) return sendJSON(ws, { t: 'error', error: 'full' });

  ws.att = { ...ws.att, code, host: false, mode: ha.mode };
  if (multi) {
    const players = members(code).map(c => ({ id: c.att.uid, name: c.att.name }));
    return sendJSON(ws, { t: 'room', code, host: ha.uid, players });
  }
  sendJSON(ws, { t: 'room', code, host: ha.uid, players: [{ id: ha.uid, name: ha.name }, { id: ws.att.uid, name: ws.att.name }] });
  sendJSON(host, { t: 'peer-joined', id: ws.att.uid, name: ws.att.name });
}

function relay(ws, m) {
  if (!ws.att.code) return;
  const data = m.data && typeof m.data === 'object' ? m.data : {};
  const kind = String(m.kind || '').slice(0, 20);
  const others = members(ws.att.code).filter(c => c !== ws);
  const targets = ws.att.mode === 'multi' ? others : others.slice(0, 1);
  for (const p of targets) sendJSON(p, { t: 'relay', from: ws.att.uid, kind, data });
}

function leave(ws) {
  const att = ws.att;
  if (!att || !att.code) return;
  const code = att.code;
  ws.att = { ...att, code: null, host: false, mode: null };
  for (const s of members(code)) sendJSON(s, { t: 'peer-left', id: att.uid });
}

// ---------- بررسی initData تلگرام (طبق مستندات رسمی Mini Apps) ----------
function verifyInitData(initData) {
  if (!BOT_TOKEN || !initData) return null;
  const params = new URLSearchParams(initData);
  const hash = params.get('hash');
  if (!hash) return null;
  params.delete('hash');
  const check = [...params.entries()]
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([k, v]) => `${k}=${v}`)
    .join('\n');
  const secret = crypto.createHmac('sha256', 'WebAppData').update(BOT_TOKEN).digest();
  const expected = crypto.createHmac('sha256', secret).update(check).digest('hex');
  const a = Buffer.from(expected), bb = Buffer.from(hash.toLowerCase());
  if (a.length !== bb.length || !crypto.timingSafeEqual(a, bb)) return null;
  const authDate = Number(params.get('auth_date') || 0);
  if (!authDate || Math.floor(Date.now() / 1000) - authDate > AUTH_MAX_AGE_SEC) return null;
  let user;
  try { user = JSON.parse(params.get('user') || 'null'); } catch (_) { return null; }
  if (!user || user.id === undefined) return null;
  const name = [user.first_name, user.last_name].filter(Boolean).join(' ').slice(0, 64) || 'بازیکن';
  return { uid: String(user.id), name };
}

function normalizeDigits(s) {
  const fa = '۰۱۲۳۴۵۶۷۸۹', ar = '٠١٢٣٤٥٦٧٨٩';
  return String(s).replace(/[۰-۹٠-٩]/g, ch => {
    const i = fa.indexOf(ch); if (i >= 0) return String(i);
    return String(ar.indexOf(ch));
  });
}

// ping دوره‌ای برای اتصال‌های WebSocket تا پراکسی‌ها آن‌ها را نبندند
setInterval(() => { for (const c of conns) if (c.open && c.writeFrame) c.writeFrame(0x9, Buffer.alloc(0)); }, 25000).unref();

if (require.main === module) {
  server.listen(PORT, '0.0.0.0', () => console.log('[mts-arena] listening on port ' + PORT));
}
module.exports = { server };
