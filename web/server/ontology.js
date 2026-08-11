// ontology.js — 桥接 Python 本体问答套件
// 通过 child_process 调用仓库 codes/ 下的 Python 脚本
import { execFile } from 'child_process';
import { writeFileSync, mkdirSync, existsSync, readFileSync, readdirSync, statSync, rmSync } from 'fs';
import { join, dirname } from 'path';
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

// Web 上传支持的文本格式(文本可直接传输; 二进制 sqlite/xlsx 走命令行 codes/ 套件)
const TEXT_EXT = ['.csv', '.json'];
// 示例数据目录（相对套件 codes/），供前端免手选文件直接体验
const EXAMPLE_DIRS = ['data_valve', 'data'];

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
 * 读取指定示例文件内容（安全：只允许 data_valve/ 或 data/ 下，防目录穿越）
 * @param {string} relPath 相对套件 codes/ 的路径，如 data_valve/valve_equipment.csv
 * @returns {{ok, content?, name?, size?, error?}}
 */
export function readExample(relPath) {
  const p = String(relPath || '').replace(/^[\\/]+/, '').replace(/\\/g, '/');
  if (!/^(data_valve|data)\/[^/]+$/.test(p)) return { ok: false, error: '非法示例路径' };
  const fp = join(KIT, p);
  try {
    const content = readFileSync(fp, 'utf-8');
    return { ok: true, content, name: p.split('/').pop(), size: content.length };
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  }
}

/**
 * 上传并建模：把文本格式文件(CSV/JSON)写入套件 data/，调用 run.py setup 生成本体+词典
 * @param {string} fileName 文件名 (如 equipment.csv / equipment.json)
 * @param {string} fileContent 文件内容(文本)
 * @returns {Promise<{ok, table?, attrs?, error?}>}
 */
export async function setupOntology(fileName, fileContent) {
  try {
    // 安全：只允许文本格式(.csv/.json)，去掉路径分隔符防目录穿越
    const safeName = fileName.split(/[\\/]/).pop().replace(/[^\w.\-\u4e00-\u9fff]/g, '_');
    const ext = safeName.slice(safeName.lastIndexOf('.')).toLowerCase();
    if (!TEXT_EXT.includes(ext)) {
      return { ok: false, error: `仅支持 ${TEXT_EXT.join('/')} 文本格式；二进制 sqlite/xlsx 请在命令行用 codes/ 套件（python run.py setup <文件>）` };
    }
    const dataDir = join(KIT, 'data');
    if (!existsSync(dataDir)) mkdirSync(dataDir, { recursive: true });
    const filePath = join(dataDir, safeName);
    writeFileSync(filePath, fileContent, 'utf-8');

    const table = safeName.replace(/\.(csv|json)$/i, '');
    const r = await run(PY, ['run.py', 'setup', filePath], KIT);
    if (!r.ok) return { ok: false, error: r.error || '建模失败' };

    // 解析词典概要：形如 "  power_kw = 功率" 的行 → 提取 {字段: 中文名}
    const attrs = [];
    const lines = r.output.split('\n');
    let inAttrs = false;
    for (const line of lines) {
      if (line.includes('词典概要')) { inAttrs = true; continue; }
      if (inAttrs && line.includes('=')) {
        const m = line.match(/\s*(\S+)\s*=\s*(.+)/);
        if (m) attrs.push({ field: m[1].trim(), cn: m[2].trim() });
      }
      // 概要结束：遇到建模完成后的空行或下一段
      if (inAttrs && !line.trim()) inAttrs = false;
    }
    // 记录 Web 应用自己的状态（防 current.json 被覆盖）
    saveWebState({ table, nt: `output/${table}_deep.nt`, lexicon: `config/lexicon_${table}.json` });
    return { ok: true, table, attrs, output: r.output.slice(-2000) };
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
export async function setupOntologyMulti(files) {
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

    const table = `factory_multi_${Date.now()}`;
    const r = await run(PY, ['multi_model.py', tmp, table], KIT);
    if (!r.ok) return { ok: false, error: r.error || '多文件建模失败' };
    // 解析输出里的表清单，作为 attrs 供前端展示
    const m = r.output.match(/表:\s*\[([^\]]*)\]/);
    const attrs = m ? m[1].split(',').map(s => s.trim().replace(/'/g, '').replace(/"/g, '')).filter(Boolean)
                    : cleaned.map(f => f.name);
    saveWebState({ table, nt: `output/${table}.nt` });
    return { ok: true, table, attrs, output: r.output.slice(-2000) };
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
    saveWebState({ table, nt: `output/${table}.nt` });
    return { ok: true, table, output: r.output.slice(-2000) };
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  } finally {
    if (tmp && existsSync(tmp)) { try { rmSync(tmp, { recursive: true, force: true }); } catch { /* 忽略 */ } }
  }
}

/**
 * 自然语言问答
 * @param {string} question
 * @returns {Promise<{ok, answer?, error?}>}
 */
export async function askOntology(question) {
  try {
    const r = await run(PY, ['run.py', 'ask', question], KIT);
    if (!r.ok) return { ok: false, error: r.error || '问答失败' };
    return { ok: true, answer: r.output.trim() };
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  }
}

/**
 * 聚合统计（从当前建模的本体 .nt 计算设备分布）供前端可视化
 * 数据源：优先读 Web 应用自己的 web_state（Web 建模的），fallback 套件 current.json
 * 空态：无建模数据(.nt 不存在/为空)时返回 empty:true 而非 error，前端显示"尚未建模"
 * @returns {Promise<{ok, stats?, empty?, error?}>}
 */
export async function statsOntology() {
  try {
    // 优先读 Web 应用自己的状态（防套件 current.json 被测试覆盖）
    const web = loadWebState();
    let nt = null;
    if (web && web.nt) {
      nt = web.nt;
    } else {
      try {
        const cur = JSON.parse(readFileSync(join(KIT, 'current.json'), 'utf-8'));
        nt = cur.nt;
      } catch (e) { /* 无 current.json 视为未建模 */ }
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
 * @returns {Promise<{ok, line?, error?}>}
 */
export async function lineInfo(lineId) {
  try {
    const r = await statsOntology();
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
 * 根据 current.json 读取当前建模的本体和词典（动态，建模什么就显示什么）
 * @returns {Promise<{ok, schema?, error?}>}
 */
export async function schemaOntology() {
  try {
    // 优先读 Web 应用自己的状态（防套件 current.json 被测试覆盖）
    const web = loadWebState();
    let nt, lex;
    if (web && web.nt) {
      nt = web.nt; lex = web.lexicon || 'config/lexicon_equipment.json';
    } else {
      const cur = JSON.parse(readFileSync(join(KIT, 'current.json'), 'utf-8'));
      nt = cur.nt || 'output/equipment.nt';
      lex = cur.lexicon || 'config/lexicon_equipment.json';
    }
    const r = await run(PY, ['model_schema.py', nt, lex], KIT);
    if (!r.ok) return { ok: false, error: r.error || '模型结构解析失败' };
    const schema = JSON.parse(r.output);
    return { ok: true, schema };
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  }
}

/**
 * 智能分析（统计摘要 + LLM 洞察），返回结构化数据供前端画图+展示报告
 * @param {string} question 分析类问题
 * @returns {Promise<{ok, report?, stats?, error?}>}
 */
export async function analyzeOntology(question) {
  try {
    const dataDir = join(KIT, 'data');
    const eqPath = join(dataDir, 'equipment.csv');
    const linePath = join(dataDir, 'line.csv');
    const r = await run(PY, ['analysis.py', eqPath, linePath, question], KIT);
    if (!r.ok) return { ok: false, error: r.error || '分析失败' };
    const res = JSON.parse(r.output);
    return { ok: true, report: res.report, stats: res.stats };
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
    writeFileSync(cfgPath, JSON.stringify(cfg, null, 2), 'utf-8');
    return { ok: true, active: key };
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  }
}
