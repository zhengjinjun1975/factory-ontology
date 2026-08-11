// index.js — 工厂本体问答 Web 应用服务器
// 静态服务 public/ + API（/api/ontology/setup, /api/ontology/ask）+ health
import { createServer } from 'http';
import { readFileSync, existsSync } from 'fs';
import { extname, join, dirname } from 'path';
import { fileURLToPath } from 'url';
import { setupOntology, askOntology, statsOntology, lineInfo, schemaOntology, analyzeOntology, getModel, setModel, listExamples, readExample, setupOntologyMulti, dbSetup } from './ontology.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const PORT = process.env.PORT || 3001;
const DIST_DIR = join(__dirname, '..', 'public');

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

const server = createServer(async (req, res) => {
  const url = req.url.split('?')[0];

  // ── API: 上传建模 ──
  if (req.method === 'POST' && url === '/api/ontology/setup') {
    try {
      const body = JSON.parse((await readBody(req)) || '{}');
      const { csvName, csvContent } = body;
      if (!csvName || typeof csvContent !== 'string') {
        res.writeHead(400, { 'Content-Type': 'application/json;charset=utf-8' });
        res.end(JSON.stringify({ ok: false, error: 'csvName 和 csvContent 必填' }));
        return;
      }
      const result = await setupOntology(csvName, csvContent);
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
      const { files } = body;
      if (!Array.isArray(files) || files.length === 0) {
        res.writeHead(400, { 'Content-Type': 'application/json;charset=utf-8' });
        res.end(JSON.stringify({ ok: false, error: 'files 必填（[{name, content}] 数组）' }));
        return;
      }
      const result = await setupOntologyMulti(files);
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
      const { question } = body;
      if (!question || typeof question !== 'string') {
        res.writeHead(400, { 'Content-Type': 'application/json;charset=utf-8' });
        res.end(JSON.stringify({ ok: false, error: 'question 必填' }));
        return;
      }
      const result = await askOntology(question);
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

  // ── API: 代码版本（读 codes/run.py 的 __version__，单一事实源）──
  if (url === '/api/ontology/version') {
    let version = '0.1.4';
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

  // ── API: 智能分析（统计+洞察，前端画图+报告）──
  if (req.method === 'POST' && url === '/api/ontology/analyze') {
    try {
      const body = JSON.parse((await readBody(req)) || '{}');
      const { question } = body;
      if (!question || typeof question !== 'string') {
        res.writeHead(400, { 'Content-Type': 'application/json;charset=utf-8' });
        res.end(JSON.stringify({ ok: false, error: 'question 必填' }));
        return;
      }
      const result = await analyzeOntology(question);
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
      const result = await schemaOntology();
      res.writeHead(result.ok ? 200 : 500, { 'Content-Type': 'application/json;charset=utf-8', 'Cache-Control': 'no-cache, no-store, must-revalidate' });
      res.end(JSON.stringify(result));
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

  // ── API: 聚合统计（可视化）──
  if (req.method === 'GET' && url === '/api/ontology/stats') {
    try {
      const result = await statsOntology();
      res.writeHead(result.ok ? 200 : 500, { 'Content-Type': 'application/json;charset=utf-8' });
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
      const result = await lineInfo(lineId);
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
