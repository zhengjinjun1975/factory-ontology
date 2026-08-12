<script>
  // EvalPanel — 评测展示面板：调用前端 eval-benchmark 转发端点，展示问答命中率
  // 端点：GET /api/ontology/eval-benchmark?kb=<kb>（前端 server 暴露的后端评测转发）
  // 成功信封：{ok:true, data:{kb, questions_n, hits, score(0.0-1.0)}, elapsed_s}
  // 失败信封：{ok:false, error:{code, message}}
  let kb = $state('food');          // 知识库标识
  let result = $state(null);        // {kb, questions_n, hits, score}
  let elapsed = $state(null);       // 耗时秒
  let loading = $state(false);
  let error = $state('');
  let ran = $state(false);          // 是否已运行过（控制结果区显隐）

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
    const k = (kb || '').trim() || 'food';
    kb = k;
    loading = true; error = '';
    try {
      const resp = await fetch('/api/ontology/eval-benchmark?kb=' + encodeURIComponent(k));
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
</script>

<div class="eval">
  <!-- ─── 顶部：kb 选择 + 运行 ─── -->
  <div class="eval-top">
    <label class="eval-kb">
      <span class="eval-kb-label">知识库</span>
      <input
        type="text"
        placeholder="如 food"
        value={kb}
        oninput={(e) => kb = e.target.value}
        onkeydown={(e) => { if (e.key === 'Enter' && !loading) run(); }}
      />
    </label>
    <button class="btn-run" onclick={run} disabled={loading}>
      {loading ? '评测进行中…' : '运行评测'}
    </button>
    <span class="eval-hint">对所选知识库的示例题目跑评测基线，统计问答命中率</span>
  </div>

  <!-- ─── 错误态 ─── -->
  {#if error}
    <div class="eval-empty eval-err">
      {error}
      <button class="eval-retry" onclick={run}>重试</button>
    </div>
  {/if}

  <!-- ─── 加载态 ─── -->
  {#if loading}
    <div class="eval-empty">正在评测 {kb} 知识库…</div>
  {/if}

  <!-- ─── 结果区 ─── -->
  {#if ran && !loading && !error && result}
    <div class="eval-result">
      <!-- 大数字指标：题数 / 命中 / 得分 -->
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

      <!-- 命中率进度条 -->
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
  .btn-run {
    background: #2563eb; color: #fff;
    border: none; border-radius: 4px;
    padding: 8px 16px; font-size: 13px; font-weight: 600;
    cursor: pointer; transition: background 0.15s;
  }
  .btn-run:hover:not(:disabled) { background: #1d4ed8; }
  .btn-run:disabled { background: #94a3b8; cursor: not-allowed; }
  .eval-hint { font-size: 11px; color: #64748b; }

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

  /* ─── 结果区 ─── */
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

  /* ─── 进度条 ─── */
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

  @media (max-width: 720px) {
    .kpi-row { grid-template-columns: repeat(3, 1fr); }
    .eval-top { flex-direction: column; align-items: stretch; }
    .eval-kb { justify-content: space-between; }
  }
</style>
