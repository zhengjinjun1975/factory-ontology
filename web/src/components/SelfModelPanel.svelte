<script>
  // SelfModelPanel — 自助建模（三步流程：选数据源 → AI 建议可编辑 → 人拍板确认生效）
  //
  // 与"数据建模"tab 的区别：那条路是系统全自动直出（setup/build），本面板把中间一步交回给人 ——
  //   ① 选数据源：填 kb 名 + 数据目录（如 data_valve），点「AI 建议」；
  //   ② AI 建议可编辑：后端 /api/ontology/suggest 只读预览（不落盘），返回 entities/relations/
  //      constraints/stats，人工在此改中文 label / 业务域 / 实体名 / 主键；
  //   ③ 人拍板：点「确认生效」把编辑后的完整 schema 原样回传 /api/ontology/confirm，
  //      后端按该 schema 产出 nt + lexicon、注册 kb，返回产物路径 + 已可问答。
  //
  // 关键约束：entities 里的每个 attribute 必须原样带回（含 role 字段），丢了 role 词典就认不出
  // 类型列/状态列，问答会答"没有相关数据"。因此本面板只改实体/关系/约束的表层字段，
  // 绝不重建 attributes —— 编辑总是发生在 suggest 返回的原对象上。
  //
  // 单企业收敛：默认 kb 跟随父级注入的 currentKb（用户仍可手改，用于试建新库）。

  import { suggestSchema, confirmSchema } from '../lib/api.js';

  let { kb = '' } = $props();    // 当前登录企业唯一 kb（只读 prop，作默认值）

  let step = $state(1);          // 1 选数据源 | 2 建议编辑 | 3 已生效
  let formKb = $state('');       // 目标知识库名
  let dataDir = $state('data_valve'); // 数据目录（相对仓库根）
  let kbTouched = $state(false); // 用户是否手改过 kb（改过则不再跟随 prop 覆盖）

  let busy = $state(false);      // AI 建议中
  let suggestErr = $state('');
  let confirmErr = $state('');
  let suggestNote = $state('');  // 后端 note（预览未落盘提示）
  let source = $state('');       // 建议来源（auto-inferred 等）

  let schema = $state(null);     // 编辑态 schema {version,name,industry,entities,relations,constraints}
  let stats = $state(null);      // {entities,relations,constraints,domains[]}
  let openAttrs = $state({});    // 实体 id → 属性表是否展开

  let confirmBusy = $state(false);
  let result = $state(null);     // confirm 产物 {kb,schema_path,nt,lexicon,status,ask_ready}

  // 默认 kb 跟随当前企业（用户没手改过时才跟随；重置/换企业后自动对齐）
  $effect(() => {
    const cur = kb;
    if (!kbTouched && cur) { formKb = cur; }
  });

  // 错误信息安全格式化：后端 error 可能是对象 {code,message}，避免渲染成 [object Object]
  const fmtErr = (err) => {
    if (err === null || err === undefined) return '后端无响应';
    if (typeof err === 'string') return err;
    if (typeof err === 'object') {
      if (err.message) return String(err.message);
      try { return JSON.stringify(err); } catch (e) { return String(err); }
    }
    return String(err);
  };

  // ─── 第1步 → 第2步：调 AI 建议（只读预览，可反复调）───
  async function doSuggest() {
    if (busy) return;
    const k = formKb.trim(), d = dataDir.trim();
    if (!k) { suggestErr = '请输入知识库名称（kb）'; return; }
    if (!d) { suggestErr = '请输入数据目录（如 data_valve）'; return; }
    busy = true; suggestErr = ''; confirmErr = ''; result = null;
    try {
      const res = await suggestSchema(k, d);
      if (res && res.ok && res.data) {
        const dd = res.data;
        // 编辑态 schema：完整保留后端给的 entities/relations/constraints（含 attribute.role）
        schema = {
          version: '1.0',
          name: 'auto-inferred-ontology',
          industry: k,
          entities: Array.isArray(dd.entities) ? dd.entities : [],
          relations: Array.isArray(dd.relations) ? dd.relations : [],
          constraints: Array.isArray(dd.constraints) ? dd.constraints : [],
        };
        stats = dd.stats || null;
        suggestNote = dd.note || '';
        source = dd.source || 'auto-inferred';
        openAttrs = {};
        step = 2;
      } else {
        suggestErr = 'AI 建议失败：' + fmtErr(res && res.error);
      }
    } catch (e) {
      suggestErr = 'AI 建议失败：' + fmtErr(e && e.message ? e.message : e);
    } finally {
      busy = false;
    }
  }

  // ─── 第3步：人拍板 → 确认生效（原样回传编辑后的 schema）───
  async function doConfirm() {
    if (confirmBusy || !schema) return;
    const k = formKb.trim();
    if (!k) { confirmErr = '请输入知识库名称（kb）'; return; }
    if (!schema.entities.length) { confirmErr = '没有可确认的实体，请先获取 AI 建议'; return; }
    confirmBusy = true; confirmErr = '';
    try {
      // 深拷贝回传，确保 attributes（含 role/required）一个字段都不丢
      const payload = JSON.parse(JSON.stringify({
        entities: schema.entities,
        relations: schema.relations,
        constraints: schema.constraints,
        version: schema.version || '1.0',
        name: schema.name || 'auto-inferred-ontology',
        industry: k,
      }));
      const res = await confirmSchema(k, payload, dataDir.trim());
      if (res && res.ok && res.data) {
        result = res.data;
        step = 3;
      } else {
        confirmErr = '确认生效失败：' + fmtErr(res && res.error);
      }
    } catch (e) {
      confirmErr = '确认生效失败：' + fmtErr(e && e.message ? e.message : e);
    } finally {
      confirmBusy = false;
    }
  }

  // 回到第1步重来（换数据源/重新建议）
  function restart() {
    step = 1; schema = null; stats = null; result = null;
    suggestErr = ''; confirmErr = ''; suggestNote = ''; openAttrs = {};
  }
  // 回到第2步继续编辑（已建议过才可用）
  function backToEdit() {
    if (!schema) { restart(); return; }
    step = 2; confirmErr = '';
  }
  function toggleAttrs(id) { openAttrs[id] = !openAttrs[id]; }

  const domainOptions = $derived((stats && Array.isArray(stats.domains)) ? stats.domains : []);
  const entityCount = $derived(schema && schema.entities ? schema.entities.length : 0);
  const attrCount = $derived(
    schema && schema.entities
      ? schema.entities.reduce((n, e) => n + ((e && e.attributes) ? e.attributes.length : 0), 0)
      : 0
  );
  // 属性语义角色 → 中文（role 丢了问答就失效，这里显式展示便于人工核对）
  const ROLE_CN = { key: '主键', metric: '指标', dimension: '维度', type: '类型', status: '状态', time: '时间', text: '文本' };
  const roleCn = (r) => (r ? (ROLE_CN[r] || r) : '—');
</script>

<div class="selfmodel">
  <!-- 步骤条 -->
  <ol class="steps">
    <li class="step" class:step-on={step === 1} class:step-done={step > 1}>
      <span class="step-no">1</span><span class="step-txt">选数据源</span>
    </li>
    <li class="step" class:step-on={step === 2} class:step-done={step > 2}>
      <span class="step-no">2</span><span class="step-txt">看 AI 建议并修改</span>
    </li>
    <li class="step" class:step-on={step === 3}>
      <span class="step-no">3</span><span class="step-txt">人拍板确认生效</span>
    </li>
  </ol>

  <!-- ═══ 第1步：选数据源 ═══ -->
  {#if step === 1}
  <div class="card">
    <div class="card-title">选择数据源</div>
    <div class="card-desc">
      指定要建模的企业数据。系统会读取该目录下的 CSV / JSON / XLSX 表，先由 AI 给出建议（只读预览，不写入任何产物），
      再由人工核对修改，最后点「确认生效」才真正落地为本体与词典。
    </div>
    <div class="form-row">
      <label class="form-label" for="sm-kb">知识库名称</label>
      <input
        id="sm-kb" class="input" type="text" placeholder="如 valve3"
        bind:value={formKb}
        oninput={() => { kbTouched = true; suggestErr = ''; }}
        onkeydown={(e) => { if (e.key === 'Enter') doSuggest(); }}
      />
      <span class="form-hint">本体与词典按该名称生成</span>
    </div>
    <div class="form-row">
      <label class="form-label" for="sm-dir">数据目录</label>
      <input
        id="sm-dir" class="input" type="text" placeholder="如 data_valve"
        bind:value={dataDir}
        oninput={() => { suggestErr = ''; }}
        onkeydown={(e) => { if (e.key === 'Enter') doSuggest(); }}
      />
      <span class="form-hint">相对仓库根目录，如 data_valve / data_food</span>
    </div>
    <div class="actions">
      <button class="btn btn-primary" onclick={doSuggest} disabled={busy}>
        {busy ? '⏳ 正在分析数据…' : '🤖 AI 建议'}
      </button>
      {#if busy}<span class="busy-hint">正在读取数据并推断实体/关系/约束，请稍候…</span>{/if}
    </div>
    {#if suggestErr}<div class="msg msg-err">{suggestErr}</div>{/if}
  </div>
  {/if}

  <!-- ═══ 第2步：AI 建议可编辑 ═══ -->
  {#if step === 2 && schema}
  <div class="card">
    <div class="card-title">AI 建议（可修改）</div>
    <div class="card-desc">
      以下是系统推断结果，尚未落盘。可直接修改中文标签 / 业务域 / 实体名 / 主键；
      改完点底部「确认生效」，系统将按修改后的结果产出本体(nt)与词典(lexicon)。
    </div>

    <!-- 统计概览 -->
    <div class="stats">
      <span class="stat">实体 <b>{entityCount}</b></span>
      <span class="stat">属性 <b>{attrCount}</b></span>
      <span class="stat">关系 <b>{schema.relations.length}</b></span>
      <span class="stat">约束 <b>{schema.constraints.length}</b></span>
      {#if source}<span class="stat stat-src">来源：{source}</span>{/if}
    </div>
    {#if domainOptions.length}
      <div class="domains">业务域：{domainOptions.join('、')}</div>
    {/if}
    {#if suggestNote}<div class="note">{suggestNote}</div>{/if}
  </div>

  <!-- 实体 / 属性 -->
  <div class="card">
    <div class="card-title">实体与属性<span class="title-sub">{entityCount} 个实体</span></div>
    <table class="tbl">
      <thead>
        <tr>
          <th class="w-id">实体名</th>
          <th class="w-label">中文标签</th>
          <th class="w-domain">业务域</th>
          <th class="w-key">主键</th>
          <th class="w-table">数据表</th>
          <th class="w-attr">属性</th>
        </tr>
      </thead>
      <tbody>
        {#each schema.entities as e (e.id)}
          <tr>
            <td><input class="cell" type="text" bind:value={e.id} /></td>
            <td><input class="cell" type="text" placeholder="中文标签" bind:value={e.label} /></td>
            <td>
              <input class="cell" type="text" placeholder="业务域" list="sm-domains" bind:value={e.domain} />
            </td>
            <td><input class="cell cell-mono" type="text" bind:value={e.key} /></td>
            <td class="td-mono">{e.table || '—'}</td>
            <td>
              <button class="btn-mini" onclick={() => toggleAttrs(e.id)}>
                {(e.attributes || []).length} 个 {openAttrs[e.id] ? '▾' : '▸'}
              </button>
            </td>
          </tr>
          {#if openAttrs[e.id]}
            <tr class="row-attrs">
              <td colspan="6">
                <table class="tbl tbl-sub">
                  <thead>
                    <tr><th>字段</th><th>中文标签</th><th>类型</th><th>语义角色</th><th>必填</th></tr>
                  </thead>
                  <tbody>
                    {#each (e.attributes || []) as a, i (i)}
                      <tr>
                        <td class="td-mono">{a.name}</td>
                        <td>{a.label || '—'}</td>
                        <td class="td-mono">{a.type || '—'}</td>
                        <td><span class="chip" class:chip-key={a.role === 'key'}>{roleCn(a.role)}</span></td>
                        <td>{a.required ? '是' : '否'}</td>
                      </tr>
                    {/each}
                  </tbody>
                </table>
                <div class="attr-hint">属性由数据字段自动推断，语义角色（类型列/状态列等）随实体一并提交，不可删除。</div>
              </td>
            </tr>
          {/if}
        {/each}
      </tbody>
    </table>
    <datalist id="sm-domains">
      {#each domainOptions as d}<option value={d}></option>{/each}
    </datalist>
  </div>

  <!-- 关系 -->
  <div class="card">
    <div class="card-title">实体关系<span class="title-sub">{schema.relations.length} 条</span></div>
    {#if schema.relations.length === 0}
      <div class="empty-line">未推断出关系（数据中没有可识别的外键/关联列）</div>
    {:else}
      <table class="tbl">
        <thead>
          <tr><th>关系</th><th>起点</th><th>终点</th><th>外键列</th><th>基数</th></tr>
        </thead>
        <tbody>
          {#each schema.relations as r, i (i)}
            <tr>
              <td><input class="cell" type="text" placeholder="关系名" bind:value={r.label} /></td>
              <td class="td-mono">{r.from || '—'}</td>
              <td class="td-mono">{r.to || '—'}</td>
              <td class="td-mono">{r.fk || '—'}</td>
              <td class="td-mono">{r.cardinality || '—'}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
  </div>

  <!-- 约束 -->
  <div class="card">
    <div class="card-title">约束<span class="title-sub">{schema.constraints.length} 条</span></div>
    {#if schema.constraints.length === 0}
      <div class="empty-line">无约束</div>
    {:else}
      <table class="tbl">
        <thead>
          <tr><th class="w-ctype">类型</th><th>作用对象</th><th>说明</th></tr>
        </thead>
        <tbody>
          {#each schema.constraints as c, i (i)}
            <tr>
              <td class="td-mono">{c.type || '—'}</td>
              <td class="td-mono">{c.on || '—'}</td>
              <td><input class="cell" type="text" placeholder="说明" bind:value={c.msg} /></td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
  </div>

  <!-- 拍板 -->
  <div class="card card-confirm">
    <div class="card-title">确认生效</div>
    <div class="card-desc">
      确认后将按以上内容生成本体（nt）与词典（lexicon），并把知识库「{formKb.trim()}」设为可用，
      随后即可在「查询分析」中问答。此操作会覆盖同名知识库的现有本体与词典。
    </div>
    <div class="actions">
      <button class="btn btn-primary" onclick={doConfirm} disabled={confirmBusy}>
        {confirmBusy ? '⏳ 正在生成本体与词典…' : '✅ 确认生效'}
      </button>
      <button class="btn" onclick={restart} disabled={confirmBusy}>↩ 换数据源重来</button>
      <button class="btn" onclick={doSuggest} disabled={confirmBusy || busy}>
        {busy ? '⏳ 重新建议…' : '🔄 重新获取 AI 建议'}
      </button>
    </div>
    {#if confirmErr}<div class="msg msg-err">{confirmErr}</div>{/if}
  </div>
  {/if}

  <!-- ═══ 第3步：已生效 ═══ -->
  {#if step === 3 && result}
  <div class="card card-done">
    <div class="card-title">🎉 已确认生效</div>
    <div class="card-desc">
      知识库「{result.kb}」的本体与词典已按人工确认的建模结果生成并注册。
    </div>
    <table class="tbl tbl-kv">
      <tbody>
        <tr><th>知识库</th><td class="td-mono">{result.kb}</td></tr>
        <tr><th>状态</th><td><span class="chip chip-ok">{result.status || 'confirmed'}</span></td></tr>
        <tr><th>本体(nt)</th><td class="td-mono">{result.nt || '—'}</td></tr>
        <tr><th>词典(lexicon)</th><td class="td-mono">{result.lexicon || '—'}</td></tr>
        <tr><th>schema</th><td class="td-mono">{result.schema_path || '—'}</td></tr>
        <tr><th>问答</th><td>{result.ask_ready ? '已可问答' : '未就绪'}</td></tr>
      </tbody>
    </table>
    <div class="msg msg-ok">
      {result.ask_ready ? '已可问答：切到「查询分析」即可用自然语言提问该知识库的数据。' : '产物已生成，但后端标记为未就绪，请检查数据目录。'}
    </div>
    <div class="actions">
      <button class="btn btn-primary" onclick={restart}>🧭 再建一个知识库</button>
      <button class="btn" onclick={backToEdit}>✏️ 回到编辑继续调整</button>
    </div>
  </div>
  {/if}
</div>

<style>
  .selfmodel { display: flex; flex-direction: column; gap: 12px; }

  /* ── 步骤条 ── */
  .steps { display: flex; flex-wrap: wrap; gap: 8px; list-style: none; margin: 0; padding: 0; }
  .step {
    display: inline-flex; align-items: center; gap: 8px;
    padding: 6px 14px; font-size: 12px; color: var(--text-secondary);
    background: var(--bg-elevated); border: 1px solid var(--border); border-radius: 999px;
  }
  .step-no {
    display: inline-flex; align-items: center; justify-content: center;
    width: 18px; height: 18px; border-radius: 50%;
    font-size: 11px; font-weight: 700; color: #fff; background: var(--text-muted);
  }
  .step-on { color: var(--brand); background: var(--brand-soft); border-color: var(--brand-line); }
  .step-on .step-no { background: var(--brand); }
  .step-done { color: var(--success-fg); background: var(--success-bg); border-color: var(--border); }
  .step-done .step-no { background: var(--success); }

  /* ── 卡片 ── */
  .card {
    background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius-md);
    padding: 14px; box-shadow: var(--shadow-card);
    display: flex; flex-direction: column; gap: 10px;
  }
  .card-confirm { border-color: var(--brand-line); background: var(--brand-soft); }
  .card-done { border-color: var(--brand-line); }
  .card-title { font-size: 13px; font-weight: 700; color: var(--text-primary); letter-spacing: 0.3px; }
  .title-sub { margin-left: 8px; font-size: 11px; font-weight: 500; color: var(--text-muted); }
  .card-desc { font-size: 12px; color: var(--text-secondary); line-height: 1.7; }

  /* ── 表单 ── */
  .form-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .form-label { width: 88px; font-size: 12px; font-weight: 600; color: var(--text-primary); white-space: nowrap; }
  .input {
    flex: 0 0 260px; padding: 6px 10px; font-size: 13px; color: var(--text-primary);
    border: 1px solid var(--border-strong); border-radius: var(--radius-sm); background: #fff;
  }
  .input:focus { outline: none; border-color: var(--brand); box-shadow: 0 0 0 2px var(--brand-soft); }
  .form-hint { font-size: 11px; color: var(--text-muted); }

  /* ── 按钮 ── */
  .actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .btn {
    padding: 6px 16px; font-size: 13px; color: var(--text-primary);
    background: var(--bg-card); border: 1px solid var(--border-strong); border-radius: var(--radius-sm);
    cursor: pointer; transition: background 150ms ease-out, border-color 150ms ease-out;
  }
  .btn:hover:not(:disabled) { background: var(--bg-hover); border-color: var(--brand-line); }
  .btn:disabled { color: var(--text-muted); cursor: not-allowed; background: var(--bg-elevated); }
  .btn-primary { background: var(--brand); border-color: var(--brand); color: #fff; font-weight: 600; }
  .btn-primary:hover:not(:disabled) { background: var(--brand-dark); border-color: var(--brand-dark); }
  .btn-primary:disabled { background: var(--brand-line); border-color: var(--brand-line); color: #fff; }
  .btn-mini {
    padding: 3px 10px; font-size: 12px; color: var(--brand); background: var(--brand-soft);
    border: 1px solid var(--brand-line); border-radius: var(--radius-sm); cursor: pointer; white-space: nowrap;
  }
  .btn-mini:hover { background: #fff; }
  .busy-hint { font-size: 12px; color: var(--text-muted); }

  /* ── 提示 ── */
  .msg { font-size: 12px; padding: 6px 10px; border-radius: var(--radius-sm); border: 1px solid var(--border); line-height: 1.6; }
  .msg-err { color: var(--danger-fg); background: var(--danger-bg); }
  .msg-ok { color: var(--success-fg); background: var(--success-bg); }
  .note { font-size: 11px; color: var(--text-muted); }
  .empty-line { font-size: 12px; color: var(--text-muted); padding: 8px 0; }

  /* ── 统计 ── */
  .stats { display: flex; gap: 18px; flex-wrap: wrap; font-size: 12px; color: var(--text-secondary); }
  .stat b { color: var(--brand); font-size: 14px; margin-left: 2px; }
  .stat-src { color: var(--text-muted); }
  .domains { font-size: 12px; color: var(--text-secondary); }

  /* ── 表格 ── */
  .tbl { width: 100%; border-collapse: collapse; }
  .tbl thead th {
    padding: 8px 8px; font-size: 12px; font-weight: 600; text-align: left;
    color: var(--text-secondary); background: var(--bg-elevated); border-bottom: 1px solid var(--border);
  }
  .tbl tbody td {
    padding: 6px 8px; font-size: 13px; color: var(--text-primary);
    border-bottom: 1px solid var(--border); vertical-align: middle;
  }
  .tbl tbody tr:last-child td { border-bottom: none; }
  .tbl tbody tr:hover td { background: var(--bg-hover); }
  .tbl-sub thead th { background: var(--bg-card); font-size: 11px; }
  .tbl-sub tbody td { font-size: 12px; }
  .row-attrs td { background: var(--bg-elevated); }
  .row-attrs:hover td { background: var(--bg-elevated); }
  .tbl-kv { max-width: 720px; }
  .tbl-kv th {
    width: 120px; text-align: left; font-size: 12px; font-weight: 600;
    color: var(--text-secondary); padding: 8px 8px; border-bottom: 1px solid var(--border);
  }
  .tbl-kv td { padding: 8px 8px; border-bottom: 1px solid var(--border); }
  .tbl-kv tr:last-child th, .tbl-kv tr:last-child td { border-bottom: none; }

  .cell {
    width: 100%; padding: 5px 8px; font-size: 13px; color: var(--text-primary);
    border: 1px solid var(--border-strong); border-radius: var(--radius-sm); background: #fff;
  }
  .cell:focus { outline: none; border-color: var(--brand); box-shadow: 0 0 0 2px var(--brand-soft); }
  .cell-mono { font-family: ui-monospace, 'SF Mono', SFMono-Regular, Menlo, Consolas, monospace; }
  .td-mono { font-family: ui-monospace, 'SF Mono', SFMono-Regular, Menlo, Consolas, monospace; color: var(--text-secondary); }
  .attr-hint { margin-top: 6px; font-size: 11px; color: var(--text-muted); }

  .w-id { width: 15%; } .w-label { width: 20%; } .w-domain { width: 15%; }
  .w-key { width: 13%; } .w-table { width: 13%; } .w-attr { width: 12%; } .w-ctype { width: 100px; }

  /* 语义角色标记 */
  .chip {
    display: inline-block; padding: 2px 8px; font-size: 11px;
    border-radius: var(--radius-sm); color: var(--text-secondary);
    background: var(--bg-elevated); border: 1px solid var(--border); white-space: nowrap;
  }
  .chip-key { color: var(--brand); background: var(--brand-soft); border-color: var(--brand-line); font-weight: 600; }
  .chip-ok { color: var(--success-fg); background: var(--success-bg); border-color: var(--border); }

  @media (max-width: 720px) {
    .form-row { align-items: flex-start; }
    .form-label { width: 100%; }
    .input { flex: 1 1 100%; }
    .w-id, .w-label, .w-domain, .w-key, .w-table, .w-attr { width: auto; }
  }
</style>
