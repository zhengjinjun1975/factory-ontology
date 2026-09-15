// api.js — 前端 API 封装（fetchRetry：429/5xx 指数退避重试，上限 3 次）
const MAX_RETRIES = 3;
const TOKEN_KEY = 'factory_enterprise_token';

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// 会话 token 读写（localStorage）
export function getToken() {
  try { return localStorage.getItem(TOKEN_KEY) || ''; } catch (e) { return ''; }
}
export function setToken(t) {
  try { if (t) localStorage.setItem(TOKEN_KEY, t); else localStorage.removeItem(TOKEN_KEY); } catch (e) { /* 忽略 */ }
}
// 附加鉴权头（Authorization: Bearer）供所有业务请求
function authHeaders(headers = {}) {
  const t = getToken();
  if (t) headers['Authorization'] = 'Bearer ' + t;
  return headers;
}

// 429/5xx 自动重试：指数退避(1s,2s,4s)+抖动，上限 MAX_RETRIES 次；其余状态直接返回。
async function fetchRetry(url, options = {}) {
  let lastErr = null;
  const opts = { ...options, headers: authHeaders({ ...(options.headers || {}) }) };
  for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
    try {
      const resp = await fetch(url, opts);
      const retriable = resp.status === 429 || resp.status >= 500;
      if (resp.status === 401) {
        // 会话失效 → 返回统一 unauthenticated 标记，前端跳登录页
        try { const d = await resp.json(); return { ok: false, status: 401, ...d, unauthenticated: true }; }
        catch (e) { return { ok: false, status: 401, unauthenticated: true, error: '未登录或会话已失效' }; }
      }
      if (!retriable || attempt === MAX_RETRIES) {
        try {
          return await resp.json();
        } catch (e) {
          // 非 JSON 响应（网关/超时等）：fail-open 构造结构化错误
          return { ok: false, status: resp.status, error: `HTTP ${resp.status}` };
        }
      }
      lastErr = resp.status;
      await sleep(Math.pow(2, attempt) * 1000 + Math.random() * 500);
    } catch (err) {
      if (attempt === MAX_RETRIES) throw err;
      lastErr = err;
      await sleep(Math.pow(2, attempt) * 1000 + Math.random() * 500);
    }
  }
  throw lastErr;
}

const JSON_HEADERS = { 'Content-Type': 'application/json' };

// ── 企业用户登录/注册/会话 ──
export async function authLogin(username, password) {
  const res = await fetchRetry('/api/auth/login', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ username, password }) });
  if (res && res.ok && res.token) setToken(res.token);
  return res;
}
export async function authRegister(cfg) {
  const res = await fetchRetry('/api/auth/register', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(cfg) });
  if (res && res.ok && res.token) setToken(res.token);
  return res;
}
export async function authMe() {
  return fetchRetry('/api/auth/me', { cache: 'no-store' });
}
export async function authLogout() {
  try { await fetchRetry('/api/auth/logout', { method: 'POST', headers: JSON_HEADERS, body: '{}' }); } catch (e) { /* 忽略 */ }
  setToken('');
}
// 引导 onboarding 完成（确认企业 + 选行业 + 建本体后标记解锁）
export async function onboardEnterprise(cfg) {
  return fetchRetry('/api/enterprise/onboard', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(cfg) });
}
// 企业重置（清空当前企业数据 → 重新 onboarding）
export async function resetEnterprise() {
  return fetchRetry('/api/enterprise/reset', { method: 'POST', headers: JSON_HEADERS, body: '{}' });
}

export async function setupOntology(csvName, csvContent) {
  return fetchRetry('/api/ontology/setup', {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify({ csvName, csvContent }),
  });
}

export async function setupOntologyMulti(files, kb) {
  return fetchRetry('/api/ontology/setup-multi', {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify({ files, kb }),
  });
}

export async function dbSetup(cfg) {
  return fetchRetry('/api/ontology/db-setup', {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify(cfg),
  });
}

export async function askOntology(question, kb, deepRecall = false) {
  return fetchRetry('/api/ontology/ask', {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify({ question, kb, deep_recall: !!deepRecall }),
  });
}

// ── 多租户 kb 切换 ──
// 读取已注册知识库列表 + 当前激活 kb（后端 /api/ontology/kbs）
export async function fetchKbs() {
  return fetchRetry('/api/ontology/kbs', { cache: 'no-store' });
}

// 切换当前激活 kb（后端 setCurrentKb 持久化到 web_state）
export async function setKb(kb) {
  return fetchRetry('/api/ontology/kb', {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify({ kb }),
  });
}

export async function fetchStats(kb) {
  const q = kb ? `?kb=${encodeURIComponent(kb)}` : '';
  return fetchRetry(`/api/ontology/stats${q}`);
}

/** 本体标准合规度（GB/T 48000.3 描述项 + 命名空间 + SHACL + 类层次 + 导出物） */
export function fetchStandard() {
  return fetchRetry('/api/ontology/standard');
}

/** 生成标准导出物（ontology.ttl / shapes.ttl / ontology.jsonld） */
export function buildStandardExport() {
  return fetchRetry('/api/ontology/standard-export', { method: 'POST' });
}

/** 导入层自检：导出的 ontology.ttl 读回来是否无损往返 */
export function fetchRoundtrip() {
  return fetchRetry('/api/ontology/standard-roundtrip');
}

/** 外部本体对齐：把外部本体（Turtle/JSON-LD 文本）与 schema 做同名类匹配报告 */
export function importAlign(content) {
  return fetchRetry('/api/ontology/standard-import-align', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content }),
  });
}

/** 建模质量门：标签/定义/外键关系/结构 体检 + 阈值判定（当前 kb） */
export function fetchQuality(kb) {
  return fetchRetry('/api/ontology/standard-quality' + (kb ? `?kb=${encodeURIComponent(kb)}` : ''));
}

/** 下载标准导出物。
 *  必须走带鉴权头的 fetch：直接 a.href 导航不会带 Authorization → 服务端 401,
 *  浏览器就报"无法从网站上提取文件"。取回 blob 再触发下载。 */
export async function downloadStandard(file) {
  const resp = await fetch(`/api/ontology/standard-download?file=${encodeURIComponent(file)}`,
                           { headers: authHeaders() });
  if (!resp.ok) {
    let msg = `HTTP ${resp.status}`;
    try { const j = await resp.json(); if (j && j.error) msg = j.error; } catch (e) { /* 非 JSON */ }
    throw new Error(msg);
  }
  const url = URL.createObjectURL(await resp.blob());
  const a = document.createElement('a');
  a.href = url;
  a.download = file;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
}

export async function fetchLine(lineId, kb) {
  const q = kb ? `?kb=${encodeURIComponent(kb)}` : '';
  return fetchRetry(`/api/ontology/line/${encodeURIComponent(lineId)}${q}`);
}

export async function fetchSchema(kb) {
  const q = kb ? `?kb=${encodeURIComponent(kb)}` : '';
  return fetchRetry(`/api/ontology/schema${q}`, { cache: 'no-store' });
}

// 模型结构图（图结构 nodes/edges）：/api/ontology/graph → 后端本体实例图，供 ECharts 力导向图渲染
// @param {string} [kb] 知识库名；传入则图跟随该 kb（调 ?kb=<kb>），缺省不传由后端走当前激活 kb
export async function fetchGraph(kb) {
  // 有 kb 时带上 ?kb=<kb>，让模型结构图跟随当前本体（非 food 默认）；无则省略走后端默认
  const q = kb ? `?kb=${encodeURIComponent(kb)}` : '';
  return fetchRetry(`/api/ontology/graph${q}`, { cache: 'no-store' });
}

export async function analyzeOntology(question, kb) {
  return fetchRetry('/api/ontology/analyze', {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify({ question, kb }),
  });
}

export async function getModel() {
  return fetchRetry('/api/ontology/model');
}

export async function setModel(key) {
  return fetchRetry('/api/ontology/model', {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify({ key }),
  });
}

export async function getModels() {
  return fetchRetry('/api/ontology/models');
}

export async function saveModels(cfg) {
  return fetchRetry('/api/ontology/models', {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify(cfg),
  });
}

export async function fetchVersion() {
  return fetchRetry('/api/ontology/version');
}

export async function fetchExamples() {
  return fetchRetry('/api/ontology/examples');
}

// 隔离评测（自定义问题集）：转发到前端 server /api/ontology/eval-isolate
export async function evalIsolate(kb, questions) {
  return fetchRetry('/api/ontology/eval-isolate', {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify({ kb, questions }),
  });
}

export async function fetchExample(path) {
  return fetchRetry(`/api/ontology/example?path=${encodeURIComponent(path)}`);
}

export async function browseFiles(dir) {
  const q = dir ? `?dir=${encodeURIComponent(dir)}` : '';
  return fetchRetry(`/api/ontology/browse${q}`);
}

export async function readDataFile(path) {
  return fetchRetry(`/api/ontology/read-data?path=${encodeURIComponent(path)}`);
}

// ── 自助建模（四步流程的前三步：选数据源 → AI 建议可编辑 → 人拍板确认生效）──
/** ① AI 建议（只读预览，不落盘）：从 kb + 数据目录推断实体/属性/关系/约束/业务域。
 *  可反复调用直到人工满意，不产生任何产物。 */
export async function suggestSchema(kb, dataDir) {
  return fetchRetry('/api/ontology/suggest', {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify({ kb, data_dir: dataDir }),
  });
}

/** ② 人拍板：把人工确认/修改后的完整 schema 原样回传（entities 必须含 attribute.role），
 *  后端按它产出 nt + lexicon 并注册 kb。 */
export async function confirmSchema(kb, schema, dataDir) {
  return fetchRetry('/api/ontology/confirm', {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify({ kb, schema, data_dir: dataDir }),
  });
}

// ── 企业设置（企业名/logo/行业，后端持久化）──
export async function fetchEnterprise() {
  return fetchRetry('/api/ontology/enterprise', { cache: 'no-store' });
}

export async function saveEnterprise(cfg) {
  return fetchRetry('/api/ontology/enterprise', {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify(cfg),
  });
}

// ── 行业清单与行业切换（事件驱动无死角）──
// 行业下拉动态加载：从后端 kbs.json 全量读取全部可建模行业 {kb,name,icon,dir}。
export async function fetchIndustries() {
  return fetchRetry('/api/ontology/industries', { cache: 'no-store' });
}

// 行业切换（显式触发自动建模）：后端用该行业数据目录自动建本体并联动企业 kb/激活 kb。
export async function switchIndustry(industry) {
  return fetchRetry('/api/ontology/industry-switch', {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify({ industry }),
  });
}
