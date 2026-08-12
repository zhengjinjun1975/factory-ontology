// auth.js — 企业用户登录会话体系（多租户 = 多用户）
// 用户存储: web/users.json。密码 scrypt 哈希加盐。登录签发不透明随机 token（内存会话，含过期）。
// 每个企业用户绑定唯一 kb（单企业唯一性），数据隔离复用后端多租户内核。
import { scryptSync, randomBytes, timingSafeEqual } from 'crypto';
import { readFileSync, writeFileSync, existsSync } from 'fs';
import { join, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
// 用户存储文件（web/users.json，相对 web/server -> ../users.json）
const USERS_FILE = join(__dirname, '..', 'users.json');

// 会话有效期（毫秒），默认 12 小时
const SESSION_TTL = 12 * 3600 * 1000;

// 内存会话表：token -> {username, expires}
const sessions = new Map();

// ── 用户文件读写 ─────────────────────────────────────────────
function loadUsers() {
  try {
    if (existsSync(USERS_FILE)) {
      const d = JSON.parse(readFileSync(USERS_FILE, 'utf-8'));
      return (d && d.users) || {};
    }
  } catch (e) { /* 损坏视为空 */ }
  return {};
}

function saveUsers(users) {
  writeFileSync(USERS_FILE, JSON.stringify({ users }, null, 2), 'utf-8');
}

// ── 密码哈希（scrypt 加盐）────────────────────────────────────
function hashPassword(password, salt) {
  return scryptSync(String(password), salt, 64).toString('hex');
}

function verifyPassword(password, salt, expectedHash) {
  if (!salt || !expectedHash) return false;
  const actual = scryptSync(String(password), salt, 64);
  const expected = Buffer.from(expectedHash, 'hex');
  if (actual.length !== expected.length) return false;
  return timingSafeEqual(actual, expected);
}

// ── 公开用户对象（剔除敏感字段）────────────────────────────────
export function publicUser(u) {
  if (!u) return null;
  return {
    username: u.username,
    enterpriseName: u.enterpriseName || '',
    logo: u.logo || '',
    industry: u.industry || '',
    kb: u.kb || '',
    onboarded: !!u.onboarded,
  };
}

// ── 企业用户 CRUD ─────────────────────────────────────────────
/** 创建企业用户。kb 缺省自动生成 ent_<username>（单企业唯一命名空间）。 */
export function createUser({ username, password, enterpriseName = '', logo = '', industry = '', kb = '', onboarded = false }) {
  const name = String(username || '').trim();
  if (!name) return { ok: false, error: '用户名必填' };
  if (!password) return { ok: false, error: '密码必填' };
  const users = loadUsers();
  if (users[name]) return { ok: false, error: '用户名已存在' };
  const salt = randomBytes(16).toString('hex');
  const user = {
    username: name,
    salt,
    hash: hashPassword(password, salt),
    enterpriseName: String(enterpriseName || '').trim().slice(0, 50),
    logo: String(logo || '').trim().slice(0, 500),
    industry: String(industry || '').trim().slice(0, 30),
    kb: kb || 'ent_' + name.replace(/[^A-Za-z0-9_-]/g, '_'),
    onboarded: !!onboarded,
    created: Date.now(),
  };
  users[name] = user;
  saveUsers(users);
  return { ok: true, user: publicUser(user) };
}

/** 取用户（私有，含哈希）。 */
function getUserByUsername(username) {
  const users = loadUsers();
  return users[username] || null;
}

/** 读取企业用户（公开）。 */
export function getUser(username) {
  return publicUser(getUserByUsername(username));
}

/** 更新企业用户字段（enterpriseName/logo/industry/onboarded/kb）。 */
export function updateUser(username, patch) {
  const users = loadUsers();
  const u = users[username];
  if (!u) return { ok: false, error: '用户不存在' };
  if (patch.enterpriseName !== undefined) u.enterpriseName = String(patch.enterpriseName).trim().slice(0, 50);
  if (patch.logo !== undefined) u.logo = String(patch.logo).trim().slice(0, 500);
  if (patch.industry !== undefined) u.industry = String(patch.industry).trim().slice(0, 30);
  if (patch.kb !== undefined && patch.kb) u.kb = String(patch.kb).trim();
  if (patch.onboarded !== undefined) u.onboarded = !!patch.onboarded;
  users[username] = u;
  saveUsers(users);
  return { ok: true, user: publicUser(u) };
}

/** 删除企业用户。 */
export function deleteUser(username) {
  const users = loadUsers();
  if (!users[username]) return { ok: false, error: '用户不存在' };
  delete users[username];
  saveUsers(users);
  return { ok: true };
}

// ── 登录/会话 ─────────────────────────────────────────────────
/** 校验用户名密码，成功签发会话 token。 */
export function login(username, password) {
  const u = getUserByUsername(String(username || '').trim());
  if (!u) return { ok: false, error: '用户名或密码错误' };
  if (!verifyPassword(password, u.salt, u.hash)) {
    return { ok: false, error: '用户名或密码错误' };
  }
  const token = randomBytes(32).toString('hex');
  sessions.set(token, { username: u.username, expires: Date.now() + SESSION_TTL });
  return { ok: true, token, user: publicUser(u) };
}

/** 注销（使 token 失效）。 */
export function logout(token) {
  if (token) sessions.delete(token);
  return { ok: true };
}

/** 解析 token → 公开用户。会话失效/不存在返回 null。 */
export function resolveUser(token) {
  if (!token) return null;
  const s = sessions.get(token);
  if (!s) return null;
  if (s.expires < Date.now()) { sessions.delete(token); return null; }
  return publicUser(getUserByUsername(s.username));
}

/** 会话健康检查（供 /api/auth/me 用，返回当前用户公开对象 + 会话剩余）。 */
export function me(token) {
  const u = resolveUser(token);
  if (!u) return { ok: false, error: '未登录或会话已失效', unauthenticated: true };
  return { ok: true, user: u };
}

// ── 首次启动种子用户（仅当 users.json 不存在或为空）────────────
export function seedUsersIfEmpty() {
  const users = loadUsers();
  if (Object.keys(users).length > 0) return;
  // 预置两个演示企业用户：
  //  - admin / admin123：已 onboard，绑定额外阀 kb=valve（复用现成 data_valve 示例，登录即见企业系统）
  //  - demo  / demo123：新企业未配置，用于演示引导 onboarding
  createUser({ username: 'admin', password: 'admin123', enterpriseName: '示例制造公司', logo: '🏭', industry: '阀门制造', kb: 'valve', onboarded: true });
  createUser({ username: 'demo', password: 'demo123', enterpriseName: '示例企业B', logo: '🌱', industry: '', kb: 'ent_demo', onboarded: false });
}
