<script>
  // DashboardPanel — 数据可视化面板（设备类型分布 / 状态分布 / 产线统计）
  // 纯 CSS 条形图，零图表依赖
  import { onMount } from 'svelte';
  import { fetchStats } from '../lib/api.js';

  let stats = $state(null);
  let loading = $state(true);
  let error = $state('');

  // 图表配色
  const TYPE_COLORS = ['#3b82f6', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6', '#06b6d4', '#ec4899', '#84cc16'];
  const STATUS_COLOR = { running: '#10b981', idle: '#f59e0b', alarm: '#ef4444', maintenance: '#8b5cf6', offline: '#64748b' };

  // 计算最大数用于比例
  const maxType = $derived(stats && stats.device_type_dist?.length ? Math.max(...stats.device_type_dist.map(d => d.count)) : 1);
  const maxStatus = $derived(stats && stats.status_dist?.length ? Math.max(...stats.status_dist.map(d => d.count)) : 1);
  const maxLine = $derived(stats && stats.line_stats?.length ? Math.max(...stats.line_stats.map(d => d.device_count)) : 1);

  onMount(async () => {
    try {
      const res = await fetchStats();
      if (res.ok && res.stats) {
        stats = res.stats;
      } else {
        error = res.error || '统计加载失败';
      }
    } catch (e) {
      error = '网络错误';
    } finally {
      loading = false;
    }
  });
</script>

<div class="dash">
  {#if loading}
    <div class="dash-empty">正在加载统计数据…</div>
  {:else if error}
    <div class="dash-empty dash-err">{error}</div>
  {:else if stats}
    <!-- 顶部概览指标 -->
    <div class="kpi-row">
      <div class="kpi">
        <div class="kpi-num">{stats.total_devices}</div>
        <div class="kpi-label">设备总数</div>
      </div>
      <div class="kpi">
        <div class="kpi-num" style="color:#10b981">{stats.status_dist?.find(s => s.status === 'running')?.count ?? 0}</div>
        <div class="kpi-label">运行中</div>
      </div>
      <div class="kpi">
        <div class="kpi-num" style="color:#ef4444">{stats.status_dist?.find(s => ['alarm','maintenance','offline'].includes(s.status))?.count ?? 0}</div>
        <div class="kpi-label">异常/维护</div>
      </div>
      <div class="kpi">
        <div class="kpi-num" style="color:#f59e0b">{Math.round(stats.fault_rate * 100)}%</div>
        <div class="kpi-label">故障率</div>
      </div>
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
          <tr><th>产线</th><th>名称</th><th>区域</th><th>负责人</th><th>设备数</th><th>运行</th><th>异常</th><th>总功率(kW)</th></tr>
        </thead>
        <tbody>
          {#each stats.line_stats as l}
            <tr>
              <td class="mono">{l.line}</td>
              <td>{l.name}</td>
              <td>{l.area}</td>
              <td>{l.supervisor}</td>
              <td class="mono">{l.device_count}</td>
              <td class="mono" style="color:#10b981">{l.running}</td>
              <td class="mono" style="color:{l.alarm > 0 ? '#ef4444' : '#94a3b8'}">{l.alarm}</td>
              <td class="mono">{l.total_power_kw}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</div>

<style>
  .dash { display: flex; flex-direction: column; gap: 12px; }
  .dash-empty { color: #94a3b8; font-size: 13px; text-align: center; padding: 30px; }
  .dash-err { color: #dc2626; }

  /* KPI */
  .kpi-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
  .kpi {
    background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 4px;
    padding: 12px; text-align: center;
  }
  .kpi-num { font-size: 22px; font-weight: 700; color: #1e293b; font-family: 'Consolas', monospace; }
  .kpi-label { font-size: 11px; color: #64748b; margin-top: 2px; }

  /* Charts */
  .chart-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .chart-card {
    background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 4px;
    padding: 14px;
  }
  .chart-title { font-size: 12px; font-weight: 700; color: #1e293b; margin-bottom: 12px; letter-spacing: 0.3px; }

  .bars { display: flex; flex-direction: column; gap: 8px; }
  .bar-row { display: flex; align-items: center; gap: 8px; }
  .bar-label { width: 70px; font-size: 12px; color: #334155; text-align: right; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .bar-track { flex: 1; height: 16px; background: #e2e8f0; border-radius: 3px; overflow: hidden; }
  .bar-fill { height: 100%; border-radius: 3px; transition: width 0.5s ease; min-width: 2px; }
  .bar-val { width: 30px; font-size: 12px; font-weight: 600; color: #475569; font-family: 'Consolas', monospace; }

  /* Line table */
  .line-card { padding: 14px; }
  .line-table { width: 100%; border-collapse: collapse; }
  .line-table th, .line-table td {
    padding: 6px 8px; font-size: 12px; text-align: left;
    border-bottom: 1px solid #e2e8f0;
  }
  .line-table th { color: #64748b; font-weight: 600; background: #f1f5f9; }
  .line-table td { color: #334155; }
  .mono { font-family: 'Consolas', monospace; }

  @media (max-width: 720px) {
    .chart-grid { grid-template-columns: 1fr; }
    .kpi-row { grid-template-columns: repeat(2, 1fr); }
  }
</style>
