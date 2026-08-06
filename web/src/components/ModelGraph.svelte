<script>
  // ModelGraph — 本体模型结构 SVG 图（真实反映建模结果）
  // 中央主类 + 左侧数据属性 + 右侧对象属性/目标类
  import { onMount } from 'svelte';
  import { fetchSchema } from '../lib/api.js';

  let { refreshKey = 0 } = $props();   // 建模时间戳，变化时重新加载

  let schema = $state(null);
  let loading = $state(true);
  let error = $state('');

  // SVG 布局常量
  const W = 720, H = 360;
  const CX = 360, CY = 180;      // 主类中心
  const MAIN_RX = 90, MAIN_RY = 36;

  async function load() {
    loading = true; error = '';
    try {
      const res = await fetchSchema();
      if (res.ok && res.schema) {
        schema = res.schema;
      } else {
        error = res.error || '模型结构加载失败';
      }
    } catch (e) {
      error = '网络错误';
    } finally {
      loading = false;
    }
  }

  onMount(load);
  // refreshKey 变化时（重新建模）重新加载
  $effect(() => {
    if (refreshKey > 0) load();
  });

  // 目标类位置（右侧环形分布）
  const targetPos = $derived(
    (schema?.target_classes || []).map((t, i) => {
      const n = Math.max(schema.target_classes.length, 1);
      const angle = -Math.PI/2 + (i + 0.5) * (Math.PI / n);
      const rx = 300, ry = 110;
      return { name: t, x: CX + rx * Math.cos(angle), y: CY + ry * Math.sin(angle) };
    })
  );

  // 数据属性位置（左侧列表）
  const dataPos = $derived(
    (schema?.data_properties || []).map((d, i) => ({
      ...d,
      x: 150, y: 70 + i * 34,
    }))
  );

  // 类型层级：按父类分组
  const hierarchyGroups = $derived(
    (() => {
      const groups = {};
      for (const h of (schema?.type_hierarchy || [])) {
        if (!groups[h.parent]) groups[h.parent] = [];
        groups[h.parent].push(h.child);
      }
      return Object.entries(groups).map(([parent, children]) => ({ parent, children }));
    })()
  );

  // 找到对象属性对应的目标位置
  function objTargetPos(rel) {
    const target = schema?.object_properties.find(o => o.rel === rel)?.target;
    return targetPos.find(p => p.name === target);
  }
</script>

<div class="model-graph">
  {#if loading}
    <div class="mg-empty">正在解析模型结构…</div>
  {:else if error}
    <div class="mg-empty mg-err">{error}</div>
  {:else if schema}
    <div class="mg-meta">
      <span class="mg-cls">类：{schema.class}</span>
      <span class="mg-inst">{schema.instance_count} 个实例</span>
      <span class="mg-obj">{schema.object_properties.length} 个对象关系</span>
      <span class="mg-dp">{schema.data_properties.length} 个数据属性</span>
    </div>

    <svg viewBox="0 0 {W} {H}" class="mg-svg" xmlns="http://www.w3.org/2000/svg" font-family="'Microsoft YaHei','PingFang SC',sans-serif">
      <!-- 数据属性 → 主类 连线 -->
      {#each dataPos as d}
        <line x1={d.x + 55} y1={d.y} x2={CX - MAIN_RX} y2={CY} stroke="#94a3b8" stroke-width="1" stroke-dasharray="4,3"/>
      {/each}

      <!-- 对象属性连线（主类 → 目标类） -->
      {#each schema.object_properties as op}
        {@const tp = objTargetPos(op.rel)}
        {#if tp}
          <line x1={CX + MAIN_RX} y1={CY} x2={tp.x - 60} y2={tp.y} stroke="#3b82f6" stroke-width="1.5"/>
          <text x={(CX + MAIN_RX + tp.x - 60)/2} y={(CY + tp.y)/2 - 4} text-anchor="middle" font-size="11" fill="#2563eb">{op.label}</text>
        {/if}
      {/each}

      <!-- 主类节点 -->
      <rect x={CX - MAIN_RX} y={CY - MAIN_RY} width={MAIN_RX*2} height={MAIN_RY*2} rx="8"
            fill="#2563eb" stroke="#1d4ed8" stroke-width="2"/>
      <text x={CX} y={CY - 4} text-anchor="middle" font-size="16" font-weight="700" fill="#fff">{schema.class}</text>
      <text x={CX} y={CY + 16} text-anchor="middle" font-size="11" fill="#dbeafe">{schema.instance_count} 实例</text>

      <!-- 数据属性节点 -->
      {#each dataPos as d}
        <rect x={d.x} y={d.y - 12} width="110" height="24" rx="4"
              fill="#f1f5f9" stroke="#cbd5e1" stroke-width="1"/>
        <text x={d.x + 55} y={d.y + 4} text-anchor="middle" font-size="11" fill="#334155">{d.label}</text>
      {/each}

      <!-- 目标类节点 -->
      {#each targetPos as t}
        <rect x={t.x - 60} y={t.y - 14} width="120" height="28" rx="5"
              fill="#eff6ff" stroke="#93c5fd" stroke-width="1.5"/>
        <text x={t.x} y={t.y + 4} text-anchor="middle" font-size="12" fill="#1d4ed8" font-weight="600">{t.name}</text>
      {/each}
    </svg>

    <!-- 类型层级（subClassOf） -->
    {#if hierarchyGroups.length > 0}
      <div class="mg-hierarchy">
        <div class="mg-hier-title">设备类型层级</div>
        <div class="mg-hier-groups">
          {#each hierarchyGroups as g}
            <div class="mg-hier-group">
              <span class="mg-hier-parent">{g.parent}</span>
              <div class="mg-hier-children">
                {#each g.children as c}
                  <span class="mg-hier-child">{c}</span>
                {/each}
              </div>
            </div>
          {/each}
        </div>
      </div>
    {/if}

    <!-- 实体属性（属性本体：各关联类的自身属性） -->
    {#if schema?.class_attributes && Object.keys(schema.class_attributes).length > 0}
      <div class="mg-attrs">
        <div class="mg-hier-title">实体属性</div>
        <div class="mg-attr-groups">
          {#each Object.entries(schema.class_attributes) as [cls, attrs]}
            {#if cls !== 'DeviceType'}
              <div class="mg-attr-group">
                <span class="mg-attr-cls">{cls}</span>
                <div class="mg-attr-items">
                  {#each attrs as a}
                    <span class="mg-attr-item">{a}</span>
                  {/each}
                </div>
              </div>
            {/if}
          {/each}
        </div>
      </div>
    {/if}

    <div class="mg-legend">
      <span class="lg-item"><span class="lg-dot lg-main"></span>主类</span>
      <span class="lg-item"><span class="lg-dot lg-target"></span>目标类（对象关系指向）</span>
      <span class="lg-item"><span class="lg-line"></span>对象关系</span>
    </div>
  {/if}
</div>

<style>
  .model-graph { display: flex; flex-direction: column; gap: 10px; }
  .mg-empty { color: #94a3b8; font-size: 13px; text-align: center; padding: 40px; }
  .mg-err { color: #dc2626; }

  .mg-meta {
    display: flex; gap: 16px; flex-wrap: wrap;
    font-size: 12px; color: #475569;
  }
  .mg-cls { font-weight: 700; color: #1e293b; }
  .mg-inst, .mg-obj, .mg-dp { color: #2563eb; }

  .mg-svg {
    width: 100%; max-width: 720px;
    border: 1px solid #e2e8f0; border-radius: 4px;
    background: #fbfcfe;
  }

  .mg-legend { display: flex; gap: 16px; flex-wrap: wrap; font-size: 11px; color: #64748b; align-items: center; }
  .lg-item { display: flex; align-items: center; gap: 6px; }
  .lg-dot { width: 10px; height: 10px; border-radius: 3px; }
  .lg-main { background: #2563eb; }
  .lg-target { background: #eff6ff; border: 1px solid #93c5fd; }
  .lg-line { width: 20px; height: 0; border-top: 2px solid #3b82f6; }

  /* 类型层级 */
  .mg-hierarchy {
    border: 1px solid #e2e8f0; border-radius: 4px; padding: 12px; background: #f8fafc;
  }
  .mg-hier-title { font-size: 12px; font-weight: 700; color: #1e293b; margin-bottom: 10px; letter-spacing: 0.3px; }
  .mg-hier-groups { display: flex; flex-direction: column; gap: 10px; }
  .mg-hier-group {
    display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
    padding: 8px 10px; background: #fff; border: 1px solid #e2e8f0; border-radius: 4px;
  }
  .mg-hier-parent {
    font-size: 13px; font-weight: 700; color: #2563eb;
    background: #eff6ff; border: 1px solid #93c5fd; border-radius: 4px;
    padding: 3px 10px; white-space: nowrap;
  }
  .mg-hier-children { display: flex; gap: 6px; flex-wrap: wrap; }
  .mg-hier-child {
    font-size: 11px; color: #475569;
    background: #f1f5f9; border: 1px solid #e2e8f0; border-radius: 3px;
    padding: 2px 8px;
  }

  /* 实体属性 */
  .mg-attrs { border: 1px solid #e2e8f0; border-radius: 4px; padding: 12px; background: #f8fafc; }
  .mg-attr-groups { display: flex; flex-direction: column; gap: 8px; }
  .mg-attr-group {
    display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
    padding: 7px 10px; background: #fff; border: 1px solid #e2e8f0; border-radius: 4px;
  }
  .mg-attr-cls {
    font-size: 12px; font-weight: 700; color: #0ea5e9;
    background: #f0f9ff; border: 1px solid #bae6fd; border-radius: 4px;
    padding: 2px 9px; white-space: nowrap;
  }
  .mg-attr-items { display: flex; gap: 5px; flex-wrap: wrap; }
  .mg-attr-item {
    font-size: 10px; color: #64748b;
    background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 3px;
    padding: 1px 7px;
  }
</style>
