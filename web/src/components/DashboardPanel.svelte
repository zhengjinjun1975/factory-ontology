<script>
  // DashboardPanel — 数据可视化面板（设备类型分布 / 状态分布 / 产线统计）
  // 纯 CSS 条形图，零图表依赖
  // 单企业收敛：kb 由父级（App.svelte）注入 currentKb（当前登录企业唯一 kb），
  // kb prop 变化时 $effect 自动重新加载（看板跟随当前企业）。
  import { fetchStats } from '../lib/api.js';

  let { kb = '' } = $props();    // 当前企业唯一 kb（只读 prop，跟随登录企业）

  let stats = $state(null);
  let loading = $state(true);
  let error = $state('');
  let empty = $state(false);   // 未建模空态（stats 为 null 且 empty=true，非报错）

  // 图表配色
  const TYPE_COLORS = ['#0D9488', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6', '#06b6d4', '#ec4899', '#84cc16'];
  const STATUS_COLOR = { running: '#10b981', idle: '#f59e0b', alarm: '#ef4444', maintenance: '#8b5cf6', offline: '#64748b' };

  // 计算最大数用于比例
  const maxType = $derived(stats && stats.device_type_dist?.length ? Math.max(...stats.device_type_dist.map(d => d.count)) : 1);
  const maxStatus = $derived(stats && stats.status_dist?.length ? Math.max(...stats.status_dist.map(d => d.count)) : 1);
  const maxLine = $derived(stats && stats.line_stats?.length ? Math.max(...stats.line_stats.map(d => d.device_count)) : 1);

  // 诊断式：异常汇总 + 关键指标（黄金三角置顶）
  const total = $derived(stats?.total_devices ?? 0);
  const running = $derived(stats?.status_dist?.find(s => s.status === 'running')?.count ?? 0);
  const anomalyCount = $derived((stats?.status_dist || []).filter(s => ['alarm', 'maintenance', 'offline'].includes(s.status)).reduce((sum, s) => sum + (s.count || 0), 0));
  const faultPct = $derived(Math.round((stats?.fault_rate ?? 0) * 100));
  const anomalyPct = $derived(total ? Math.round(anomalyCount / total * 100) : 0);
  const faultAlert = $derived(faultPct > 5 || anomalyCount > 0);
  const kpis = $derived([
    { label: '故障/异常设备', value: anomalyCount, color: anomalyCount > 0 ? '#ef4444' : '#10b981', alert: anomalyCount > 0 },
    { label: '故障率', value: faultPct + '%', color: faultAlert ? '#ef4444' : '#10b981', alert: faultAlert },
    { label: '设备总数', value: total, color: '#0D9488', alert: false },
    { label: '运行中', value: running, color: '#10b981', alert: false },
  ]);

  async function load() {
    loading = true; error = ''; empty = false;
    try {
      const res = await fetchStats(kb);
      if (res.ok && res.stats) {
        stats = res.stats;
      } else if (res.ok && res.empty) {
        empty = true;   // 尚未建模：友好空态而非报错
      } else {
        error = res.error || '统计加载失败';
      }
    } catch (e) {
      error = '网络错误';   // 仅真网络异常
    } finally {
      loading = false;
    }
  }

  // 初始 + kb prop 变化（切换/重置后）→ 重新加载当前企业统计
  $effect(() => { load(); });
</script>

<div class="dash">
  {#if loading}
    <div class="skel-block">
      <div class="skel-row">
        <div class="skel skel-kpi"></div>
        <div class="skel skel-kpi"></div>
        <div class="skel skel-kpi"></div>
      </div>
      <div class="skel skel-line-lg"></div>
      <div class="skel skel-line-lg"></div>
      <div class="skel skel-line-lg"></div>
    </div>
  {:else if empty}
    <div class="dash-empty dash-nodata">
      <span class="empty-icon">📊</span>
      <span class="empty-text">尚未建模，请先在「数据建模」页上传数据</span>
    </div>
  {:else if error}
    <div class="dash-empty dash-err">
      {error}
      <button class="dash-retry" onclick={load}>重试</button>
    </div>
  {:else if stats}
    <!-- 异常告警横幅：突出异常而非埋在统计里 -->
    {#if faultAlert}
      <div class="alert-banner">
        <span class="alert-ico">⚠</span>
        <span>检测到 <b>{anomalyCount}</b> 台设备异常/故障，占设备总数 <b>{anomalyPct}%</b>（故障率 {faultPct}%）。</span>
      </div>
    {/if}

    <!-- 诊断关键指标（黄金三角：异常置顶左上） -->
    <div class="kpi-row">
      {#each kpis as k, i}
        <div class="mini-card" class:alert={k.alert} class:lead={i === 0}>
          <div class="mc-accent" style="background:{k.color}"></div>
          <div class="mc-num" style="color:{k.color}">{k.value}</div>
          <div class="mc-label">{k.label}</div>
        </div>
      {/each}
    </div>

    <div class="chart-grid">
      <!-- 设备类型分布 -->
      <div class="chart-card">
        <div class="chart-title">设备类型分布</div>
        <div class="bars">
          {#each stats.device_type_dist as d, i}
            <div class="bar-row">
              <span class="bar-label">{d.type}</span>
              <div class="bar-track">
                <div class="bar-fill" style="width: {Math.round(d.count / maxType * 100)}%; background: {TYPE_COLORS[i % TYPE_COLORS.length]};"></div>
              </div>
              <span class="bar-val">{d.count}</span>
            </div>
          {/each}
        </div>
      </div>

      <!-- 状态分布 -->
      <div class="chart-card">
        <div class="chart-title">设备状态分布</div>
        <div class="bars">
          {#each stats.status_dist as s}
            <div class="bar-row">
              <span class="bar-label">{s.status}</span>
              <div class="bar-track">
                <div class="bar-fill" style="width: {Math.round(s.count / maxStatus * 100)}%; background: {STATUS_COLOR[s.status] || '#94a3b8'};"></div>
              </div>
              <span class="bar-val">{s.count}</span>
            </div>
          {/each}
        </div>
      </div>
    </div>

    <!-- 产线统计（多表 join 结果） -->
    <div class="chart-card line-card">
      <div class="chart-title">产线设备统计（含负责人/区域）</div>
      <table class="line-table">
        <thead>
          <tr><th>产线</th><th>名称</th><th>区域</th><th>负责人</th><th class="col-num">设备数</th><th class="col-num">运行</th><th class="col-num">异常</th><th class="col-num">总功率(kW)</th></tr>
        </thead>
        <tbody>
          {#each stats.line_stats as l}
            <tr>
              <td class="id-col">{l.line}</td>
              <td>{l.name}</td>
              <td>{l.area}</td>
              <td>{l.supervisor}</td>
              <td class="num-col">{l.device_count}</td>
              <td class="num-col" style="color:{l.running > 0 ? 'var(--success)' : 'var(--text-muted)'}">{l.running}</td>
              <td class="num-col" style="color:{l.alarm > 0 ? 'var(--danger)' : 'var(--text-muted)'}">{l.alarm}</td>
              <td class="num-col">{l.total_power_kw}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</div>

<style>
  .dash { display: flex; flex-direction: column; gap: 12px; }
  .dash-empty {
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    gap: 10px; padding: 48px 20px; text-align: center; color: var(--text-muted); font-size: 13px;
  }
  .dash-empty .empty-icon { font-size: 28px; line-height: 1; }
  .dash-empty .empty-text { color: var(--text-muted); }

  /* 骨架屏 */
  @keyframes shimmer { 0% { background-position: -360px 0; } 100% { background-position: 360px 0; } }
  .skel {
    border-radius: 6px;
    background: linear-gradient(90deg, #eef1f5 25%, #f7f9fc 40%, #eef1f5 55%);
    background-size: 720px 100%; animation: shimmer 1.4s infinite linear;
  }
  .skel-block { display: flex; flex-direction: column; gap: 12px; }
  .skel-row { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
  .skel-kpi { height: 74px; }
  .skel-line-lg { height: 14px; width: 100%; }
  .dash-nodata { color: var(--text-muted); }   /* 未建模空态：中性灰，非红色 */
  .dash-err { color: var(--danger); }
  .dash-retry {
    margin-left: 10px; background: var(--bg-card); color: var(--brand);
    border: 1px solid var(--border); border-radius: var(--radius-sm);
    padding: 4px 12px; font-size: 12px; cursor: pointer;
    transition: background 150ms ease-out;
  }
  .dash-retry:hover { border-color: var(--brand-line); background: var(--bg-hover); }

  /* KPI 迷你卡片 + 告警 */
  .alert-banner {
    display: flex; align-items: center; gap: 8px;
    background: var(--danger-bg); border: 1px solid var(--border); color: var(--danger-fg);
    border-radius: var(--radius-md); padding: 10px 14px; font-size: 13px;
  }
  .alert-ico { font-size: 15px; }
  .kpi-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
  .mini-card {
    position: relative; overflow: hidden;
    background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius-md);
    padding: 16px 12px 12px; text-align: center;
    box-shadow: var(--shadow-card);
  }
  .mini-card.alert { border-color: var(--danger); box-shadow: 0 0 0 1px var(--danger); }
  .mini-card.lead { padding: 20px 12px 16px; }
  .mini-card.lead .mc-accent { height: 4px; }
  .mini-card.lead .mc-num { font-size: 30px; }
  .mc-accent { position: absolute; top: 0; left: 0; right: 0; height: 3px; }
  .mc-num { font-size: 24px; font-weight: 700; line-height: 1.1; font-family: ui-monospace, 'SF Mono', Consolas, monospace; font-variant-numeric: tabular-nums; }
  .mc-label { font-size: 11px; color: var(--text-secondary); margin-top: 4px; }

  /* Charts */
  .chart-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .chart-card {
    background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius-md);
    padding: 14px; box-shadow: var(--shadow-card);
  }
  .chart-title { font-size: 12px; font-weight: 700; color: var(--text-primary); margin-bottom: 12px; letter-spacing: 0.3px; }

  .bars { display: flex; flex-direction: column; gap: 8px; }
  .bar-row { display: flex; align-items: center; gap: 8px; }
  .bar-label { width: 70px; font-size: 12px; color: var(--text-primary); text-align: right; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .bar-track { flex: 1; height: 16px; background: var(--border); border-radius: var(--radius-sm); overflow: hidden; }
  .bar-fill { height: 100%; border-radius: var(--radius-sm); transition: width 150ms ease-out; min-width: 2px; }
  .bar-val { width: 30px; font-size: 12px; font-weight: 600; color: var(--text-secondary); font-family: ui-monospace, 'SF Mono', Consolas, monospace; font-variant-numeric: tabular-nums; }

  /* Line table */
  .line-card { padding: 14px; }
  .line-table { width: 100%; border-collapse: collapse; }
  .line-table thead th {
    position: sticky; top: 0; z-index: 1;
    padding: 8px 10px; font-size: 12px; font-weight: 600; text-align: left;
    color: var(--text-secondary); background: var(--bg-elevated);
    border-bottom: 1px solid var(--border);
  }
  .line-table tbody td {
    padding: 0 10px; font-size: 13px; text-align: left; color: var(--text-primary);
    border-bottom: 1px solid var(--border);
    height: 40px; transition: background 150ms ease-out;
  }
  .line-table tbody tr:last-child td { border-bottom: none; }
  .line-table tbody tr:hover td { background: var(--bg-hover); }
  .id-col { font-family: ui-monospace, 'SF Mono', SFMono-Regular, Menlo, Consolas, monospace; font-size: 13px; color: var(--text-secondary); }
  .num-col { text-align: right; font-variant-numeric: tabular-nums; font-family: ui-monospace, 'SF Mono', Consolas, monospace; }
  .col-num { text-align: right; }

  @media (max-width: 720px) {
    .chart-grid { grid-template-columns: 1fr; }
    .kpi-row { grid-template-columns: repeat(2, 1fr); }
    .skel-row { grid-template-columns: 1fr; }
  }
</style>
