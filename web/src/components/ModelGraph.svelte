<script>
  // ModelGraph — 本体模型力导向图（ECharts graph，学习 sme 库 instance-graph.js）
  // 数据源：/api/ontology/graph 图结构 {nodes, edges}（后端本体实例图，nodes=[{id,name,entity}], edges=[{from,to,rel}]）
  // 层级：企业 hub 顶部 → 业务域/实体类 → 实例；roam 缩放平移 + 分类着色 + 多样化链接
  import { onMount } from 'svelte';
  import echarts from '../lib/echarts.cjs';
  import { fetchGraph } from '../lib/api.js';

  let { refreshKey = 0 } = $props();   // 建模时间戳，变化时重新加载

  let graph = $state(null);            // {nodes:[{id,name,entity}], edges:[{source,target,rel}]}
  let loading = $state(true);
  let error = $state('');
  let chartEl = $state(null);
  let chart = $state(null);

  // 关系类型 → 颜色/线型（多样化链接：不同 rel 不同色）
  const REL_STYLE = {
    owns:    { color: '#2563eb', width: 2,  type: 'solid',  opacity: 0.55 },  // 企业→实体
    type:    { color: '#94a3b8', width: 1,  type: 'solid',  opacity: 0.5 },   // 类→实例
  };
  const OBJ_REL_COLORS = ['#0ea5e9', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6', '#ec4899', '#14b8a6', '#f97316', '#64748b'];
  function relStyle(rel) {
    if (REL_STYLE[rel]) return REL_STYLE[rel];
    // 对象属性按 rel 哈希取稳定颜色，实现多样化链接
    let h = 0;
    for (let i = 0; i < rel.length; i++) h = (h * 31 + rel.charCodeAt(i)) >>> 0;
    return { color: OBJ_REL_COLORS[h % OBJ_REL_COLORS.length], width: 1.5, type: 'dashed', opacity: 0.7 };
  }

  // 实体(类)分类色
  const ENTITY_COLORS = ['#2563eb', '#0ea5e9', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6', '#ec4899', '#14b8a6', '#f97316', '#64748b', '#6366f1', '#84cc16'];
  function entityColor(name) {
    let h = 0;
    for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
    return ENTITY_COLORS[h % ENTITY_COLORS.length];
  }

  // 节点分类名（着色依据）：hub / cls_ 类 / 其余按实体类
  function nodeCat(n) {
    if (n.id === '__hub__') return '企业';
    if (String(n.id).startsWith('cls_')) return '类';
    return n.entity || n.name || '其他';
  }

  function render() {
    if (!chartEl || !graph?.nodes?.length) return;
    // 归一化边：后端图结构用 from/to，统一转成 source/target 供 ECharts 消费
    const nodes = graph.nodes, edges = (graph.edges || []).map(e => ({
      source: e.source ?? e.from, target: e.target ?? e.to, rel: e.rel,
    }));
    // 分类 = 所有实体类名（着色依据）
    const cats = [...new Set(nodes.map(nodeCat))];
    const catIdx = {}; cats.forEach((c, i) => (catIdx[c] = i));

    const data = nodes.map(n => {
      const isHub = n.id === '__hub__';
      const isCls = String(n.id).startsWith('cls_');
      const color = isHub ? '#2563eb' : (isCls ? '#1e293b' : entityColor(nodeCat(n)));
      return {
        id: n.id, name: n.name,
        symbolSize: isHub ? 64 : (isCls ? 30 : 11),
        category: catIdx[nodeCat(n)] ?? 0,
        itemStyle: { color, borderColor: isCls ? color : '#ffffff', borderWidth: isCls ? 0 : 1 },
        label: isCls ? { show: true, fontSize: 12, fontWeight: 'bold', color: '#1e293b' } : undefined,
        tooltip: { formatter: `<b>${nodeCat(n)}</b> ${n.name}<br/>id：${n.id}` },
      };
    });

    const ids = new Set(nodes.map(n => n.id));
    const links = edges
      .filter(e => ids.has(e.source) && ids.has(e.target))
      .map(e => {
        const st = relStyle(e.rel);
        return {
          source: e.source, target: e.target,
          lineStyle: { color: st.color, width: st.width, opacity: st.opacity, type: st.type },
          label: { show: e.rel !== 'type' && e.rel !== 'owns', fontSize: 9, color: st.color, formatter: e.rel },
        };
      });

    if (chart) chart.dispose();
    chart = echarts.init(chartEl);
    chart.setOption({
      tooltip: { show: true },
      animationDuration: 600,
      legend: {
        top: 4, left: 'center', type: 'scroll',
        textStyle: { fontSize: 10, color: '#475569' },
        icon: 'circle', itemWidth: 10, itemHeight: 10,
        data: cats.map(c => ({ name: c, itemStyle: { color: c === '企业' ? '#2563eb' : (c === '类' ? '#1e293b' : entityColor(c)) } })),
      },
      series: [{
        type: 'graph', layout: 'force', roam: true,
        force: { repulsion: 420, edgeLength: [70, 200], gravity: 0.08, friction: 0.6, layoutAnimation: true },
        label: { show: true, position: 'right', fontSize: 9, color: '#1a2233', formatter: p => p.name && p.name.slice(0, 12) },
        edgeSymbol: ['none', 'arrow'], edgeSymbolSize: [0, 6],
        lineStyle: { color: '#cbd5e1', width: 1, curveness: 0.15 },
        categories: cats.map(c => ({ name: c })),
        data, links,
        emphasis: { focus: 'adjacency', label: { show: true, fontSize: 11, fontWeight: 'bold' } },
      }],
    });
  }

  async function load() {
    loading = true; error = '';
    try {
      const res = await fetchGraph();
      if (res.ok && Array.isArray(res.nodes)) {
        graph = { nodes: res.nodes, edges: res.edges || [] };
        render();
      } else {
        error = res.error || '模型结构加载失败';
      }
    } catch (e) {
      error = '网络错误';
    } finally {
      loading = false;
    }
  }

  onMount(() => {
    load();
    window.addEventListener('resize', onResize);
    return () => {
      window.removeEventListener('resize', onResize);
      if (chart) chart.dispose();
    };
  });
  function onResize() { if (chart) chart.resize(); }

  // refreshKey 变化时（重新建模）重新加载
  $effect(() => {
    if (refreshKey > 0) load();
  });

  // 图统计：节点/边/关系种类（图结构直接推导，无类级元数据时归零）
  const instCount = $derived((graph?.nodes || []).filter(n => n.id !== '__hub__' && !String(n.id).startsWith('cls_')).length);
  const clsCount = $derived((graph?.nodes || []).filter(n => String(n.id).startsWith('cls_')).length);
  const edgeCount = $derived((graph?.edges || []).length);
  const rels = $derived([...new Set((graph?.edges || []).map(e => e.rel))].filter(r => r !== 'owns' && r !== 'type'));
</script>

<div class="model-graph">
  {#if loading}
    <div class="mg-empty">正在解析模型结构…</div>
  {:else if error}
    <div class="mg-empty mg-err">{error}</div>
  {:else if graph}
    <div class="mg-meta">
      <span class="mg-graph">节点：{graph.nodes.length}</span>
      <span class="mg-inst">{instCount} 个实例</span>
      <span class="mg-cls">{clsCount} 个类</span>
      <span class="mg-obj">{rels.length} 种对象关系</span>
      <span class="mg-dp">边：{edgeCount}</span>
    </div>

    <div class="mg-echarts" bind:this={chartEl}></div>

    {#if rels.length > 0}
      <div class="mg-legend">
        <span class="lg-title">多样化链接：</span>
        {#each rels as r}
          <span class="lg-item">
            <span class="lg-line" style="background:{relStyle(r).color}"></span>
            {r}
          </span>
        {/each}
      </div>
    {/if}

    <div class="mg-hint">拖动 / 滚轮缩放看全图 · 悬停高亮邻居 · 不同关系不同颜色线型</div>
  {/if}
</div>

<style>
  .model-graph { display: flex; flex-direction: column; gap: 10px; }
  .mg-empty { color: #94a3b8; font-size: 13px; text-align: center; padding: 40px; }
  .mg-err { color: #dc2626; }

  .mg-meta { display: flex; gap: 16px; flex-wrap: wrap; font-size: 12px; color: #475569; }
  .mg-cls { font-weight: 700; color: #1e293b; }
  .mg-inst, .mg-obj, .mg-dp, .mg-graph { color: #2563eb; }

  .mg-echarts {
    width: 100%; height: 520px;
    border: 1px solid #e2e8f0; border-radius: 4px;
    background: #ffffff;
  }

  .mg-legend { display: flex; gap: 12px; flex-wrap: wrap; font-size: 11px; color: #64748b; align-items: center; }
  .lg-title { color: #334155; font-weight: 600; }
  .lg-item { display: flex; align-items: center; gap: 5px; }
  .lg-line { width: 20px; height: 2px; border-radius: 1px; }

  .mg-hint { font-size: 11px; color: #94a3b8; }
</style>
