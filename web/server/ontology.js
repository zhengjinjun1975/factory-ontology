// ontology.js — 桥接 Python 本体问答套件
// 通过 child_process 调用仓库 codes/ 下的 Python 脚本
import { execFile } from 'child_process';
import os from 'os';
import { writeFileSync, mkdirSync, existsSync, readFileSync, readdirSync, statSync, rmSync, renameSync } from 'fs';
import { join, dirname, basename, extname, sep, relative } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
// 本仓库 codes/ 套件目录（相对本文件：web/server -> ../codes）
const KIT = join(__dirname, '..', '..', 'codes');
const PY = 'python';
// Web 应用自己的状态文件（不依赖套件全局 current.json，避免被测试/其他调用覆盖）
const WEB_STATE = join(__dirname, '..', 'web_state.json');

function loadWebState() {
  try {
    if (existsSync(WEB_STATE)) return JSON.parse(readFileSync(WEB_STATE, 'utf-8'));
  } catch (e) { /* 忽略 */ }
  return null;
}

function saveWebState(state) {
  try { writeFileSync(WEB_STATE, JSON.stringify(state, null, 2), 'utf-8'); } catch (e) { /* 忽略 */ }
}

// ── 企业配置（企业级品牌：企业名/logo/行业）──────────────────────────
// 独立持久化到 web/enterprise.json，与多租户 kb 解耦（企业品牌跟随当前部署实例）
const ENTERPRISE_FILE = join(__dirname, '..', 'enterprise.json');
// logo 合法形态：emoji（≤2 码元）或 图片 URL(dataURL/相对/绝对 http(s))
const LOGO_URL_RE = /^(data:image\/[a-z0-9+.-]+;base64,|\/|https?:\/\/)/i;

function loadEnterprise() {
  try {
    if (existsSync(ENTERPRISE_FILE)) return JSON.parse(readFileSync(ENTERPRISE_FILE, 'utf-8'));
  } catch (e) { /* 无配置或损坏则视为未配置 */ }
  return null;
}

/**
 * 读取企业配置（企业级品牌）。未配置时返回空字段，前端回退默认"工厂智能体"。
 * @returns {{ok:true, data:{name,logo,industry,hasConfig}}}
 */
export function getEnterprise() {
  const d = loadEnterprise();
  const name = (d && d.name) || '';
  const logo = (d && d.logo) || '';
  const industry = (d && d.industry) || '';
  return {
    ok: true,
    data: { name, logo, industry, hasConfig: !!(name || logo || industry) },
  };
}

/**
 * 保存企业配置（企业名/logo/行业），原子写回 enterprise.json。
 * @param {{name?,logo?,industry?}} cfg
 * @returns {{ok, data?:{name,logo,industry}, error?}}
 */
export function saveEnterprise(cfg) {
  try {
    const name = String((cfg && cfg.name) || '').trim().slice(0, 50);
    let logo = String((cfg && cfg.logo) || '').trim();
    const industry = String((cfg && cfg.industry) || '').trim().slice(0, 30);
    // logo 校验：非空时必须为 emoji(≤2 码元) 或 URL/dataURL 图片
    if (logo && [...logo].length > 2 && !LOGO_URL_RE.test(logo)) {
      return { ok: false, error: 'logo 需为 emoji 或图片地址(dataURL/URL)' };
    }
    const obj = { name, logo, industry };
    atomicWriteJson(ENTERPRISE_FILE, obj);
    return { ok: true, data: obj };
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  }
}

function run(cmd, args, cwd) {
  return new Promise((resolve) => {
    execFile(cmd, args, { cwd, timeout: 180000, maxBuffer: 10 * 1024 * 1024 }, (err, stdout, stderr) => {
      if (err) {
        resolve({ ok: false, error: (stderr || err.message || '').trim().split('\n').pop() });
      } else {
        resolve({ ok: true, output: stdout });
      }
    });
  });
}

// 带额外环境变量的运行（DB 密码经环境变量传 Python，不入库/不落盘）
function runWithEnv(cmd, args, cwd, env) {
  return new Promise((resolve) => {
    execFile(cmd, args, { cwd, env: { ...process.env, ...env }, timeout: 180000, maxBuffer: 10 * 1024 * 1024 }, (err, stdout, stderr) => {
      if (err) {
        resolve({ ok: false, error: (stderr || err.message || '').trim().split('\n').pop() });
      } else {
        resolve({ ok: true, output: stdout });
      }
    });
  });
}

// ── 多租户后端(api_server) REST 客户端 ────────────────────────────────
// 后端地址与 API Key 从环境变量读取(不硬编码); 不可达时降级 run.py CLI。
const API_URL = (process.env.API_URL || 'http://127.0.0.1:8000').replace(/\/+$/, '');
const API_KEY = String(process.env.API_KEY || readApiKeyConfig() || '').trim();
// 知识库注册表(kbs.json), 后端多租户共享: 每行业一个隔离数据/本体/词典
const KBS_FILE = join(KIT, 'config', 'kbs.json');

/** 从 server 侧配置文件读 API Key(可选; 优先级低于环境变量 API_KEY) */
function readApiKeyConfig() {
  try {
    const p = join(__dirname, 'apiserver.config.json');
    if (existsSync(p)) return (JSON.parse(readFileSync(p, 'utf-8')).apiKey || '').trim();
  } catch (e) { /* 无配置文件则返回空 */ }
  return '';
}

/**
 * 调 api_server 的统一 REST 封装(Node 原生 fetch):
 * - 加 X-API-Key 头(环境变量/配置, 非硬编码)
 * - 解析统一信封 {ok, data?, error?, elapsed_s}(ask 直接返回 {ok, answer, mode, kb})
 * - 网络不可达/超时 → 返回 {ok:false, offline:true}, 供调用方降级 run.py CLI
 * - timeout 按端点配置: 评测/快照/建本体等慢请求传更大值(如 60000), 默认 15000
 * @param {number} [opts.timeout] 请求超时毫秒数, 默认 15000
 * @returns {Promise<{ok, data?, error?, offline?} & object>}
 */
async function apiFetch(path, { method = 'GET', body, timeout = 15000 } = {}) {
  const headers = { 'Content-Type': 'application/json' };
  if (API_KEY) headers['X-API-Key'] = API_KEY;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeout);
  try {
    const resp = await fetch(API_URL + path, {
      method, headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: ctrl.signal,
    });
    const text = await resp.text();
    let data = null;
    try { data = JSON.parse(text); } catch (e) { /* 非 JSON 视为异常 */ }
    if (!data) return { ok: false, offline: false, error: `后端返回非 JSON (HTTP ${resp.status})` };
    return data;
  } catch (e) {
    // 网络不可达/超时: 置 offline 标志供调用方降级
    const aborted = e && e.name === 'AbortError';
    return { ok: false, offline: true, error: aborted ? '后端超时' : String((e && e.message) || e) };
  } finally {
    clearTimeout(timer);
  }
}

// ── 多租户当前激活 kb 状态 ─────────────────────────────────────────────
/** 读知识库注册表 kbs.json, 返回 {kb: 配置} 映射(空则 {}) */
function loadKbs() {
  try { return (JSON.parse(readFileSync(KBS_FILE, 'utf-8')).kbs) || {}; } catch (e) { return {}; }
}

/**
 * 取当前激活 kb：一企业一行业一数据。
 * 只认当前登录企业绑定的 kb（index.js 每次请求已按 user.kb 调 setCurrentKb 写入 web_state）。
 * 绝不兜底到注册表第一个或历史遗留 'food'——那会把 A 企业的本体错当成 B 企业。
 * web.kb 失效/为空 → 返回空串，调用方据此引导重新配置，而非错用别的企业数据。
 * @returns {string}
 */
export function getCurrentKb() {
  const web = loadWebState();
  if (web && web.kb) {
    const kbs = loadKbs();
    // 前端/登录用户绑定的 kb 若仍注册则采用（不校验会锁定失效值）
    if (kbs[web.kb]) return web.kb;
  }
  return '';
}

/** 设置当前激活 kb, 持久化到前端自身状态 */
export function setCurrentKb(kb) {
  const safe = String(kb || '').trim();
  if (!safe) return;
  const web = loadWebState() || {};
  web.kb = safe;
  saveWebState(web);
}

/**
 * 返回知识库注册表列表 + 当前激活 kb(供前端展示/切换多租户)
 * @returns {{ok, kbs?: {key,name,icon,examples,nt,lexicon}[], current?: string}}
 */
export function listKbs() {
  const kbs = loadKbs();
  const arr = Object.entries(kbs).map(([key, v]) => ({
    key, name: v.name || key, icon: v.icon || '🗂️',
    examples: Array.isArray(v.examples) ? v.examples : [],
    nt: v.nt, lexicon: v.lexicon,
  }));
  return { ok: true, kbs: arr, current: getCurrentKb() };
}

/**
 * 从词典文件提取字段映射作为 attrs(best-effort, 仅 REST 建本体后展示用)
 * 词典形如 {attr_en2cn: {field: 中文名}} → [{field, cn}]
 */
function readLexiconAttrs(lexPath) {
  try {
    if (!lexPath || !existsSync(lexPath)) return [];
    const d = JSON.parse(readFileSync(lexPath, 'utf-8'));
    const m = d.attr_en2cn || {};
    return Object.entries(m).map(([field, cn]) => ({ field, cn: String(cn) }));
  } catch (e) { return []; }
}

// Web 上传支持的文本格式(文本可直接传输; 二进制 sqlite/xlsx 走命令行 codes/ 套件)
const TEXT_EXT = ['.csv', '.json'];
// 示例数据目录（相对套件 codes/），供前端免手选文件直接体验
const EXAMPLE_DIRS = ['data_valve', 'data_chem', 'data_machining', 'data_precision',
  'data_bellows', 'data_eco', 'data_ship', 'data_seismic', 'data_food_co', 'data'];

/**
 * 扫描套件示例数据目录，返回示例文件列表
 * @returns {{name:string, path:string, size:number}[]}
 */
export function listExamples() {
  const out = [];
  for (const dir of EXAMPLE_DIRS) {
    const abs = join(KIT, dir);
    if (!existsSync(abs)) continue;
    let entries;
    try { entries = readdirSync(abs); } catch { continue; }
    for (const f of entries) {
      if (f === '.gitignore' || f === 'README' || f === 'README.md') continue;
      if (!/\.(csv|json)$/i.test(f)) continue;
      const fp = join(abs, f);
      let size = 0;
      try { size = statSync(fp).size; } catch { /* 忽略 */ }
      out.push({ name: f, path: `${dir}/${f}`, size });
    }
  }
  return out;
}

/**
 * 读取指定示例文件内容（安全：只允许九大行业示例目录 + data 下，防目录穿越）
 * @param {string} relPath 相对套件 codes/ 的路径，如 data_chem/chem_equipment.csv
 * @returns {{ok, content?, name?, size?, error?}}
 */
export function readExample(relPath) {
  const p = String(relPath || '').replace(/^[\\\\/]+/, '').replace(/\\\\/g, '/');
  // 白名单放开到九大行业示例目录(data_chem/data_machining/data_precision/data_bellows/
  // data_eco/data_ship/data_seismic/data_food_co/data_valve) + 通用 data/, 拒绝其它目录防穿越
  if (!/^(data_valve|data_chem|data_machining|data_precision|data_bellows|data_eco|data_ship|data_seismic|data_food_co|data)\/[^/]+$/.test(p)) return { ok: false, error: '非法示例路径' };
  const fp = join(KIT, p);
  try {
    const content = readFileSync(fp, 'utf-8');
    return { ok: true, content, name: p.split('/').pop(), size: content.length };
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  }
}

// 浏览时过滤的隐藏/系统目录（solo-agent-kit 同款）
const HIDDEN_DIRS = new Set([
  '$Recycle.Bin', 'System Volume Information', 'Recovery',
  'Windows', '.git', '__pycache__', 'node_modules', '.venv', '.solo', '$RECYCLE.BIN',
]);
// 浏览时只列的数据文件扩展名
const DATA_EXTS = ['.csv', '.json', '.db', '.sqlite', '.xlsx'];
// 浏览根：套件 codes/ 目录（默认根 data_valve 示例数据直接可见，可导航上级/子目录）
const BROWSE_ROOT = KIT;

/**
 * 文件浏览（学习 solo-agent-kit /api/browse）：
 * 默认根 = codes/data_valve（示例数据直接可见），dir 参数导航上级/子目录。
 * 过滤隐藏系统目录，只列数据文件。返回 {dir, parent, dirs, files}（均为相对 codes/ 路径，防穿越）。
 * @param {string} [dirArg] 相对 codes/ 的目录，如 'data_valve' 或 ''
 * @returns {{ok, dir, parent, dirs, files, error?}}
 */
export function browse(dirArg) {
  // 相对路径安全解析：统一分隔符、去掉首分隔、过滤 . 和 .. 穿越
  const raw = String(dirArg || '').replace(/\\/g, '/').replace(/^\/+/, '');
  const parts = raw.split('/').filter(s => s && s !== '.' && s !== '..');
  // 默认根 = 示例目录（无参数/非法参数时落回）
  if (parts.length === 0) parts.push('data_valve');
  let dirRel = parts.join('/');
  let abs = join(BROWSE_ROOT, dirRel);
  // 落到不存在/非目录时回退默认根
  if (!existsSync(abs) || !statSync(abs).isDirectory()) {
    dirRel = 'data_valve';
    abs = join(BROWSE_ROOT, dirRel);
  }
  const dirs = [], files = [];
  try {
    for (const name of readdirSync(abs).sort()) {
      const full = join(abs, name);
      let st;
      try { st = statSync(full); } catch { continue; }
      const relPath = relative(BROWSE_ROOT, full).split(sep).join('/');
      if (st.isDirectory()) {
        if (!HIDDEN_DIRS.has(name) && !name.startsWith('.')) {
          dirs.push({ path: relPath, name, dir: true });
        }
      } else {
        if (DATA_EXTS.includes(extname(name).toLowerCase())) {
          files.push({ path: relPath, name, dir: false });
        }
      }
    }
  } catch (e) { /* 目录不可读则返回空列表 */ }
  // parent：上级目录（根则空）
  const parentRel = relative(BROWSE_ROOT, dirname(abs)).split(sep).join('/');
  const parent = dirRel && parentRel && parentRel !== dirRel ? parentRel : '';
  return { ok: true, dir: dirRel, parent, dirs, files };
}

/**
 * 读取浏览框选中的数据文件内容（安全：仅允许 codes/ 内文本数据文件 .csv/.json，防穿越）
 * @param {string} relPath 相对 codes/ 的路径
 * @returns {{ok, content?, name?, size?, error?}}
 */
export function readDataFile(relPath) {
  const p = String(relPath || '').replace(/\\/g, '/').replace(/^\/+/, '');
  if (!p) return { ok: false, error: '路径为空' };
  const fp = join(BROWSE_ROOT, p);
  // 防穿越：解析后必须仍在 BROWSE_ROOT 内
  if (fp !== BROWSE_ROOT && !fp.startsWith(BROWSE_ROOT + sep)) {
    return { ok: false, error: '非法路径（超出允许范围）' };
  }
  try {
    if (!existsSync(fp) || !statSync(fp).isFile()) return { ok: false, error: '文件不存在' };
    const ext = extname(fp).toLowerCase();
    if (ext !== '.csv' && ext !== '.json') {
      return { ok: false, error: '仅支持 .csv/.json 文本文件建模' };
    }
    const content = readFileSync(fp, 'utf-8');
    return { ok: true, content, name: basename(fp), size: content.length };
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  }
}

/**
 * 上传并建模: 把文本格式文件(CSV/JSON)写入套件 data/, 优先调后端 POST /api/ontology/build
 * (多租户, 按 kb 建本体+词典, 更新 kbs.json); 后端不可达时降级 run.py setup。
 * @param {string} fileName 文件名 (如 equipment.csv / equipment.json)
 * @param {string} fileContent 文件内容(文本)
 * @param {string} [kb] 目标知识库(kb 名), 缺省用当前激活 kb
 * @returns {Promise<{ok, table?, attrs?, error?}>}
 */
export async function setupOntology(fileName, fileContent, kb) {
  try {
    // 安全：只允许文本格式(.csv/.json)，去掉路径分隔符防目录穿越
    const safeName = fileName.split(/[\\\\/]/).pop().replace(/[^\w.\-\u4e00-\u9fff]/g, '_');
    const ext = safeName.slice(safeName.lastIndexOf('.')).toLowerCase();
    if (!TEXT_EXT.includes(ext)) {
      return { ok: false, error: `仅支持 ${TEXT_EXT.join('/')} 文本格式；二进制 sqlite/xlsx 请在命令行用 codes/ 套件（python run.py setup <文件>）` };
    }
    const dataDir = join(KIT, 'data');
    if (!existsSync(dataDir)) mkdirSync(dataDir, { recursive: true });
    const filePath = join(dataDir, safeName);
    writeFileSync(filePath, fileContent, 'utf-8');

    kb = kb || getCurrentKb();
    // 优先走后端 REST 多租户建本体(数据源限定在 codes/data 内, 传相对路径)
    const r = await apiFetch('/api/ontology/build', {
      method: 'POST',
      body: { kb, csv_path: `data/${safeName}` },
      timeout: 60000,
    });
    if (r && r.ok && r.data) {
      const d = r.data;
      const builtKb = d.kb || kb;
      // 从词典提取字段映射展示; 记录前端激活 kb(多租户问答/统计对齐)
      const attrs = readLexiconAttrs(d.lexicon ? join(KIT, d.lexicon) : null);
      setCurrentKb(builtKb);
      saveWebState({ kb: builtKb, table: builtKb, nt: d.nt, lexicon: d.lexicon });
      return { ok: true, table: builtKb, attrs, output: JSON.stringify(r, null, 2).slice(-2000) };
    }
    if (r && r.offline) {
      // 降级: 后端不可达, 回退现有 run.py CLI(单库, 保持前端可用)
      const cli = await run(PY, ['run.py', 'setup', filePath], KIT);
      if (!cli.ok) return { ok: false, error: cli.error || '建模失败' };

      // 解析词典概要：形如 "  power_kw = 功率" 的行 → 提取 {字段: 中文名}
      const attrs = [];
      const lines = cli.output.split('\n');
      let inAttrs = false;
      for (const line of lines) {
        if (line.includes('词典概要')) { inAttrs = true; continue; }
        if (inAttrs && line.includes('=')) {
          const m = line.match(/\s*(\S+)\s*=\s*(.+)/);
          if (m) attrs.push({ field: m[1].trim(), cn: m[2].trim() });
        }
        if (inAttrs && !line.trim()) inAttrs = false;
      }
      // 记录 Web 应用自己的状态（防 current.json 被覆盖）
      const table = safeName.replace(/\.(csv|json)$/i, '');
      saveWebState({ kb, table, nt: `output/${table}_deep.nt`, lexicon: `config/lexicon_${table}.json` });
      return { ok: true, table, attrs, output: cli.output.slice(-2000) };
    }
    return { ok: false, error: (r && r.error) || '后端建模失败' };
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  }
}

/** 安全文件名校验：仅保留文件名，去路径分隔符，防目录穿越 */
function safeFileName(name) {
  return String(name || '').split(/[\\/]/).pop().replace(/[^\w.\-\u4e00-\u9fff]/g, '_');
}

/**
 * 多文件统一建模：把多个文本文件写入 data/ 下临时目录 → 复用 multi_model.py
 * （schema_ontology: load_all + suggest_schema 自动推断 + to_nt）统一建多表本体
 * @param {{name:string, content:string}[]} files 文件数组
 * @returns {Promise<{ok, table?, attrs?, error?}>}
 */
export async function setupOntologyMulti(files, kb) {
  let tmp = null;
  try {
    if (!Array.isArray(files) || files.length === 0) {
      return { ok: false, error: '未选择任何文件' };
    }
    // 校验：全部为文本格式(.csv/.json)，并做文件名校验防路径穿越
    const cleaned = [];
    for (const f of files) {
      const safeName = safeFileName(f && f.name);
      const ext = safeName.slice(safeName.lastIndexOf('.')).toLowerCase();
      if (!TEXT_EXT.includes(ext)) {
        return { ok: false, error: `仅支持 ${TEXT_EXT.join('/')} 文本格式；文件 "${f && f.name}" 不支持` };
      }
      cleaned.push({ name: safeName, content: String(f.content || '') });
    }
    // 唯一临时目录（时间戳），写入全部文件
    const dataDir = join(KIT, 'data');
    if (!existsSync(dataDir)) mkdirSync(dataDir, { recursive: true });
    tmp = join(dataDir, `.multi_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`);
    mkdirSync(tmp, { recursive: true });
    for (const f of cleaned) writeFileSync(join(tmp, f.name), f.content, 'utf-8');

    kb = kb || getCurrentKb();
    // 优先走后端 REST 多表建模(数据目录限定在 codes/data 内, 传相对路径 data_dir)
    const r = await apiFetch('/api/ontology/build', {
      method: 'POST',
      body: { kb, data_dir: `data/${basename(tmp)}` },
      timeout: 60000,
    });
    if (r && r.ok && r.data) {
      const d = r.data;
      const builtKb = d.kb || kb;
      const attrs = readLexiconAttrs(d.lexicon ? join(KIT, d.lexicon) : null) ||
                    cleaned.map(f => f.name);
      setCurrentKb(builtKb);
      saveWebState({ kb: builtKb, table: builtKb, nt: d.nt, lexicon: d.lexicon });
      return { ok: true, table: builtKb, attrs, output: JSON.stringify(r, null, 2).slice(-2000) };
    }
    if (r && r.offline) {
      // 降级: 后端不可达, 回退现有 CLI multi_model.py
      const table = `factory_multi_${Date.now()}`;
      const cli = await run(PY, ['multi_model.py', tmp, table], KIT);
      if (!cli.ok) return { ok: false, error: cli.error || '多文件建模失败' };
      const m = cli.output.match(/表:\s*\[([^\]]*)\]/);
      const attrs = m ? m[1].split(',').map(s => s.trim().replace(/'/g, '').replace(/"/g, '')).filter(Boolean)
                      : cleaned.map(f => f.name);
      saveWebState({ kb, table, nt: `output/${table}.nt` });
      return { ok: true, table, attrs, output: cli.output.slice(-2000) };
    }
    return { ok: false, error: (r && r.error) || '后端建模失败' };
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  } finally {
    if (tmp && existsSync(tmp)) { try { rmSync(tmp, { recursive: true, force: true }); } catch { /* 忽略 */ } }
  }
}

/**
 * 数据库接入建模：复用 db_loader.load_db 读指定表 → 写 CSV 到 data/ 临时目录 →
 * 复用 multi_model.build 多表建模（本地局域网场景，密码只在内存经环境变量传入，不入库）
 * @param {{db_type,host,port,user,password,database,tables: string[]}} cfg
 * @returns {Promise<{ok, table?, error?}>}
 */
export async function dbSetup(cfg) {
  let tmp = null;
  try {
    cfg = cfg || {};
    const dbType = String(cfg.db_type || '').toLowerCase();
    if (!['mysql', 'postgres', 'postgresql', 'pg'].includes(dbType)) {
      return { ok: false, error: 'db_type 必须为 mysql 或 postgres' };
    }
    const tables = (Array.isArray(cfg.tables) ? cfg.tables : String(cfg.tables || '').split(/[,，]/).map(s => s.trim()).filter(Boolean));
    if (tables.length === 0) return { ok: false, error: 'tables 必填（表名，可多个，逗号分隔）' };
    // 表名白名单校验（防 SQL 注入，与 db_loader 一致）
    for (const t of tables) {
      if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(t)) return { ok: false, error: `非法表名: ${t}` };
    }
    const dataDir = join(KIT, 'data');
    if (!existsSync(dataDir)) mkdirSync(dataDir, { recursive: true });
    tmp = join(dataDir, `.db_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`);
    mkdirSync(tmp, { recursive: true });

    const table = `factory_db_${Date.now()}`;
    // 密码经环境变量传 Python（内存用），不写入文件/不入库
    const env = { DB_CFG: JSON.stringify({ ...cfg, db_type: dbType, tables }) };
    const r = await runWithEnv(PY, ['db_setup.py', tmp, table], KIT, env);
    if (!r.ok) return { ok: false, error: r.error || '数据库建模失败' };
    saveWebState({ kb: getCurrentKb(), table, nt: `output/${table}.nt` });
    return { ok: true, table, output: r.output.slice(-2000) };
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  } finally {
    if (tmp && existsSync(tmp)) { try { rmSync(tmp, { recursive: true, force: true }); } catch { /* 忽略 */ } }
  }
}

/**
 * 自然语言问答
 * 优先调后端 POST /api/ask(多租户, 带 kb, 根治串台); 后端不可达时降级 run.py CLI。
 * @param {string} question
 * @param {string} [kb] 目标知识库(kb 名), 缺省用当前激活 kb
 * @returns {Promise<{ok, answer?, mode?, kb?, error?}>}
 */
export async function askOntology(question, kb) {
  try {
    kb = kb || getCurrentKb();
    const r = await apiFetch('/api/ask', { method: 'POST', body: { kb, question } });
    if (r && r.ok && (r.answer != null || (r.data && r.data.answer != null))) {
      // 后端 ask 直接返回 {ok, answer, mode, kb, evidence?}(部分版本套 data 信封, 兼容两者)
      // evidence 为文档 RAG(kb_rag) 的溯源切块[{doc_id,title,chunk,score}], 透传供 SPA 展示
      const d = (r.data && r.data.answer != null) ? r.data : r;
      return { ok: true, answer: String(d.answer).trim(), mode: d.mode, kb: d.kb || kb, evidence: d.evidence || null };
    }
    if (r && r.offline) {
      // 降级: 后端不可达, 回退现有 run.py CLI(单库, 保持前端可用)
      const cli = await run(PY, ['run.py', 'ask', question], KIT);
      if (!cli.ok) return { ok: false, error: cli.error || '问答失败' };
      return { ok: true, answer: cli.output.trim(), kb };
    }
    return { ok: false, error: (r && r.error) || '后端问答失败' };
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  }
}

/**
 * 评测：转发后端 GET /api/eval/benchmark?kb= （多租户按 kb）
 * 复用 apiFetch（自动带 X-API-Key 头）。后端返回 {ok, data:{questions_n,hits,score}}。
 * @param {string} [kb] 知识库名, 缺省用当前激活 kb
 * @returns {Promise<{ok, data?, error?}>}
 */
export async function evalBenchmark(kb) {
  try {
    kb = kb || getCurrentKb();
    const q = new URLSearchParams({ kb }).toString();
    const r = await apiFetch(`/api/eval/benchmark?${q}`, { timeout: 60000 });
    return r && r.ok ? { ok: true, data: r.data } : { ok: false, error: (r && r.error) || '后端评测失败' };
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  }
}

/**
 * 隔离评测：转发后端 POST /api/eval/isolate（自定义问题集，只问答不打分）。
 * 复用 apiFetch（自动带 X-API-Key 头）。后端返回 {ok, data:{kb, questions_n, answers:[{q, answer, hit}]}}。
 * @param {string} [kb] 知识库名, 缺省用当前激活 kb
 * @param {string[]} [questions] 自定义问题列表(非空字符串数组)
 * @returns {Promise<{ok, data?, error?}>}
 */
export async function evalIsolate(kb, questions) {
  try {
    kb = kb || getCurrentKb();
    const list = (Array.isArray(questions) ? questions : [])
      .map(q => String(q || '').trim()).filter(Boolean);
    if (list.length === 0) return { ok: false, error: '未提供自定义问题集' };
    const r = await apiFetch('/api/eval/isolate', {
      method: 'POST',
      body: { kb, questions: list },
      timeout: 120000,
    });
    return r && r.ok ? { ok: true, data: r.data } : { ok: false, error: (r && r.error) || '后端隔离评测失败' };
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  }
}

/**
 * 知识库：转发后端 GET /api/knowledge/list?kb= （文档清单，供 SPA 展示）
 * 后端返回 {ok, data:{docs}}。
 * @param {string} [kb] 知识库名, 缺省用当前激活 kb
 * @returns {Promise<{ok, data?, error?}>}
 */
export async function knowledgeList(kb) {
  try {
    kb = kb || getCurrentKb();
    const q = new URLSearchParams({ kb }).toString();
    const r = await apiFetch(`/api/knowledge/list?${q}`);
    return r && r.ok ? { ok: true, data: r.data } : { ok: false, error: (r && r.error) || '后端知识库读取失败' };
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  }
}

/**
 * 知识库：转发后端 POST /api/knowledge/ingest（multipart 上传文档）
 * multipart 请求体(含 boundary)由前端 server 透传，本函数只负责把原始体+Content-Type
 * 原样转发给后端(文件+kb+doc_id 由前端 FormData 组装)。后端返回统一信封 {ok, data, error}。
 * @param {string} contentType 原始 multipart Content-Type（含 boundary）
 * @param {Buffer} rawBody multipart 原始二进制体
 * @returns {Promise<{ok, data?, offline?, error?}>}
 */
export async function knowledgeIngest(contentType, rawBody) {
  const headers = { 'Content-Type': contentType };
  if (API_KEY) headers['X-API-Key'] = API_KEY;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 60000);
  try {
    const resp = await fetch(API_URL + '/api/knowledge/ingest', {
      method: 'POST', headers, body: rawBody, signal: ctrl.signal,
    });
    const text = await resp.text();
    let data = null;
    try { data = JSON.parse(text); } catch (e) { /* 非 JSON */ }
    if (!data) return { ok: false, offline: false, error: `后端返回非 JSON (HTTP ${resp.status})` };
    return data;
  } catch (e) {
    const aborted = e && e.name === 'AbortError';
    return { ok: false, offline: true, error: aborted ? '后端超时' : String((e && e.message) || e) };
  } finally {
    clearTimeout(timer);
  }
}

/**
 * 知识库：转发后端 POST /api/knowledge/delete（删除一篇文档，幂等）
 * @param {string} kb 知识库名
 * @param {string} doc_id 文档 id
 * @returns {Promise<{ok, data?, offline?, error?}>}
 */
export async function knowledgeDelete(kb, doc_id) {
  try {
    kb = kb || getCurrentKb();
    const r = await apiFetch('/api/knowledge/delete', { method: 'POST', body: { kb, doc_id } });
    return r && r.ok ? { ok: true, data: r.data } : { ok: false, error: (r && r.error) || '后端删除失败' };
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  }
}

/**
 * 知识库：转发后端 POST /api/knowledge/query（文档 RAG 检索，供前端"查看内容"展示切块）
 * 后端返回 {ok, data:{kb, answer, evidence:[{doc_id,title,chunk,score}]}}。
 * @param {string} kb 知识库名
 * @param {string} q 检索问题
 * @param {number} [top_k] 检索块数
 * @returns {Promise<{ok, data?, offline?, error?}>}
 */
export async function knowledgeQuery(kb, q, top_k = 8) {
  try {
    kb = kb || getCurrentKb();
    const r = await apiFetch('/api/knowledge/query', { method: 'POST', body: { kb, q, top_k }, timeout: 60000 });
    return r && r.ok ? { ok: true, data: r.data } : { ok: false, error: (r && r.error) || '后端检索失败' };
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  }
}

/**
 * 资产：转发后端 GET /api/assets/list?kb= （版本清单 + 当前激活版本）
 * 后端返回 {ok, data:{versions, active_version}}。
 * @param {string} [kb] 知识库名, 缺省用当前激活 kb
 * @returns {Promise<{ok, data?, error?}>}
 */
export async function assetsList(kb) {
  try {
    kb = kb || getCurrentKb();
    const q = new URLSearchParams({ kb }).toString();
    const r = await apiFetch(`/api/assets/list?${q}`);
    return r && r.ok ? { ok: true, data: r.data } : { ok: false, error: (r && r.error) || '后端资产读取失败' };
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  }
}

/**
 * 资产：转发后端 POST /api/assets/snapshot （为某 kb 创建语义资产快照）
 * 复用 apiFetch（自动带 X-API-Key 头）。后端返回 {ok, data:{version, hash, kb}}。
 * @param {string} [kb] 知识库名, 缺省用当前激活 kb
 * @param {string} [changelog] 快照说明（仅前端展示用，后端契约忽略该字段）
 * @returns {Promise<{ok, data?, error?}>}
 */
export async function assetsSnapshot(kb, changelog) {
  try {
    kb = kb || getCurrentKb();
    // 后端 AssetSnapshotReq 只契约 kb，changelog 仅随请求带上（前端留痕），后端忽略
    const body = { kb };
    if (changelog !== undefined && changelog !== null && String(changelog).trim() !== '') body.changelog = String(changelog).trim();
    const r = await apiFetch('/api/assets/snapshot', { method: 'POST', body, timeout: 60000 });
    return r && r.ok ? { ok: true, data: r.data } : { ok: false, error: (r && r.error) || '后端快照失败' };
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  }
}

/**
 * 资产：转发后端 POST /api/assets/rollback （回滚某 kb 到指定版本并重载内存本体）
 * 复用 apiFetch（自动带 X-API-Key 头）。后端返回 {ok, data:{active_version, kb}}。
 * @param {string} [kb] 知识库名, 缺省用当前激活 kb
 * @param {string} version 要回滚到的版本号（manifest 中的 key）
 * @returns {Promise<{ok, data?, error?}>}
 */
export async function assetsRollback(kb, version) {
  try {
    kb = kb || getCurrentKb();
    if (!version) return { ok: false, error: 'version 必填' };
    const r = await apiFetch('/api/assets/rollback', { method: 'POST', body: { kb, version } });
    return r && r.ok ? { ok: true, data: r.data } : { ok: false, error: (r && r.error) || '后端回滚失败' };
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  }
}

/**
 * 聚合统计（从当前建模的本体 .nt 计算设备分布）供前端可视化
 * 数据源：优先读 Web 应用自己的 web_state（Web 建模的），fallback 套件 current.json
 * 多用户单企业：支持显式传入 kb，按该 kb 从注册表读本体（优先于全局 web_state）。
 * 空态：无建模数据(.nt 不存在/为空)时返回 empty:true 而非 error，前端显示"尚未建模"
 * @param {string} [kb] 目标知识库(kb 名)；缺省按原逻辑(web_state/current.json)
 * @returns {Promise<{ok, stats?, empty?, error?}>}
 */
export async function statsOntology(kb) {
  try {
    // 多用户单企业：显式 kb 时优先从 kbs.json 注册表读该 kb 本体（数据隔离）
    if (kb) {
      const kbs = loadKbs();
      const kbc = kbs[kb];
      const nt = kbc && kbc.nt ? kbc.nt : null;
      if (!nt) return { ok: true, stats: null, empty: true };
      const ntPath = join(KIT, nt);
      if (!existsSync(ntPath) || statSync(ntPath).size === 0) {
        return { ok: true, stats: null, empty: true };
      }
      const r = await run(PY, ['ontology_stats.py', ntPath], KIT);
      if (!r.ok) return { ok: false, error: r.error || '统计失败' };
      const stats = JSON.parse(r.output);
      if (!stats || !stats.total_devices) return { ok: true, stats: null, empty: true };
      return { ok: true, stats };
    }
    // 原逻辑：优先读 Web 应用自己的状态（防套件 current.json 被测试覆盖）
    const web = loadWebState();
    let nt = null;
    if (web && web.nt) {
      nt = web.nt;
    } else {
      // 多租户: 按当前激活 kb 从注册表读本体(与后端 /api/ask 对齐)
      const curKb = getCurrentKb();
      const kbs = loadKbs();
      if (kbs[curKb] && kbs[curKb].nt) nt = kbs[curKb].nt;
      if (!nt) {
        try {
          const cur = JSON.parse(readFileSync(join(KIT, 'current.json'), 'utf-8'));
          nt = cur.nt;
        } catch (e) { /* 无 current.json 视为未建模 */ }
      }
    }
    if (!nt) return { ok: true, stats: null, empty: true };
    const ntPath = join(KIT, nt);
    if (!existsSync(ntPath) || statSync(ntPath).size === 0) {
      return { ok: true, stats: null, empty: true };
    }
    const r = await run(PY, ['ontology_stats.py', ntPath], KIT);
    if (!r.ok) return { ok: false, error: r.error || '统计失败' };
    const stats = JSON.parse(r.output);
    // 解析出空统计(如无设备实例)也按空态处理，避免看板渲染空数据
    if (!stats || !stats.total_devices) return { ok: true, stats: null, empty: true };
    return { ok: true, stats };
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  }
}

/**
 * 产线属性查询（多表 join：产线→设备）
 * @param {string} lineId 如 L1
 * @param {string} [kb] 目标知识库(kb 名)，缺省按原逻辑
 * @returns {Promise<{ok, line?, error?}>}
 */
export async function lineInfo(lineId, kb) {
  try {
    const r = await statsOntology(kb);
    if (!r.ok) return r;
    const line = (r.stats.line_stats || []).find(l => l.line === lineId);
    if (!line) return { ok: false, error: `未找到产线 ${lineId}` };
    return { ok: true, line };
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  }
}

/**
 * 模型结构（本体 schema：类/数据属性/对象属性/实例数），供前端画结构图
 * 多用户单企业：支持显式传入 kb，按该 kb 从注册表读本体/词典。
 * @param {string} [kb] 目标知识库(kb 名)；缺省按原逻辑(web_state/current.json)
 * @returns {Promise<{ok, schema?, error?}>}
 */
export async function schemaOntology(kb) {
  try {
    // 多用户单企业：显式 kb 时从 kbs.json 读该 kb 本体（数据隔离）
    if (kb) {
      const kbs = loadKbs();
      const kbc = kbs[kb];
      const nt = kbc && kbc.nt ? kbc.nt : null;
      const lex = kbc && kbc.lexicon ? 'config/' + kbc.lexicon : null;
      if (!nt) return { ok: true, schema: null, empty: true };
      const r = await run(PY, ['ontology_schema_info.py', join(KIT, nt)], KIT);
      if (!r.ok) return { ok: false, error: r.error || '模型结构解析失败' };
      const schema = JSON.parse(r.output);
      return { ok: true, schema };
    }
    // 原逻辑：优先读 Web 应用自己的状态（防套件 current.json 被测试覆盖）
    const web = loadWebState();
    let nt, lex;
    if (web && web.nt) {
      nt = web.nt; lex = web.lexicon || 'config/lexicon_equipment.json';
    } else {
      // 多租户: 按当前激活 kb 从注册表读本体/词典(与后端建模对齐)
      const curKb = getCurrentKb();
      const kbs = loadKbs();
      if (kbs[curKb] && kbs[curKb].nt) {
        nt = kbs[curKb].nt; lex = kbs[curKb].lexicon || 'config/lexicon_equipment.json';
      } else {
        const cur = JSON.parse(readFileSync(join(KIT, 'current.json'), 'utf-8'));
        nt = cur.nt || 'output/equipment.nt';
        lex = cur.lexicon || 'config/lexicon_equipment.json';
      }
    }
    const r = await run(PY, ['ontology_schema_info.py', nt], KIT);
    if (!r.ok) return { ok: false, error: r.error || '模型结构解析失败' };
    const schema = JSON.parse(r.output);
    return { ok: true, schema };
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  }
}

/**
 * 模型结构图（图结构：节点+边），供前端 ECharts 力导向图渲染。
 * 优先转发后端 GET /api/ontology/graph（多租户按 kb，返回 {ok, nodes, edges}，
 * nodes=[{id,name,entity}], edges=[{from,to,rel}]）；后端不可达时降级由 schema 组装最小图，
 * 保证离线也能画出类结构。
 * @param {string} [kb] 知识库名, 缺省用当前激活 kb
 * @returns {Promise<{ok, graph?: {nodes, edges}, error?}>}
 */
export async function graphOntology(kb) {
  try {
    kb = kb || getCurrentKb();
    const q = new URLSearchParams({ kb }).toString();
    const r = await apiFetch(`/api/ontology/graph?${q}`);
    // 后端返回统一信封 {ok, nodes, edges, counts}（本体实例图）
    if (r && r.ok && Array.isArray(r.nodes)) {
      return { ok: true, graph: { nodes: r.nodes, edges: r.edges || [] } };
    }
    if (r && r.offline) {
      // 降级：后端不可达，由本地 schema 组装最小图（类节点 + 类间对象关系），
      // 供前端离线仍能渲染类结构（节点/边为前端通用形状）
      const s = await schemaOntology();
      if (!s.ok || !s.schema) return { ok: false, error: (s && s.error) || '模型结构图获取失败' };
      const schema = s.schema;
      const nodes = [], edges = [];
      // 每个类一个节点（id 用 cls_ 前缀，与 ModelGraph 类节点渲染约定一致）
      const classes = Array.isArray(schema.class) ? schema.class
        : Object.keys(schema.type_hierarchy || {});
      for (const c of classes) {
        nodes.push({ id: 'cls_' + c, name: c, entity: 'cls_' + c });
      }
      // 类间对象属性连成边（对象属性形如 obj_xxx，两端落在类上）
      const objProps = Array.isArray(schema.object_properties) ? schema.object_properties : [];
      const seen = new Set();
      for (const op of objProps) {
        const nm = (typeof op === 'object' && op !== null) ? (op.name || op.rel || op.predicate) : op;
        const from = 'cls_' + (op && op.domain || '');
        const to = 'cls_' + (op && op.range || '');
        if (from !== 'cls_' && to !== 'cls_' && from !== to && !seen.has(from + to)) {
          edges.push({ source: from, target: to, rel: String(nm) });
          seen.add(from + to);
        }
      }
      return { ok: true, graph: { nodes, edges } };
    }
    return { ok: false, error: (r && r.error) || '后端模型图获取失败' };
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  }
}

/**
 * 智能分析（统计摘要 + LLM 洞察），返回结构化数据供前端画图+展示报告
 * 降级方案：不再调用不存在的 analysis.py(ENOENT)，改调后端 POST /api/ask
 * （已有 规则+GraphRAG+文档RAG 融合，能答"分析/比较/趋势"类问题）。
 * 复用 apiFetch + getCurrentKb（多租户）。返回 {ok, report, stats, evidence} 适配前端 AnalysisResult。
 * @param {string} question 分析类问题
 * @returns {Promise<{ok, report?, stats?, evidence?, error?}>}
 */
export async function analyzeOntology(question, kb) {
  try {
    // 复用普通问答入口：后端 /api/ask 自带 规则+GraphRAG+文档RAG 融合，能覆盖分析类意图
    kb = kb || getCurrentKb();
    const r = await apiFetch('/api/ask', { method: 'POST', body: { kb, question } });
    if (r && r.ok && (r.answer != null || (r.data && r.data.answer != null))) {
      // 兼容后端直返/套 data 信封两种形态；evidence 为文档 RAG 溯源切块，透传供前端展示
      const d = (r.data && r.data.answer != null) ? r.data : r;
      // stats 置 null：无专项统计图表，仅展示 /api/ask 的 markdown 报告（AnalysisResult 兼容）
      return { ok: true, report: String(d.answer).trim(), stats: null, evidence: d.evidence || null, kb: d.kb || kb };
    }
    if (r && r.offline) {
      // 后端不可达：降级 run.py CLI 问答（单库），保证前端可用
      const cli = await run(PY, ['run.py', 'ask', question], KIT);
      if (!cli.ok) return { ok: false, error: cli.error || '分析失败' };
      return { ok: true, report: cli.output.trim(), stats: null, kb };
    }
    return { ok: false, error: (r && r.error) || '后端分析失败' };
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  }
}

/**
 * 读取当前生效模型配置
 * @returns {Promise<{ok, active?, models?, error?}>}
 */
export async function getModel() {
  try {
    const cfg = JSON.parse(readFileSync(join(KIT, 'config', 'model_config.json'), 'utf-8'));
    const models = Object.entries(cfg.models || {}).map(([k, v]) => ({ key: k, name: v.name, model: v.model, type: v.type }));
    return { ok: true, active: cfg.active, models };
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  }
}

/**
 * 切换当前生效模型
 * @param {string} key 'local' | 'cloud'
 * @returns {Promise<{ok, active?, error?}>}
 */
export async function setModel(key) {
  try {
    const cfgPath = join(KIT, 'config', 'model_config.json');
    const cfg = JSON.parse(readFileSync(cfgPath, 'utf-8'));
    if (!cfg.models || !cfg.models[key]) {
      return { ok: false, error: `未知模型 key: ${key}（可用: ${Object.keys(cfg.models||{}).join(', ')}）` };
    }
    cfg.active = key;
    atomicWriteJson(cfgPath, cfg);
    return { ok: true, active: key };
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  }
}

/** 原子写 JSON（先写临时文件再替换，防写坏） */
function atomicWriteJson(path, obj) {
  const tmp = path + '.tmp';
  writeFileSync(tmp, JSON.stringify(obj, null, 2), 'utf-8');
  renameSync(tmp, path);
}

/** 判断 api_key 是否为脱敏占位（含 *），是则保留原值不覆盖 */
function isMaskedKey(k) {
  return String(k || '').includes('*');
}

/**
 * 读取完整模型配置（供前端管理界面），api_key 脱敏不回传明文
 * @returns {Promise<{ok, active?, models?, error?}>}
 * models: [{key,name,type,base_url,model,has_key,api_key_status,active}]
 */
export async function getModels() {
  try {
    const cfg = JSON.parse(readFileSync(join(KIT, 'config', 'model_config.json'), 'utf-8'));
    // 检查用户 ~/.env 是否有云端 key（cloud 运行时从 .env 读）
    let envCloudKey = '';
    try {
      const envFile = join(os.homedir(), '.env');
      if (existsSync(envFile)) {
        const envTxt = readFileSync(envFile, 'utf-8');
        const m = envTxt.match(/^\s*(?:export\s+)?DEEPSEEK_API_KEY\s*=\s*['"]?([^'"\s]+)/m);
        if (m && m[1]) envCloudKey = m[1];
      }
    } catch (e) { /* 忽略 */ }
    const models = Object.entries(cfg.models || {}).map(([k, v]) => {
      let ak = String(v.api_key || '').trim();
      // cloud 型且未在配置里配 key → 检查 .env（运行时从 .env 读）
      let fromEnv = false;
      if (!ak && v.type === 'openai' && envCloudKey) { ak = envCloudKey; fromEnv = true; }
      const hasKey = !!ak;
      // 脱敏：长 key 显示前4+***+后2；短 key(如 ollama)仅标"已配置"
      let status = hasKey ? (ak.length > 6 ? ak.slice(0, 4) + '***' + ak.slice(-2) : '已配置') : '未配置';
      if (fromEnv && hasKey) status = status + '（.env）';
      return { key: k, name: v.name, type: v.type, base_url: v.base_url, model: v.model, has_key: hasKey, api_key_status: status, active: cfg.active === k };
    });
    // 向量检索(embedding)模型配置（只读返回非敏感字段）
    const emb = (cfg.embedding && typeof cfg.embedding === 'object') ? cfg.embedding : {};
    const embedding = {
      name: emb.name || '本地向量模型',
      type: emb.type || 'ollama',
      base_url: emb.base_url || 'http://127.0.0.1:11434',
      model: emb.model || 'nomic-embed-text',
    };
    return { ok: true, active: cfg.active, models, embedding };
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  }
}

/**
 * 保存完整模型配置（增删改模型 + 设 active + 更新 api_key），原子写回
 * @param {{models:{key,name,type,base_url,model,api_key}[], active:string}} cfg
 * @returns {Promise<{ok, active?, error?}>}
 */
export async function saveModels(cfg) {
  try {
    const cfgPath = join(KIT, 'config', 'model_config.json');
    const existing = JSON.parse(readFileSync(cfgPath, 'utf-8'));
    const oldModels = existing.models || {};
    const list = Array.isArray(cfg.models) ? cfg.models : [];
    if (list.length === 0) return { ok: false, error: '至少保留一个模型' };

    const newModels = {};
    let active = String(cfg.active || '');
    let seenActive = false;
    for (const m of list) {
      let key = String(m.key || '').trim();
      if (!key) key = 'model_' + Date.now() + '_' + Math.random().toString(36).slice(2, 6);
      const old = oldModels[key] || {};
      // api_key：空/未传/脱敏占位 → 保留原值；否则为新值
      const apiKey = (m.api_key === undefined || m.api_key === '' || isMaskedKey(m.api_key)) ? (old.api_key || '') : String(m.api_key);
      newModels[key] = {
        name: String(m.name || key),
        type: String(m.type || 'ollama'),
        base_url: String(m.base_url || ''),
        model: String(m.model || ''),
        api_key: apiKey,
      };
      if (active === key) seenActive = true;
    }
    if (!seenActive || !active) active = Object.keys(newModels)[0];

    // embedding 配置：前端传入则更新，否则保留原值（不破坏向量模型配置）
    const emb = (cfg.embedding && typeof cfg.embedding === 'object') ? cfg.embedding : {};
    const embedding = {
      name: String(emb.name || '本地向量模型'),
      type: String(emb.type || 'ollama'),
      base_url: String(emb.base_url || 'http://127.0.0.1:11434'),
      model: String(emb.model || 'nomic-embed-text'),
      api_key: String(emb.api_key || ''),
    };

    atomicWriteJson(cfgPath, { ...existing, active, models: newModels, embedding });
    return { ok: true, active };
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  }
}

/**
 * 重置指定 kb 的企业数据（清空本体/词典/知识库/资产），用于"换企业重新 onboarding"。
 * 从 kbs.json 注册表移除该 kb，删除 output/<kb>.nt、config/lexicon_<kb>.json 及 data/<kb> 目录。
 * 后端 api_server 的 kb 数据目录为 codes/data/<kb>（隔离知识库/资产/本体中间态），一并清理。
 * @param {string} kb 要重置的知识库名
 * @returns {{ok, error?}}
 */
export function resetKb(kb) {
  const name = String(kb || '').trim();
  if (!name) return { ok: false, error: '非法知识库名' };
  // 从 kbs.json 注册表移除
  try {
    const data = JSON.parse(readFileSync(KBS_FILE, 'utf-8'));
    if (data.kbs && data.kbs[name]) {
      delete data.kbs[name];
      atomicWriteJson(KBS_FILE, data);
    }
  } catch (e) { /* 注册表不可写则忽略 */ }
  // 清理本体/词典文件
  const targets = [
    join(KIT, 'output', `${name}.nt`),
    join(KIT, 'config', `lexicon_${name}.json`),
  ];
  for (const t of targets) {
    try { if (existsSync(t)) rmSync(t, { force: true }); } catch (e) { /* 忽略 */ }
  }
  // 清理后端数据目录 codes/data/<kb>（知识库/资产/隔离数据）
  const dataDir = join(KIT, 'data', name);
  try { if (existsSync(dataDir)) rmSync(dataDir, { recursive: true, force: true }); } catch (e) { /* 忽略 */ }
  // 若当前激活 kb 是被重置的，重置前端激活状态回退
  try {
    const web = loadWebState();
    if (web && web.kb === name) {
      web.kb = '';
      saveWebState(web);
    }
  } catch (e) { /* 忽略 */ }
  return { ok: true };
}
