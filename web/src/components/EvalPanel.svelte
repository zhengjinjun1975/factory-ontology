<script>
  // EvalPanel — 评测展示面板：调用前端 eval-benchmark 转发端点，展示问答命中率；支持 isolate 自定义问题集逐题作答。
  // 端点：
  //   GET  /api/ontology/eval-benchmark?kb=<kb>   （前端 server 暴露的后端评测转发）
  //   POST /api/ontology/eval-isolate             （前端 server 暴露的后端隔离评测转发，body {kb, questions}）
  // 成功信封 benchmark: {ok:true, data:{kb, questions_n, hits, score(0.0-1.0)}, elapsed_s}
  // 成功信封 isolate:   {ok:true, data:{kb, questions_n, answers:[{q, answer, hit}]}}
  // 失败信封：{ok:false, error:{code, message}}
  // 单企业收敛：kb 由父级（App.svelte）注入 currentKb（当前登录企业唯一 kb），面板内无 kb 切换/轮询。

  let { kb = '' } = $props();      // 当前企业唯一 kb（只读 prop，跟随登录企业）

  import { getToken } from '../lib/api.js';

  let result = $state(null);       // {kb, questions_n, hits, score}（benchmark 模式）
  let elapsed = $state(null);      // 耗时秒（benchmark）
  let loading = $state(false);
  let error = $state('');
  let ran = $state(false);         // 是否已运行过（控制结果区显隐）

  // isolate 自定义问题集
  let mode = $state('benchmark');  // 'benchmark' | 'isolate'
  let isoQuestions = $state('');   // 逗号分隔的自定义问题
  let isoResult = $state(null);    // {kb, questions_n, answers:[{q, answer, hit}]}
  let isoLoading = $state(false);
  let isoError = $state('');

  // score(0-1) → 命中率百分比 + 进度条宽度
  const pct = $derived(result && result.questions_n
    ? Math.round(result.hits / result.questions_n * 100)
    : (result && result.score != null ? Math.round(result.score * 100) : 0));
  // 进度条配色：<60 橙、<80 蓝、其余绿（浅色护眼）
  const barColor = $derived(pct < 60 ? '#f59e0b' : pct < 80 ? '#3b82f6' : '#10b981');
  const verdict = $derived(
    pct >= 80 ? '命中表现良好' : pct >= 60 ? '命中表现中等' : '命中偏低，建议补充词典/示例'
  );

  async function run() {
    const k = (kb || '').trim();
    if (!k) { error = '当前企业知识库为空，请先建模'; return; }
    loading = true; error = ''; ran = false; result = null;
    try {
      const resp = await fetch('/api/ontology/eval-benchmark?kb=' + encodeURIComponent(k), { headers: { 'Authorization': 'Bearer ' + getToken() } });
      const res = await resp.json();
      if (res && res.ok && res.data) {
        result = res.data;
        elapsed = res.elapsed_s != null ? res.elapsed_s : null;
        ran = true;
      } else {
        error = (res && res.error && (res.error.message || res.error)) || '评测失败';
      }
    } catch (e) {
      error = '网络错误，请确认后端已启动';
    } finally {
      loading = false;
    }
  }

  // isolate：把逗号分隔问题集拆成列表 → POST /api/ontology/eval-isolate → 逐题展示答案
  function parseQuestions(text) {
    return String(text || '').split(/[,，\n]/).map(s => s.trim()).filter(Boolean);
  }

  async function runIsolate() {
    const k = (kb || '').trim();
    if (!k) { isoError = '当前企业知识库为空，请先建模'; return; }
    const qs = parseQuestions(isoQuestions);
    if (qs.length === 0) { isoError = '请输入至少一个自定义问题（多个用逗号分隔）'; return; }
    isoLoading = true; isoError = ''; isoResult = null;
    try {
      const resp = await fetch('/api/ontology/eval-isolate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + getToken() },
        body: JSON.stringify({ kb: k, questions: qs }),
      });
      const res = await resp.json();
      if (res && res.ok && res.data) {
        isoResult = res.data;
      } else {
        const em = res && res.error;
        isoError = (em && (em.message || em)) || '隔离评测失败';
      }
    } catch (e) {
      isoError = '网络错误，请确认后端已启动';
    } finally {
      isoLoading = false;
    }
  }

  function switchMode(m) {
    mode = m;
    error = ''; isoError = ''; ran = false; isoResult = null; result = null;
  }
</script>

<div class="eval">
  <!-- ─── 顶部：当前企业知识库 + 模式切换 + 运行 ─── -->
  <div class="eval-top">
    <div class="eval-modes">
      <button class="mode-btn" class:mode-on={mode === 'benchmark'} onclick={() => switchMode('benchmark')}>基线评测</button>
      <button class="mode-btn" class:mode-on={mode === 'isolate'} onclick={() => switchMode('isolate')}>自定义问题</button>
    </div>
    <span class="eval-hint">评测跟随当前企业知识库「{kb || '—'}」</span>
  </div>

  {#if mode === 'benchmark'}
    <!-- ═══ 基线评测 ═══ -->
    <div class="bench-ctl">
      <button class="btn-run" onclick={run} disabled={loading}>
        {loading ? '评测进行中…' : '运行评测'}
      </button>
      <span class="eval-hint">对所选知识库的示例题目跑评测基线，统计问答命中率</span>
    </div>

    {#if error}
      <div class="eval-empty eval-err">
        {error}
        <button class="eval-retry" onclick={run}>重试</button>
      </div>
    {/if}

    {#if loading}
      <div class="eval-empty">正在评测 {kb} 知识库…</div>
    {/if}

    {#if ran && !loading && !error && result}
      <div class="eval-result">
        <div class="kpi-row">
          <div class="mini-card">
            <div class="mc-accent" style="background:#2563eb"></div>
            <div class="mc-num" style="color:#2563eb">{result.questions_n}</div>
            <div class="mc-label">评测题数</div>
          </div>
          <div class="mini-card">
            <div class="mc-accent" style="background:#10b981"></div>
            <div class="mc-num" style="color:#10b981">{result.hits}</div>
            <div class="mc-label">命中题数</div>
          </div>
          <div class="mini-card">
            <div class="mc-accent" style="background:{barColor}"></div>
            <div class="mc-num" style="color:{barColor}">{pct}%</div>
            <div class="mc-label">命中率</div>
          </div>
        </div>

        <div class="score-card">
          <div class="score-head">
            <span class="score-title">命中率</span>
            <span class="score-verdict" style="color:{barColor}">{verdict}</span>
            {#if elapsed != null}
              <span class="score-elapsed">耗时 {elapsed}s</span>
            {/if}
          </div>
          <div class="score-track">
            <div class="score-fill" style="width: {pct}%; background: {barColor};"></div>
          </div>
          <div class="score-scale">
            <span>0</span><span>{result.questions_n}</span>
          </div>
          {#if result.score != null}
            <div class="score-note">后端基线得分：{result.score}</div>
          {/if}
        </div>
      </div>
    {/if}
  {:else}
    <!-- ═══ 自定义问题集（isolate）═══ -->
    <div class="iso-ctl">
      <div class="iso-label">自定义问题集（多个用逗号或换行分隔）</div>
      <textarea
        class="iso-input"
        rows="4"
        placeholder="例如：有多少台设备，功率最大的设备，设备类型有哪些"
        bind:value={isoQuestions}
      ></textarea>
      <div class="iso-bar">
        <button class="btn-run" onclick={runIsolate} disabled={isoLoading}>
          {isoLoading ? '评测进行中…' : '逐题评测'}
        </button>
        <span class="eval-hint">对 {kb} 知识库逐题问答（只作答，不打分）</span>
      </div>
    </div>

    {#if isoError}
      <div class="eval-empty eval-err">
        {isoError}
        <button class="eval-retry" onclick={runIsolate}>重试</button>
      </div>
    {/if}

    {#if isoLoading}
      <div class="eval-empty">正在对 {kb} 知识库逐题作答…</div>
    {/if}

    {#if isoResult && !isoLoading && !isoError}
      <div class="iso-result">
        <div class="iso-head">
          <span class="score-title">逐题作答结果</span>
          <span class="iso-count">共 {isoResult.questions_n} 题 · 知识库 {isoResult.kb}</span>
        </div>
        <div class="qa-list">
          {#each isoResult.answers as a, i}
            <div class="qa-item">
              <div class="qa-q"><span class="qa-num">{i + 1}.</span>{a.q}</div>
              <div class="qa-a">
                <span class="qa-a-label">答案</span>
                <pre class="qa-text">{a.answer}</pre>
              </div>
            </div>
          {/each}
        </div>
      </div>
    {/if}
  {/if}
</div>

<style>
  .eval { display: flex; flex-direction: column; gap: 14px; }

  /* ─── 顶部控制区 ─── */
  .eval-top { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .eval-kb { display: flex; align-items: center; gap: 8px; }
  .eval-kb-label { font-size: 12px; font-weight: 600; color: #334155; }
  .eval-kb input {
    width: 180px; padding: 7px 10px; font-size: 13px;
    border: 1px solid #cbd5e1; border-radius: 4px;
    background: #fff; color: #1e293b; outline: none;
    transition: border-color 0.15s;
  }
  .eval-kb input:focus { border-color: #3b82f6; }
  .eval-modes { display: flex; gap: 4px; }
  .mode-btn {
    background: #f1f5f9; color: #475569; border: 1px solid #e2e8f0;
    border-radius: 4px; padding: 7px 12px; font-size: 12px; font-weight: 600;
    cursor: pointer; transition: all 0.15s;
  }
  .mode-btn:hover { background: #e2e8f0; }
  .mode-btn.mode-on { background: #2563eb; color: #fff; border-color: #2563eb; }
  .eval-hint { font-size: 11px; color: #64748b; }
  .bench-ctl { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .iso-bar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }

  .btn-run {
    background: #2563eb; color: #fff;
    border: none; border-radius: 4px;
    padding: 8px 16px; font-size: 13px; font-weight: 600;
    cursor: pointer; transition: background 0.15s;
  }
  .btn-run:hover:not(:disabled) { background: #1d4ed8; }
  .btn-run:disabled { background: #94a3b8; cursor: not-allowed; }

  /* ─── isolate 输入区 ─── */
  .iso-ctl { display: flex; flex-direction: column; gap: 8px; }
  .iso-label { font-size: 12px; font-weight: 600; color: #334155; }
  .iso-input {
    width: 100%; padding: 10px 12px; font-size: 13px; line-height: 1.6;
    border: 1px solid #cbd5e1; border-radius: 4px;
    background: #fff; color: #1e293b; outline: none; resize: vertical;
    transition: border-color 0.15s; font-family: inherit;
  }
  .iso-input:focus { border-color: #3b82f6; }

  /* ─── 空态 / 错误 / 加载 ─── */
  .eval-empty { color: #94a3b8; font-size: 13px; text-align: center; padding: 30px; }
  .eval-err { color: #dc2626; }
  .eval-retry {
    margin-left: 10px; background: #fff; color: #2563eb;
    border: 1px solid #cbd5e1; border-radius: 4px;
    padding: 4px 12px; font-size: 12px; cursor: pointer;
    transition: all 0.15s;
  }
  .eval-retry:hover { border-color: #3b82f6; background: #f8fafc; }

  /* ─── 结果区（benchmark）─── */
  .eval-result { display: flex; flex-direction: column; gap: 12px; }
  .kpi-row { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
  .mini-card {
    position: relative; overflow: hidden;
    background: #fff; border: 1px solid #e2e8f0; border-radius: 6px;
    padding: 16px 12px 12px; text-align: center;
    box-shadow: 0 1px 2px rgba(15, 23, 42, 0.05);
  }
  .mc-accent { position: absolute; top: 0; left: 0; right: 0; height: 3px; }
  .mc-num { font-size: 26px; font-weight: 700; line-height: 1.1; font-family: 'Consolas', monospace; }
  .mc-label { font-size: 11px; color: #64748b; margin-top: 4px; }

  .score-card {
    background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 4px;
    padding: 14px;
  }
  .score-head {
    display: flex; align-items: center; gap: 10px; margin-bottom: 10px;
  }
  .score-title { font-size: 12px; font-weight: 700; color: #1e293b; letter-spacing: 0.3px; }
  .score-verdict { font-size: 12px; font-weight: 600; }
  .score-elapsed { margin-left: auto; font-size: 11px; color: #64748b; font-family: 'Consolas', monospace; }
  .score-track { height: 22px; background: #e2e8f0; border-radius: 4px; overflow: hidden; }
  .score-fill { height: 100%; border-radius: 4px; transition: width 0.5s ease; min-width: 2px; }
  .score-scale {
    display: flex; justify-content: space-between;
    font-size: 10px; color: #94a3b8; margin-top: 4px; font-family: 'Consolas', monospace;
  }
  .score-note { margin-top: 8px; font-size: 11px; color: #64748b; }

  /* ─── 结果区（isolate 逐题）─── */
  .iso-result { display: flex; flex-direction: column; gap: 10px; }
  .iso-head {
    display: flex; align-items: center; gap: 10px;
    padding: 10px 12px; background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 4px;
  }
  .iso-count { margin-left: auto; font-size: 11px; color: #64748b; font-family: 'Consolas', monospace; }
  .qa-list { display: flex; flex-direction: column; gap: 8px; }
  .qa-item { background: #fff; border: 1px solid #e2e8f0; border-radius: 4px; overflow: hidden; }
  .qa-q {
    padding: 8px 12px; background: #f1f5f9;
    font-size: 13px; font-weight: 600; color: #1e293b;
    border-bottom: 1px solid #e2e8f0;
  }
  .qa-num { color: #2563eb; margin-right: 6px; font-family: 'Consolas', monospace; }
  .qa-a { padding: 8px 12px; }
  .qa-a-label { font-size: 11px; font-weight: 600; color: #64748b; }
  .qa-text {
    margin: 4px 0 0; white-space: pre-wrap; word-break: break-word;
    font-family: 'Consolas', 'Menlo', monospace;
    font-size: 12px; line-height: 1.7; color: #334155;
  }

  @media (max-width: 720px) {
    .kpi-row { grid-template-columns: repeat(3, 1fr); }
    .eval-top { flex-direction: column; align-items: stretch; }
    .eval-kb { justify-content: space-between; }
  }
</style>
