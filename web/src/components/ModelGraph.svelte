<script>
  // ModelGraph — 本体模型力导向图（ECharts graph，专业工业风浅色 · 分色分类 · 多样化链接）
  // 数据源：/api/ontology/graph 图结构 {nodes, edges}（nodes=[{id,name,entity}], edges=[{from,to,rel}]）
  // 层级：企业 hub（顶部大节点）→ 实体类（中号类节点）→ 实例（小节点）；roam 缩放平移 + 分类着色 + 多样化链接
  // 多租户跟随：接收 kb prop，fetchGraph(kb) 加载对应行业本体，切换行业图随之重建。
  import { onMount } from 'svelte';
  import echarts from '../lib/echarts.cjs';
  import { fetchGraph } from '../lib/api.js';

  let { refreshKey = 0, kb = '' } = $props();   // 建模时间戳变化重新加载；kb=当前激活知识库，图跟随该本体

  let graph = $state(null);            // {nodes:[{id,name,entity}], edges:[{from,to,rel}]}
  let loading = $state(true);
  let error = $state('');
  let chartEl = $state(null);
  let chart = $state(null);

  // ── kb → 行业标题（icon + 名）──
  const INDUSTRY_META = {
    valve:     { icon: '🔧', name: '阀门制造' },
    chem:      { icon: '🧪', name: '化工企业' },
    machining: { icon: '⚙️', name: '机械加工' },
    precision: { icon: '🔩', name: '精密加工' },
    bellows:   { icon: '🌀', name: '波纹管' },
    eco:       { icon: '♻️', name: '环保工程' },
    ship:      { icon: '🚢', name: '造船' },
    seismic:   { icon: '🌍', name: '地震勘探' },
    food_co:   { icon: '🥛', name: '食品溯源' },
  };
  function industryTitle(k) {
    const m = INDUSTRY_META[k];
    return m ? `${m.icon} ${m.name}` : (k ? k.toUpperCase() : '企业');
  }

  // ── 关系类型 → 颜色/线型（多样化链接：不同 rel 不同色）──
  const REL_STYLE = {
    owns:    { color: '#6366f1', width: 2.2, type: 'solid',  opacity: 0.5 },  // 企业→实体
    type:    { color: '#cbd5e1', width: 1,   type: 'solid',  opacity: 0.45 },  // 类→实例
  };
  // 对象属性多样化色板（专业工业风浅色系）
  const OBJ_REL_COLORS = ['#0ea5e9', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6', '#ec4899', '#14b8a6', '#f97316', '#64748b'];
  function relStyle(rel) {
    if (REL_STYLE[rel]) return REL_STYLE[rel];
    let h = 0;
    for (let i = 0; i < rel.length; i++) h = (h * 31 + rel.charCodeAt(i)) >>> 0;
    return { color: OBJ_REL_COLORS[h % OBJ_REL_COLORS.length], width: 1.4, type: 'dashed', opacity: 0.65 };
  }

  // 实体(类)分类色（工业风浅色系，分类稳定）
  const ENTITY_COLORS = ['#0D9488', '#06b6d4', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6', '#ec4899', '#14b8a6', '#f97316', '#64748b', '#6366f1', '#84cc16', '#0ea5e9', '#f43f5e'];
  function entityColor(name) {
    let h = 0;
    for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
    return ENTITY_COLORS[h % ENTITY_COLORS.length];
  }

  // 节点分类名（着色依据）：企业hub（无下划线的顶层实体）/ 实体类（带下划线的类）/ 实例(连接)节点
  function nodeCat(n) {
    if (n.id === '__hub__') return '企业';
    if (String(n.id).startsWith('cls_')) return '实体类';
    const ent = n.entity || '';
    // 顶层实体 = entity 无下划线且不等于自身类名（如 Valve / Chem / Ship）→ 企业hub
    if (ent && !ent.includes('_')) return '企业';
    return ent || n.name || '其他';
  }

  // 实体类中文标注（去掉行业前缀取语义段）
  const CLASS_ZH = {
    batches: '批次', customers: '客户', equipment: '设备', products: '产品',
    qc: '质检', raw_materials: '原材料', sales: '销售', batch_ingredient: '批次配料',
    productsCategory: '产品分类', orders: '订单', vessels: '船舶', teams: '班组',
    lines: '测线', shots: '炮点', projects: '项目', vessels_orders: '船舶订单',
  };
  function classZh(ent) {
    const seg = ent.split('_').slice(1).join('_');
    return CLASS_ZH[seg] || ent;
  }

  function render() {
    if (!chartEl || !graph?.nodes?.length) return;
    const nodes = graph.nodes, edges = (graph.edges || []).map(e => ({
      source: e.source ?? e.from, target: e.target ?? e.to, rel: e.rel,
    }));
    const local = u => String(u).split('#').pop().replace(/[<>]/g, '');
    // 实体种类：hub = 无下划线的顶层实体(Valve/Chem)；类/连接实体 = 其余 distinct entity
    const distinctEnts = new Set(nodes.map(n => n.entity).filter(Boolean));
    const hubEnts = new Set([...distinctEnts].filter(e => !e.includes('_')));
    // 类代表节点：local名 == entity（Valve_batches / valve_sales_product 等类与连接实体）
    // 节点角色：hub(企业大节点) / cls(类·连接代表节点中号) / 其余实例小节点
    const isHub = n => hubEnts.has(n.entity) && local(n.id) === n.entity;
    const isCls = n => !isHub(n) && local(n.id) === n.entity;

    // 分类 = 所有实体类名（着色依据；hub 归企业，类按 entity 着色）
    const cats = [...new Set(nodes.map(nodeCat))];
    const catIdx = {}; cats.forEach((c, i) => (catIdx[c] = i));

    const data = nodes.map(n => {
      const hub = isHub(n), cls = isCls(n);
      const color = hub ? '#4f46e5' : (cls ? '#475569' : entityColor(nodeCat(n)));
      const label = n.name;
      return {
        id: n.id, name: label,
        symbolSize: hub ? 72 : (cls ? 30 : 10),
        category: catIdx[nodeCat(n)] ?? 0,
        itemStyle: {
          color,
          borderColor: hub ? '#312e81' : '#ffffff',
          borderWidth: hub ? 2 : 1,
          shadowBlur: hub ? 14 : 5, shadowColor: 'rgba(79,70,229,0.25)',
        },
        label: {
          show: true,
          position: hub ? 'inside' : (cls ? 'bottom' : 'right'),
          fontSize: hub ? 14 : (cls ? 12 : 9),
          fontWeight: hub || cls ? 'bold' : 'normal',
          color: hub ? '#ffffff' : (cls ? '#334155' : '#1e293b'),
          formatter: p => {
            const t = p.name || '';
            return cls ? (classZh(t) || t) : (t.length > 10 ? t.slice(0, 10) + '…' : t);
          },
        },
        tooltip: { formatter: `<b>${nodeCat(n)}</b> ${label}<br/>id：${n.id}` },
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

    // 渲染前清理旧实例，避免内存/实例堆积（渲染不卡顿）
    if (chart) { chart.dispose(); chart = null; }
    chart = echarts.init(chartEl);
    chart.setOption({
      backgroundColor: '#ffffff',
      tooltip: { show: true, backgroundColor: 'rgba(255,255,255,0.96)', borderColor: '#e2e8f0', textStyle: { color: '#1e293b', fontSize: 12 } },
      animationDuration: 500,
      animationDurationUpdate: 300,
      legend: {
        top: 4, left: 'center', type: 'scroll',
        textStyle: { fontSize: 10, color: '#475569' },
        icon: 'circle', itemWidth: 9, itemHeight: 9, itemGap: 10,
        data: cats.map(c => ({ name: c, itemStyle: { color: c === '企业' ? '#4f46e5' : (c === '实体类' ? '#475569' : entityColor(c)) } })),
      },
      series: [{
        type: 'graph', layout: 'force', roam: true,
        force: { repulsion: 320, edgeLength: [60, 170], gravity: 0.12, friction: 0.6, layoutAnimation: false },
        label: { show: true, position: 'right', fontSize: 9, color: '#1e293b' },
        edgeSymbol: ['none', 'arrow'], edgeSymbolSize: [0, 6],
        lineStyle: { color: '#e2e8f0', width: 1, curveness: 0.15 },
        categories: cats.map(c => ({ name: c })),
        data, links,
        emphasis: { focus: 'adjacency', label: { show: true, fontSize: 11, fontWeight: 'bold' } },
      }],
    });
  }

  async function load() {
    loading = true; error = '';
    try {
      const res = await fetchGraph(kb);
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

  // refreshKey（重新建模）或 kb（切换知识库）变化时重新加载
  $effect(() => {
    if (refreshKey > 0 || kb) load();
  });

  // 图统计：节点/实体类/实例/关系种类
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
      <span class="mg-cur">当前行业：<b>{industryTitle(kb)}</b></span>
      <span class="mg-graph">节点：{graph.nodes.length}</span>
      <span class="mg-cls">{clsCount} 个实体类</span>
      <span class="mg-inst">{instCount} 个实例</span>
      <span class="mg-dp">边：{edgeCount}</span>
      {#if rels.length > 0}<span class="mg-obj">{rels.length} 种对象关系</span>{/if}
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

    <div class="mg-hint">拖动 / 滚轮缩放看全图 · 悬停高亮邻居 · 不同关系不同颜色线型 · 切换行业自动跟随</div>
  {/if}
</div>

<style>
  .model-graph { display: flex; flex-direction: column; gap: 10px; }
  .mg-empty { color: #94a3b8; font-size: 13px; text-align: center; padding: 40px; }
  .mg-err { color: #dc2626; }

  .mg-meta { display: flex; gap: 16px; flex-wrap: wrap; font-size: 12px; color: #475569; }
  .mg-cls { font-weight: 700; color: #475569; }
  .mg-inst, .mg-obj, .mg-dp, .mg-graph { color: var(--brand); }
  .mg-cur b { color: #3730a3; font-weight: 700; }

  .mg-echarts {
    width: 100%; height: 540px;
    border: 1px solid #e2e8f0; border-radius: 8px;
    background: #ffffff;
  }

  .mg-legend { display: flex; gap: 12px; flex-wrap: wrap; font-size: 11px; color: #64748b; align-items: center; }
  .lg-title { color: #334155; font-weight: 600; }
  .lg-item { display: flex; align-items: center; gap: 5px; }
  .lg-line { width: 20px; height: 2px; border-radius: 1px; }

  .mg-hint { font-size: 11px; color: #94a3b8; }
</style>
