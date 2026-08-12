// api.js — 前端 API 封装（fetchRetry：429/5xx 指数退避重试，上限 3 次）
const MAX_RETRIES = 3;

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// 429/5xx 自动重试：指数退避(1s,2s,4s)+抖动，上限 MAX_RETRIES 次；其余状态直接返回。
async function fetchRetry(url, options = {}) {
  let lastErr = null;
  for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
    try {
      const resp = await fetch(url, options);
      const retriable = resp.status === 429 || resp.status >= 500;
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

export async function askOntology(question, kb) {
  return fetchRetry('/api/ontology/ask', {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify({ question, kb }),
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

export async function fetchStats() {
  return fetchRetry('/api/ontology/stats');
}

export async function fetchLine(lineId) {
  return fetchRetry(`/api/ontology/line/${encodeURIComponent(lineId)}`);
}

export async function fetchSchema() {
  return fetchRetry('/api/ontology/schema', { cache: 'no-store' });
}

// 模型结构图（图结构 nodes/edges）：/api/ontology/graph → 后端本体实例图，供 ECharts 力导向图渲染
export async function fetchGraph() {
  return fetchRetry('/api/ontology/graph', { cache: 'no-store' });
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
