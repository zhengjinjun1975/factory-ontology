<script>
  // AssetPanel — 资产版本面板（资产版本链 / 当前激活版本 / 快照 / 回滚）
  // 调前端 server 转发的 /api/ontology/assets-list?kb= 获取某知识库的语义资产版本清单
  // 后端 data = { kb, versions:[{version, hash, created, assets:{lexicon,ontology,knowledge}}], active_version }
  // 支持：创建快照（POST assets-snapshot）+ 回滚到历史版本（POST assets-rollback）
  // 跟随当前激活 kb：与"本体中心、知识库配合、建哪个检索哪个"一致，不复用硬编码 food。
  import { onMount } from 'svelte';
  import { fetchKbs } from '../lib/api.js';

  let kb = $state('');           // 知识库名（初始跟随当前激活 kb，见 onMount）
  let versions = $state([]);     // 版本链 [{version, hash, created, assets}]
  let activeVersion = $state(''); // 当前激活版本
  let curKb = $state('');
  let loading = $state(false);
  let loaded = $state(false);
  let error = $state('');
  let empty = $state(false);     // 该 kb 无版本空态

  // 操作态
  let changelog = $state('');    // 快照说明（可选，仅前端留痕）
  let opBusy = $state(false);    // 快照/回滚进行中，禁用按钮防重
  let opMsg = $state('');        // 操作结果提示（成功/失败）

  // 资产名 → 中文展示（与后端 assets 字典键对齐）
  const ASSET_LABELS = {
    lexicon:   { cn: '词典',   icon: '📖' },
    ontology:  { cn: '本体',   icon: '🧬' },
    knowledge: { cn: '知识库', icon: '📚' },
  };
  // 固定展示顺序
  const ASSET_KEYS = ['lexicon', 'ontology', 'knowledge'];

  // 错误信息安全格式化：后端 error 可能是对象 {code,message}，避免渲染成 [object Object]
  const fmtError = (err) => {
    if (err === null || err === undefined) return '后端无响应';
    if (typeof err === 'string') return err;
    if (typeof err === 'object') {
      if (err.message) return String(err.message);
      try { return JSON.stringify(err); } catch (e) { return String(err); }
    }
    return String(err);
  };

  // 跟随当前激活 kb：读后端 getCurrentKb（/api/ontology/kbs → current），
  // 加载/快照/回滚全部使用该当前 kb，不复用硬编码 food。
  async function fetchCurrentKb() {
    try {
      const res = await fetchKbs();
      if (res && res.ok && res.current) return String(res.current);
    } catch (e) { /* 后端未就绪时忽略 */ }
    return '';
  }

  let followTimer = null;

  onMount(async () => {
    const cur = await fetchCurrentKb();
    if (cur) kb = cur;
    await load();
    // 实时跟随当前激活 kb：切换本体（头部下拉）后资产面板自动跟随刷新
    followTimer = setInterval(async () => {
      const cur = await fetchCurrentKb();
      if (cur && cur !== kb) { kb = cur; await load(); }
    }, 2000);
    return () => { if (followTimer) clearInterval(followTimer); };
  });

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

  // 创建快照：POST assets-snapshot → 刷新列表
  async function createSnapshot() {
    const kbName = kb.trim();
    if (!kbName) { error = '请输入知识库名称'; return; }
    if (!curKb && !loaded) return; // 未加载时不允许（需先加载确认 kb 有效）
    if (opBusy) return;
    opBusy = true; opMsg = ''; error = '';
    try {
      const res = await fetch('/api/ontology/assets-snapshot', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ kb: kbName, changelog: changelog.trim() }),
      });
      const json = await res.json().catch(() => null);
      if (!json || !json.ok) {
        opMsg = '❌ 快照失败：' + fmtError(json && json.error);
      } else {
        opMsg = '✅ 已创建快照 ' + (json.data && json.data.version ? json.data.version : '');
        changelog = ''; // 成功后清空说明
        await load();   // 刷新版本链（快照后端自动设为 active）
      }
    } catch (e) {
      opMsg = '❌ 网络错误，快照失败';
    } finally {
      opBusy = false;
    }
  }

  // 回滚到指定版本：确认后 POST assets-rollback → 刷新列表
  async function rollbackTo(v) {
    if (opBusy) return;
    if (!confirm(`确认回滚到版本「${v.version}」？\n系统将把词典/本体/知识库替换为该版本，并立即生效。`)) return;
    const kbName = kb.trim();
    if (!kbName) { error = '请输入知识库名称'; return; }
    opBusy = true; opMsg = ''; error = '';
    try {
      const res = await fetch('/api/ontology/assets-rollback', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ kb: kbName, version: v.version }),
      });
      const json = await res.json().catch(() => null);
      if (!json || !json.ok) {
        opMsg = '❌ 回滚失败：' + fmtError(json && json.error);
      } else {
        opMsg = '✅ 已回滚到 ' + (json.data && json.data.active_version ? json.data.active_version : v.version);
        await load();
      }
    } catch (e) {
      opMsg = '❌ 网络错误，回滚失败';
    } finally {
      opBusy = false;
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

  // 该版本某资产是否包含（assets 字典布尔）
  const hasAsset = (v, key) => {
    const a = v && v.assets;
    if (!a || typeof a !== 'object') return false;
    return a[key] === true;
  };
</script>

<div class="asset">
  <!-- 顶部：kb 选择 + 加载 -->
  <div class="asset-toolbar">
    <label class="kb-label" for="asset-kb">知识库</label>
    <input
      id="asset-kb"
      class="kb-input"
      type="text"
      placeholder="当前激活知识库（自动跟随本体切换）"
      bind:value={kb}
      onkeydown={(e) => { if (e.key === 'Enter') load(); }}
    />
    <button class="btn-load" onclick={load} disabled={loading || opBusy}>
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
    <!-- 快照操作区 -->
    <div class="asset-snapbar">
      <input
        class="snap-input"
        type="text"
        placeholder="快照说明（可选）"
        bind:value={changelog}
        onkeydown={(e) => { if (e.key === 'Enter') createSnapshot(); }}
      />
      <button class="btn-snap" onclick={createSnapshot} disabled={opBusy}>
        {opBusy ? '⏳ 处理中…' : '📸 创建快照'}
      </button>
    </div>
    {#if opMsg}
      <div class="asset-opmsg">{opMsg}</div>
    {/if}

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
            <th>资产状态</th>
            <th class="col-op"></th>
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
              <td class="asset-status">
                {#each ASSET_KEYS as key}
                  <span
                    class="status-chip"
                    class:chip-on={hasAsset(v, key)}
                    class:chip-off={!hasAsset(v, key)}
                    title={hasAsset(v, key) ? `包含${ASSET_LABELS[key].cn}` : `不含${ASSET_LABELS[key].cn}`}
                  >{ASSET_LABELS[key].icon} {ASSET_LABELS[key].cn}{hasAsset(v, key) ? '' : ' ✕'}</span>
                {/each}
              </td>
              <td class="col-op">
                {#if !isActive(v)}
                  <button class="btn-rollback" onclick={() => rollbackTo(v)} disabled={opBusy}>
                    回滚
                  </button>
                {/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>

    <div class="asset-hint">● 当前激活版本为系统正在使用的语义资产；可对任意历史版本点「回滚」切换。</div>
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

  /* 快照操作区 */
  .asset-snapbar { display: flex; gap: 8px; align-items: center; }
  .snap-input {
    flex: 1; max-width: 320px;
    padding: 6px 10px; font-size: 13px; color: #1e293b;
    border: 1px solid #cbd5e1; border-radius: 4px; background: #fff;
    transition: border-color 0.15s;
  }
  .snap-input:focus { outline: none; border-color: #10b981; }
  .btn-snap {
    background: #059669; color: #fff; border: none; border-radius: 4px;
    padding: 6px 16px; font-size: 13px; cursor: pointer; transition: background 0.15s;
    white-space: nowrap;
  }
  .btn-snap:hover { background: #047857; }
  .btn-snap:disabled { background: #6ee7b7; cursor: not-allowed; }
  .asset-opmsg {
    font-size: 12px; color: #065f46; background: #ecfdf5;
    border: 1px solid #a7f3d0; border-radius: 4px; padding: 6px 10px;
  }

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
  .col-op { width: 56px; text-align: center; }

  /* 当前激活版本醒目行 */
  .row-active { background: #ecfeff; }
  .row-active td { border-bottom-color: #a5f3fc; }
  .active-badge {
    display: inline-block;
    font-size: 11px; font-weight: 700; color: #0e7490;
    background: #cffafe; border: 1px solid #67e8f9;
    border-radius: 10px; padding: 1px 8px; white-space: nowrap;
  }

  /* 资产状态可视化（词典/本体/知识库是否含） */
  .asset-status { display: flex; gap: 4px; flex-wrap: wrap; }
  .status-chip {
    font-size: 11px; border-radius: 10px; padding: 1px 7px; white-space: nowrap;
  }
  .chip-on { color: #065f46; background: #d1fae5; border: 1px solid #6ee7b7; }
  .chip-off { color: #94a3b8; background: #f1f5f9; border: 1px solid #e2e8f0; }

  /* 回滚按钮 */
  .btn-rollback {
    background: #fff; color: #d97706; border: 1px solid #fcd34d;
    border-radius: 4px; padding: 3px 10px; font-size: 12px; cursor: pointer;
    transition: all 0.15s; white-space: nowrap;
  }
  .btn-rollback:hover { background: #fffbeb; border-color: #f59e0b; color: #b45309; }
  .btn-rollback:disabled { color: #d6d3d1; border-color: #e7e5e4; cursor: not-allowed; background: #fafaf9; }

  .asset-hint { font-size: 11px; color: #94a3b8; }

  @media (max-width: 600px) {
    .asset-toolbar { flex-wrap: wrap; }
    .kb-input { max-width: 100%; flex-basis: 100%; }
    .asset-snapbar { flex-wrap: wrap; }
    .snap-input { max-width: 100%; flex-basis: 100%; }
  }
</style>
