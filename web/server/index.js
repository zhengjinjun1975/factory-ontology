// index.js — 工厂本体问答 Web 应用服务器
// 静态服务 public/ + API（/api/ontology/setup, /api/ontology/ask）+ health
import { createServer } from 'http';
import { readFileSync, existsSync } from 'fs';
import { extname, join, dirname } from 'path';
import { fileURLToPath } from 'url';
import { setupOntology, askOntology, statsOntology, lineInfo, schemaOntology, graphOntology, analyzeOntology, getModel, setModel, getModels, saveModels, listExamples, readExample, setupOntologyMulti, dbSetup, browse, readDataFile, getCurrentKb, setCurrentKb, listKbs, listIndustries, buildIndustry, evalBenchmark, evalIsolate, knowledgeList, assetsList, assetsSnapshot, assetsRollback, knowledgeIngest, knowledgeDelete, knowledgeQuery, getEnterprise, saveEnterprise, resetKb } from './ontology.js';
import { login as authLogin, logout as authLogout, me as authMe, createUser, updateUser, seedUsersIfEmpty, restoreSessions } from './auth.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const PORT = process.env.PORT || 3001;
const DIST_DIR = join(__dirname, '..', 'public');

// 行业→kb 映射：事件驱动无死角，不再硬编码 9 个，而是从 kbs.json 注册表全量动态读取。
// listIndustries() 返回全部可建模行业 [{kb,name,icon,dir}]，行业下拉/改行业联动都以此为准。

const MIME = {
  '.html': 'text/html;charset=utf-8',
  '.js':   'application/javascript',
  '.css':  'text/css;charset=utf-8',
  '.svg':  'image/svg+xml',
  '.png':  'image/png',
  '.jpg':  'image/jpeg',
  '.ico':  'image/x-icon',
  '.json': 'application/json',
  '.woff2':'font/woff2',
  '.map':  'application/json',
};

function readBody(req, max = 2 * 1024 * 1024) {
  return new Promise((resolve, reject) => {
    let body = '';
    req.on('data', (c) => {
      body += c;
      if (body.length > max) { req.destroy(); reject(new Error('body too large')); }
    });
    req.on('end', () => resolve(body));
    req.on('error', reject);
  });
}

// 读取原始二进制请求体（multipart 上传透传用，保留 boundary 与文件字节）
function readRawBody(req, max = 60 * 1024 * 1024) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on('data', (c) => {
      size += c.length;
      if (size > max) { req.destroy(); reject(new Error('body too large')); }
      chunks.push(c);
    });
    req.on('end', () => resolve(Buffer.concat(chunks)));
    req.on('error', reject);
  });
}

const server = createServer(async (req, res) => {
  const url = req.url.split('?')[0];

  // ── 启动时种子企业用户（仅当 users.json 为空）+ 恢复持久化会话（node 重启不掉线）──
  seedUsersIfEmpty();
  restoreSessions();

  // ═══ 鉴权辅助 ═══
  // 从 Authorization: Bearer <token> 或 X-Auth-Token 头取会话 token
  function getToken() {
    const ah = req.headers['authorization'] || '';
    const m = ah.match(/^Bearer\s+(.+)$/i);
    if (m) return m[1].trim();
    return String(req.headers['x-auth-token'] || '').trim();
  }
  // 校验会话，返回公开用户；未登录/失效返回 null 并写 401
  function requireAuth() {
    const user = authMe(getToken());
    if (!user.ok) {
      res.writeHead(401, { 'Content-Type': 'application/json;charset=utf-8' });
      res.end(JSON.stringify({ ok: false, error: user.error, unauthenticated: true }));
      return null;
    }
    return user.user;
  }
  const writeErr = (code, obj) => {
    res.writeHead(code, { 'Content-Type': 'application/json;charset=utf-8' });
    res.end(JSON.stringify(obj));
  };

  // ── API: 用户登录 ──
  if (req.method === 'POST' && url === '/api/auth/login') {
    try {
      const body = JSON.parse((await readBody(req)) || '{}');
      const { username, password } = body;
      if (!username || !password) { writeErr(400, { ok: false, error: '用户名和密码必填' }); return; }
      const result = authLogin(username, password);
      if (!result.ok) { writeErr(401, { ok: false, error: result.error }); return; }
      res.writeHead(200, { 'Content-Type': 'application/json;charset=utf-8' });
      res.end(JSON.stringify(result));
    } catch (err) { writeErr(500, { ok: false, error: String(err.message || err) }); }
    return;
  }

  // ── API: 企业用户注册（新建企业 → 引导 onboarding）──
  if (req.method === 'POST' && url === '/api/auth/register') {
    try {
      const body = JSON.parse((await readBody(req)) || '{}');
      const { username, password, enterpriseName, logo, industry } = body;
      const result = createUser({ username, password, enterpriseName, logo, industry });
      if (!result.ok) { writeErr(400, { ok: false, error: result.error }); return; }
      // 注册成功后自动登录
      const loginResult = authLogin(username, password);
      res.writeHead(200, { 'Content-Type': 'application/json;charset=utf-8' });
      res.end(JSON.stringify(loginResult.ok ? { ok: true, token: loginResult.token, user: result.user } : { ok: true, user: result.user }));
    } catch (err) { writeErr(500, { ok: false, error: String(err.message || err) }); }
    return;
  }

  // ── API: 会话信息（me）/ 退出登录 ──
  if (req.method === 'GET' && url === '/api/auth/me') {
    const user = requireAuth(); if (!user) return;
    res.writeHead(200, { 'Content-Type': 'application/json;charset=utf-8' });
    res.end(JSON.stringify({ ok: true, user }));
    return;
  }
  if (req.method === 'POST' && url === '/api/auth/logout') {
    authLogout(getToken());
    res.writeHead(200, { 'Content-Type': 'application/json;charset=utf-8' });
    res.end(JSON.stringify({ ok: true }));
    return;
  }

  // ═══ 鉴权门禁：以下全部业务端点需登录 ═══
  const isAuthPath = url.startsWith('/api/auth/');
  if (!isAuthPath && (url.startsWith('/api/ontology/') || url.startsWith('/api/enterprise/') || url === '/api/enterprise' || url.startsWith('/api/eval/'))) {
    const user = requireAuth(); if (!user) return;
    // 单企业收敛：把当前登录用户的 kb 设为会话默认激活（各功能跟随该企业本体）
    if (user.kb) { try { setCurrentKb(user.kb); } catch (e) { /* 忽略 */ } }
    req.user = user;
  }

  // ── API: 企业设置（读/存）—— 改为按当前登录企业用户返回（单企业唯一性）──
  if (req.method === 'POST' && url === '/api/ontology/setup') {
    try {
      const body = JSON.parse((await readBody(req)) || '{}');
      const { csvName, csvContent, kb } = body;
      if (!csvName || typeof csvContent !== 'string') {
        res.writeHead(400, { 'Content-Type': 'application/json;charset=utf-8' });
        res.end(JSON.stringify({ ok: false, error: 'csvName 和 csvContent 必填' }));
        return;
      }
      // kb 可选: 缺省用当前激活 kb(多租户), 显式传入则切到该 kb 建本体
      const result = await setupOntology(csvName, csvContent, kb);
      res.writeHead(result.ok ? 200 : 500, { 'Content-Type': 'application/json;charset=utf-8' });
      res.end(JSON.stringify(result));
    } catch (err) {
      res.writeHead(500, { 'Content-Type': 'application/json;charset=utf-8' });
      res.end(JSON.stringify({ ok: false, error: String(err.message || err) }));
    }
    return;
  }

  // ── API: 多文件统一建模 ──
  if (req.method === 'POST' && url === '/api/ontology/setup-multi') {
    try {
      const body = JSON.parse((await readBody(req)) || '{}');
      const { files, kb } = body;
      if (!Array.isArray(files) || files.length === 0) {
        res.writeHead(400, { 'Content-Type': 'application/json;charset=utf-8' });
        res.end(JSON.stringify({ ok: false, error: 'files 必填（[{name, content}] 数组）' }));
        return;
      }
      const result = await setupOntologyMulti(files, kb);
      res.writeHead(result.ok ? 200 : 500, { 'Content-Type': 'application/json;charset=utf-8' });
      res.end(JSON.stringify(result));
    } catch (err) {
      res.writeHead(500, { 'Content-Type': 'application/json;charset=utf-8' });
      res.end(JSON.stringify({ ok: false, error: String(err.message || err) }));
    }
    return;
  }

  // ── API: 数据库接入建模（本地局域网场景）──
  if (req.method === 'POST' && url === '/api/ontology/db-setup') {
    try {
      const body = JSON.parse((await readBody(req)) || '{}');
      const { db_type, host, port, user, password, database, tables } = body;
      const cfg = { db_type, host, port, user, password, database, tables };
      const result = await dbSetup(cfg);
      res.writeHead(result.ok ? 200 : 500, { 'Content-Type': 'application/json;charset=utf-8' });
      res.end(JSON.stringify(result));
    } catch (err) {
      res.writeHead(500, { 'Content-Type': 'application/json;charset=utf-8' });
      res.end(JSON.stringify({ ok: false, error: String(err.message || err) }));
    }
    return;
  }

  // ── API: 问答 ──
  if (req.method === 'POST' && url === '/api/ontology/ask') {
    try {
      const body = JSON.parse((await readBody(req)) || '{}');
      const { question, kb } = body;
      if (!question || typeof question !== 'string') {
        res.writeHead(400, { 'Content-Type': 'application/json;charset=utf-8' });
        res.end(JSON.stringify({ ok: false, error: 'question 必填' }));
        return;
      }
      // kb 可选: 缺省用当前激活 kb(多租户问答), 显式传入则问该 kb
      const result = await askOntology(question, kb);
      res.writeHead(result.ok ? 200 : 500, { 'Content-Type': 'application/json;charset=utf-8' });
      res.end(JSON.stringify(result));
    } catch (err) {
      res.writeHead(500, { 'Content-Type': 'application/json;charset=utf-8' });
      res.end(JSON.stringify({ ok: false, error: String(err.message || err) }));
    }
    return;
  }

  // ── health ──
  if (url === '/health') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ status: 'ok', uptime: process.uptime() }));
    return;
  }

  // ── API: 企业设置（读）—— 按当前登录企业用户返回（单企业唯一性）──
  if (req.method === 'GET' && url === '/api/ontology/enterprise') {
    const user = req.user;
    const data = {
      name: (user && user.enterpriseName) || '',
      logo: (user && user.logo) || '',
      industry: (user && user.industry) || '',
      kb: (user && user.kb) || '',
      onboarded: !!(user && user.onboarded),
      hasConfig: !!(user && (user.enterpriseName || user.logo || user.industry)),
    };
    res.writeHead(200, { 'Content-Type': 'application/json;charset=utf-8', 'Cache-Control': 'no-cache, no-store, must-revalidate' });
    res.end(JSON.stringify({ ok: true, data }));
    return;
  }
  // ── API: 企业设置（存：企业名/logo/行业，写回当前登录用户）──
  // 事件驱动无死角：改行业 → 自动用新行业数据建模（buildIndustry），并把企业唯一 kb
  // 联动更新到新行业 kb，问答/看板/资产全部跟随。行业名用 kbs.json 注册表全量动态解析。
  if (req.method === 'POST' && url === '/api/ontology/enterprise') {
    try {
      const body = JSON.parse((await readBody(req)) || '{}');
      const user = req.user;
      const patch = { enterpriseName: body.name, logo: body.logo, industry: body.industry };
      let ind = null;
      if (body.industry) {
        const all = listIndustries();
        ind = all.find(i => i.name === body.industry) || all.find(i => i.kb === body.industry) || null;
        if (ind && ind.kb) patch.kb = ind.kb; // 行业→kb 联动（kbs.json 已注册）
      }
      // 改行业 → 自动建模（事件驱动）：用新行业数据目录重建该行业 kb 的本体/词典，
      // 使界面显示的"已就绪"是新行业的真实模型，而非旧行业残留。
      if (ind && ind.dir && body.industry !== (user && user.industry)) {
        const built = await buildIndustry(ind.dir, ind.kb);
        if (!built.ok) {
          // 建模失败不放行保存（否则界面"已就绪"但模型是旧的，属死角）
          writeErr(500, { ok: false, error: `行业「${ind.name}」自动建模失败：${built.error || ''}` });
          return;
        }
      }
      const result = updateUser(user.username, patch);
      if (!result.ok) { writeErr(500, result); return; }
      const d = result.user;
      // 改行业后把前端激活 kb 一并切到新行业 kb，问答/看板/资产随行
      if (ind && ind.kb) { try { setCurrentKb(ind.kb); } catch (e) { /* 忽略 */ } }
      res.writeHead(200, { 'Content-Type': 'application/json;charset=utf-8' });
      res.end(JSON.stringify({ ok: true, data: d, industry: ind ? { kb: ind.kb, name: ind.name, icon: ind.icon, dir: ind.dir } : null }));
    } catch (err) { writeErr(500, { ok: false, error: String(err.message || err) }); }
    return;
  }

  // ── API: 行业清单（数据驱动，前端行业下拉动态加载）──
  // 返回全部可建模行业 {kb,name,icon,dir}，不再由前端硬编码 7/9 个。
  if (req.method === 'GET' && url === '/api/ontology/industries') {
    res.writeHead(200, { 'Content-Type': 'application/json;charset=utf-8', 'Cache-Control': 'no-cache, no-store, must-revalidate' });
    res.end(JSON.stringify({ ok: true, industries: listIndustries(), current: (req.user && req.user.kb) || '' }));
    return;
  }

  // ── API: 行业切换（显式触发自动建模，供 onboarding/欢迎页/一键建示例用）──
  // body: {industry} 行业名（kbs.json 全量解析）→ 自动用该行业数据建模并联动 kb。
  if (req.method === 'POST' && url === '/api/ontology/industry-switch') {
    try {
      const body = JSON.parse((await readBody(req)) || '{}');
      const user = req.user;
      const industry = String(body.industry || '').trim();
      if (!industry) { writeErr(400, { ok: false, error: 'industry 必填' }); return; }
      const all = listIndustries();
      const ind = all.find(i => i.name === industry) || all.find(i => i.kb === industry);
      if (!ind) { writeErr(400, { ok: false, error: `未知行业：${industry}` }); return; }
      const built = await buildIndustry(ind.dir, ind.kb);
      if (!built.ok) { writeErr(500, { ok: false, error: `行业「${ind.name}」自动建模失败：${built.error || ''}` }); return; }
      // 联动：企业唯一 kb + 行业 + 前端激活 kb 全部更新到新行业
      const upd = updateUser(user.username, { industry: ind.name, kb: ind.kb });
      if (!upd.ok) { writeErr(500, upd); return; }
      try { setCurrentKb(ind.kb); } catch (e) { /* 忽略 */ }
      res.writeHead(200, { 'Content-Type': 'application/json;charset=utf-8' });
      res.end(JSON.stringify({ ok: true, data: { ...upd.user, industry: ind.name, kb: ind.kb }, industry: { kb: ind.kb, name: ind.name, icon: ind.icon, dir: ind.dir }, table: built.table, attrs: built.attrs || [] }));
    } catch (err) { writeErr(500, { ok: false, error: String(err.message || err) }); }
    return;
  }


  // ── API: 企业重置（清空当前企业数据 → 重新 onboarding）──
  if (req.method === 'POST' && url === '/api/enterprise/reset') {
    try {
      const user = req.user;
      if (!user || !user.kb) { writeErr(400, { ok: false, error: '无企业数据可重置' }); return; }
      const r = resetKb(user.kb);
      if (!r.ok) { writeErr(500, r); return; }
      // 重置企业字段为未配置 + onboarded=false + kb 清空 → 前端进入引导 onboarding，
      // 下次 onboarding 会用新建/重建的 kb，避免旧中文用户名遗留的 ent_______ 残留。
      const upd = updateUser(user.username, { enterpriseName: '', logo: '', industry: '', onboarded: false, kb: '' });
      res.writeHead(200, { 'Content-Type': 'application/json;charset=utf-8' });
      res.end(JSON.stringify({ ok: true, data: upd.ok ? upd.user : null, reset: r }));
    } catch (err) { writeErr(500, { ok: false, error: String(err.message || err) }); }
    return;
  }

  // ── API: 引导 onboarding 完成（确认企业 + 选行业 + 建本体后标记已配置解锁功能）──
  if (req.method === 'POST' && url === '/api/enterprise/onboard') {
    try {
      const body = JSON.parse((await readBody(req)) || '{}');
      const user = req.user;
      const upd = updateUser(user.username, {
        enterpriseName: body.name, logo: body.logo, industry: body.industry,
        kb: body.kb || user.kb, onboarded: true,
      });
      if (!upd.ok) { writeErr(500, upd); return; }
      if (upd.user.kb) { try { setCurrentKb(upd.user.kb); } catch (e) { /* 忽略 */ } }
      res.writeHead(200, { 'Content-Type': 'application/json;charset=utf-8' });
      res.end(JSON.stringify({ ok: true, data: upd.user }));
    } catch (err) { writeErr(500, { ok: false, error: String(err.message || err) }); }
    return;
  }

  // ── API: 上传建模 ──

  // ── API: 多租户知识库列表 + 当前激活 kb ──
  // 单企业收敛：仅返回当前登录用户绑定的唯一 kb（不再暴露其它企业数据）
  if (req.method === 'GET' && url === '/api/ontology/kbs') {
    const user = req.user;
    const all = listKbs();
    const userKb = (user && user.kb) || '';
    // 只保留当前用户的企业 kb
    const kbs = (Array.isArray(all.kbs) ? all.kbs : []).filter(k => !userKb || k.key === userKb);
    res.writeHead(200, { 'Content-Type': 'application/json;charset=utf-8' });
    res.end(JSON.stringify({ ok: true, kbs, current: userKb || all.current }));
    return;
  }
  // ── API: 切换当前激活 kb(多租户) ──
  if (req.method === 'POST' && url === '/api/ontology/kb') {
    try {
      const body = JSON.parse((await readBody(req)) || '{}');
      const kb = String(body.kb || '').trim();
      if (!kb) {
        res.writeHead(400, { 'Content-Type': 'application/json;charset=utf-8' });
        res.end(JSON.stringify({ ok: false, error: 'kb 必填' }));
        return;
      }
      setCurrentKb(kb);
      res.writeHead(200, { 'Content-Type': 'application/json;charset=utf-8' });
      res.end(JSON.stringify({ ok: true, current: kb }));
    } catch (err) {
      res.writeHead(500, { 'Content-Type': 'application/json;charset=utf-8' });
      res.end(JSON.stringify({ ok: false, error: String(err.message || err) }));
    }
    return;
  }

  // ── API: 代码版本（读 codes/run.py 的 __version__，单一事实源）──
  if (url === '/api/ontology/version') {
    let version = '0.1.6';
    try {
      const runSrc = readFileSync(join(__dirname, '..', '..', 'codes', 'run.py'), 'utf-8');
      const m = runSrc.match(/__version__\s*=\s*["']([^"']+)["']/);
      if (m) version = m[1];
    } catch (e) { /* 忽略，用默认 */ }
    res.writeHead(200, { 'Content-Type': 'application/json;charset=utf-8' });
    res.end(JSON.stringify({ ok: true, version }));
    return;
  }

  // ── API: 模型配置（读/切）──
  if (url === '/api/ontology/model' && req.method === 'GET') {
    try {
      const result = await getModel();
      res.writeHead(result.ok ? 200 : 500, { 'Content-Type': 'application/json;charset=utf-8' });
      res.end(JSON.stringify(result));
    } catch (err) {
      res.writeHead(500, { 'Content-Type': 'application/json;charset=utf-8' });
      res.end(JSON.stringify({ ok: false, error: String(err.message || err) }));
    }
    return;
  }
  if (url === '/api/ontology/model' && req.method === 'POST') {
    try {
      const body = JSON.parse((await readBody(req)) || '{}');
      const result = await setModel(body.key);
      res.writeHead(result.ok ? 200 : 500, { 'Content-Type': 'application/json;charset=utf-8' });
      res.end(JSON.stringify(result));
    } catch (err) {
      res.writeHead(500, { 'Content-Type': 'application/json;charset=utf-8' });
      res.end(JSON.stringify({ ok: false, error: String(err.message || err) }));
    }
    return;
  }

  // ── API: 模型配置（完整读/写，api_key 脱敏）──
  if (url === '/api/ontology/models' && req.method === 'GET') {
    try {
      const result = await getModels();
      res.writeHead(result.ok ? 200 : 500, { 'Content-Type': 'application/json;charset=utf-8' });
      res.end(JSON.stringify(result));
    } catch (err) {
      res.writeHead(500, { 'Content-Type': 'application/json;charset=utf-8' });
      res.end(JSON.stringify({ ok: false, error: String(err.message || err) }));
    }
    return;
  }
  if (url === '/api/ontology/models' && req.method === 'POST') {
    try {
      const body = JSON.parse((await readBody(req)) || '{}');
      const result = await saveModels(body);
      res.writeHead(result.ok ? 200 : 500, { 'Content-Type': 'application/json;charset=utf-8' });
      res.end(JSON.stringify(result));
    } catch (err) {
      res.writeHead(500, { 'Content-Type': 'application/json;charset=utf-8' });
      res.end(JSON.stringify({ ok: false, error: String(err.message || err) }));
    }
    return;
  }

  // ── API: 智能分析（统计+洞察，前端画图+报告）──
  if (req.method === 'POST' && url === '/api/ontology/analyze') {
    try {
      const body = JSON.parse((await readBody(req)) || '{}');
      const { question, kb } = body;
      if (!question || typeof question !== 'string') {
        res.writeHead(400, { 'Content-Type': 'application/json;charset=utf-8' });
        res.end(JSON.stringify({ ok: false, error: 'question 必填' }));
        return;
      }
      const result = await analyzeOntology(question, kb);
      res.writeHead(result.ok ? 200 : 500, { 'Content-Type': 'application/json;charset=utf-8' });
      res.end(JSON.stringify(result));
    } catch (err) {
      res.writeHead(500, { 'Content-Type': 'application/json;charset=utf-8' });
      res.end(JSON.stringify({ ok: false, error: String(err.message || err) }));
    }
    return;
  }

  // ── API: 模型结构（可视化）──
  if (req.method === 'GET' && url === '/api/ontology/schema') {
    try {
      const kb = new URL(req.url, 'http://x').searchParams.get('kb') || (req.user && req.user.kb) || '';
      const result = await schemaOntology(kb);
      res.writeHead(result.ok ? 200 : 500, { 'Content-Type': 'application/json;charset=utf-8', 'Cache-Control': 'no-cache, no-store, must-revalidate' });
      res.end(JSON.stringify(result));
    } catch (err) {
      res.writeHead(500, { 'Content-Type': 'application/json;charset=utf-8', 'Cache-Control': 'no-cache, no-store, must-revalidate' });
      res.end(JSON.stringify({ ok: false, error: String(err.message || err) }));
    }
    return;
  }

  // ── API: 模型结构图（图结构 nodes/edges，供 ECharts 力导向图渲染）──
  if (req.method === 'GET' && url === '/api/ontology/graph') {
    try {
      const kb = new URL(req.url, 'http://x').searchParams.get('kb') || '';
      const result = await graphOntology(kb);
      res.writeHead(result.ok ? 200 : 500, { 'Content-Type': 'application/json;charset=utf-8', 'Cache-Control': 'no-cache, no-store, must-revalidate' });
      res.end(JSON.stringify(result.ok ? { ok: true, nodes: result.graph.nodes, edges: result.graph.edges } : result));
    } catch (err) {
      res.writeHead(500, { 'Content-Type': 'application/json;charset=utf-8', 'Cache-Control': 'no-cache, no-store, must-revalidate' });
      res.end(JSON.stringify({ ok: false, error: String(err.message || err) }));
    }
    return;
  }

  // ── API: 示例数据列表/读取（免手选文件直接体验）──
  if (req.method === 'GET' && url === '/api/ontology/examples') {
    res.writeHead(200, { 'Content-Type': 'application/json;charset=utf-8' });
    res.end(JSON.stringify({ ok: true, examples: listExamples() }));
    return;
  }
  if (req.method === 'GET' && url === '/api/ontology/example') {
    const path = new URL(req.url, 'http://x').searchParams.get('path');
    const result = readExample(path);
    res.writeHead(result.ok ? 200 : 400, { 'Content-Type': 'application/json;charset=utf-8' });
    res.end(JSON.stringify(result));
    return;
  }

  // ── API: 文件浏览框（默认到 data_valve 示例目录，目录导航；学习 solo-agent-kit /api/browse）──
  if (req.method === 'GET' && url === '/api/ontology/browse') {
    const dir = new URL(req.url, 'http://x').searchParams.get('dir') || '';
    const result = browse(dir);
    res.writeHead(result.ok ? 200 : 400, { 'Content-Type': 'application/json;charset=utf-8' });
    res.end(JSON.stringify(result));
    return;
  }
  // ── API: 读取浏览框选中的数据文件内容（仅 codes/ 内 .csv/.json，防穿越）──
  if (req.method === 'GET' && url === '/api/ontology/read-data') {
    const path = new URL(req.url, 'http://x').searchParams.get('path');
    const result = readDataFile(path);
    res.writeHead(result.ok ? 200 : 400, { 'Content-Type': 'application/json;charset=utf-8' });
    res.end(JSON.stringify(result));
    return;
  }

  // ── API: 聚合统计（可视化）──
  if (req.method === 'GET' && url === '/api/ontology/stats') {
    try {
      const kb = new URL(req.url, 'http://x').searchParams.get('kb') || (req.user && req.user.kb) || '';
      const result = await statsOntology(kb);
      res.writeHead(result.ok ? 200 : 500, { 'Content-Type': 'application/json;charset=utf-8' });
      res.end(JSON.stringify(result));
    } catch (err) {
      res.writeHead(500, { 'Content-Type': 'application/json;charset=utf-8' });
      res.end(JSON.stringify({ ok: false, error: String(err.message || err) }));
    }
    return;
  }

  // ── API: 评测（转发后端 /api/eval/benchmark，SPA 展示层）──
  if (req.method === 'GET' && url === '/api/ontology/eval-benchmark') {
    try {
      const kb = new URL(req.url, 'http://x').searchParams.get('kb') || '';
      const result = await evalBenchmark(kb);
      res.writeHead(result.ok ? 200 : 500, { 'Content-Type': 'application/json;charset=utf-8', 'Cache-Control': 'no-cache, no-store, must-revalidate' });
      res.end(JSON.stringify(result));
    } catch (err) {
      res.writeHead(500, { 'Content-Type': 'application/json;charset=utf-8' });
      res.end(JSON.stringify({ ok: false, error: String(err.message || err) }));
    }
    return;
  }

  // ── API: 隔离评测（转发后端 POST /api/eval/isolate，自定义问题集逐题作答）──
  if (req.method === 'POST' && url === '/api/ontology/eval-isolate') {
    try {
      const body = JSON.parse((await readBody(req)) || '{}');
      const { kb, questions } = body;
      const result = await evalIsolate(kb, questions);
      res.writeHead(result.ok ? 200 : 500, { 'Content-Type': 'application/json;charset=utf-8', 'Cache-Control': 'no-cache, no-store, must-revalidate' });
      res.end(JSON.stringify(result));
    } catch (err) {
      res.writeHead(500, { 'Content-Type': 'application/json;charset=utf-8' });
      res.end(JSON.stringify({ ok: false, error: String(err.message || err) }));
    }
    return;
  }

  // ── API: 知识库列表（转发后端 /api/knowledge/list）──
  if (req.method === 'GET' && url === '/api/ontology/knowledge-list') {
    try {
      const kb = new URL(req.url, 'http://x').searchParams.get('kb') || '';
      const result = await knowledgeList(kb);
      res.writeHead(result.ok ? 200 : 500, { 'Content-Type': 'application/json;charset=utf-8', 'Cache-Control': 'no-cache, no-store, must-revalidate' });
      res.end(JSON.stringify(result));
    } catch (err) {
      res.writeHead(500, { 'Content-Type': 'application/json;charset=utf-8' });
      res.end(JSON.stringify({ ok: false, error: String(err.message || err) }));
    }
    return;
  }

  // ── API: 知识库上传文档（multipart 透传后端 /api/knowledge/ingest）──
  if (req.method === 'POST' && url === '/api/ontology/knowledge-ingest') {
    try {
      const contentType = String(req.headers['content-type'] || '');
      if (!contentType.toLowerCase().includes('multipart/form-data')) {
        res.writeHead(400, { 'Content-Type': 'application/json;charset=utf-8' });
        res.end(JSON.stringify({ ok: false, error: 'Content-Type 必须为 multipart/form-data' }));
        return;
      }
      const raw = await readRawBody(req, 60 * 1024 * 1024);
      const result = await knowledgeIngest(contentType, raw);
      res.writeHead(result.ok ? 200 : 500, { 'Content-Type': 'application/json;charset=utf-8', 'Cache-Control': 'no-cache, no-store, must-revalidate' });
      res.end(JSON.stringify(result));
    } catch (err) {
      res.writeHead(500, { 'Content-Type': 'application/json;charset=utf-8' });
      res.end(JSON.stringify({ ok: false, error: String(err.message || err) }));
    }
    return;
  }

  // ── API: 知识库删除文档（转发后端 /api/knowledge/delete）──
  if (req.method === 'POST' && url === '/api/ontology/knowledge-delete') {
    try {
      const body = JSON.parse((await readBody(req)) || '{}');
      const { kb, doc_id } = body;
      if (!kb || !doc_id) {
        res.writeHead(400, { 'Content-Type': 'application/json;charset=utf-8' });
        res.end(JSON.stringify({ ok: false, error: 'kb 和 doc_id 必填' }));
        return;
      }
      const result = await knowledgeDelete(kb, doc_id);
      res.writeHead(result.ok ? 200 : 500, { 'Content-Type': 'application/json;charset=utf-8', 'Cache-Control': 'no-cache, no-store, must-revalidate' });
      res.end(JSON.stringify(result));
    } catch (err) {
      res.writeHead(500, { 'Content-Type': 'application/json;charset=utf-8' });
      res.end(JSON.stringify({ ok: false, error: String(err.message || err) }));
    }
    return;
  }

  // ── API: 知识库检索（转发后端 /api/knowledge/query，供查看文档切块）──
  if (req.method === 'POST' && url === '/api/ontology/knowledge-query') {
    try {
      const body = JSON.parse((await readBody(req)) || '{}');
      const { kb, q, top_k } = body;
      if (!q) {
        res.writeHead(400, { 'Content-Type': 'application/json;charset=utf-8' });
        res.end(JSON.stringify({ ok: false, error: 'q 必填' }));
        return;
      }
      const result = await knowledgeQuery(kb, q, top_k);
      res.writeHead(result.ok ? 200 : 500, { 'Content-Type': 'application/json;charset=utf-8', 'Cache-Control': 'no-cache, no-store, must-revalidate' });
      res.end(JSON.stringify(result));
    } catch (err) {
      res.writeHead(500, { 'Content-Type': 'application/json;charset=utf-8' });
      res.end(JSON.stringify({ ok: false, error: String(err.message || err) }));
    }
    return;
  }

  // ── API: 资产列表（转发后端 /api/assets/list）──
  if (req.method === 'GET' && url === '/api/ontology/assets-list') {
    try {
      const kb = new URL(req.url, 'http://x').searchParams.get('kb') || '';
      const result = await assetsList(kb);
      res.writeHead(result.ok ? 200 : 500, { 'Content-Type': 'application/json;charset=utf-8', 'Cache-Control': 'no-cache, no-store, must-revalidate' });
      res.end(JSON.stringify(result));
    } catch (err) {
      res.writeHead(500, { 'Content-Type': 'application/json;charset=utf-8' });
      res.end(JSON.stringify({ ok: false, error: String(err.message || err) }));
    }
    return;
  }

  // ── API: 资产快照（转发后端 /api/assets/snapshot）──
  if (req.method === 'POST' && url === '/api/ontology/assets-snapshot') {
    try {
      const body = JSON.parse((await readBody(req)) || '{}');
      const { kb, changelog } = body;
      if (!kb || typeof kb !== 'string') {
        res.writeHead(400, { 'Content-Type': 'application/json;charset=utf-8' });
        res.end(JSON.stringify({ ok: false, error: 'kb 必填' }));
        return;
      }
      const result = await assetsSnapshot(kb, changelog);
      res.writeHead(result.ok ? 200 : 500, { 'Content-Type': 'application/json;charset=utf-8', 'Cache-Control': 'no-cache, no-store, must-revalidate' });
      res.end(JSON.stringify(result));
    } catch (err) {
      res.writeHead(500, { 'Content-Type': 'application/json;charset=utf-8' });
      res.end(JSON.stringify({ ok: false, error: String(err.message || err) }));
    }
    return;
  }

  // ── API: 资产回滚（转发后端 /api/assets/rollback）──
  if (req.method === 'POST' && url === '/api/ontology/assets-rollback') {
    try {
      const body = JSON.parse((await readBody(req)) || '{}');
      const { kb, version } = body;
      if (!kb || typeof kb !== 'string' || !version || typeof version !== 'string') {
        res.writeHead(400, { 'Content-Type': 'application/json;charset=utf-8' });
        res.end(JSON.stringify({ ok: false, error: 'kb 和 version 必填' }));
        return;
      }
      const result = await assetsRollback(kb, version);
      res.writeHead(result.ok ? 200 : 500, { 'Content-Type': 'application/json;charset=utf-8', 'Cache-Control': 'no-cache, no-store, must-revalidate' });
      res.end(JSON.stringify(result));
    } catch (err) {
      res.writeHead(500, { 'Content-Type': 'application/json;charset=utf-8' });
      res.end(JSON.stringify({ ok: false, error: String(err.message || err) }));
    }
    return;
  }

  // ── API: 评测聚合（一次返回 benchmark + knowledge + assets，供 SPA 单 tab）──
  if (req.method === 'GET' && url === '/api/ontology/eval') {
    try {
      const kb = new URL(req.url, 'http://x').searchParams.get('kb') || '';
      // 并发拉取三组数据，任一失败不阻断整体（各子块带独立 ok 标志）
      const [benchmark, knowledge, assets] = await Promise.all([
        evalBenchmark(kb), knowledgeList(kb), assetsList(kb),
      ]);
      const result = { ok: true, kb, eval: benchmark, knowledge, assets };
      res.writeHead(200, { 'Content-Type': 'application/json;charset=utf-8', 'Cache-Control': 'no-cache, no-store, must-revalidate' });
      res.end(JSON.stringify(result));
    } catch (err) {
      res.writeHead(500, { 'Content-Type': 'application/json;charset=utf-8' });
      res.end(JSON.stringify({ ok: false, error: String(err.message || err) }));
    }
    return;
  }

  // ── API: 产线属性（多表 join）──
  if (req.method === 'GET' && url.startsWith('/api/ontology/line/')) {
    try {
      const lineId = decodeURIComponent(url.split('/').pop());
      const kb = new URL(req.url, 'http://x').searchParams.get('kb') || (req.user && req.user.kb) || '';
      const result = await lineInfo(lineId, kb);
      res.writeHead(result.ok ? 200 : 500, { 'Content-Type': 'application/json;charset=utf-8' });
      res.end(JSON.stringify(result));
    } catch (err) {
      res.writeHead(500, { 'Content-Type': 'application/json;charset=utf-8' });
      res.end(JSON.stringify({ ok: false, error: String(err.message || err) }));
    }
    return;
  }

  // ── 静态服务 public/ ──
  let path = url === '/' ? '/index.html' : url;
  if (!extname(path)) path = '/index.html';
  const filePath = join(DIST_DIR, path.replace(/^\/+/, ''));
  if (existsSync(filePath) && !existsSync(filePath + '.map')) {
    const ext = extname(filePath);
    res.writeHead(200, { 'Content-Type': MIME[ext] || 'application/octet-stream' });
    res.end(readFileSync(filePath));
  } else {
    // SPA fallback
    const idx = join(DIST_DIR, 'index.html');
    if (existsSync(idx)) {
      res.writeHead(200, { 'Content-Type': 'text/html;charset=utf-8' });
      res.end(readFileSync(idx));
    } else {
      res.writeHead(404, { 'Content-Type': 'text/plain;charset=utf-8' });
      res.end('前端未构建。请先运行 npm run build');
    }
  }
});

server.listen(PORT, '0.0.0.0', () => {
  console.log(`🌐 工厂本体问答服务已启动: http://localhost:${PORT}`);
  console.log(`   建模: POST /api/ontology/setup  |  问答: POST /api/ontology/ask`);
});
