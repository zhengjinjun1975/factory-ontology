<script>
  // 工厂智能体 · 本体问答 — 独立 Web 应用（工业软件浅色风格）
  import { onMount } from 'svelte';
  import { setupOntology, askOntology, analyzeOntology, getModel, setModel, fetchVersion, fetchExamples, fetchExample } from './lib/api.js';
  import DashboardPanel from './components/DashboardPanel.svelte';
  import ModelGraph from './components/ModelGraph.svelte';
  import AnalysisResult from './components/AnalysisResult.svelte';

  // ─── 状态 ───
  let activeTab = $state('model');   // model | query | dashboard
  let fileName = $state('');
  let fileContent = $state('');
  let modeling = $state(false);
  let modelResult = $state(null);   // {table, attrs}
  let question = $state('');
  let asking = $state(false);
  let answer = $state('');
  let answerHTML = $state(null);   // 结构化答案 HTML（列表/表格）；null 则退回 <pre>
  let evidence = $state(null);     // 问答证据溯源
  let evidenceOpen = $state(false);
  let analysis = $state(null);       // {report, stats} 智能分析结果
  let modelList = $state([]);        // 可用模型
  let activeModel = $state('');      // 当前生效模型 key
  let appVersion = $state('');       // 代码版本(读后端)
  let examples = $state([]);         // 示例文件列表 [{name, path, size}]
  let examplePath = $state('');      // 当前选中的示例路径
  let status = $state('idle');       // idle | modeling | ready | asking
  let statusMsg = $state('等待数据导入');
  let statusType = $state('info');   // info | ok | err
  let answerBox = $state(null);

  const quickQuestions = [
    '有多少台运行中的设备',
    '车间A的设备有哪些',
    '功率最大的设备',
    '有多少台报警的',
    'L1产线的设备有哪些',
  ];

  function setStatus(type, msg) {
    statusType = type; statusMsg = msg;
  }

  function switchTab(tab) {
    activeTab = tab;
  }

  // ─── 模型配置加载与切换 ───
  onMount(async () => {
    try {
      const res = await getModel();
      if (res.ok) {
        modelList = res.models || [];
        activeModel = res.active || '';
      }
    } catch (e) { /* 忽略 */ }
    try {
      const v = await fetchVersion();
      if (v.ok && v.version) appVersion = v.version;
    } catch (e) { /* 忽略 */ }
    // 加载示例数据，默认选中第一个并自动建模
    try {
      const ex = await fetchExamples();
      if (ex.ok && Array.isArray(ex.examples) && ex.examples.length) {
        examples = ex.examples;
        applyExample(ex.examples[0].path);
      }
    } catch (e) { /* 忽略 */ }
  });

  // 加载示例文件内容并建模（复用现有 fileName/fileContent/doSetup 流程）
  async function applyExample(path) {
    if (!path || modeling) return;
    try {
      const res = await fetchExample(path);
      if (!res.ok) {
        setStatus('err', res.error || '示例加载失败'); return;
      }
      fileName = res.name || (examples.find(x => x.path === path)?.name) || path;
      fileContent = res.content;
      examplePath = path;
      await doSetup();
    } catch (err) {
      setStatus('err', '网络错误，请确认服务已启动');
    }
  }

  async function switchModel(e) {
    const key = e.target.value;
    try {
      const res = await setModel(key);
      if (res.ok) {
        activeModel = res.active;
        setStatus('ok', `模型已切换：${res.active}`);
      } else {
        setStatus('err', res.error || '切换失败');
      }
    } catch (err) {
      setStatus('err', '网络错误');
    }
  }

  // ─── 文件选择 ───
  function onFileChange(e) {
    const file = e.target.files && e.target.files[0];
    if (!file) return;
    if (!/\.(csv|json)$/i.test(file.name)) {
      setStatus('err', '仅支持 .csv / .json 文本文件'); return;
    }
    fileName = file.name;
    const reader = new FileReader();
    reader.onload = () => { fileContent = reader.result; };
    reader.readAsText(file, 'utf-8');
  }

  // ─── 上传建模 ───
  async function doSetup() {
    if (!fileName || !fileContent || modeling) return;
    modeling = true; status = 'modeling';
    setStatus('info', `正在建模 ${fileName} …`);
    try {
      const res = await setupOntology(fileName, fileContent);
      if (!res.ok) {
        setStatus('err', res.error || '建模失败'); status = 'idle';
      } else {
        modelResult = { table: res.table, attrs: res.attrs || [], ts: Date.now() };
        status = 'ready';
        setStatus('ok', `建模完成：${res.table}，共 ${(res.attrs||[]).length} 个字段`);
      }
    } catch (err) {
      setStatus('err', '网络错误，请确认服务已启动'); status = 'idle';
    } finally { modeling = false; }
  }

  // ─── 提问（普通问答 + 智能分析路由）───
  // 分析意图关键词
  const ANALYZE_KEYWORDS = ['分析', '比较', '对比', '关注', '趋势', '整体', '状况', '健康', '产能', '评估', '总结', '分布', '建议', '异常情况'];

  function isAnalyzeQuestion(q) {
    return ANALYZE_KEYWORDS.some(k => q.includes(k));
  }

  async function doAsk(text) {
    const q = (text ?? question).trim();
    if (!q || asking) return;
    question = ''; asking = true; status = 'asking';
    answer = ''; answerHTML = null; evidence = null; evidenceOpen = false; analysis = null;
    setStatus('info', `查询：${q}`);
    try {
      if (isAnalyzeQuestion(q)) {
        // 智能分析：统计摘要 + LLM 洞察（前端画图 + 报告）
        const res = await analyzeOntology(q);
        if (!res.ok) {
          setStatus('err', res.error || '分析失败'); status = 'ready';
        } else {
          analysis = { report: res.report, stats: res.stats };
          status = 'ready';
          setStatus('ok', `分析完成：${q}`);
        }
      } else {
        // 普通问答
        const res = await askOntology(q);
        if (!res.ok) {
          setStatus('err', res.error || '问答失败'); status = 'ready';
        } else {
          answer = res.answer || '（无结果）';
          answerHTML = renderAnswerHTML(res.answer);
          evidence = res.evidence || null;
          status = 'ready';
          setStatus('ok', `查询完成：${q}`);
        }
      }
    } catch (err) {
      setStatus('err', '网络错误，请确认服务已启动'); status = 'ready';
    } finally {
      asking = false;
      if (answerBox) answerBox.scrollTop = answerBox.scrollHeight;
    }
  }

  function onKeydown(e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); doAsk(); }
  }

  // ─── 结构化答案渲染（极简，无第三方库）───
  function escapeHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  // 返回 HTML；若不含表格/列表结构则返回 null（调用方退回 <pre>）
  function renderAnswerHTML(text) {
    const lines = String(text || '').split('\n');
    let html = ''; let changed = false; let i = 0;
    while (i < lines.length) {
      const line = lines[i]; const t = line.trim();
      // 表格：连续两行以上含 |
      if (line.includes('|')) {
        const rows = [];
        while (i < lines.length && lines[i].includes('|')) {
          const cells = lines[i].split('|').map(c => c.trim());
          while (cells.length && cells[0] === '') cells.shift();
          while (cells.length && cells[cells.length - 1] === '') cells.pop();
          rows.push(cells); i++;
        }
        if (rows.length >= 2) {
          changed = true;
          html += '<table class="ans-table"><thead><tr>' +
            rows[0].map(c => '<th>' + escapeHtml(c) + '</th>').join('') +
            '</tr></thead><tbody>' +
            rows.slice(1).map(r => '<tr>' + r.map(c => '<td>' + escapeHtml(c) + '</td>').join('') + '</tr>').join('') +
            '</tbody></table>';
        } else {
          for (const r of rows) html += escapeHtml(r.join(' | ')) + '<br>';
        }
        continue;
      }
      // 列表：`信息(N):`/`字段信息(N):` 后跟 `- xxx`
      const m = t.match(/^(.*?信息\s*[（(]?\d+[）)]?:?)$/);
      if (m) {
        let j = i + 1; const items = [];
        while (j < lines.length && /^\s*[-•]\s/.test(lines[j])) {
          items.push(lines[j].replace(/^\s*[-•]\s*/, '')); j++;
        }
        if (items.length > 0) {
          changed = true;
          html += '<div class="ans-head">' + escapeHtml(t) + '</div><ul class="ans-list">' +
            items.map(it => '<li>' + escapeHtml(it) + '</li>').join('') + '</ul>';
          i = j; continue;
        }
      }
      html += escapeHtml(line) + '<br>'; i++;
    }
    return changed ? html : null;
  }
  // 证据溯源渲染（entities / 对象 / 数组，通用展示）
  function renderEvidence(ev) {
    if (!ev) return '';
    if (typeof ev === 'string') return '<div class="ev-text">' + escapeHtml(ev) + '</div>';
    const list = Array.isArray(ev) ? ev : (ev.entities || ev.rows || []);
    const items = [];
    if (Array.isArray(list) && list.length) {
      for (const it of list) {
        if (typeof it === 'string') items.push('<li>' + escapeHtml(it) + '</li>');
        else if (it && typeof it === 'object')
          items.push('<li>' + Object.entries(it)
            .map(([k, v]) => '<span class="ev-k">' + escapeHtml(k) + '</span>：' + escapeHtml(typeof v === 'string' ? v : JSON.stringify(v)))
            .join('；') + '</li>');
      }
    } else if (ev && typeof ev === 'object') {
      for (const [k, v] of Object.entries(ev))
        items.push('<li><span class="ev-k">' + escapeHtml(k) + '</span>：' + escapeHtml(typeof v === 'string' ? v : JSON.stringify(v)) + '</li>');
    }
    return items.length ? '<ul class="ev-list">' + items.join('') + '</ul>' : '';
  }
</script>

<div class="app">
  <!-- ═══ 顶部工具栏 ═══ -->
  <header class="toolbar">
    <div class="toolbar-left">
      <span class="logo">🏭</span>
      <div class="brand">
        <div class="brand-name">工厂智能体 · 本体问答</div>
        <div class="brand-sub">Factory Ontology QA System{#if appVersion} · v{appVersion}{/if}</div>
      </div>
    </div>
    <div class="toolbar-right">
      {#if modelList.length > 0}
        <label class="model-select">
          <span class="model-label">模型</span>
          <select value={activeModel} onchange={switchModel}>
            {#each modelList as m}
              <option value={m.key}>{m.name}</option>
            {/each}
          </select>
        </label>
      {/if}
      <span class="status-indicator" class:st-ok={statusType === 'ok'} class:st-err={statusType === 'err'} class:st-info={statusType === 'info'}>
        <span class="status-dot"></span>
        <span class="status-text">{statusMsg}</span>
      </span>
    </div>
  </header>

  <!-- ═══ 标签栏 ═══ -->
  <nav class="tabbar">
    <button class="tab" class:active={activeTab === 'model'} onclick={() => switchTab('model')}>
      <span class="tab-icon">📊</span> 数据建模
    </button>
    <button class="tab" class:active={activeTab === 'query'} onclick={() => switchTab('query')}>
      <span class="tab-icon">💬</span> 查询分析
    </button>
    <button class="tab" class:active={activeTab === 'dashboard'} onclick={() => switchTab('dashboard')}>
      <span class="tab-icon">📈</span> 数据看板
    </button>
  </nav>

  <!-- ═══ 主区域 ═══ -->
  <main class="workspace">
    {#if activeTab === 'model'}
    <!-- ─── 左栏：数据建模 ─── -->
    <section class="pane pane-left">
      <div class="pane-title">数据建模</div>

      <div class="form-group">
        <label class="form-label" for="example-select">示例数据（一键体验）</label>
        <select
          id="example-select"
          class="example-select"
          value={examplePath}
          onchange={(e) => applyExample(e.target.value)}
          disabled={examples.length === 0}
        >
          <option value="">选择示例文件…</option>
          {#each examples as ex}
            <option value={ex.path}>{ex.name}</option>
          {/each}
        </select>
      </div>

      <div class="form-group">
        <label class="form-label">数据文件（CSV/JSON）</label>
        <label class="file-input">
          <input type="file" accept=".csv,.json" onchange={onFileChange} />
          <span class="file-icon">📄</span>
          <span class="file-name">{fileName || '选择 CSV/JSON 文件…'}</span>
        </label>
      </div>

      <div class="form-group">
        <button class="btn-action" onclick={doSetup} disabled={modeling || !fileName || !fileContent}>
          <span class="btn-icon">{modeling ? '⏳' : '⚙'}</span>
          {modeling ? '建模进行中…' : '上传并建模'}
        </button>
      </div>

      {#if modelResult}
        <div class="model-panel">
          <div class="model-head">
            <span class="model-table">数据表：{modelResult.table}</span>
            <span class="model-count">{modelResult.attrs.length} 字段</span>
          </div>
          <table class="data-table">
            <thead>
              <tr><th>字段</th><th>中文名</th></tr>
            </thead>
            <tbody>
              {#each modelResult.attrs as a}
                <tr><td>{a.field}</td><td>{a.cn}</td></tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    </section>

    <!-- ─── 右栏：模型结构图 ─── -->
    <section class="pane pane-right">
      <div class="pane-title">模型结构</div>
      <div class="graph-body">
        {#if modelResult}
          <!-- refreshKey=ts：重新建模 ts 变 → ModelGraph 的 $effect 触发重新加载 -->
          <ModelGraph refreshKey={modelResult.ts} />
        {:else}
          <div class="graph-empty">上传并建模后，此处展示本体的真实结构（类 / 属性 / 关系）。</div>
        {/if}
      </div>
    </section>
    {:else if activeTab === 'query'}
    <!-- ─── 查询分析（独立标签页）─── -->
    <section class="pane pane-full">
      <div class="pane-title">查询分析</div>
      <div class="query-body">
        <div class="query-bar">
          <input
            type="text"
            class="query-input"
            placeholder="输入中文查询语句…"
            bind:value={question}
            onkeydown={onKeydown}
            disabled={asking || status !== 'ready'}
          />
          <button class="btn-query" onclick={() => doAsk()} disabled={asking || !question.trim() || status !== 'ready'}>
            {asking ? '查询中…' : '查 询'}
          </button>
        </div>

        <div class="quick-bar">
          {#each quickQuestions as q}
            <button class="quick-btn" onclick={() => doAsk(q)} disabled={asking || status !== 'ready'}>{q}</button>
          {/each}
        </div>

        <div class="result-panel">
          {#if status === 'asking'}
            <div class="result-empty loading">
              <span class="load-dot"></span><span class="load-dot"></span><span class="load-dot"></span>
              正在查询本体…
            </div>
          {:else if analysis}
            <div class="analysis-body">
              <AnalysisResult stats={analysis.stats} report={analysis.report} />
            </div>
          {:else if answer}
            <div class="result-head">
              <span class="result-label">查询结果</span>
            </div>
            <div class="result-scroll" bind:this={answerBox}>
              {#if answerHTML}
                <div class="ans-body">{@html answerHTML}</div>
              {:else}
                <pre class="result-text">{answer}</pre>
              {/if}
            </div>
            {#if evidence}
              <div class="evidence-wrap">
                <button class="evidence-toggle" onclick={() => (evidenceOpen = !evidenceOpen)}>
                  📎 证据溯源
                  <span class="chevron">{evidenceOpen ? '▾' : '▸'}</span>
                </button>
                {#if evidenceOpen}
                  <div class="evidence-body">{@html renderEvidence(evidence)}</div>
                {/if}
              </div>
            {/if}
          {:else}
            <div class="result-empty">请先在「数据建模」中完成建模，再进行查询。</div>
          {/if}
        </div>
      </div>
    </section>
    {:else if activeTab === 'dashboard'}
    <section class="pane pane-full">
      <div class="pane-title">数据看板</div>
      <div class="dashboard-body">
        <DashboardPanel />
      </div>
    </section>
    {/if}
  </main>

  <footer class="statusbar">
    <span class="sb-left">工厂智能体 · 本体问答系统</span>
    <span class="sb-right">数据本地处理 ｜ 运行时：Node.js + Python</span>
  </footer>
</div>

<style>
  :global(*) { box-sizing: border-box; }
  :global(body) {
    margin: 0;
    font-family: 'Segoe UI', 'Microsoft YaHei', 'PingFang SC', sans-serif;
    background: #eef1f5;  /* 工业浅灰底 */
    color: #2d3436;
    min-height: 100vh;
  }

  .app { min-height: 100vh; display: flex; flex-direction: column; }

  /* ─── 顶部工具栏 ─── */
  .toolbar {
    display: flex; align-items: center; justify-content: space-between;
    padding: 10px 18px;
    background: #ffffff;
    border-bottom: 1px solid #d5dbe3;
    box-shadow: 0 1px 2px rgba(0,0,0,0.04);
    flex-shrink: 0;
  }
  .toolbar-left { display: flex; align-items: center; gap: 12px; }
  .toolbar-right { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .model-select { display: flex; align-items: center; gap: 6px; font-size: 11px; color: #64748b; }
  .model-label { font-weight: 600; }
  .model-select select {
    background: #fff; border: 1px solid #d5dbe3; border-radius: 4px;
    padding: 4px 8px; font-size: 12px; color: #1e293b; cursor: pointer;
  }
  .model-select select:focus { outline: none; border-color: #3b82f6; }
  .logo { font-size: 22px; }
  .brand-name { font-size: 15px; font-weight: 700; color: #1e293b; letter-spacing: 0.2px; }
  .brand-sub { font-size: 11px; color: #8892a4; letter-spacing: 0.4px; }

  .status-indicator {
    display: flex; align-items: center; gap: 8px;
    font-size: 12px; font-weight: 500;
    padding: 5px 14px; border-radius: 4px;
    border: 1px solid transparent;
    background: #f1f5f9;
  }
  .status-dot { width: 8px; height: 8px; border-radius: 50%; background: #94a3b8; }
  .st-info .status-dot { background: #3b82f6; }
  .st-ok   .status-dot { background: #16a34a; }
  .st-err  .status-dot { background: #dc2626; }
  .st-info .status-text { color: #2563eb; }
  .st-ok   .status-text { color: #16a34a; }
  .st-err  .status-text { color: #dc2626; }
  .st-ok  { background: #f0fdf4; border-color: #bbf7d0; }
  .st-err { background: #fef2f2; border-color: #fecaca; }
  .st-info{ background: #eff6ff; border-color: #bfdbfe; }

  /* ─── 标签栏 ─── */
  .tabbar {
    display: flex; gap: 4px; padding: 10px 18px 0;
    flex-shrink: 0;
  }
  .tab {
    display: flex; align-items: center; gap: 6px;
    background: #e2e8f0; border: 1px solid #d5dbe3; border-bottom: none;
    border-radius: 6px 6px 0 0;
    padding: 8px 18px; font-size: 13px; font-weight: 600; color: #64748b;
    cursor: pointer; transition: all 0.15s;
  }
  .tab:hover { color: #1e293b; background: #eef2f7; }
  .tab.active {
    background: #ffffff; color: #2563eb; border-color: #d5dbe3;
    box-shadow: 0 -2px 6px rgba(0,0,0,0.04);
  }
  .tab-icon { font-size: 14px; }

  /* ─── 主区域 ─── */
  .workspace {
    flex: 1; display: grid;
    grid-template-columns: minmax(300px, 360px) 1fr;
    gap: 14px; padding: 14px 18px;
    min-height: 0;
  }
  .pane-full {
    grid-column: 1 / -1;
    background: #ffffff;
    border: 1px solid #d5dbe3; border-radius: 4px;
    display: flex; flex-direction: column; min-height: 0;
  }
  .dashboard-body { padding: 14px; overflow-y: auto; }
  .graph-body { padding: 14px; overflow-y: auto; }
  .graph-empty { color: #94a3b8; font-size: 13px; text-align: center; padding: 60px 20px; }
  .query-body { padding: 14px; display: flex; flex-direction: column; gap: 12px; }

  .pane {
    background: #ffffff;
    border: 1px solid #d5dbe3;
    border-radius: 4px;
    display: flex; flex-direction: column;
    min-height: 0;
  }
  .pane-title {
    padding: 9px 14px;
    font-size: 12px; font-weight: 700; color: #1e293b;
    background: #f8fafc;
    border-bottom: 1px solid #e2e8f0;
    letter-spacing: 0.5px;
    flex-shrink: 0;
  }

  /* ─── 左栏表单 ─── */
  .pane-left { padding: 14px; gap: 14px; overflow-y: auto; }
  .form-group { display: flex; flex-direction: column; gap: 6px; }
  .form-label { font-size: 12px; color: #64748b; font-weight: 600; }

  .example-select {
    width: 100%; background: #fff; border: 1px solid #cbd5e1;
    border-radius: 4px; padding: 9px 12px; font-size: 13px; color: #1e293b;
    cursor: pointer; outline: none; transition: border-color 0.15s;
  }
  .example-select:focus { border-color: #3b82f6; }
  .example-select:disabled { background: #f8fafc; cursor: not-allowed; }

  .file-input {
    display: flex; align-items: center; gap: 10px;
    background: #f8fafc; border: 1px solid #cbd5e1;
    border-radius: 4px; padding: 10px 12px;
    cursor: pointer; transition: border-color 0.15s;
  }
  .file-input:hover { border-color: #3b82f6; }
  .file-input input { display: none; }
  .file-icon { font-size: 16px; }
  .file-name { font-size: 13px; color: #334155; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

  .btn-action {
    display: flex; align-items: center; justify-content: center; gap: 8px;
    background: #2563eb; color: #fff;
    border: none; border-radius: 4px;
    padding: 10px 16px; font-size: 13px; font-weight: 600;
    cursor: pointer; transition: background 0.15s;
  }
  .btn-action:hover:not(:disabled) { background: #1d4ed8; }
  .btn-action:disabled { background: #94a3b8; cursor: not-allowed; }
  .btn-icon { font-size: 14px; }

  /* ─── 字段表 ─── */
  .model-panel { border: 1px solid #e2e8f0; border-radius: 4px; overflow: hidden; }
  .model-head {
    display: flex; justify-content: space-between; align-items: center;
    padding: 8px 12px; background: #f8fafc;
    border-bottom: 1px solid #e2e8f0;
  }
  .model-table { font-size: 12px; font-weight: 700; color: #1e293b; }
  .model-count { font-size: 11px; color: #64748b; }
  .data-table { width: 100%; border-collapse: collapse; }
  .data-table th, .data-table td {
    padding: 6px 12px; font-size: 12px; text-align: left;
    border-bottom: 1px solid #f1f5f9;
  }
  .data-table th { background: #f8fafc; color: #64748b; font-weight: 600; }
  .data-table td { color: #334155; }
  .data-table td:nth-child(1) { font-family: 'Consolas', monospace; color: #2563eb; }

  /* ─── 右栏 ─── */
  .pane-right { padding: 14px; gap: 12px; }
  .query-bar { display: flex; gap: 8px; }
  .query-input {
    flex: 1; background: #fff; border: 1px solid #cbd5e1;
    border-radius: 4px; padding: 9px 12px;
    font-size: 14px; color: #1e293b; outline: none;
    transition: border-color 0.15s;
  }
  .query-input:focus { border-color: #3b82f6; }
  .query-input:disabled { background: #f8fafc; }

  .btn-query {
    background: #1e293b; color: #fff; border: none; border-radius: 4px;
    padding: 9px 20px; font-size: 13px; font-weight: 600; cursor: pointer;
    transition: background 0.15s;
  }
  .btn-query:hover:not(:disabled) { background: #0f172a; }
  .btn-query:disabled { background: #94a3b8; cursor: not-allowed; }

  .quick-bar { display: flex; gap: 6px; flex-wrap: wrap; }
  .quick-btn {
    background: #f1f5f9; color: #334155; border: 1px solid #e2e8f0;
    font-size: 11px; padding: 4px 10px; border-radius: 3px;
    cursor: pointer; transition: all 0.15s;
  }
  .quick-btn:hover:not(:disabled) { background: #e2e8f0; border-color: #cbd5e1; }
  .quick-btn:disabled { opacity: 0.5; cursor: not-allowed; }

  /* ─── 结果区 ─── */
  .result-panel {
    flex: 1; border: 1px solid #e2e8f0; border-radius: 4px;
    background: #fbfcfe; min-height: 220px;
    display: flex; flex-direction: column; overflow: hidden;
  }
  .result-head {
    padding: 7px 12px; background: #f8fafc;
    border-bottom: 1px solid #e2e8f0;
  }
  .result-label { font-size: 11px; font-weight: 700; color: #64748b; letter-spacing: 0.5px; }
  .result-scroll { flex: 1; overflow-y: auto; }
  .result-text {
    margin: 0; padding: 14px;
    font-family: 'Consolas', 'Menlo', monospace;
    font-size: 13px; line-height: 1.7; color: #1e293b;
    white-space: pre-wrap; word-break: break-word;
  }
  .ans-body { padding: 14px; font-size: 13px; line-height: 1.8; color: #1e293b; }
  :global(.ans-head) { font-weight: 700; color: #1e293b; margin: 6px 0 4px; }
  :global(.ans-list) { margin: 0 0 8px; padding-left: 20px; }
  :global(.ans-list li) { margin: 2px 0; }
  :global(.ans-table) { width: 100%; border-collapse: collapse; margin: 6px 0 10px; }
  :global(.ans-table th), :global(.ans-table td) {
    border: 1px solid #e2e8f0; padding: 6px 10px; text-align: left;
    font-size: 12px; color: #334155;
  }
  :global(.ans-table th) { background: #f1f5f9; color: #475569; font-weight: 600; }

  /* ─── 证据溯源 ─── */
  .evidence-wrap { border-top: 1px solid #e2e8f0; background: #f8fafc; }
  .evidence-toggle {
    width: 100%; display: flex; align-items: center; gap: 6px;
    padding: 8px 12px; background: transparent; border: none;
    font-size: 12px; font-weight: 600; color: #475569; cursor: pointer;
    text-align: left; transition: background 0.15s;
  }
  .evidence-toggle:hover { background: #f1f5f9; color: #2563eb; }
  .chevron { margin-left: auto; font-size: 11px; color: #94a3b8; }
  .evidence-body { padding: 4px 12px 12px; border-top: 1px dashed #e2e8f0; }
  :global(.ev-list) { margin: 6px 0 0; padding-left: 18px; font-size: 12px; color: #334155; }
  :global(.ev-list li) { margin: 3px 0; line-height: 1.6; }
  :global(.ev-k) { color: #2563eb; font-weight: 600; }
  :global(.ev-text) { padding: 8px 0; font-size: 12px; color: #334155; }
  .result-empty {
    flex: 1; display: flex; align-items: center; justify-content: center;
    color: #94a3b8; font-size: 12px;
  }
  .analysis-body { flex: 1; overflow-y: auto; padding: 14px; }
  .loading { gap: 6px; }
  .load-dot {
    width: 6px; height: 6px; border-radius: 50%; background: #3b82f6;
    animation: bounce 1.2s infinite;
  }
  .load-dot:nth-child(2) { animation-delay: 0.15s; }
  .load-dot:nth-child(3) { animation-delay: 0.3s; }
  @keyframes bounce { 0%, 60%, 100% { transform: translateY(0); } 30% { transform: translateY(-4px); } }

  /* ─── 底部状态栏 ─── */
  .statusbar {
    display: flex; justify-content: space-between;
    padding: 5px 18px; background: #ffffff;
    border-top: 1px solid #d5dbe3;
    font-size: 11px; color: #8892a4;
    flex-shrink: 0;
  }

  ::-webkit-scrollbar { width: 6px; height: 6px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 3px; }
  ::-webkit-scrollbar-thumb:hover { background: #94a3b8; }

  @media (max-width: 720px) {
    .workspace { grid-template-columns: 1fr; }
  }
</style>
