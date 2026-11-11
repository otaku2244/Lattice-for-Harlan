import { readFileSync } from 'node:fs';
import { scryptSync, timingSafeEqual } from 'node:crypto';

// 登录失败限制：同一来源在时间窗内失败过多则暂时拒绝，防止面板密码被在线爆破。
// 浏览器在收到 401 后会对页面内每个静态资源重复携带同一错误凭据，
// 因此对短时间内的连续失败做去抖动，避免一次页面加载就把自己锁住。
const FAIL_LIMIT = Number(process.env.LATTICE_AUTH_FAIL_LIMIT || 20);
const FAIL_WINDOW_MS = Number(process.env.LATTICE_AUTH_FAIL_WINDOW_MS || 10 * 60 * 1000);
const LOCK_MS = Number(process.env.LATTICE_AUTH_LOCK_MS || 10 * 60 * 1000);
const DEBOUNCE_MS = Number(process.env.LATTICE_AUTH_DEBOUNCE_MS || 3000);

const attempts = new Map();

const PRIVATE = /^(127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|::1$|::ffff:(127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.))/;

function clientKey(request) {
  const address = request.socket?.remoteAddress || '';
  if (PRIVATE.test(address)) {
    const forwarded = String(request.headers['x-forwarded-for'] || '').split(',')[0].trim();
    if (forwarded) return `xff:${forwarded}`;
  }
  return address || 'unknown';
}

setInterval(() => {
  const now = Date.now();
  for (const [key, entry] of attempts) {
    const stale = Math.max(FAIL_WINDOW_MS, LOCK_MS);
    if ((entry.lockedUntil || entry.last) + stale < now) attempts.delete(key);
  }
}, 60_000).unref();

export function authenticate(header, record) {
  if (!header?.startsWith('Basic ')) return false;
  const text = Buffer.from(header.slice(6), 'base64').toString('utf8');
  const colon = text.indexOf(':');
  if (colon < 0 || text.slice(0, colon) !== record.username) return false;
  const expected = Buffer.from(record.hash, 'hex');
  const actual = scryptSync(text.slice(colon + 1), Buffer.from(record.salt, 'hex'), 32);
  return expected.length === actual.length && timingSafeEqual(expected, actual);
}

function recordFailure(key, now) {
  let entry = attempts.get(key);
  if (!entry || entry.last + FAIL_WINDOW_MS < now) entry = { count: 0, last: 0, lockedUntil: 0 };
  // 同一来源在去抖窗口内的连续失败合并为一次，避免浏览器批量资源请求触发锁定
  if (entry.last && now - entry.last < DEBOUNCE_MS) { attempts.set(key, entry); return; }
  entry.count += 1;
  entry.last = now;
  if (entry.count >= FAIL_LIMIT) { entry.lockedUntil = now + LOCK_MS; entry.count = 0; }
  attempts.set(key, entry);
}

export function requireWebAuth(request, response, filename) {
  const key = clientKey(request);
  const now = Date.now();
  const entry = attempts.get(key);

  if (entry?.lockedUntil > now) {
    const wait = Math.ceil((entry.lockedUntil - now) / 1000);
    response.writeHead(429, {
      'Retry-After': String(wait),
      'Cache-Control': 'no-store',
      'Content-Type': 'text/plain; charset=utf-8',
      'X-Content-Type-Options': 'nosniff',
    });
    response.end(`Too many failed sign-in attempts. Retry in ${wait}s.`);
    return false;
  }

  let record;
  try {
    record = JSON.parse(readFileSync(filename, 'utf8'));
  } catch {
    response.writeHead(503, { 'Content-Type': 'text/plain; charset=utf-8' });
    response.end('页面鉴权尚未配置，请运行管理脚本的选项 0。');
    return false;
  }

  if (authenticate(request.headers.authorization, record)) {
    attempts.delete(key);
    return true;
  }

  recordFailure(key, now);
  response.writeHead(401, { 'WWW-Authenticate': 'Basic realm="Lattice", charset="UTF-8"', 'Cache-Control': 'no-store',
    'Content-Type': 'text/plain; charset=utf-8', 'X-Content-Type-Options': 'nosniff' });
  response.end('Authentication required');
  return false;
}
