<script>
  // AnalysisResult — 智能分析结果可视化：SVG 图表 + 分析报告
  // stats: {total_devices, status_dist, device_type_dist, zone_dist, line_stats, fault_rate}
  // report: LLM 生成的 markdown 分析文本
  let { stats, report } = $props();

  const STATUS_COLOR = { running: '#10b981', idle: '#f59e0b', alarm: '#ef4444', maintenance: '#8b5cf6', offline: '#64748b', '正常': '#10b981', '故障': '#ef4444' };
  const PALETTE = ['#0D9488', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6', '#06b6d4', '#ec4899', '#84cc16'];

  // 转数组（stats 里是 dict）
  const statusArr = $derived(Object.entries(stats?.status_dist || {}).map(([k, v], i) => ({ name: k, value: v, color: STATUS_COLOR[k] || PALETTE[i % PALETTE.length] })));
  const typeArr = $derived(Object.entries(stats?.device_type_dist || {}).map(([k, v], i) => ({ name: k, value: v, color: PALETTE[i % PALETTE.length] })));
  const maxStatus = $derived(statusArr.length ? Math.max(...statusArr.map(d => d.value)) : 1);
  const maxType = $derived(typeArr.length ? Math.max(...typeArr.map(d => d.value)) : 1);

  // 产线对比（多表 join 结果）
  const lineArr = $derived(stats?.line_stats || []);
  const maxLine = $derived(lineArr.length ? Math.max(...lineArr.map(l => l.device_count)) : 1);

  // L2 关键指标迷你卡片（总数/运行/异常/故障率）
  const FAULT_THRESHOLD = 0.05;  // 故障率告警阈值 5%
  const anomalyCount = $derived(Object.entries(stats?.status_dist || {}).filter(([k]) => ['alarm', 'maintenance', 'offline'].includes(k)).reduce((s, [, v]) => s + v, 0));
  const faultPct = $derived(Math.round((stats?.fault_rate ?? 0) * 100));
  const faultAlert = $derived(faultPct > FAULT_THRESHOLD * 100);
  const kpis = $derived([
    { label: '设备总数', value: stats?.total_devices ?? 0, color: '#0D9488', alert: false },
    { label: '运行中', value: stats?.status_dist?.running ?? 0, color: '#10b981', alert: false },
    { label: '异常/维护', value: anomalyCount, color: anomalyCount > 0 ? '#ef4444' : '#10b981', alert: anomalyCount > 0 },
    { label: '故障率', value: faultPct + '%', color: faultAlert ? '#ef4444' : '#10b981', alert: faultAlert },
  ]);

  // 环形图（状态分布）
  const totalStatus = $derived(statusArr.reduce((s, d) => s + d.value, 0) || 1);
  function arcPath(index) {
    // 简易环形（用 stroke-dasharray 实现）
    return { index };
  }

  // 时序观测趋势
  const trendArr = $derived(stats?.observations || []);
  const trendColor = $derived((dir) => dir === '上升' ? '#ef4444' : dir === '下降' ? '#10b981' : '#f59e0b');

  // markdown 表格渲染：提取 | 行 | 转为表格
  function parseMarkdown(text) {
    const lines = (text || '').split('\n');
    const blocks = [];
    let inTable = false, table = [];
    for (const line of lines) {
      const trimmed = line.trim();
      if (trimmed.startsWith('|')) {
        const cells = trimmed.split('|').filter(c => c.trim() !== '').map(c => c.trim().replace(/\*\*/g, ''));
        if (cells.some(c => /^[-:]+$/.test(c))) { inTable = true; continue; }  // 分隔行
        table.push(cells);
        inTable = true;
        continue;
      }
      if (inTable && table.length) { blocks.push({ type: 'table', rows: table }); table = []; inTable = false; }
      if (trimmed.startsWith('#')) {
        const level = trimmed.match(/^#+/)[0].length;
        blocks.push({ type: 'h', level, text: trimmed.replace(/^#+\s*/, '') });
      } else if (trimmed) {
        blocks.push({ type: 'p', text: trimmed.replace(/\*\*/g, '') });
      }
    }
    if (inTable && table.length) blocks.push({ type: 'table', rows: table });
    return blocks;
  }
  const blocks = $derived(parseMarkdown(report));
</script>

<div class="analysis">
  {#if stats}
    <!-- L2 关键指标迷你卡片 -->
    <div class="kpi-row">
      {#each kpis as k}
        <div class="mini-card" class:alert={k.alert}>
          <div class="mc-accent" style="background:{k.color}"></div>
          <div class="mc-num" style="color:{k.color}">{k.value}</div>
          <div class="mc-label">{k.label}</div>
        </div>
      {/each}
    </div>

    <div class="chart-grid">
      <!-- 状态分布（SVG 环形） -->
      <div class="chart-card">
        <div class="chart-title">设备状态分布</div>
        <div class="donut-wrap">
          <svg viewBox="0 0 120 120" class="donut">
            {#each statusArr as d, i}
              {@const pct = d.value / totalStatus}
              {@const dash = pct * 226}
              {@const offset = 113 - (statusArr.slice(0, i).reduce((s,x)=>s+x.value,0) / totalStatus) * 226}
              <circle cx="60" cy="60" r="36" fill="none" stroke={d.color} stroke-width="14"
                      stroke-dasharray="{dash} 226" stroke-dashoffset="{offset}"
                      transform="rotate(-90 60 60)" stroke-linecap="butt"/>
            {/each}
            <text x="60" y="57" text-anchor="middle" font-size="16" font-weight="700" fill="#1e293b">{totalStatus}</text>
            <text x="60" y="73" text-anchor="middle" font-size="8" fill="#94a3b8">总数</text>
          </svg>
          <div class="donut-legend">
            {#each statusArr as d}
              <div class="lg"><span class="lg-dot" style="background:{d.color}"></span>{d.name} <b>{d.value}</b></div>
            {/each}
          </div>
        </div>
      </div>

      <!-- 类型分布（柱状图） -->
      <div class="chart-card">
        <div class="chart-title">设备类型分布</div>
        <div class="bars">
          {#each typeArr as d}
            <div class="bar-row">
              <span class="bar-label">{d.name}</span>
              <div class="bar-track"><div class="bar-fill" style="width:{Math.round(d.value/maxType*100)}%;background:{d.color}"></div></div>
              <span class="bar-val">{d.value}</span>
            </div>
          {/each}
        </div>
      </div>
    </div>

    <!-- 产线对比表 -->
    {#if lineArr.length}
      <div class="chart-card">
        <div class="chart-title">产线设备对比（负责人 / 异常 / 功率）</div>
        <table class="line-table">
          <thead><tr><th>产线</th><th>名称</th><th>区域</th><th>负责人</th><th>设备</th><th>运行</th><th>异常</th><th>功率(kW)</th></tr></thead>
          <tbody>
            {#each lineArr as l}
              <tr>
                <td class="mono">{l.line}</td><td>{l.name}</td><td>{l.area}</td><td>{l.supervisor}</td>
                <td class="mono">{l.device_count}</td>
                <td class="mono" style="color:#10b981">{l.running}</td>
                <td class="mono" style="color:{l.alarm>0?'#ef4444':'#94a3b8'}">{l.alarm}</td>
                <td class="mono">{l.total_power_kw}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}

    <!-- 时序观测趋势 -->
    {#if trendArr.length}
      <div class="chart-card">
        <div class="chart-title">时序观测趋势（传感器指标变化）</div>
        <table class="line-table">
          <thead><tr><th>传感器</th><th>指标</th><th>起始值</th><th>当前值</th><th>变化率</th><th>趋势</th></tr></thead>
          <tbody>
            {#each trendArr as t}
              <tr>
                <td class="mono">{t.sensor}</td>
                <td>{t.metric}</td>
                <td class="mono">{t.start}</td>
                <td class="mono">{t.end}</td>
                <td class="mono" style="color:{t.change>0?'#ef4444':'#10b981'}">{t.change_pct > 0 ? '+' : ''}{t.change_pct}%</td>
                <td>
                  <span class="trend-badge" style="background:{trendColor(t.direction)}18;color:{trendColor(t.direction)};border-color:{trendColor(t.direction)}40;">
                    {t.direction === '上升' ? '↗' : t.direction === '下降' ? '↘' : '→'} {t.direction}
                  </span>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  {/if}

  <!-- 分析报告 -->
  {#if report}
    <div class="report">
      <div class="report-title">📊 分析报告</div>
      <div class="report-body">
        {#each blocks as b}
          {#if b.type === 'h'}
            {#if b.level === 1}
              <h4 class="r-h1">{b.text}</h4>
            {:else if b.level === 2}
              <h5 class="r-h2">{b.text}</h5>
            {:else}
              <h6 class="r-h3">{b.text}</h6>
            {/if}
          {:else if b.type === 'table'}
            <table class="r-table">
              {#each b.rows as row, ri}
                {#if ri === 0}
                  <thead><tr>{#each row as c}<th>{c}</th>{/each}</tr></thead>
                {:else}
                  <tbody><tr>{#each row as c}<td>{c}</td>{/each}</tr></tbody>
                {/if}
              {/each}
            </table>
          {:else}
            <p class="r-p">{b.text}</p>
          {/if}
        {/each}
      </div>
    </div>
  {/if}
</div>

<style>
  .analysis { display: flex; flex-direction: column; gap: 12px; }
  .kpi-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
  .mini-card { position:relative; overflow:hidden; background:#fff; border:1px solid #e2e8f0; border-radius:6px; padding:16px 12px 12px; text-align:center; box-shadow:0 1px 2px rgba(15,23,42,.05); }
  .mini-card.alert { border-color:#ef4444; box-shadow:0 0 0 1px #ef4444; }
  .mc-accent { position:absolute; top:0; left:0; right:0; height:3px; }
  .mc-num { font-size:24px; font-weight:700; line-height:1.1; font-family:monospace; }
  .mc-label { font-size:11px; color:#64748b; margin-top:4px; }

  .chart-grid { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
  .chart-card { background:#f8fafc; border:1px solid #e2e8f0; border-radius:4px; padding:14px; }
  .chart-title { font-size:12px; font-weight:700; color:#1e293b; margin-bottom:10px; }

  /* 环形 */
  .donut-wrap { display:flex; align-items:center; gap:14px; }
  .donut { width:120px; height:120px; flex-shrink:0; }
  .donut-legend { display:flex; flex-direction:column; gap:4px; font-size:11px; color:#475569; }
  .lg { display:flex; align-items:center; gap:6px; }
  .lg-dot { width:8px; height:8px; border-radius:2px; }
  .lg b { margin-left:auto; color:#1e293b; }

  /* 柱状 */
  .bars { display:flex; flex-direction:column; gap:7px; }
  .bar-row { display:flex; align-items:center; gap:8px; }
  .bar-label { width:62px; font-size:11px; color:#475569; text-align:right; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .bar-track { flex:1; height:14px; background:#e2e8f0; border-radius:3px; overflow:hidden; }
  .bar-fill { height:100%; border-radius:3px; transition:width .5s; min-width:2px; }
  .bar-val { width:24px; font-size:11px; font-weight:600; color:#475569; font-family:monospace; }

  /* 产线表 */
  .line-table { width:100%; border-collapse:collapse; }
  .line-table th,.line-table td { padding:5px 8px; font-size:11px; text-align:left; border-bottom:1px solid #e2e8f0; }
  .line-table th { color: #64748b; font-weight: 600; background: #f1f5f9; }
  .mono { font-family: monospace; }
  .trend-badge {
    display: inline-block; font-size: 11px; font-weight: 600;
    padding: 2px 8px; border-radius: 10px; border: 1px solid;
  }

  /* 报告 */
  .report { background:#fff; border:1px solid #e2e8f0; border-radius:4px; padding:14px; }
  .report-title { font-size:13px; font-weight:700; color:#1e293b; margin-bottom:10px; }
  .report-body { font-size:12px; color:#334155; line-height:1.7; }
  .r-h1 { font-size:15px; color:#1e293b; margin:14px 0 6px; border-bottom:1px solid #e2e8f0; padding-bottom:4px; }
  .r-h2 { font-size:13px; color:var(--brand); margin:10px 0 4px; }
  .r-h3 { font-size:12px; color:#475569; margin:8px 0 3px; }
  .r-p { margin:5px 0; }
  .r-table { border-collapse:collapse; margin:6px 0; width:100%; }
  .r-table th,.r-table td { border:1px solid #e2e8f0; padding:4px 8px; font-size:11px; text-align:left; }
  .r-table th { background:#f8fafc; font-weight:600; }

  @media(max-width:720px){ .chart-grid{grid-template-columns:1fr;} .kpi-row{grid-template-columns:repeat(2,1fr);} }
</style>
