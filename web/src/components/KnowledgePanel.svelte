<script>
  // KnowledgePanel — 知识库管理面板（上传 / 删除 / 查看内容 / 列表）
  // 单企业收敛：kb 由父级（App.svelte）注入 currentKb（当前登录企业唯一 kb），面板内无 kb 切换/轮询，
  // 上传/加载/删除全部使用该当前企业 kb；kb prop 变化时 $effect 自动重新加载（面板跟随当前企业）。

  const JSON_HEADERS = { 'Content-Type': 'application/json' };
  const ALLOWED_EXT = ['.pdf', '.doc', '.docx', '.txt'];
  import { getToken } from '../lib/api.js';
  const authedFetch = (url, opts = {}) => {
    const h = { ...(opts.headers || {}) };
    const t = getToken();
    if (t) h['Authorization'] = 'Bearer ' + t;
    return fetch(url, { ...opts, headers: h });
  };

  let { kb = '' } = $props();    // 当前企业唯一 kb（只读 prop，跟随登录企业）
  let docs = $state([]);          // [{doc_id,title,chunks,ingested_at}]

  // 入库时间戳(秒/毫秒) → "YYYY-MM-DD HH:mm" 可读格式；无/非法返回 ''
  function fmtTime(v) {
    if (!v) return '';
    const n = Number(v);
    if (!isFinite(n) || n <= 0) return '';
    // 秒级(<1e12)统一乘1000转毫秒，毫秒级(>=1e12)直接用
    const ms = n < 1e12 ? n * 1000 : n;
    const d = new Date(ms);
    if (isNaN(d.getTime())) return '';
    const p = (x) => String(x).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }
  let loading = $state(false);
  let loaded = $state(false);     // 是否已成功加载过一次（区分未加载空态 vs 真正空态）
  let error = $state('');

  // 上传
  let file = $state(null);
  let fileName = $state('');
  let uploading = $state(false);

  // 删除
  let deletingDoc = $state('');
 // 批量选择: selDocs=选中文档id集合, selAllDocs=全选标记
 let selDocs = $state(new Set());
 let selAllDocs = $state(false);
 function toggleDoc(id) { selDocs.has(id) ? selDocs.delete(id) : selDocs.add(id); selDocs = new Set(selDocs); updateSelAll(); }
 function toggleAllDocs(e) { const on = e.target.checked; selDocs = on ? new Set(docs.map(d => d.doc_id)) : new Set(); selAllDocs = on; }
 function updateSelAll() { selAllDocs = docs.length > 0 && selDocs.size === docs.length; }
 async function batchDeleteDocs() {
   if (!selDocs.size) return;
   if (!window.confirm('确定删除选中的 ' + selDocs.size + ' 篇文档？此操作不可恢复。')) return;
   batchDeleting = true;
   let okCount = 0, failCount = 0;
   for (const id of Array.from(selDocs)) {
     const d = docs.find(x => x.doc_id === id);
     try {
       const resp = await authedFetch('/api/ontology/knowledge-delete', {
         method: 'POST', headers: JSON_HEADERS,
         body: JSON.stringify({ kb: kb.trim(), doc_id: id }),
       });
       const res = await resp.json();
       if (res && res.ok) okCount++; else failCount++;
     } catch (e) { failCount++; }
   }
   batchDeleting = false;
   selDocs = new Set(); selAllDocs = false;
   flash(okCount > 0 ? 'ok' : 'err', '批量删除完成：成功 ' + okCount + '，失败 ' + failCount);
   await loadDocs();
 }
 let batchDeleting = $state(false);

  // 查看
  let viewingDoc = $state('');
  let viewContent = $state([]);   // [{doc_id,title,chunk,score}]
  let viewLoading = $state(false);
  let viewError = $state('');

  // 操作反馈
  let notice = $state({ type: '', text: '' });
  let noticeTimer = null;
  function flash(type, text) {
    notice = { type, text };
    if (noticeTimer) clearTimeout(noticeTimer);
    noticeTimer = setTimeout(() => { notice = { type: '', text: '' }; }, 4000);
  }

  function fmtError(res) {
    if (res && res.error) {
      if (typeof res.error === 'object' && res.error.message) return res.error.message;
      return String(res.error);
    }
    return '操作失败';
  }

  async function load() {
    const name = kb.trim();
    if (!name) { error = '请输入知识库名称'; return; }
    loading = true; error = '';
    try {
      const resp = await authedFetch('/api/ontology/knowledge-list?kb=' + encodeURIComponent(name));
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
        error = fmtError(res) || '加载失败';
      }
    } catch (e) {
      error = '网络错误，请确认服务已启动';
    } finally {
      loading = false;
    }
  }

  // kb prop 变化（切换/重置后当前企业 kb 更新）→ 自动重新加载该企业文档列表
  $effect(() => {
    const name = kb;
    if (name) load();
  });

  // ── 上传 ──
  function onFilePick(e) {
    const f = e.target.files && e.target.files[0];
    e.target.value = '';
    if (!f) return;
    const ext = '.' + (f.name.split('.').pop() || '').toLowerCase();
    if (!ALLOWED_EXT.includes(ext)) {
      flash('err', '仅支持 PDF / Word / TXT 文件');
      return;
    }
    file = f;
    fileName = f.name;
  }

  async function doUpload() {
    const name = kb.trim();
    if (!file) { flash('err', '请先选择文件'); return; }
    if (!name) { flash('err', '请输入知识库名称'); return; }
    uploading = true;
    flash('', '');
    try {
      const fd = new FormData();
      fd.append('file', file, file.name);
      fd.append('kb', name);
      const resp = await authedFetch('/api/ontology/knowledge-ingest', { method: 'POST', body: fd });
      const res = await resp.json();
      if (res && res.ok) {
        const d = res.data || {};
        flash('ok', '上传成功：' + (d.title || file.name) + '（' + (d.chunks ?? '?') + ' 分块）');
        file = null; fileName = '';
        await load();
        if (d.doc_id) { viewingDoc = d.doc_id; await viewDoc({ doc_id: d.doc_id, title: d.title || file.name }); }
      } else {
        flash('err', fmtError(res) || '上传失败');
      }
    } catch (e) {
      flash('err', '网络错误，请确认服务已启动');
    } finally {
      uploading = false;
    }
  }

  // ── 删除 ──
  async function deleteDoc(d) {
    if (!window.confirm('确定删除文档「' + (d.title || d.doc_id) + '」？此操作不可恢复。')) return;
    deletingDoc = d.doc_id;
    flash('', '');
    try {
      const resp = await authedFetch('/api/ontology/knowledge-delete', {
        method: 'POST', headers: JSON_HEADERS,
        body: JSON.stringify({ kb: kb.trim(), doc_id: d.doc_id }),
      });
      const res = await resp.json();
      if (res && res.ok) {
        flash('ok', '已删除文档：' + (d.title || d.doc_id));
        if (viewingDoc === d.doc_id) { viewingDoc = ''; viewContent = []; }
        await load();
      } else {
        flash('err', fmtError(res) || '删除失败');
      }
    } catch (e) {
      flash('err', '网络错误，请确认服务已启动');
    } finally {
      deletingDoc = '';
    }
  }

  // ── 查看内容（knowledge-query 检索该文档切块）──
  async function viewDoc(d) {
    if (viewingDoc === d.doc_id) { viewingDoc = ''; viewContent = []; viewError = ''; return; }
    viewingDoc = d.doc_id;
    viewContent = []; viewError = '';
    viewLoading = true;
    try {
      const resp = await authedFetch('/api/ontology/knowledge-query', {
        method: 'POST', headers: JSON_HEADERS,
        body: JSON.stringify({ kb: kb.trim(), q: (d.title || d.doc_id) + ' 内容 说明 参数 详情 知识 技术文档', top_k: 20 }),
      });
      const res = await resp.json();
      if (res && res.ok) {
        const ev = (res.data && Array.isArray(res.data.evidence)) ? res.data.evidence : [];
        viewContent = ev.filter(h => String(h.doc_id) === String(d.doc_id));
        if (viewContent.length === 0) viewError = '该文档未检索到切块内容（可能知识库为空或文档未被命中）';
      } else {
        viewError = fmtError(res) || '查看失败';
      }
    } catch (e) {
      viewError = '网络错误，请确认服务已启动';
    } finally {
      viewLoading = false;
    }
  }

  // 跟随当前激活 kb 的注释与轮询已移除：单企业收敛，kb 由父级注入，无需内部获取/轮询。
</script>

<div class="kp">
  {#if notice.text}
    <div class="kp-notice {notice.type === 'ok' ? 'ok' : 'err'}">{notice.type === 'ok' ? '✅ ' : '⚠️ '}{notice.text}</div>
  {/if}

  <!-- 当前企业知识库标签 -->
  <div class="kb-bar">
    <span class="kb-label">当前企业知识库</span>
    <span class="kb-tag" title="跟随当前登录企业，不可切换">{kb || '—'}</span>
  </div>

  <!-- 上传区 -->
  <div class="upload-bar">
    <label class="file-pick">
      <span class="btn-icon">📄</span>
      {fileName || '选择文件（.pdf/.doc/.docx/.txt）'}
      <input type="file" accept=".pdf,.doc,.docx,.txt" hidden onchange={onFilePick} />
    </label>
    {#if fileName}
      <span class="file-name">{fileName}</span>
    {/if}
    <button class="btn-upload" onclick={doUpload} disabled={uploading || !fileName}>
      {uploading ? '上传中…' : '上传文档'}
    </button>
    {#if file}
      <button class="btn-clear" onclick={() => { file = null; fileName = ''; }}>取消</button>
    {/if}
  </div>

  {#if error}
    <div class="dash-empty dash-err">
      {error}
      <button class="dash-retry" onclick={load}>重试</button>
    </div>
  {:else if loading}
    <div class="skel-block">
      <div class="skel skel-line-lg"></div>
      <div class="skel skel-line-lg"></div>
      <div class="skel skel-line-md"></div>
      <div class="skel skel-line-lg"></div>
      <div class="skel skel-line-md"></div>
    </div>
  {:else if loaded && docs.length === 0}
    <div class="dash-empty dash-nodata">
      <span class="empty-icon">🗂️</span>
      <span class="empty-text">暂无文档，可通过上方「选择文件 + 上传文档」添加</span>
    </div>
  {:else if loaded}
    <div class="batch-del-bar" class:show={selDocs.size > 0}>
      <span>已选 {selDocs.size} 篇</span>
      <button class="btn-del" onclick={batchDeleteDocs} disabled={batchDeleting}>
        {batchDeleting ? '批量删除中…' : '批量删除'}
      </button>
    </div>
    <div class="doc-table-wrap">
      <table class="doc-table">
        <thead>
          <tr>
            <th style="width:32px"><input type="checkbox" title="选择全部" onchange={toggleAllDocs} checked={selAllDocs}></th>
            <th>文档 ID</th><th>标题</th><th class="col-num">分块数</th><th>入库时间</th><th class="col-act">操作</th>
          </tr>
        </thead>
        <tbody>
          {#each docs as d}
            <tr class:row-selected={selDocs.has(d.doc_id)}>
              <td style="text-align:center"><input type="checkbox" checked={selDocs.has(d.doc_id)} onchange={() => toggleDoc(d.doc_id)}></td>
              <td class="id-col">{d.doc_id}</td>
              <td>{d.title}</td>
              <td class="num-col">{d.chunks}</td>
              <td class="time-col">{fmtTime(d.ingested_at) || '—'}</td>
              <td class="col-act">
                <div class="row-actions">
                  <button class="btn-view" onclick={() => viewDoc(d)} disabled={viewLoading}>
                    {viewingDoc === d.doc_id ? '收起' : '查看'}
                  </button>
                  <button class="btn-del" onclick={() => deleteDoc(d)} disabled={deletingDoc === d.doc_id}>
                    {deletingDoc === d.doc_id ? '删除中…' : '删除'}
                  </button>
                </div>
              </td>
            </tr>
            {#if viewingDoc === d.doc_id}
              <tr class="view-row">
                <td colspan="5">
                  {#if viewLoading}
                    <div class="dash-empty">正在检索该文档切块…</div>
                  {:else if viewError}
                    <div class="dash-empty dash-err">{viewError}</div>
                  {:else if viewContent.length === 0}
                    <div class="dash-empty dash-nodata">该文档暂无可见切块</div>
                  {:else}
                    <div class="chunk-list">
                      {#each viewContent as c, i}
                        <div class="chunk-item">
                          <div class="chunk-head">切块 {i + 1}<span class="chunk-score">相似度 {c.score != null ? Number(c.score).toFixed(3) : '—'}</span></div>
                          <pre class="chunk-text">{c.chunk}</pre>
                        </div>
                      {/each}
                    </div>
                  {/if}
                </td>
              </tr>
            {/if}
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

  .kp-notice {
    padding: 8px 12px; font-size: 12px; border-radius: 4px;
    border: 1px solid transparent;
  }
  .kp-notice.ok { color: #166534; background: #ecfdf5; border-color: #a7f3d0; }
  .kp-notice.err { color: #991b1b; background: #fef2f2; border-color: #fecaca; }

  .kb-bar { display: flex; align-items: center; gap: 8px; }
  .kb-label { font-size: 12px; font-weight: 700; color: #1e293b; }
  .kb-tag {
    display: inline-flex; align-items: center;
    padding: 4px 12px; font-size: 12px; font-weight: 600; color: var(--brand);
    background: var(--brand-soft); border: 1px solid var(--brand-line); border-radius: 12px;
  }
  .kb-input {
    flex: 1; max-width: 280px;
    padding: 6px 10px; font-size: 13px; color: #1e293b;
    background: #fff; border: 1px solid #cbd5e1; border-radius: 4px;
    outline: none; transition: border-color 0.15s;
  }
  .kb-input:focus { border-color: var(--brand); }
  .kb-load {
    display: flex; align-items: center; gap: 5px;
    background: var(--brand); color: #fff;
    border: 1px solid var(--brand); border-radius: 4px;
    padding: 6px 14px; font-size: 12px; cursor: pointer;
    transition: all 0.15s;
  }
  .kb-load:hover:not(:disabled) { background: var(--brand-dark); }
  .btn-icon { font-size: 12px; }

  /* 上传区 */
  .upload-bar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .file-pick {
    display: inline-flex; align-items: center; gap: 5px;
    padding: 6px 12px; font-size: 12px; color: #334155;
    background: #fff; border: 1px dashed #94a3b8; border-radius: 4px;
    cursor: pointer; transition: all 0.15s;
  }
  .file-pick:hover { border-color: var(--brand); color: var(--brand); background: #f8fafc; }
  .file-name { font-size: 12px; color: #475569; }
  .btn-upload {
    padding: 6px 14px; font-size: 12px; cursor: pointer;
    color: #166534; background: #ecfdf5;
    border: 1px solid #10b981; border-radius: 4px; transition: all 0.15s;
  }
  .btn-upload:hover:not(:disabled) { background: #d1fae5; }
  .btn-upload:disabled { opacity: 0.6; cursor: not-allowed; }
  .btn-clear {
    padding: 6px 10px; font-size: 12px; cursor: pointer;
    color: #64748b; background: #fff;
    border: 1px solid #cbd5e1; border-radius: 4px; transition: all 0.15s;
  }
  .btn-clear:hover { border-color: #94a3b8; }

  .dash-empty {
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    gap: 10px; padding: 48px 20px; text-align: center; color: var(--text-muted); font-size: 13px;
  }
  .dash-empty .empty-icon { font-size: 28px; line-height: 1; }
  .dash-empty .empty-text { color: var(--text-muted); }

  /* 骨架屏 */
  @keyframes shimmer { 0% { background-position: -360px 0; } 100% { background-position: 360px 0; } }
  .skel {
    border-radius: 6px;
    background: linear-gradient(90deg, #eef1f5 25%, #f7f9fc 40%, #eef1f5 55%);
    background-size: 720px 100%; animation: shimmer 1.4s infinite linear;
  }
  .skel-block { display: flex; flex-direction: column; gap: 12px; }
  .skel-line-lg { height: 14px; width: 100%; }
  .skel-line-md { height: 14px; width: 70%; }
  .dash-nodata { color: #64748b; }
  .dash-err { color: #dc2626; }
  .dash-retry {
    margin-left: 10px; background: #fff; color: var(--brand);
    border: 1px solid #cbd5e1; border-radius: 4px;
    padding: 4px 12px; font-size: 12px; cursor: pointer;
    transition: all 0.15s;
  }
  .dash-retry:hover { border-color: var(--brand); background: #f8fafc; }

  .doc-table-wrap {
    background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius-md);
    padding: 14px; box-shadow: var(--shadow-card);
  }
  .doc-table { width: 100%; border-collapse: collapse; }
  .doc-table thead th {
    position: sticky; top: 0; z-index: 1;
    padding: 8px 10px; font-size: 12px; font-weight: 600; text-align: left;
    color: var(--text-secondary); background: var(--bg-elevated);
    border-bottom: 1px solid var(--border);
  }
  .doc-table tbody td {
    padding: 0 10px; font-size: 13px; text-align: left; color: var(--text-primary);
    border-bottom: 1px solid var(--border);
    height: 40px; transition: background 150ms ease-out;
  }
  .doc-table tbody tr:last-child td { border-bottom: none; }
  .doc-table tbody tr:hover td { background: var(--bg-hover); }
  .id-col { font-family: ui-monospace, 'SF Mono', SFMono-Regular, Menlo, Consolas, monospace; font-size: 13px; color: var(--text-secondary); }
  .num-col { text-align: right; font-variant-numeric: tabular-nums; font-family: ui-monospace, 'SF Mono', Consolas, monospace; }
  .time-col { font-variant-numeric: tabular-nums; }
  .col-num { text-align: right; }
  .col-act { width: 130px; }
  .doc-count { margin-top: 8px; font-size: 11px; color: var(--text-muted); }

  .row-actions { display: flex; gap: 6px; }
  .btn-view {
    padding: 3px 10px; font-size: 12px; cursor: pointer;
    color: var(--brand-dark); background: var(--brand-soft);
    border: 1px solid var(--brand-line); border-radius: var(--radius-sm);
    transition: background 150ms ease-out;
  }
  .btn-view:hover:not(:disabled) { background: var(--bg-selected); }
  .btn-view:disabled { opacity: 0.6; cursor: not-allowed; }
  .btn-del {
    padding: 3px 10px; font-size: 12px; cursor: pointer;
    color: var(--danger-fg); background: var(--danger-bg);
    border: 1px solid var(--border); border-radius: var(--radius-sm);
    transition: background 150ms ease-out;
  }
  .btn-del:hover:not(:disabled) { background: var(--bg-hover); }
  .btn-del:disabled { opacity: 0.6; cursor: not-allowed; }

  .view-row td { background: var(--bg-elevated); }
  .chunk-list { display: flex; flex-direction: column; gap: 8px; }
  .chunk-item {
    background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 8px 10px;
  }
  .chunk-head { font-size: 11px; font-weight: 600; color: var(--text-secondary); margin-bottom: 4px; }
  .chunk-score { font-weight: 400; color: var(--text-muted); margin-left: 8px; }
  .chunk-text {
    margin: 0; white-space: pre-wrap; word-break: break-word;
    font-family: inherit; font-size: 12px; line-height: 1.6; color: var(--text-primary);
  }
  /* 批量删除 */
  .batch-del-bar { display: none; align-items: center; gap: 8px; margin: 6px 0 8px; padding: 6px 10px; background: var(--bg-hover, #f8fafc); border-radius: 6px; font-size: 12px; font-weight: 600; }
  .batch-del-bar.show { display: flex; }
  .batch-del-bar .btn-del { padding: 3px 10px; font-size: 12px; border: none; border-radius: 4px; background: var(--danger, #dc2626); color: #fff; cursor: pointer; }
  .batch-del-bar .btn-del:disabled { opacity: .5; cursor: not-allowed; }
  .row-selected { background: var(--bg-selected, #eff4ff) !important; }
  .doc-table thead th:first-child, .doc-table tbody td:first-child { width: 32px; text-align: center; }
</style>
