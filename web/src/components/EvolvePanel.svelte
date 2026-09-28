<script>
  let { kb = '' } = $props();
  import { onMount } from 'svelte';

    import { evolvePending, evolveConfirm, evolveReject, evolveRollback, evolveHistory, evolveTrigger, getToken } from '../lib/api.js';

  let version = $state(0);
  let pending = $state([]);
  let snapshots = $state([]);
  let rejected = $state(0);
  let err = $state('');
  let msg = $state('');
  let busy = $state('');
  let openName = $state('');
  let showHistory = $state(false);
  let classList = $state([]);
  let clsPick = $state({});

  const hasSnapshots = $derived(snapshots.length > 0);

  async function refresh() {
    err = '';
    try {
      const p = await evolvePending();
      if (p && p.ok === false) throw new Error(p.message || 'pending 失败');
      version = p.version ?? version;
      pending = Array.isArray(p.pending) ? p.pending : [];
      const h = await evolveHistory();
      if (h && h.ok === false) throw new Error(h.message || 'history 失败');
      version = h.version ?? version;
      snapshots = Array.isArray(h.snapshots) ? h.snapshots : [];
      rejected = h.rejected ?? 0;
      await loadClasses();
    } catch (e) {
      err = String(e && e.message ? e.message : e);
    }
  }

  async function act(kind, path, body) {
    if (busy) return;
    busy = kind;
    msg = '';
    err = '';
    try {
      const r = path.includes('/confirm') ? await evolveConfirm(body)
              : path.includes('/reject') ? await evolveReject(body)
              : path.includes('/rollback') ? await evolveRollback(body)
              : path.includes('/trigger') ? await evolveTrigger()
              : await evolvePending();
      if (r && r.ok === false) throw new Error(r.message || '操作失败');
      msg = r.message || '操作成功';
      if (typeof r.version === 'number') version = r.version;
      await refresh();
    } catch (e) {
      err = String(e && e.message ? e.message : e);
    } finally {
      busy = '';
    }
  }

  function confirm(name) {
    const cls = clsPick[name] || 'Evolved';
    act('confirm:' + name, '/api/ontology/evolve/confirm', { name, cls });
  }
  function reject(name) { act('reject:' + name, '/api/ontology/evolve/reject', { name }); }
  function trigger() { act('trigger', '/api/ontology/evolve/trigger', {}); }
  function rollback() {
    const target = snapshots.length >= 2 ? snapshots[snapshots.length - 2] : undefined;
    act('rollback', '/api/ontology/evolve/rollback', target != null ? { target_version: target } : {});
  }
  function toggle(name) { openName = openName === name ? '' : name; }

  async function loadClasses() {
    try {
      const _t = getToken();
      const _h = _t ? { Authorization: 'Bearer ' + _t } : {};
      const r = await fetch('/api/ontology/graph?kb=' + encodeURIComponent(kb), { cache: 'no-store', headers: _h });
      const d = await r.json();
      // 后端从 .nt 里取的真类（含中文 label），不再从实例名硬切
      classList = Array.isArray(d && d.classes) ? d.classes : [];
    } catch (e) {
      classList = [];
    }
  }

  onMount(() => { refresh(); });
</script>

<div class="panel">
  <div class="head">
    <span class="title">本体自演进</span>
    <span class="ver">v{version}</span>
    <span class="kb">{kb}</span>
    <button class="btn" disabled={!!busy} onclick={refresh}>刷新</button>
    <button class="btn primary" disabled={!!busy} onclick={trigger}>重新提取候选</button>
  </div>

  {#if err}<div class="err">{err}</div>{/if}
  {#if msg}<div class="msg">{msg}</div>{/if}

  <div class="list">
    {#if pending.length === 0}
      <div class="empty">暂无待确认候选</div>
    {:else}
      {#each pending as c (c.name)}
        <div class="row">
          <div class="line" role="button" tabindex="0" onclick={() => toggle(c.name)} onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(c.name); } }}>
            <span class="name">{c.name}</span>
            <span class="count">出现 {c.count} 次</span>
            <span class="caret">{openName === c.name ? '收起' : '展开'}</span>
            <select class="cls" value={clsPick[c.name] || 'Evolved'} disabled={!!busy}
              onclick={(e) => e.stopPropagation()}
              onchange={(e) => { clsPick = { ...clsPick, [c.name]: e.currentTarget.value }; }}>
              <option value="Evolved">其他/未定</option>
              {#each classList as g}<option value={g.cls} title={g.cls}>{g.label || g.cls}</option>{/each}
            </select>
            <button class="btn ok" disabled={!!busy} onclick={(e) => { e.stopPropagation(); confirm(c.name); }}>确认</button>
            <button class="btn no" disabled={!!busy} onclick={(e) => { e.stopPropagation(); reject(c.name); }}>拒绝</button>
          </div>
          {#if openName === c.name}
            <div class="sample">
              {#if c.sample}<pre>{typeof c.sample === 'string' ? c.sample : JSON.stringify(c.sample, null, 2)}</pre>{:else}<span class="muted">无例句</span>{/if}
            </div>
          {/if}
        </div>
      {/each}
    {/if}
  </div>

  <div class="foot">
    <button class="btn link" onclick={() => (showHistory = !showHistory)}>
      历史与回退 {showHistory ? '收起' : '展开'}
    </button>
    {#if showHistory}
      <div class="hist">
        <div class="hrow"><span class="k">当前版本</span><span class="v">v{version}</span></div>
        <div class="hrow"><span class="k">快照</span><span class="v">{snapshots.length ? snapshots.join(', ') : '无'}</span></div>
        <div class="hrow"><span class="k">待确认</span><span class="v">{pending.length}</span></div>
        <div class="hrow"><span class="k">已拒绝</span><span class="v">{rejected}</span></div>
        {#if hasSnapshots}
          <button class="btn warn" disabled={!!busy} onclick={rollback}>回退到上一版</button>
        {/if}
      </div>
    {/if}
  </div>
</div>

<style>
  .panel {
    background: #f7faf9;
    color: #1f2d2a;
    border: 1px solid #d9e6e2;
    border-radius: 8px;
    padding: 12px 14px;
    font-size: 14px;
    line-height: 1.5;
  }
  .head {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
    padding-bottom: 8px;
    border-bottom: 1px solid #e2ece9;
  }
  .title { font-weight: 600; color: #2f9e8f; }
  .ver { color: #7c5cff; font-weight: 600; }
  .kb { color: #6b7c78; font-size: 12px; }
  .btn {
    border: 1px solid #cfe0dc;
    background: #fff;
    color: #1f2d2a;
    border-radius: 6px;
    padding: 3px 10px;
    font-size: 13px;
    cursor: pointer;
  }
  .btn:hover:not(:disabled) { border-color: #2f9e8f; }
  .btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .btn.primary { background: #2f9e8f; color: #fff; border-color: #2f9e8f; }
  .btn.ok { color: #2f9e8f; border-color: #b6ddd6; }
  .btn.no { color: #b04a4a; border-color: #e6cccc; }
  .btn.warn { background: #7c5cff; color: #fff; border-color: #7c5cff; margin-top: 6px; }
  .btn.link { border: none; background: none; color: #2f9e8f; padding: 4px 0; }
  .err { color: #b04a4a; background: #fbecec; border: 1px solid #e6cccc; border-radius: 6px; padding: 6px 10px; margin-top: 8px; }
  .msg { color: #2f9e8f; background: #e9f6f3; border: 1px solid #b6ddd6; border-radius: 6px; padding: 6px 10px; margin-top: 8px; }
  .list { margin-top: 8px; }
  .empty { color: #6b7c78; padding: 10px 2px; }
  .row { border-bottom: 1px solid #eef4f2; }
  .line { display: flex; align-items: center; gap: 10px; padding: 7px 2px; cursor: pointer; }
  .name { font-weight: 600; }
  .count { color: #6b7c78; font-size: 12px; }
  .cls {
    font-size: 12px;
    border: 1px solid #d9e6e2;
    border-radius: 4px;
    padding: 1px 4px;
    background: #fff;
    color: #1f2d2a;
    max-width: 150px;
  }
  .caret { color: #7c5cff; font-size: 12px; margin-left: auto; }
  .sample { padding: 4px 2px 10px; }
  .sample pre { background: #fff; border: 1px solid #e2ece9; border-radius: 6px; padding: 8px; margin: 0; font-size: 12px; overflow: auto; }
  .muted { color: #9aa8a4; font-size: 12px; }
  .foot { margin-top: 10px; }
  .hist { margin-top: 6px; padding: 8px 10px; background: #fff; border: 1px solid #e2ece9; border-radius: 6px; }
  .hrow { display: flex; gap: 10px; padding: 2px 0; }
  .hrow .k { color: #6b7c78; min-width: 72px; }
  .hrow .v { color: #1f2d2a; }
</style>

<!--
《人工验收清单》
① 打开面板能看到版本号：顶部「v{version}」应显示后端返回的当前版本（初始 0 或实际值）。
② 点「重新提取候选」后列表有变化或无变化能解释：调用 trigger 后重调 pending，列表条数/名称应与后端候选一致；无变化说明后端未产生新候选。
③ 点确认后版本号 +1：对某候选点「确认」，成功后 msg 显示结果，顶部版本号应递增 1（由 confirm 返回的 version 或刷新后的 history 反映）。
-->