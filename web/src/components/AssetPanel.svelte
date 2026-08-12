<script>
  // AssetPanel — 资产版本面板（资产版本链 / 当前激活版本醒目标记）
  // 调前端 server 转发的 /api/ontology/assets-list?kb= 获取某知识库的语义资产版本清单
  // 后端 data = { kb, versions:[{version, hash, created, assets}], active_version }

  let kb = $state('food');       // 知识库名
  let versions = $state([]);     // 版本链 [{version, hash, created, assets}]
  let activeVersion = $state(''); // 当前激活版本
  let curKb = $state('');
  let loading = $state(false);
  let loaded = $state(false);
  let error = $state('');
  let empty = $state(false);     // 该 kb 无版本空态

  async function load() {
    const kbName = kb.trim();
    if (!kbName) { error = '请输入知识库名称'; return; }
    loading = true; error = ''; empty = false; loaded = false;
    try {
      const res = await fetch('/api/ontology/assets-list?kb=' + encodeURIComponent(kbName));
      const json = await res.json().catch(() => null);
      if (!json || !json.ok || !json.data) {
        error = (json && json.error) || '资产版本加载失败';
      } else {
        const d = json.data;
        curKb = d.kb || kbName;
        versions = Array.isArray(d.versions) ? d.versions : [];
        activeVersion = d.active_version || '';
        empty = versions.length === 0;
        loaded = true;
      }
    } catch (e) {
      error = '网络错误，请确认服务已启动';
    } finally {
      loading = false;
    }
  }

  // 每行是否当前激活
  const isActive = (v) => v.version === activeVersion;

  // 时间格式化（后端 created 多为 ISO/字符串）
  function fmtCreated(c) {
    if (!c) return '—';
    const d = new Date(c);
    if (isNaN(d.getTime())) return String(c);
    const p = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  // hash 缩写（前 8 位）
  const shortHash = (h) => (h && String(h).length > 8) ? String(h).slice(0, 8) + '…' : (h || '—');
  const fullHash = (h) => h || '—';
</script>

<div class="asset">
  <!-- 顶部：kb 选择 + 加载 -->
  <div class="asset-toolbar">
    <label class="kb-label" for="asset-kb">知识库</label>
    <input
      id="asset-kb"
      class="kb-input"
      type="text"
      placeholder="输入知识库名，如 food"
      bind:value={kb}
      onkeydown={(e) => { if (e.key === 'Enter') load(); }}
    />
    <button class="btn-load" onclick={load} disabled={loading}>
      {loading ? '⏳ 加载中…' : '加载'}
    </button>
  </div>

  {#if loading}
    <div class="asset-empty">正在加载资产版本…</div>
  {:else if error}
    <div class="asset-empty asset-err">
      {error}
      <button class="asset-retry" onclick={load}>重试</button>
    </div>
  {:else if empty}
    <div class="asset-empty asset-nodata">知识库「{curKb}」暂无资产版本，请先在后端生成语义资产</div>
  {:else if loaded}
    <div class="asset-meta">
      <span class="meta-kb">知识库：<b>{curKb}</b></span>
      <span class="meta-count">{versions.length} 个版本</span>
      {#if activeVersion}
        <span class="meta-active">当前激活：<b>{activeVersion}</b></span>
      {/if}
    </div>

    <!-- 版本链表格 -->
    <div class="asset-card">
      <div class="asset-card-title">版本链</div>
      <table class="asset-table">
        <thead>
          <tr>
            <th class="col-active"></th>
            <th>版本</th>
            <th>哈希</th>
            <th>创建时间</th>
            <th>资产数</th>
          </tr>
        </thead>
        <tbody>
          {#each versions as v}
            <tr class:row-active={isActive(v)}>
              <td class="col-active">
                {#if isActive(v)}
                  <span class="active-badge">● 当前</span>
                {/if}
              </td>
              <td class="mono version-name">{v.version}</td>
              <td class="mono hash-cell" title={fullHash(v.hash)}>{shortHash(v.hash)}</td>
              <td class="mono">{fmtCreated(v.created)}</td>
              <td class="mono">{v.assets ? Object.keys(v.assets).length : '—'}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>

    <div class="asset-hint">● 当前激活版本为系统正在使用的语义资产；其余版本可回滚切换（见资产回滚接口）。</div>
  {/if}
</div>

<style>
  .asset { display: flex; flex-direction: column; gap: 12px; }

  /* 顶部工具条 */
  .asset-toolbar { display: flex; align-items: center; gap: 8px; }
  .kb-label { font-size: 12px; color: #334155; font-weight: 600; white-space: nowrap; }
  .kb-input {
    flex: 1; max-width: 260px;
    padding: 6px 10px; font-size: 13px; color: #1e293b;
    border: 1px solid #cbd5e1; border-radius: 4px; background: #fff;
    transition: border-color 0.15s;
  }
  .kb-input:focus { outline: none; border-color: #3b82f6; }
  .btn-load {
    background: #2563eb; color: #fff; border: none; border-radius: 4px;
    padding: 6px 16px; font-size: 13px; cursor: pointer; transition: background 0.15s;
  }
  .btn-load:hover { background: #1d4ed8; }
  .btn-load:disabled { background: #93c5fd; cursor: not-allowed; }

  /* 空态 / 错误 */
  .asset-empty { color: #94a3b8; font-size: 13px; text-align: center; padding: 30px; }
  .asset-nodata { color: #64748b; }
  .asset-err { color: #dc2626; }
  .asset-retry {
    margin-left: 10px; background: #fff; color: #2563eb;
    border: 1px solid #cbd5e1; border-radius: 4px;
    padding: 4px 12px; font-size: 12px; cursor: pointer; transition: all 0.15s;
  }
  .asset-retry:hover { border-color: #3b82f6; background: #f8fafc; }

  /* 元信息 */
  .asset-meta { display: flex; gap: 16px; flex-wrap: wrap; font-size: 12px; color: #475569; }
  .meta-kb b { color: #1e293b; }
  .meta-count { color: #2563eb; }
  .meta-active b { color: #0ea5e9; }

  /* 版本链表格 */
  .asset-card {
    background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 4px; padding: 14px;
  }
  .asset-card-title { font-size: 12px; font-weight: 700; color: #1e293b; margin-bottom: 12px; letter-spacing: 0.3px; }
  .asset-table { width: 100%; border-collapse: collapse; }
  .asset-table th, .asset-table td {
    padding: 7px 8px; font-size: 12px; text-align: left;
    border-bottom: 1px solid #e2e8f0;
  }
  .asset-table th { color: #64748b; font-weight: 600; background: #f1f5f9; }
  .asset-table td { color: #334155; }
  .mono { font-family: 'Consolas', monospace; }
  .version-name { font-weight: 600; color: #1e293b; }
  .hash-cell { color: #64748b; }
  .col-active { width: 64px; }

  /* 当前激活版本醒目行 */
  .row-active { background: #ecfeff; }
  .row-active td { border-bottom-color: #a5f3fc; }
  .active-badge {
    display: inline-block;
    font-size: 11px; font-weight: 700; color: #0e7490;
    background: #cffafe; border: 1px solid #67e8f9;
    border-radius: 10px; padding: 1px 8px; white-space: nowrap;
  }

  .asset-hint { font-size: 11px; color: #94a3b8; }

  @media (max-width: 600px) {
    .asset-toolbar { flex-wrap: wrap; }
    .kb-input { max-width: 100%; flex-basis: 100%; }
  }
</style>
