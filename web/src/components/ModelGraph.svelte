<script>
  // ModelGraph — 本体层级示意图（SVG，企业→实体类→实例）
  // 数据源：/api/ontology/graph 图结构 {nodes, edges}（nodes=[{id,name,entity}], edges=[{from,to,rel}]）
  // 渲染：1280×900 HD 画布，顶部企业标题区，中间实体类卡片（中文标注），底部按实体归类的实例节点，关系线连接。
  // 跟随当前 kb：切换行业（阀门→化工）SVG 重建显示对应行业本体。
  import { onMount } from 'svelte';
  import { fetchGraph } from '../lib/api.js';

  let { refreshKey = 0, kb = '' } = $props();   // 建模时间戳变化重新加载；kb=当前激活知识库，图跟随该本体

  let graph = $state(null);            // {nodes:[{id,name,entity}], edges:[{from,to,rel}]}
  let loading = $state(true);
  let error = $state('');

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

  // ── 实体类 → 中文标注 ──
  const CLASS_ZH = {
    batches: '批次', customers: '客户', equipment: '设备', products: '产品',
    qc: '质检', raw_materials: '原材料', sales: '销售', batch_ingredient: '批次配料',
    productsCategory: '产品分类', orders: '订单', vessels: '船舶', teams: '班组',
    lines: '测线', shots: '炮点', projects: '项目', vessels_orders: '船舶订单',
  };
  function classZh(ent) {
    // 取实体类名最后一个语义段（去掉行业前缀），映射中文；无映射则保留原类名
    const seg = ent.split('_').slice(1).join('_');
    return CLASS_ZH[seg] || ent;
  }

  // 实体类区分色（浅色护眼）
  const CLASS_COLORS = [
    { fill: '#eef2ff', border: '#c7d2fe', head: '#6366f1', text: '#312e81' },
    { fill: '#ecfeff', border: '#a5f3fc', head: '#0891b2', text: '#164e63' },
    { fill: '#f0fdf4', border: '#bbf7d0', head: '#16a34a', text: '#14532d' },
    { fill: '#fffbeb', border: '#fde68a', head: '#d97706', text: '#78350f' },
    { fill: '#fef2f2', border: '#fecaca', head: '#dc2626', text: '#7f1d1d' },
    { fill: '#faf5ff', border: '#e9d5ff', head: '#9333ea', text: '#581c87' },
    { fill: '#f8fafc', border: '#cbd5e1', head: '#475569', text: '#1e293b' },
    { fill: '#fdf2f8', border: '#fbcfe8', head: '#db2777', text: '#831843' },
    { fill: '#ecfdf5', border: '#a7f3d0', head: '#059669', text: '#064e3b' },
    { fill: '#eff6ff', border: '#bfdbfe', head: '#2563eb', text: '#1e3a8a' },
  ];
  function colorFor(idx) { return CLASS_COLORS[idx % CLASS_COLORS.length]; }

  // ── 构建层级数据：企业 → 实体类 → 实例 ──
  // 顶层实体类 = 有"类节点"(local名==entity) 且其父 entity 不是类（过滤 _id 连接属性伪类）
  function buildHierarchy(nodes) {
    const local = u => String(u).split('#').pop().replace(/[<>]/g, '');
    const real = nodes.filter(n => !local(n.id).endsWith('_id'));
    const byLocal = new Map(real.map(n => [local(n.id), n]));
    const byEnt = {};
    for (const n of real) (byEnt[n.entity] = byEnt[n.entity] || []).push(n);
    const classes = Object.keys(byEnt)
      .filter(E => byLocal.has(E))                       // 有类节点（local 名 == entity）
      .filter(E => {
        const parent = byLocal.get(E).entity;
        return !(parent && byLocal.has(parent));          // 其父 entity 本身不是类（排除嵌套类）
      })
      .filter(E => (byEnt[E] || []).filter(n => local(n.id) !== E).length > 0);  // 有真实实例
    return classes.map((E, i) => {
      const insts = (byEnt[E] || []).filter(n => local(n.id) !== E);
      return {
        ent: E, zh: classZh(E), color: colorFor(i),
        instances: insts.map(n => ({ id: n.id, label: local(n.id).replace(E + '_', ''), full: local(n.id) })),
      };
    });
  }

  // ── 布局计算：1280×900 ──
  const W = 1280, H = 900;
  const PAD = 28;                 // 画布内边距
  const TITLE_H = 54;             // 标题区高
  const HUB_H = 82;               // 企业节点高
  const CARD_HEAD = 52;           // 实体类卡片头高
  const CHIP_H = 26, CHIP_GAP = 6;
  const INST_SHOW = 8;            // 每类最多展示实例数（超出折叠计数）
  const GAP = 18;

  let layout = $state(null);

  function computeLayout(hier) {
    const n = hier.length;
    if (!n) return null;
    // 列数：<=4 一排放满；5~6 三列；>6 四列
    const cols = n <= 4 ? n : (n <= 6 ? 3 : 4);
    const rows = Math.ceil(n / cols);
    const availW = W - PAD * 2;
    const cardW = (availW - (cols - 1) * GAP) / cols;
    // 卡片高度取决于本列最大实例展示数（最多 INST_SHOW）
    const maxInst = Math.min(INST_SHOW, Math.max(...hier.map(c => c.instances.length)));
    const cardH = CARD_HEAD + (maxInst > 0 ? maxInst * CHIP_H + (maxInst - 1) * CHIP_GAP + 16 : 40);
    // 垂直：标题 + 企业节点 + 卡片区
    const hubY = TITLE_H + 14;
    const cardTop = hubY + HUB_H + 28;
    // 企业节点宽度按标题长度
    const hubW = 360;
    const hubX = (W - hubW) / 2;
    const hubCX = W / 2;

    const cards = hier.map((c, i) => {
      const r = Math.floor(i / cols), col = i % cols;
      const x = PAD + col * (cardW + GAP);
      const y = cardTop + r * (cardH + GAP);
      return { ...c, x, y, w: cardW, h: cardH, cx: x + cardW / 2 };
    });
    return { cards, hubX, hubY, hubW, hubH: HUB_H, hubCX, hubCY: hubY + HUB_H / 2, cardTop, cardH };
  }

  // 计算完成后设置 layout
  let hier = $state([]);
  $effect(() => {
    if (graph?.nodes?.length) {
      hier = buildHierarchy(graph.nodes);
      layout = computeLayout(hier);
    } else {
      hier = []; layout = null;
    }
  });

  // 图统计
  const instCount = $derived((graph?.nodes || []).length);
  const edgeCount = $derived((graph?.edges || []).length);

  async function load() {
    loading = true; error = '';
    try {
      const res = await fetchGraph(kb);
      if (res.ok && Array.isArray(res.nodes)) {
        graph = { nodes: res.nodes, edges: res.edges || [] };
      } else {
        error = res.error || '模型结构加载失败';
      }
    } catch (e) {
      error = '网络错误';
    } finally {
      loading = false;
    }
  }

  onMount(() => { load(); });
  // refreshKey（重新建模）或 kb（切换知识库）变化时重新加载
  $effect(() => { if (refreshKey > 0 || kb) load(); });
</script>

<div class="model-graph">
  {#if loading}
    <div class="mg-empty">正在解析模型结构…</div>
  {:else if error}
    <div class="mg-empty mg-err">{error}</div>
  {:else if graph}
    <div class="mg-meta">
      <span class="mg-graph">节点：{graph.nodes.length}</span>
      <span class="mg-inst">实体类：{hier.length}</span>
      <span class="mg-dp">边：{edgeCount}</span>
      <span class="mg-cur">当前行业：<b>{industryTitle(kb)}</b></span>
    </div>

    <div class="mg-svg">
      <svg viewBox="0 0 {W} {H}" role="img" aria-label="本体层级示意图">
        <!-- 背景 -->
        <rect x="0" y="0" width="{W}" height="{H}" fill="#fbfcfe" rx="8" />

        {#if layout && hier.length}
          <!-- 标题区 -->
          <text x="{W/2}" y="34" text-anchor="middle" font-size="26" font-weight="700" fill="#1e293b">{industryTitle(kb)} · 本体结构</text>
          <text x="{W/2}" y="56" text-anchor="middle" font-size="12" fill="#94a3b8">企业 → 实体类 → 实例 层级示意图</text>

          <!-- 企业 hub 节点 -->
          <g>
            <rect x="{layout.hubX}" y="{layout.hubY}" width="{layout.hubW}" height="{layout.hubH}" rx="14"
              fill="#e0e7ff" stroke="#6366f1" stroke-width="2" />
            <text x="{layout.hubCX}" y="{layout.hubY + 32}" text-anchor="middle" font-size="18" font-weight="700" fill="#3730a3">{industryTitle(kb)}</text>
            <text x="{layout.hubCX}" y="{layout.hubY + 55}" text-anchor="middle" font-size="12" fill="#6366f1">🏢 企业</text>
          </g>

          <!-- 关系线：企业 → 实体类 -->
          {#each layout.cards as c}
            <line x1="{layout.hubCX}" y1="{layout.hubY + layout.hubH}" x2="{c.cx}" y2="{c.y}" stroke="#94a3b8" stroke-width="1.4" stroke-dasharray="5 4" opacity="0.7" />
          {/each}

          <!-- 实体类卡片 -->
          {#each layout.cards as c}
            <g class="class-card">
              <rect x="{c.x}" y="{c.y}" width="{c.w}" height="{c.h}" rx="12" fill="{c.color.fill}" stroke="{c.color.border}" stroke-width="1.4" />
              <!-- 卡片头 -->
              <rect x="{c.x}" y="{c.y}" width="{c.w}" height="{CARD_HEAD}" rx="12" fill="{c.color.head}" opacity="0.92" />
              <rect x="{c.x}" y="{c.y + CARD_HEAD - 12}" width="{c.w}" height="12" fill="{c.color.head}" />
              <text x="{c.x + 14}" y="{c.y + 25}" font-size="15" font-weight="700" fill="#ffffff">{c.zh}</text>
              <text x="{c.x + 14}" y="{c.y + 43}" font-size="10.5" fill="#ffffff" opacity="0.85">{c.ent}</text>
              <text x="{c.x + c.w - 14}" y="{c.y + 25}" text-anchor="end" font-size="11" font-weight="600" fill="#ffffff" opacity="0.9">{c.instances.length} 实例</text>
              <!-- 实例节点 -->
              {#each c.instances.slice(0, INST_SHOW) as inst, ii}
                <g transform="translate({c.x + 12}, {c.y + CARD_HEAD + 12 + ii * (CHIP_H + CHIP_GAP)})">
                  <rect width="{c.w - 24}" height="{CHIP_H}" rx="6" fill="#ffffff" stroke="{c.color.border}" stroke-width="1" />
                  <text x="8" y="17" font-size="11" fill="{c.color.text}" font-weight="500">{inst.label}</text>
                </g>
              {/each}
              {#if c.instances.length > INST_SHOW}
                <text x="{c.x + c.w/2}" y="{c.y + CARD_HEAD + 12 + INST_SHOW * (CHIP_H + CHIP_GAP)}" text-anchor="middle" font-size="11" fill="{c.color.text}" opacity="0.75">+ {c.instances.length - INST_SHOW} 更多…</text>
              {/if}
            </g>
          {/each}
        {:else}
          <text x="{W/2}" y="{H/2}" text-anchor="middle" font-size="15" fill="#94a3b8">暂无可展示的本体层级数据</text>
        {/if}
      </svg>
    </div>

    <div class="mg-hint">清晰层级：企业 → 实体类 → 实例 · 切换行业自动重建 · 浅色护眼配色</div>
  {/if}
</div>

<style>
  .model-graph { display: flex; flex-direction: column; gap: 10px; }
  .mg-empty { color: #94a3b8; font-size: 13px; text-align: center; padding: 40px; }
  .mg-err { color: #dc2626; }

  .mg-meta { display: flex; gap: 16px; flex-wrap: wrap; font-size: 12px; color: #475569; }
  .mg-inst, .mg-dp, .mg-graph { color: #2563eb; }
  .mg-cur b { color: #3730a3; font-weight: 700; }

  .mg-svg {
    width: 100%;
    border: 1px solid #e2e8f0; border-radius: 8px;
    background: #fbfcfe;
    overflow: hidden;
  }
  .mg-svg svg { display: block; width: 100%; height: auto; }

  .mg-hint { font-size: 11px; color: #94a3b8; }
</style>
