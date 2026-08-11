<script>
  // 工厂智能体 · 本体问答 — 独立 Web 应用（工业软件浅色风格）
  import { onMount } from 'svelte';
  import { setupOntologyMulti, dbSetup, askOntology, analyzeOntology, setModel, getModels, saveModels, fetchVersion, fetchExamples, fetchExample, browseFiles, readDataFile } from './lib/api.js';
  import DashboardPanel from './components/DashboardPanel.svelte';
  import ModelGraph from './components/ModelGraph.svelte';
  import AnalysisResult from './components/AnalysisResult.svelte';

  // ─── 状态 ───
  let activeTab = $state('model');   // model | query | dashboard
  let selectedFiles = $state([]);    // [{name, size, content}] 已选文件
  let defaultBusy = $state(false);   // 默认示例建模中
  // ─── 示例数据目录浏览区 ───
  let examples = $state([]);         // [{dir, files:[{name,path,size}]}]
  let examplesLoading = $state(false);
  let checkedExamples = $state([]);  // [{name,path}] 勾选的示例
  let exampleBusy = $state(false);
  // ─── 文件浏览框（学习 solo-agent-kit /api/browse，默认 data_valve 示例目录）───
  let browseDir = $state('data_valve');    // 当前目录（相对 codes/）
  let browseParent = $state('');           // 上级目录（空=根）
  let browseDirs = $state([]);             // [{path,name}] 子目录
  let browseFileList = $state([]);         // [{path,name}] 数据文件
  let browseLoading = $state(false);
  let browseChecked = $state([]);          // [{path,name}] 已选文件
  let browseBusy = $state(false);
  let modeling = $state(false);
  let modelResult = $state(null);   // {table, attrs}
  // 数据库接入
  let dbOpen = $state(false);
  let dbBusy = $state(false);
  let dbResult = $state(null);      // {ok, table?, output?, error?}
  let dbForm = $state({ db_type: 'mysql', host: '127.0.0.1', port: '3306', user: '', password: '', database: '', tables: '' });
  let question = $state('');
  let asking = $state(false);
  let answer = $state('');
  let answerHTML = $state(null);   // 结构化答案 HTML（列表/表格）；null 则退回 <pre>
  let evidence = $state(null);     // 问答证据溯源
  let evidenceOpen = $state(false);
  let analysis = $state(null);       // {report, stats} 智能分析结果
  let modelList = $state([]);        // 可用模型
  let activeModel = $state('');      // 当前生效模型 key
  let modelOpen = $state(false);     // 模型管理折叠区
  let modelEditBusy = $state(false);
  let editModels = $state([]);       // 可编辑模型配置 [{key,name,type,base_url,model,api_key}]
  let editActive = $state('');       // 编辑态 active
  let appVersion = $state('');       // 代码版本(读后端)
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
      const res = await getModels();
      if (res.ok) {
        modelList = res.models.map(m => ({ key: m.key, name: m.name }));
        activeModel = res.active || '';
        loadEditModels(res);
      }
    } catch (e) { /* 忽略 */ }
    try {
      const v = await fetchVersion();
      if (v.ok && v.version) appVersion = v.version;
    } catch (e) { /* 忽略 */ }
    loadExamples(); // 进入即展示示例数据目录
    loadBrowse();   // 文件浏览框默认到 data_valve 示例目录
  });

  // ─── 示例数据目录浏览区 ───
  async function loadExamples() {
    examplesLoading = true;
    try {
      const res = await fetchExamples();
      if (res.ok && Array.isArray(res.examples)) {
        const byDir = {};
        for (const ex of res.examples) {
          const dir = (ex.path || '').split('/')[0] || 'data';
          (byDir[dir] = byDir[dir] || []).push(ex);
        }
        examples = Object.keys(byDir).map(dir => ({ dir, files: byDir[dir] }));
      } else {
        setStatus('err', res.error || '加载示例目录失败');
      }
    } catch (e) {
      setStatus('err', '加载示例目录失败');
    } finally { examplesLoading = false; }
  }

  function toggleExample(ex) {
    const i = checkedExamples.findIndex(c => c.path === ex.path);
    checkedExamples = i >= 0
      ? checkedExamples.filter((_, idx) => idx !== i)
      : [...checkedExamples, { name: ex.name, path: ex.path }];
  }

  async function doSelectedExamples() {
    if (!checkedExamples.length || exampleBusy) return;
    exampleBusy = true;
    setStatus('info', `正在读取 ${checkedExamples.length} 个示例文件…`);
    try {
      const files = [];
      for (const c of checkedExamples) {
        const r = await fetchExample(c.path);
        if (!r.ok || r.content == null) {
          setStatus('err', r.error || `读取示例失败：${c.path}`);
          exampleBusy = false; return;
        }
        files.push({ name: c.name, content: r.content });
      }
      const res = await setupOntologyMulti(files);
      if (!res.ok) {
        setStatus('err', res.error || '建模失败');
      } else {
        modelResult = { table: res.table, attrs: res.attrs || [], ts: Date.now() };
        status = 'ready';
        setStatus('ok', `示例建模完成：${res.table}，共 ${(res.attrs || []).length} 张表`);
      }
    } catch (err) {
      setStatus('err', '网络错误，请确认服务已启动');
    } finally { exampleBusy = false; }
  }

  // ─── 文件浏览框（默认 data_valve 示例目录，目录导航 + 多选建模）───
  async function loadBrowse(dir) {
    browseLoading = true;
    try {
      const res = await browseFiles(dir);
      if (res.ok) {
        browseDir = res.dir || 'data_valve';
        browseParent = res.parent || '';
        browseDirs = res.dirs || [];
        browseFileList = res.files || [];
        // 目录切换时清空已选，避免残留旧目录勾选
        browseChecked = [];
      } else {
        setStatus('err', res.error || '浏览数据文件失败');
      }
    } catch (e) {
      setStatus('err', '浏览数据文件失败（网络错误）');
    } finally { browseLoading = false; }
  }

  function toggleBrowseFile(f) {
    const i = browseChecked.findIndex(c => c.path === f.path);
    browseChecked = i >= 0
      ? browseChecked.filter((_, idx) => idx !== i)
      : [...browseChecked, { path: f.path, name: f.name }];
  }

  // 从浏览框已选文件 → 读取内容 → 多文件建模
  async function doBrowseModel() {
    if (!browseChecked.length || browseBusy) return;
    browseBusy = true;
    setStatus('info', `正在读取 ${browseChecked.length} 个数据文件…`);
    try {
      const files = [];
      for (const c of browseChecked) {
        const r = await readDataFile(c.path);
        if (!r.ok || r.content == null) {
          setStatus('err', r.error || `读取失败：${c.path}`);
          browseBusy = false; return;
        }
        files.push({ name: r.name, content: r.content });
      }
      const res = await setupOntologyMulti(files);
      if (!res.ok) {
        setStatus('err', res.error || '建模失败');
      } else {
        modelResult = { table: res.table, attrs: res.attrs || [], ts: Date.now() };
        status = 'ready';
        setStatus('ok', `建模完成：${res.table}，共 ${(res.attrs || []).length} 张表`);
      }
    } catch (err) {
      setStatus('err', '网络错误，请确认服务已启动');
    } finally { browseBusy = false; }
  }

  // 从完整配置装载可编辑列表（api_key 用脱敏占位，保存时后端保留原值）
  function loadEditModels(res) {
    editActive = res.active || '';
    editModels = (res.models || []).map(m => ({
      key: m.key, name: m.name, type: m.type, base_url: m.base_url || '',
      model: m.model || '', api_key: m.has_key ? (m.api_key_status || '已配置') : '',
    }));
  }

  async function refreshModels() {
    const res = await getModels();
    if (res.ok) {
      modelList = res.models.map(m => ({ key: m.key, name: m.name }));
      activeModel = res.active || '';
      loadEditModels(res);
      return true;
    }
    return false;
  }

  function addModel() {
    const key = 'model_' + Date.now();
    editModels = [...editModels, { key, name: '新模型', type: 'ollama', base_url: 'http://127.0.0.1:11434', model: '', api_key: '' }];
  }

  function removeModel(i) {
    if (editModels.length <= 1) { setStatus('err', '至少保留一个模型'); return; }
    editModels = editModels.filter((_, idx) => idx !== i);
  }

  function setEditActive(key) {
    editActive = key;
  }

  async function saveModelConfig() {
    if (modelEditBusy) return;
    const list = editModels.map(m => ({
      key: m.key, name: m.name, type: m.type, base_url: m.base_url, model: m.model, api_key: m.api_key,
    }));
    if (list.length === 0) { setStatus('err', '至少保留一个模型'); return; }
    modelEditBusy = true;
    try {
      const res = await saveModels({ models: list, active: editActive });
      if (res.ok) {
        setStatus('ok', `模型配置已保存，当前：${res.active}`);
        await refreshModels();
      } else {
        setStatus('err', res.error || '保存失败');
      }
    } catch (err) {
      setStatus('err', '网络错误');
    } finally { modelEditBusy = false; }
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

  // ─── 多文件选择 ───
  function onPickFiles(e) {
    const files = Array.from(e.target.files || []);
    if (!files.length) return;
    const reads = files.map(f => new Promise((resolve) => {
      const reader = new FileReader();
      reader.onload = () => resolve({ name: f.name, size: f.size, content: reader.result });
      reader.readAsText(f, 'utf-8');
    }));
    Promise.all(reads).then(list => {
      selectedFiles = selectedFiles.concat(list);
      setStatus('info', `已选择 ${selectedFiles.length} 个文件，请确认`);
    });
    e.target.value = ''; // 允许重复选择同一文件
  }

  function removeFile(i) {
    selectedFiles = selectedFiles.filter((_, idx) => idx !== i);
    if (selectedFiles.length === 0) setStatus('info', '等待数据导入');
  }

  function fmtSize(n) {
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    return (n / 1024 / 1024).toFixed(1) + ' MB';
  }

  // ─── 默认示例一键建模（data_valve 阀门数据）───
  const DEFAULT_EXAMPLE_TABLES = [
    'valve_products', 'valve_equipment', 'valve_customers',
    'valve_batches', 'valve_raw_materials', 'valve_sales',
  ];

  async function doDefaultExample() {
    if (defaultBusy) return;
    defaultBusy = true;
    setStatus('info', '正在读取默认示例（data_valve）…');
    try {
      const files = [];
      for (const t of DEFAULT_EXAMPLE_TABLES) {
        const path = `data_valve/${t}.csv`;
        const r = await fetchExample(path);
        if (!r.ok || r.content == null) {
          setStatus('err', r.error || `读取示例失败：${path}`);
          defaultBusy = false; return;
        }
        files.push({ name: `${t}.csv`, content: r.content });
      }
      const res = await setupOntologyMulti(files);
      if (!res.ok) {
        setStatus('err', res.error || '建模失败');
      } else {
        modelResult = { table: res.table, attrs: res.attrs || [], ts: Date.now() };
        status = 'ready';
        setStatus('ok', `默认示例建模完成：${res.table}，共 ${(res.attrs || []).length} 张表`);
      }
    } catch (err) {
      setStatus('err', '网络错误，请确认服务已启动');
    } finally { defaultBusy = false; }
  }

  // ─── 多文件上传建模 ───
  async function doSetupMulti() {
    if (!selectedFiles.length || modeling) return;
    modeling = true;
    setStatus('info', `正在为 ${selectedFiles.length} 个文件建模…`);
    try {
      const payload = selectedFiles.map(f => ({ name: f.name, content: f.content }));
      const res = await setupOntologyMulti(payload);
      if (!res.ok) {
        setStatus('err', res.error || '建模失败');
      } else {
        modelResult = { table: res.table, attrs: res.attrs || [], ts: Date.now() };
        status = 'ready';
        setStatus('ok', `建模完成：${res.table}，共 ${(res.attrs || []).length} 张表`);
      }
    } catch (err) {
      setStatus('err', '网络错误，请确认服务已启动');
    } finally { modeling = false; }
  }

  // ─── 数据库接入建模（本地厂区局域网）───
  function onDbTypeChange() {
    dbForm.port = dbForm.db_type === 'postgres' ? '5432' : '3306';
  }

  async function doDbSetup() {
    if (dbBusy) return;
    const f = dbForm;
    if (!f.host.trim() || !f.user.trim() || !f.database.trim() || !f.tables.trim()) {
      setStatus('err', '请填写主机、用户、库名和表名'); return;
    }
    dbBusy = true;
    setStatus('info', '正在连接数据库并建模…');
    try {
      const cfg = {
        db_type: f.db_type,
        host: f.host.trim(),
        port: String(f.port || ''),
        user: f.user.trim(),
        password: f.password,
        database: f.database.trim(),
        tables: f.tables.split(/[,，]/).map(s => s.trim()).filter(Boolean),
      };
      const res = await dbSetup(cfg);
      if (!res.ok) {
        setStatus('err', res.error || '数据库建模失败');
        dbResult = { ok: false, error: res.error || '数据库建模失败' };
      } else {
        dbResult = { ok: true, table: res.table, output: res.output || '' };
        modelResult = { table: res.table, attrs: [], ts: Date.now() };
        status = 'ready';
        setStatus('ok', `数据库建模完成：${res.table}`);
      }
    } catch (err) {
      setStatus('err', '网络错误，请确认服务已启动');
      dbResult = { ok: false, error: '网络错误，请确认服务已启动' };
    } finally { dbBusy = false; }
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

      <!-- ─── 文件源（主要入口）─── -->
      <div class="form-group">
        <span class="form-label">数据文件（可多选 CSV / JSON）</span>
        <label class="file-input">
          <input type="file" multiple accept=".csv,.json" onchange={onPickFiles} />
          <span class="file-icon">📁</span>
          <span class="file-name">{selectedFiles.length ? `已选择 ${selectedFiles.length} 个文件` : '点击选择文件（可多选）'}</span>
        </label>

        {#if selectedFiles.length > 0}
          <div class="file-list">
            {#each selectedFiles as f, i (f.name + '-' + i)}
              <div class="file-item">
                <span class="file-item-name" title={f.name}>📄 {f.name}</span>
                <span class="file-item-size">{fmtSize(f.size)}</span>
                <button class="file-remove" onclick={() => removeFile(i)} aria-label="移除">✕</button>
              </div>
            {/each}
          </div>
          <button class="btn-action" onclick={doSetupMulti} disabled={modeling}>
            <span class="btn-icon">{modeling ? '⏳' : '⚙'}</span>
            {modeling ? '建模进行中…' : `确认并建模（${selectedFiles.length} 个文件）`}
          </button>
        {:else}
          <!-- 空状态：未选文件时提示可用默认示例一键建模 -->
          <div class="default-example">
            <span class="default-example-text">或使用默认示例（data_valve 阀门数据）</span>
            <button class="btn-action btn-default" onclick={doDefaultExample} disabled={defaultBusy}>
              <span class="btn-icon">{defaultBusy ? '⏳' : '⚙'}</span>
              {defaultBusy ? '建模进行中…' : '用默认示例建模'}
            </button>
          </div>
        {/if}
      </div>

      <!-- ─── 示例数据目录浏览区（默认浏览入口）─── -->
      <div class="example-dir">
        <div class="example-dir-head">
          <span class="form-label">📂 示例数据目录（勾选多个 → 建模）</span>
          <button class="example-refresh" onclick={loadExamples} title="刷新示例目录" disabled={examplesLoading}>{examplesLoading ? '…' : '↻'}</button>
        </div>
        {#if examplesLoading && examples.length === 0}
          <div class="example-empty">正在加载示例目录…</div>
        {:else if examples.length === 0}
          <div class="example-empty">未加载到示例文件（请确认服务已启动）</div>
        {:else}
          {#each examples as g}
            <div class="example-dir-group">
              <div class="example-dir-name">📁 {g.dir}</div>
              <div class="file-list">
                {#each g.files as ex}
                  <label class="example-item" title={ex.path}>
                    <input type="checkbox" checked={checkedExamples.some(c => c.path === ex.path)} onchange={() => toggleExample(ex)} />
                    <span class="example-item-name">📄 {ex.name}</span>
                    <span class="file-item-size">{fmtSize(ex.size)}</span>
                  </label>
                {/each}
              </div>
            </div>
          {/each}
          <button class="btn-action btn-default" onclick={doSelectedExamples} disabled={!checkedExamples.length || exampleBusy}>
            <span class="btn-icon">{exampleBusy ? '⏳' : '⚙'}</span>
            {exampleBusy ? '建模进行中…' : (checkedExamples.length ? `用所选示例建模（${checkedExamples.length}）` : '用所选示例建模')}
          </button>
        {/if}
      </div>

      <!-- ─── 文件浏览框（学习 solo-agent-kit /api/browse，默认 data_valve 示例目录）─── -->
      <div class="example-dir browse-box">
        <div class="example-dir-head">
          <span class="form-label">📂 浏览数据文件（可多选 → 建模）</span>
          <button class="example-refresh" onclick={() => loadBrowse()} title="刷新当前目录" disabled={browseLoading}>{browseLoading ? '…' : '↻'}</button>
        </div>
        <div class="browse-current" title={browseDir}>📁 当前目录：{browseDir || 'data_valve'}</div>
        <div class="browse-nav">
          {#if browseParent}
            <button class="browse-up" onclick={() => loadBrowse(browseParent)}>⬆ 上级目录</button>
          {/if}
          {#if browseDirs.length}
            <div class="browse-dirs">
              {#each browseDirs as d}
                <button class="browse-dir" onclick={() => loadBrowse(d.path)} title={d.path}>📁 {d.name}</button>
              {/each}
            </div>
          {/if}
        </div>
        {#if browseFileList.length}
          <div class="file-list browse-files">
            {#each browseFileList as f}
              <label class="example-item" title={f.path}>
                <input type="checkbox" checked={browseChecked.some(c => c.path === f.path)} onchange={() => toggleBrowseFile(f)} />
                <span class="example-item-name">📄 {f.name}</span>
                <span class="browse-file-path" title={f.path}>{f.path}</span>
              </label>
            {/each}
          </div>
          <button class="btn-action btn-default" onclick={doBrowseModel} disabled={!browseChecked.length || browseBusy}>
            <span class="btn-icon">{browseBusy ? '⏳' : '⚙'}</span>
            {browseBusy ? '建模进行中…' : (browseChecked.length ? `确认并建模（${browseChecked.length} 个文件）` : '确认并建模')}
          </button>
        {:else if !browseDirs.length}
          <div class="example-empty">当前目录无数据文件</div>
        {/if}
      </div>

      {#if modelResult}
        <div class="model-panel">
          <div class="model-head">
            <span class="model-table">数据表：{modelResult.table}</span>
            <span class="model-count">{modelResult.attrs.length} 张表</span>
          </div>
          <div class="attr-chips">
            {#each modelResult.attrs as a}
              <span class="attr-chip">{a}</span>
            {/each}
          </div>
        </div>
      {/if}

      <!-- ─── 数据库接入折叠区（本地厂区局域网）─── -->
      <div class="db-collapse">
        <button class="db-toggle" onclick={() => (dbOpen = !dbOpen)}>
          <span class="file-icon">🗄️</span> 数据库接入
          <span class="chevron">{dbOpen ? '▾' : '▸'}</span>
        </button>
        {#if dbOpen}
          <div class="db-body">
            <div class="db-hint">本地厂区局域网：连接 MES / ERP / 台账数据库，数据本地处理不出厂。</div>

            <div class="form-group">
              <label class="form-label" for="db-type">数据库类型</label>
              <select id="db-type" class="db-input" bind:value={dbForm.db_type} onchange={onDbTypeChange}>
                <option value="mysql">MySQL</option>
                <option value="postgres">PostgreSQL</option>
              </select>
            </div>

            <div class="db-row">
              <div class="form-group">
                <label class="form-label" for="db-host">主机</label>
                <input id="db-host" class="db-input" placeholder="127.0.0.1" bind:value={dbForm.host} />
              </div>
              <div class="form-group db-port">
                <label class="form-label" for="db-port">端口</label>
                <input id="db-port" class="db-input" placeholder="3306" bind:value={dbForm.port} />
              </div>
            </div>

            <div class="db-row">
              <div class="form-group">
                <label class="form-label" for="db-user">用户</label>
                <input id="db-user" class="db-input" placeholder="root" bind:value={dbForm.user} />
              </div>
              <div class="form-group">
                <label class="form-label" for="db-pass">密码</label>
                <input id="db-pass" class="db-input" type="password" placeholder="••••••" bind:value={dbForm.password} />
              </div>
            </div>

            <div class="form-group">
              <label class="form-label" for="db-database">库名</label>
              <input id="db-database" class="db-input" placeholder="factory" bind:value={dbForm.database} />
            </div>
            <div class="form-group">
              <label class="form-label" for="db-tables">表名（多个用逗号分隔）</label>
              <input id="db-tables" class="db-input" placeholder="equipment, devices" bind:value={dbForm.tables} />
            </div>

            <button class="btn-action" onclick={doDbSetup} disabled={dbBusy}>
              <span class="btn-icon">{dbBusy ? '⏳' : '🔗'}</span>
              {dbBusy ? '建模进行中…' : '确认并建模'}
            </button>

            {#if dbResult}
              <div class="db-result" class:db-err={!dbResult.ok}>
                {#if dbResult.ok}
                  <div class="model-head">
                    <span class="model-table">数据表：{dbResult.table}</span>
                    <span class="model-count">✓ 建模完成</span>
                  </div>
                {:else}
                  <div class="db-err-text">✗ {dbResult.error}</div>
                {/if}
              </div>
            {/if}
          </div>
        {/if}
      </div>

      <!-- ─── 模型管理折叠区（查看/编辑/增删/设 active，api_key 脱敏）─── -->
      <div class="db-collapse">
        <button class="db-toggle" onclick={() => (modelOpen = !modelOpen)}>
          <span class="file-icon">🤖</span> 模型配置管理
          <span class="chevron">{modelOpen ? '▾' : '▸'}</span>
        </button>
        {#if modelOpen}
          <div class="db-body">
            <div class="db-hint">模型配置持久化到 model_config.json。api_key 仅在输入新值时更新，留空/不改则保留原值。本地优先：默认 ornith/qwen 可直连 Ollama。</div>
            {#each editModels as m, i}
              <div class="m-edit">
                <div class="m-edit-head">
                  <label class="m-radio">
                    <input type="radio" checked={editActive === m.key} onclick={() => setEditActive(m.key)} />
                    <span class="m-active-tag">{editActive === m.key ? '✓ 生效' : '设为生效'}</span>
                  </label>
                  <span class="m-key">key: {m.key}</span>
                  <button class="file-remove" onclick={() => removeModel(i)} aria-label="删除">✕</button>
                </div>
                <div class="db-row">
                  <div class="form-group">
                    <span class="form-label">名称</span>
                    <input class="db-input" placeholder="模型名称" bind:value={m.name} />
                  </div>
                  <div class="form-group">
                    <span class="form-label">类型</span>
                    <select class="db-input" bind:value={m.type}>
                      <option value="ollama">ollama</option>
                      <option value="openai">openai</option>
                    </select>
                  </div>
                </div>
                <div class="db-row">
                  <div class="form-group">
                    <span class="form-label">Base URL</span>
                    <input class="db-input" placeholder="http://127.0.0.1:11434" bind:value={m.base_url} />
                  </div>
                  <div class="form-group">
                    <span class="form-label">Model</span>
                    <input class="db-input" placeholder="ornith:latest" bind:value={m.model} />
                  </div>
                </div>
                <div class="form-group">
                  <span class="form-label">API Key（{m.api_key ? '已配置，输入新值可更新' : '未配置'}{m.api_key ? '：' + m.api_key : ''}）</span>
                  <input class="db-input" type="password" placeholder="留空 = 保留原值" bind:value={m.api_key} />
                </div>
              </div>
            {/each}
            <div class="m-actions">
              <button class="btn-action btn-default" onclick={addModel}>＋ 新增模型</button>
              <button class="btn-action" onclick={saveModelConfig} disabled={modelEditBusy}>
                {modelEditBusy ? '保存中…' : '💾 保存配置'}
              </button>
            </div>
          </div>
        {/if}
      </div>
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

  /* ─── 多文件列表 ─── */
  .file-list {
    display: flex; flex-direction: column; gap: 6px;
    border: 1px solid #e2e8f0; border-radius: 4px; padding: 8px;
    background: #f8fafc; max-height: 180px; overflow-y: auto;
  }
  .file-item {
    display: flex; align-items: center; gap: 8px;
    background: #fff; border: 1px solid #e2e8f0; border-radius: 4px;
    padding: 6px 10px; font-size: 12px;
  }
  .file-item-name {
    flex: 1; color: #1e293b; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .file-item-size { color: #94a3b8; font-size: 11px; flex-shrink: 0; }
  .file-remove {
    flex-shrink: 0; width: 18px; height: 18px; border: none; border-radius: 3px;
    background: #fef2f2; color: #dc2626; cursor: pointer; font-size: 11px; line-height: 1;
    transition: background 0.15s;
  }
  .file-remove:hover { background: #fee2e2; }

  /* ─── 默认示例一键建模 ─── */
  .default-example {
    display: flex; flex-direction: column; gap: 8px;
    border: 1px dashed #cbd5e1; border-radius: 4px;
    background: #f8fafc; padding: 12px;
  }
  .default-example-text { font-size: 12px; color: #64748b; }
  .btn-default { background: #1e293b; }
  .btn-default:hover:not(:disabled) { background: #0f172a; }

  /* ─── 示例数据目录浏览区 ─── */
  .example-dir {
    display: flex; flex-direction: column; gap: 8px;
    border: 1px solid #e2e8f0; border-radius: 4px; padding: 10px;
    background: #fbfcfe;
  }
  .example-dir-head {
    display: flex; align-items: center; justify-content: space-between; gap: 8px;
  }
  .example-refresh {
    flex-shrink: 0; width: 22px; height: 22px; border: 1px solid #cbd5e1;
    border-radius: 3px; background: #fff; color: #475569; cursor: pointer;
    font-size: 13px; line-height: 1; transition: background 0.15s;
  }
  .example-refresh:hover:not(:disabled) { background: #f1f5f9; color: #2563eb; }
  .example-refresh:disabled { opacity: 0.5; cursor: default; }
  .example-empty { font-size: 12px; color: #94a3b8; padding: 8px 2px; }
  .example-dir-group { display: flex; flex-direction: column; gap: 4px; }
  .example-dir-name {
    font-size: 11px; font-weight: 700; color: #2563eb;
    background: #eff6ff; border: 1px solid #bfdbfe;
    border-radius: 3px; padding: 4px 8px;
  }
  .example-item {
    display: flex; align-items: center; gap: 6px; padding: 4px 6px;
    border-radius: 3px; cursor: pointer; transition: background 0.15s;
  }
  .example-item:hover { background: #f1f5f9; }
  .example-item input[type="checkbox"] { flex-shrink: 0; accent-color: #2563eb; cursor: pointer; }
  .example-item-name {
    flex: 1; color: #1e293b; white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis; font-size: 12px;
  }
  .example-dir .btn-action { margin-top: 2px; }

  /* ─── 文件浏览框（默认示例目录，目录导航 + 多选）─── */
  .browse-box { gap: 6px; }
  .browse-current {
    font-size: 11px; color: #1e293b; font-weight: 600;
    background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 3px;
    padding: 4px 8px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .browse-nav { display: flex; flex-direction: column; gap: 4px; }
  .browse-up {
    align-self: flex-start; border: 1px solid #cbd5e1; border-radius: 3px;
    background: #fff; color: #2563eb; font-size: 12px; cursor: pointer;
    padding: 3px 8px; transition: background 0.15s;
  }
  .browse-up:hover { background: #eff6ff; }
  .browse-dirs { display: flex; flex-wrap: wrap; gap: 4px; }
  .browse-dir {
    border: 1px solid #cbd5e1; border-radius: 3px; background: #f1f5f9;
    color: #1e293b; font-size: 12px; cursor: pointer; padding: 2px 8px;
    transition: background 0.15s;
  }
  .browse-dir:hover { background: #e2e8f0; }
  .browse-files { max-height: 200px; }
  .browse-file-path {
    flex-shrink: 0; max-width: 40%; color: #94a3b8; font-size: 10px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }

  .attr-chips { display: flex; flex-wrap: wrap; gap: 6px; padding: 10px 12px; }
  .attr-chip {
    background: #eff6ff; color: #2563eb; border: 1px solid #bfdbfe;
    border-radius: 3px; padding: 3px 8px; font-size: 11px;
    font-family: 'Consolas', monospace;
  }

  /* ─── 数据库接入折叠区 ─── */
  .db-collapse {
    border: 1px solid #e2e8f0; border-radius: 4px; overflow: hidden;
  }
  .db-toggle {
    width: 100%; display: flex; align-items: center; gap: 8px;
    padding: 10px 12px; background: #f8fafc; border: none;
    font-size: 12px; font-weight: 700; color: #1e293b; cursor: pointer;
    text-align: left; transition: background 0.15s;
  }
  .db-toggle:hover { background: #f1f5f9; color: #2563eb; }
  .db-body { padding: 12px; display: flex; flex-direction: column; gap: 10px; border-top: 1px solid #e2e8f0; }
  .db-hint { font-size: 11px; color: #64748b; background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 4px; padding: 6px 10px; }
  .db-row { display: flex; gap: 10px; }
  .db-row .form-group { flex: 1; }
  .db-port { flex: 0 0 90px; }
  .db-input {
    width: 100%; background: #fff; border: 1px solid #cbd5e1;
    border-radius: 4px; padding: 7px 10px; font-size: 12px; color: #1e293b;
    outline: none; transition: border-color 0.15s;
  }
  .db-input:focus { border-color: #3b82f6; }
  .db-result { border: 1px solid #e2e8f0; border-radius: 4px; }
  .db-result .model-head { border-bottom: none; }
  .db-result.db-err { border-color: #fecaca; background: #fef2f2; }
  .db-err-text { padding: 8px 12px; font-size: 12px; color: #b91c1c; line-height: 1.5; }

  /* ─── 模型配置管理 ─── */
  .m-edit {
    border: 1px solid #e2e8f0; border-radius: 4px; padding: 10px;
    display: flex; flex-direction: column; gap: 8px; background: #fbfcfe;
  }
  .m-edit-head { display: flex; align-items: center; gap: 10px; }
  .m-radio { display: flex; align-items: center; gap: 4px; cursor: pointer; font-size: 12px; }
  .m-active-tag {
    font-size: 11px; font-weight: 700; color: #16a34a;
    background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 3px; padding: 2px 8px;
  }
  .m-key { flex: 1; font-size: 11px; color: #94a3b8; font-family: 'Consolas', monospace; }
  .m-actions { display: flex; gap: 8px; }

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
