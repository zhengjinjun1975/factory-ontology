<script>
  // KnowledgePanel — 知识库管理面板
  // 选择 kb 加载文档列表（调前端 knowledge-list 转发端点），表格展示 doc_id/title/chunks/ingested_at
  import { onMount } from 'svelte';

  let kb = $state('food');        // 知识库名（默认与后端一致）
  let docs = $state([]);          // [{doc_id,title,chunks,ingested_at}]
  let loading = $state(false);
  let loaded = $state(false);     // 是否已成功加载过一次（区分未加载空态 vs 真正空态）
  let error = $state('');

  async function load() {
    const name = kb.trim();
    if (!name) { error = '请输入知识库名称'; return; }
    loading = true; error = '';
    try {
      const resp = await fetch('/api/ontology/knowledge-list?kb=' + encodeURIComponent(name));
      const res = await resp.json();
      if (res && res.ok) {
        // 兼容 {data:{docs}} 信封或直接 docs 数组
        const list = res.data && Array.isArray(res.data.docs) ? res.data.docs
                   : Array.isArray(res.docs) ? res.docs
                   : (res.data && Array.isArray(res.data) ? res.data : []);
        docs = list.map(d => ({
          doc_id: d.doc_id ?? d.id ?? '',
          title: d.title ?? d.doc_id ?? '',
          chunks: d.chunks ?? d.count ?? 0,
          ingested_at: d.ingested_at ?? d.created_at ?? d.time ?? '',
        }));
        loaded = true;
      } else {
        error = (res && res.error && res.error.message) || '加载失败';
      }
    } catch (e) {
      error = '网络错误，请确认服务已启动';
    } finally {
      loading = false;
    }
  }

  function onKeydown(e) {
    if (e.key === 'Enter') { e.preventDefault(); load(); }
  }

  onMount(load);
</script>

<div class="kp">
  <!-- 顶部：kb 选择 + 加载 -->
  <div class="kb-bar">
    <span class="kb-label">知识库</span>
    <input
      class="kb-input"
      type="text"
      bind:value={kb}
      onkeydown={onKeydown}
      placeholder="输入知识库名称，如 food"
    />
    <button class="kb-load" onclick={load} disabled={loading}>
      <span class="btn-icon">{loading ? '⏳' : '↻'}</span>
      {loading ? '加载中…' : '加载'}
    </button>
  </div>

  {#if error}
    <div class="dash-empty dash-err">
      {error}
      <button class="dash-retry" onclick={load}>重试</button>
    </div>
  {:else if loading}
    <div class="dash-empty">正在加载文档列表…</div>
  {:else if loaded && docs.length === 0}
    <div class="dash-empty dash-nodata">暂无文档，可上传知识文档</div>
  {:else if loaded}
    <div class="doc-table-wrap">
      <table class="doc-table">
        <thead>
          <tr><th>文档 ID</th><th>标题</th><th>分块数</th><th>入库时间</th></tr>
        </thead>
        <tbody>
          {#each docs as d}
            <tr>
              <td class="mono">{d.doc_id}</td>
              <td>{d.title}</td>
              <td class="mono">{d.chunks}</td>
              <td class="mono">{d.ingested_at || '—'}</td>
            </tr>
          {/each}
        </tbody>
      </table>
      <div class="doc-count">共 {docs.length} 篇文档</div>
    </div>
  {:else}
    <div class="dash-empty dash-nodata">点击「加载」查看该知识库的文档列表</div>
  {/if}
</div>

<style>
  .kp { display: flex; flex-direction: column; gap: 12px; }

  .kb-bar { display: flex; align-items: center; gap: 8px; }
  .kb-label { font-size: 12px; font-weight: 700; color: #1e293b; }
  .kb-input {
    flex: 1; max-width: 280px;
    padding: 6px 10px; font-size: 13px; color: #1e293b;
    background: #fff; border: 1px solid #cbd5e1; border-radius: 4px;
    outline: none; transition: border-color 0.15s;
  }
  .kb-input:focus { border-color: #3b82f6; }
  .kb-load {
    display: flex; align-items: center; gap: 5px;
    background: #2563eb; color: #fff;
    border: 1px solid #2563eb; border-radius: 4px;
    padding: 6px 14px; font-size: 12px; cursor: pointer;
    transition: all 0.15s;
  }
  .kb-load:hover:not(:disabled) { background: #1d4ed8; }
  .kb-load:disabled { opacity: 0.6; cursor: not-allowed; }
  .btn-icon { font-size: 12px; }

  .dash-empty { color: #94a3b8; font-size: 13px; text-align: center; padding: 30px; }
  .dash-nodata { color: #64748b; }
  .dash-err { color: #dc2626; }
  .dash-retry {
    margin-left: 10px; background: #fff; color: #2563eb;
    border: 1px solid #cbd5e1; border-radius: 4px;
    padding: 4px 12px; font-size: 12px; cursor: pointer;
    transition: all 0.15s;
  }
  .dash-retry:hover { border-color: #3b82f6; background: #f8fafc; }

  .doc-table-wrap {
    background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 4px;
    padding: 14px;
  }
  .doc-table { width: 100%; border-collapse: collapse; }
  .doc-table th, .doc-table td {
    padding: 6px 8px; font-size: 12px; text-align: left;
    border-bottom: 1px solid #e2e8f0;
  }
  .doc-table th { color: #64748b; font-weight: 600; background: #f1f5f9; }
  .doc-table td { color: #334155; }
  .mono { font-family: 'Consolas', monospace; }
  .doc-count { margin-top: 8px; font-size: 11px; color: #94a3b8; }
</style>
