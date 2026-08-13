<script>
  // AssetPanel — 资产版本面板（资产版本链 / 当前激活版本 / 快照 / 回滚）
  // 调前端 server 转发的 /api/ontology/assets-list?kb= 获取某知识库的语义资产版本清单
  // 后端 data = { kb, versions:[{version, hash, created, assets:{lexicon,ontology,knowledge}}], active_version }
  // 支持：创建快照（POST assets-snapshot）+ 回滚到历史版本（POST assets-rollback）
  // 单企业收敛：kb 由父级（App.svelte）注入 currentKb（当前登录企业唯一 kb），面板内无 kb 切换/轮询，
  // 加载/快照/回滚全部使用该当前企业 kb；kb prop 变化时 $effect 自动重新加载（面板跟随当前企业）。

  let { kb = '' } = $props();    // 当前企业唯一 kb（只读 prop，跟随登录企业）
  import { getToken } from '../lib/api.js';
  const authedFetch = (url, opts = {}) => {
    const h = { ...(opts.headers || {}) };
    const t = getToken();
    if (t) h['Authorization'] = 'Bearer ' + t;
    return fetch(url, { ...opts, headers: h });
  };
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

  // 跟随当前激活 kb 的注释与轮询已移除：单企业收敛，kb 由父级注入。
  // kb prop 变化（切换/重置后当前企业 kb 更新）→ 自动重新加载该企业资产版本
  $effect(() => {
    const name = kb;
    if (name) load();
  });

  async function load() {
    const kbName = kb.trim();
    if (!kbName) { error = '当前企业知识库为空，请先建模'; return; }
    loading = true; error = ''; empty = false; loaded = false;
    try {
      const res = await authedFetch('/api/ontology/assets-list?kb=' + encodeURIComponent(kbName));
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
    if (!kbName) { error = '当前企业知识库为空，请先建模'; return; }
    if (!curKb && !loaded) return; // 未加载时不允许（需先加载确认 kb 有效）
    if (opBusy) return;
    opBusy = true; opMsg = ''; error = '';
    try {
      const res = await authedFetch('/api/ontology/assets-snapshot', {
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
    if (!kbName) { error = '当前企业知识库为空，请先建模'; return; }
    opBusy = true; opMsg = ''; error = '';
    try {
      const res = await authedFetch('/api/ontology/assets-rollback', {
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
  <!-- 当前企业知识库标签 -->
  <div class="asset-toolbar">
    <span class="kb-label">当前企业知识库</span>
    <span class="kb-tag" title="跟随当前登录企业，不可切换">{kb || '—'}</span>
  </div>

  {#if loading}
    <div class="asset-empty">正在加载资产版本…</div>
  {:else if error}
    <div class="asset-empty asset-err">
      {error}
      <button class="asset-retry" onclick={load}>重试</button>
    </div>
  {:else}
    <!-- 快照操作区：始终显示(空态也能创建首个快照)，不藏在 loaded 分支里 -->
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

    {#if empty}
      <div class="asset-empty asset-nodata">
        <span class="empty-icon">🧬</span>
        <span class="empty-text">知识库「{curKb}」暂无资产版本，点击上方「📸 创建快照」生成首个版本（语义资产存档，用于版本回滚与交付）。</span>
      </div>
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
  {/if}
</div>

<style>
  .asset { display: flex; flex-direction: column; gap: 12px; }

  /* 顶部工具条 */
  .asset-toolbar { display: flex; align-items: center; gap: 8px; }
  .kb-label { font-size: 12px; color: #334155; font-weight: 600; white-space: nowrap; }
  .kb-tag {
    display: inline-flex; align-items: center;
    padding: 4px 12px; font-size: 12px; font-weight: 600; color: var(--brand);
    background: var(--brand-soft); border: 1px solid var(--brand-line); border-radius: 12px;
  }
  .kb-input {
    flex: 1; max-width: 260px;
    padding: 6px 10px; font-size: 13px; color: #1e293b;
    border: 1px solid #cbd5e1; border-radius: 4px; background: #fff;
    transition: border-color 0.15s;
  }
  .kb-input:focus { outline: none; border-color: var(--brand); }
  .btn-load {
    background: var(--brand); color: #fff; border: none; border-radius: 4px;
    padding: 6px 16px; font-size: 13px; cursor: pointer; transition: background 0.15s;
  }
  .btn-load:hover { background: var(--brand-dark); }
  .btn-load:disabled { background: var(--brand-line); cursor: not-allowed; }

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
    font-size: 12px; color: var(--success-fg); background: var(--success-bg);
    border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 6px 10px;
  }

  /* 空态 / 错误 */
  .asset-empty {
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    gap: 10px; padding: 48px 20px; text-align: center; color: var(--text-muted); font-size: 13px;
  }
  .asset-empty .empty-icon { font-size: 28px; line-height: 1; }
  .asset-empty .empty-text { color: var(--text-muted); max-width: 420px; line-height: 1.6; }
  .asset-nodata { color: var(--text-muted); }
  .asset-err { color: var(--danger); }
  .asset-retry {
    margin-left: 10px; background: var(--bg-card); color: var(--brand);
    border: 1px solid var(--border); border-radius: var(--radius-sm);
    padding: 4px 12px; font-size: 12px; cursor: pointer;
    transition: background 150ms ease-out;
  }
  .asset-retry:hover { border-color: var(--brand-line); background: var(--bg-hover); }

  /* 元信息 */
  .asset-meta { display: flex; gap: 16px; flex-wrap: wrap; font-size: 12px; color: var(--text-secondary); }
  .meta-kb b { color: var(--text-primary); }
  .meta-count { color: var(--brand); }
  .meta-active b { color: var(--brand); }

  /* 版本链表格 */
  .asset-card {
    background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius-md);
    padding: 14px; box-shadow: var(--shadow-card);
  }
  .asset-card-title { font-size: 12px; font-weight: 700; color: var(--text-primary); margin-bottom: 12px; letter-spacing: 0.3px; }
  .asset-table { width: 100%; border-collapse: collapse; }
  .asset-table thead th {
    position: sticky; top: 0; z-index: 1;
    padding: 8px 10px; font-size: 12px; font-weight: 600; text-align: left;
    color: var(--text-secondary); background: var(--bg-elevated);
    border-bottom: 1px solid var(--border);
  }
  .asset-table tbody td {
    padding: 0 10px; font-size: 13px; text-align: left; color: var(--text-primary);
    border-bottom: 1px solid var(--border);
    height: 40px; transition: background 150ms ease-out;
  }
  .asset-table tbody tr:last-child td { border-bottom: none; }
  .asset-table tbody tr:hover td { background: var(--bg-hover); }
  .mono { font-family: ui-monospace, 'SF Mono', SFMono-Regular, Menlo, Consolas, monospace; font-size: 13px; font-variant-numeric: tabular-nums; }
  .version-name { font-weight: 600; color: var(--text-primary); }
  .hash-cell { color: var(--text-secondary); }
  .col-active { width: 64px; }
  .col-op { width: 56px; text-align: center; }

  /* 当前激活版本醒目行 */
  .row-active { background: var(--bg-selected); }
  .row-active:hover td { background: var(--bg-selected); }
  .row-active td { border-bottom-color: var(--brand-line); }
  .active-badge {
    display: inline-block;
    font-size: 11px; font-weight: 700; color: var(--success-fg);
    background: var(--success-bg); border: 1px solid var(--brand-line);
    border-radius: var(--radius-sm); padding: 2px 8px; white-space: nowrap;
  }

  /* 资产状态可视化（词典/本体/知识库是否含） */
  .asset-status { display: flex; gap: 4px; flex-wrap: wrap; }
  .status-chip {
    font-size: 11px; border-radius: var(--radius-sm); padding: 2px 8px; white-space: nowrap;
  }
  .chip-on { color: var(--success-fg); background: var(--success-bg); border: 1px solid var(--border); }
  .chip-off { color: var(--text-muted); background: var(--bg-elevated); border: 1px solid var(--border); }

  /* 回滚按钮 */
  .btn-rollback {
    background: var(--bg-card); color: var(--warning); border: 1px solid var(--border);
    border-radius: var(--radius-sm); padding: 3px 10px; font-size: 12px; cursor: pointer;
    transition: background 150ms ease-out; white-space: nowrap;
  }
  .btn-rollback:hover { background: var(--warning-bg); border-color: var(--warning); color: var(--warning-fg); }
  .btn-rollback:disabled { color: var(--text-muted); border-color: var(--border); cursor: not-allowed; background: var(--bg-elevated); }

  .asset-hint { font-size: 11px; color: var(--text-muted); }

  @media (max-width: 600px) {
    .asset-toolbar { flex-wrap: wrap; }
    .kb-input { max-width: 100%; flex-basis: 100%; }
    .asset-snapbar { flex-wrap: wrap; }
    .snap-input { max-width: 100%; flex-basis: 100%; }
  }
</style>
