<script>
  // LexiconAssetPanel — 词典资产（数据资产闭环的前端入口）
  // 闭环：企业结束导出 → 同行业新企业导入复用 → 独立来源达阈值沉淀进行业层 → 新企业建模消费
  // 调 BFF：lexicon-export / lexicon-import / industry-list / industry-candidates / industry-absorb
  let { kb = '' } = $props();

  import {
    exportLexicon, importLexicon,
    fetchIndustryDicts, fetchIndustryCandidates, absorbToIndustry,
  } from '../lib/api.js';

  const INDUSTRIES = ['基础', '泵阀', '精细化工', '地球物理'];

  // 按 kb 关键词猜默认行业（与后端 industry_for_kb 同规则）
  function guessIndustry(k) {
    const s = String(k || '').toLowerCase();
    if (s.includes('valve') || s.includes('pump')) return '泵阀';
    if (s.includes('chem')) return '精细化工';
    if (s.includes('seis') || s.includes('geo')) return '地球物理';
    return '基础';
  }

  let busy = $state('');
  let msg = $state('');
  let err = $state('');

  let fileName = $state('');
  let importText = $state('');
  let preview = $state(null);
  let importMode = $state('merge');

  let industry = $state('基础');
  let indDicts = $state([]);
  let cand = $state(null);
  let absorbRes = $state(null);

  const fmtErr = (e) => {
    if (e === null || e === undefined) return '后端无响应';
    if (typeof e === 'string') return e;
    if (typeof e === 'object') {
      if (e.message) return String(e.message);
      try { return JSON.stringify(e); } catch (_) { return String(e); }
    }
    return String(e);
  };
  const reset = () => { msg = ''; err = ''; };

  async function loadIndustry() {
    const [d, c] = await Promise.all([fetchIndustryDicts(), fetchIndustryCandidates(50)]);
    indDicts = (d && d.ok) ? (d.items || []) : [];
    cand = (c && c.ok) ? c : null;
    if (d && d.ok === false) err = fmtErr(d.error);
  }

  $effect(() => {
    if (kb) { industry = guessIndustry(kb); loadIndustry(); }
  });

  async function onExport(bundle) {
    reset(); busy = 'export';
    try {
      const r = await exportLexicon(kb, bundle);
      msg = `已导出 ${r.filename}（浏览器下载目录）`;
    } catch (e) { err = fmtErr(e.message || e); }
    busy = '';
  }

  async function onPick(ev) {
    const f = ev.target.files && ev.target.files[0];
    if (!f) return;
    reset(); preview = null;
    fileName = f.name;
    importText = await f.text();
    await doPreview();
  }

  async function doPreview() {
    if (!importText) return;
    reset(); busy = 'preview';
    const r = await importLexicon(kb, importText, { mode: importMode, dryRun: true });
    busy = '';
    if (r && r.ok) preview = r;
    else { preview = null; err = fmtErr(r && r.error); }
  }

  async function doImport() {
    reset(); busy = 'import';
    const r = await importLexicon(kb, importText, { mode: importMode, dryRun: false });
    busy = '';
    if (r && r.ok) {
      const added = (r.diff && r.diff.stats && r.diff.stats.added_total) || 0;
      msg = `导入完成：新增 ${added} 个词${r.backup ? '，原词典已自动备份' : ''}`;
      await doPreview();          // 刷新差异（导入后应为空）
    } else { err = fmtErr(r && r.error); }
  }

  async function onAbsorb() {
    reset(); busy = 'absorb'; absorbRes = null;
    const r = await absorbToIndustry(kb, industry);
    busy = '';
    if (r && r.ok) { absorbRes = r; await loadIndustry(); }
    else { err = fmtErr(r && r.error); }
  }

  const diffRows = $derived.by(() => {
    const a = (preview && preview.diff && preview.diff.added) || {};
    return Object.keys(a).filter((k) => (a[k] || []).length).map((k) => ({ key: k, words: a[k] }));
  });
</script>

<div class="lexasset">
  <div class="la-toolbar">
    <span class="la-label">当前企业知识库</span>
    <span class="la-tag">{kb || '—'}</span>
    <button class="la-btn la-btn-ghost la-small" onclick={loadIndustry} disabled={!!busy}>刷新</button>
  </div>

  {#if msg}<div class="la-msg">{msg}</div>{/if}
  {#if err}<div class="la-err">{err}</div>{/if}

  <div class="la-grid">
    <div class="la-card">
      <div class="la-card-title">① 导出 —— 企业结束时把资产带走</div>
      <p class="la-desc">导出本企业的工厂词典；“整包”额外带上本体(.nt) 与建模架构(schema)，便于同行业新企业起手。</p>
      <div class="la-actions">
        <button class="la-btn" onclick={() => onExport(false)} disabled={!!busy || !kb}>
          {busy === 'export' ? '⏳ 导出中…' : '⬇️ 导出词典 (JSON)'}
        </button>
        <button class="la-btn la-btn-ghost" onclick={() => onExport(true)} disabled={!!busy || !kb}>📦 导出整包 (ZIP)</button>
      </div>
    </div>

    <div class="la-card">
      <div class="la-card-title">② 导入 —— 新企业复用同行业积累</div>
      <p class="la-desc">选择别家企业导出的 lexicon_*.json；先看差异预览再落盘，落盘前会自动备份原词典。</p>
      <div class="la-actions">
        <input class="la-file" type="file" accept=".json,application/json" onchange={onPick} disabled={!!busy} />
        <select class="la-select" bind:value={importMode} onchange={doPreview} disabled={!!busy}>
          <option value="merge">merge（保留现有词，只补缺）</option>
          <option value="replace">replace（用导入内容覆盖）</option>
        </select>
      </div>
      {#if fileName}<div class="la-file-name">已选：{fileName}</div>{/if}
      {#if preview}
        <table class="la-table">
          <thead><tr><th>词表键</th><th>将新增</th><th>示例</th></tr></thead>
          <tbody>
            {#each diffRows as r}
              <tr>
                <td class="mono">{r.key}</td>
                <td>{r.words.length}</td>
                <td class="la-words">{r.words.slice(0, 6).join('、')}</td>
              </tr>
            {/each}
            {#if !diffRows.length}
              <tr><td colspan="3" class="la-empty">无可新增词（导入内容已被现有词典覆盖）</td></tr>
            {/if}
          </tbody>
        </table>
        <div class="la-actions">
          <button class="la-btn" onclick={doImport} disabled={!!busy}>
            {busy === 'import' ? '⏳ 导入中…' : '✅ 确认导入'}
          </button>
        </div>
      {/if}
    </div>

    <div class="la-card">
      <div class="la-card-title">③ 行业积累 —— 沉淀进行业词典</div>
      <p class="la-desc">
        把本企业词典的词记入候选池；只有同一概念出现在 ≥ {cand ? cand.threshold : 3} 个独立来源
        （模板复制只算 1 个来源）才会升级进行业词典，避免一家之言污染公共层。
      </p>
      <div class="la-actions">
        <select class="la-select" bind:value={industry} disabled={!!busy}>
          {#each INDUSTRIES as i}<option value={i}>{i}</option>{/each}
        </select>
        <button class="la-btn" onclick={onAbsorb} disabled={!!busy || !kb}>
          {busy === 'absorb' ? '⏳ 吸收中…' : '📥 吸收本企业词典'}
        </button>
      </div>
      {#if absorbRes}
        <div class="la-result">
          候选池 +{absorbRes.pool_added} 词（共 {absorbRes.pool_size} 词）；全库独立来源 {absorbRes.independent_sources} 个；
          {#if absorbRes.promoted && Object.keys(absorbRes.promoted).length}
            已升级进行业层：{Object.entries(absorbRes.promoted).map(([k, n]) => `${k} +${n}`).join('、')}
          {:else}
            本次无升级（单企业来源数未达阈值，已存候选池待后续企业确认）
          {/if}
        </div>
      {/if}
    </div>
  </div>

  <div class="la-card">
    <div class="la-card-title">公共 / 行业词典现状</div>
    <table class="la-table">
      <thead><tr><th>文件</th><th>说明</th><th>类型</th><th>状态</th><th>同义词</th><th>实体</th></tr></thead>
      <tbody>
        {#each indDicts as d}
          <tr>
            <td class="mono">{d.file}</td>
            <td class="la-words">{d.description}</td>
            <td>{d.type}</td><td>{d.status}</td><td>{d.synonym}</td><td>{d.entity}</td>
          </tr>
        {/each}
        {#if !indDicts.length}
          <tr><td colspan="6" class="la-empty">未读取到行业词典</td></tr>
        {/if}
      </tbody>
    </table>
  </div>

  <div class="la-card">
    <div class="la-card-title">
      候选池 —— 待确认的行业概念{cand ? `（共 ${cand.total} 词，阈值 ${cand.threshold} 个独立来源）` : ''}
    </div>
    <table class="la-table">
      <thead><tr><th>概念</th><th>独立来源数</th><th>来源</th><th>首见</th></tr></thead>
      <tbody>
        {#each (cand ? cand.items : []) as it}
          <tr>
            <td>{it.word}</td>
            <td class:la-hot={cand && it.n >= cand.threshold}>{it.n}</td>
            <td class="la-words">{it.sources.join('、')}</td>
            <td class="mono">{it.first_seen}</td>
          </tr>
        {/each}
        {#if !cand || !cand.items.length}
          <tr><td colspan="4" class="la-empty">候选池为空（还没有企业词典被吸收）</td></tr>
        {/if}
      </tbody>
    </table>
  </div>
</div>

<style>
  .lexasset { display: flex; flex-direction: column; gap: 12px; }
  .la-toolbar { display: flex; align-items: center; gap: 8px; }
  .la-label { font-size: 12px; color: #334155; font-weight: 600; white-space: nowrap; }
  .la-tag {
    display: inline-flex; align-items: center; padding: 4px 12px; font-size: 12px; font-weight: 600;
    color: var(--brand); background: var(--brand-soft); border: 1px solid var(--brand-line); border-radius: 12px;
  }
  .la-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 12px; }
  .la-card { border: 1px solid #e2e8f0; border-radius: 10px; padding: 14px; background: #fff; display: flex; flex-direction: column; gap: 10px; }
  .la-card-title { font-size: 13px; font-weight: 700; color: #0f172a; }
  .la-desc { margin: 0; font-size: 12px; line-height: 1.7; color: #64748b; }
  .la-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .la-btn {
    padding: 6px 14px; font-size: 12px; font-weight: 600; color: #fff; background: var(--brand);
    border: 1px solid var(--brand); border-radius: 8px; cursor: pointer;
  }
  .la-btn:disabled { opacity: .55; cursor: not-allowed; }
  .la-btn-ghost { color: var(--brand); background: var(--brand-soft); border-color: var(--brand-line); }
  .la-small { padding: 4px 10px; margin-left: auto; }
  .la-file { font-size: 12px; max-width: 100%; }
  .la-file-name { font-size: 12px; color: #334155; }
  .la-select { padding: 5px 8px; font-size: 12px; border: 1px solid #cbd5e1; border-radius: 6px; background: #fff; }
  .la-msg { padding: 8px 12px; font-size: 12px; color: #166534; background: #dcfce7; border: 1px solid #bbf7d0; border-radius: 8px; }
  .la-err { padding: 8px 12px; font-size: 12px; color: #991b1b; background: #fee2e2; border: 1px solid #fecaca; border-radius: 8px; }
  .la-result { padding: 8px 12px; font-size: 12px; line-height: 1.7; color: #1e3a8a; background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 8px; }
  .la-table { width: 100%; border-collapse: collapse; font-size: 12px; }
  .la-table th { text-align: left; padding: 6px 8px; color: #475569; border-bottom: 1px solid #e2e8f0; font-weight: 600; }
  .la-table td { padding: 6px 8px; border-bottom: 1px solid #f1f5f9; color: #1e293b; }
  .la-words { color: #64748b; }
  .la-empty { color: #94a3b8; text-align: center; padding: 12px 0; }
  .la-hot { color: #b91c1c; font-weight: 700; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
</style>
