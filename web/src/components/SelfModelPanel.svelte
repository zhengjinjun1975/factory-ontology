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

  import { onMount } from 'svelte';
  import {
    suggestSchema, confirmSchema,
    fetchSelfModelCandidates, validateSelfModelDir,
    fetchFsDrives, fetchFsDirs,
  } from '../lib/api.js';

  let { kb = '' } = $props();    // 当前登录企业唯一 kb（只读 prop，作默认值）

  let step = $state(1);          // 1 选数据源 | 2 建议编辑 | 3 已生效
  let formKb = $state('');       // 目标知识库名
  let dataDir = $state('');      // 数据目录（相对 codes/ 或仓库外绝对路径）；不再写死默认
  let kbTouched = $state(false); // 用户是否手改过 kb（改过则不再跟随 prop 覆盖）
  let dirTouched = $state(false);// 用户是否主动选过目录（选过则不再自动改写）

  // ── 候选目录（从真实数据枚举，不写死）与选中校验 ──
  let candidates = $state([]);   // [{path,name,exists,has_csv,csv_count,suggested_kb,registered_by,external}]
  let candRoot = $state('');
  let candLoading = $state(false);
  let candErr = $state('');
  let candOpen = $state(false);  // 候选数据目录：默认收起（一行），点开才展开（不再常驻平铺）
  let validation = $state(null); // validateDataDir 返回的 data
  let newDirOk = $state(false);  // 未匹配到已注册目录（新目录）时，用户显式确认

  // ── 目录浏览器（仅本机）──
  let browserOpen = $state(false);
  let browseDrives = $state([]);
  let browsePath = $state('');
  let browseParent = $state('');
  let browseDirs = $state([]);
  let browseLoading = $state(false);
  let browseErr = $state('');

  let busy = $state(false);      // AI 建议中
  let suggestErr = $state('');
  let confirmErr = $state('');
  let suggestNote = $state('');  // 后端 note（预览未落盘提示）
  let source = $state('');       // 建议来源（auto-inferred 等）

  let schema = $state(null);     // 编辑态 schema {version,name,industry,entities,relations,constraints}
  let stats = $state(null);      // {entities,relations,constraints,domains[]}
  let openAttrs = $state({});    // 实体 id → 属性表是否展开

  // ── ③ 层次与定义确认（人在环）：建议一律先列出、带依据，人工确认后才写入本体 ──
  let hierarchy = $state([]);    // [{name,parent,label,kind,rule,evidence}]（kind: root/domain/stem/entity）
  let definitions = $state([]);  // [{entity,definition,rule,evidence,source}]
  let hierOpen = $state(false);  // 默认收起（一行汇总，点开才展开）
  let hierConfirmed = $state(false);  // 人显式勾选后才允许落库（后端硬校验，未确认必拒）

  // ── ③ 扩展描述项确认（具名子类 HasSubclass，人在环）：与层次/定义同一套纪律 ──
  let subclasses = $state([]);   // [{name,label,parent,kind,column,value,count,rule,evidence}]
  let extOpen = $state(false);   // 默认收起
  let extConfirmed = $state(false);   // 未勾选 → 后端拒绝落库

  let confirmBusy = $state(false);
  let result = $state(null);     // confirm 产物 {kb,schema_path,nt,lexicon,status,ask_ready}

  // 默认 kb 跟随当前企业（用户没手改过时才跟随；重置/换企业后自动对齐）
  $effect(() => {
    const cur = kb;
    if (!kbTouched && cur) { formKb = cur; }
  });

  // ── 候选目录：从真实数据枚举（打开面板即列出），并把默认值推导出来 ──
  onMount(() => { loadCandidates(); });

  // 候选里已被注册的 kb 集合（用于「唯一已注册 KB」默认选中判定）
  const registeredKbs = $derived([...new Set(candidates.flatMap(c => (c.registered_by || [])))]);

  async function loadCandidates() {
    candLoading = true; candErr = '';
    try {
      const res = await fetchSelfModelCandidates();
      if (res && res.ok && Array.isArray(res.candidates)) {
        candidates = res.candidates;
        candRoot = res.root || '';
        await applyDefaultSelection();
      } else {
        candErr = '候选目录读取失败：' + fmtErr(res && res.error);
      }
    } catch (e) {
      candErr = '候选目录读取失败：' + fmtErr(e && e.message ? e.message : e);
    } finally {
      candLoading = false;
    }
  }

  // 默认态推导：① 当前 kb 有注册候选 → 选中它；② 全库唯一已注册 KB → 选中它；
  // ③ 否则留空并提示「请从候选中选择」（绝不回退到写死目录）。
  async function applyDefaultSelection() {
    if (dirTouched) return;
    const curKb = (formKb || kb || '').trim();
    let target = null;
    if (curKb) target = candidates.find(c => (c.registered_by || []).includes(curKb)) || null;
    if (!target && registeredKbs.length === 1) {
      target = candidates.find(c => (c.registered_by || []).includes(registeredKbs[0])) || null;
    }
    if (target) { await selectDir(target.path, { silent: true, kb: curKb || registeredKbs[0] }); }
    else { dataDir = ''; validation = null; }
  }

  // 选中一个数据目录：回填 + 校验 + 反向推导 kb（data_xxx → xxx）
  async function selectDir(dirPath, { silent = false, kb: kbHint = '' } = {}) {
    dataDir = dirPath;
    const k = (kbHint || formKb || '').trim();
    try {
      const res = await validateSelfModelDir(k, dirPath);
      validation = (res && res.ok && res.data) ? res.data : null;
      if (!res || !res.ok) suggestErr = '目录校验失败：' + fmtErr(res && res.error);
      else suggestErr = '';
    } catch (e) {
      validation = null; suggestErr = '目录校验失败：' + fmtErr(e && e.message ? e.message : e);
    }
    newDirOk = false;
    // kb ↔ 目录 双向推导：目录 → 建议 kb（用户没手改过 kb 时才自动填）
    if (validation && validation.suggested_kb && !kbTouched) formKb = validation.suggested_kb;
    if (!silent) dirTouched = true;
  }

  // 输入 kb 名：输入即自动高亮/选中规范化匹配的候选目录（data_<kb> 或该 kb 已注册的目录）
  function onKbInput() {
    kbTouched = true; suggestErr = '';
    const k = formKb.trim().toLowerCase();
    if (!k || dirTouched) return;
    const m = candidates.find(c => (c.suggested_kb || '').toLowerCase() === k) ||
              candidates.find(c => (c.registered_by || []).some(x => String(x).toLowerCase() === k));
    if (m && m.path !== dataDir) selectDir(m.path, { silent: true });
  }

  // 手动输入目录：校验（不改 kbTouched，允许反推 kb）
  function onDirInput() { suggestErr = ''; validation = null; newDirOk = false; }

  // ── 目录浏览器（仅本机可访问；只列目录名，不读文件内容）──
  async function openBrowser() {
    browserOpen = true; browseErr = '';
    if (browseDrives.length === 0) {
      try {
        const d = await fetchFsDrives();
        browseDrives = (d && d.ok && Array.isArray(d.drives)) ? d.drives : [];
      } catch (e) { browseErr = '列盘符失败：' + fmtErr(e && e.message ? e.message : e); }
    }
    const start = (validation && validation.abs) || (dataDir && /^[A-Za-z]:[\\/]/.test(dataDir) ? dataDir : '');
    if (start) await browseTo(start);
    else if (browseDrives.length) await browseTo(browseDrives[0].root);
  }
  async function browseTo(path) {
    browseLoading = true; browseErr = '';
    try {
      const res = await fetchFsDirs(path);
      if (res && res.ok) { browsePath = res.path; browseParent = res.parent || ''; browseDirs = res.dirs || []; }
      else browseErr = '打开目录失败：' + fmtErr(res && res.error);
    } catch (e) { browseErr = '打开目录失败：' + fmtErr(e && e.message ? e.message : e); }
    finally { browseLoading = false; }
  }
  function browseEnter(d) { browseTo(d.path); }                 // 双击进入
  function browsePick(d) {                                      // 选中某目录 → 回填
    browserOpen = false;
    selectDir(d.path);
  }
  function browsePickCurrent() { if (browsePath) browsePick({ path: browsePath }); }
  // 面包屑：当前路径拆段，可点击跳转
  const breadcrumb = $derived(browsePath ? browsePath.replace(/\\/g, '/').split('/').filter(Boolean).map((seg, i, arr) => {
    const prefix = arr.slice(0, i + 1).join('/');
    const p = /^[A-Za-z]:$/.test(prefix) ? prefix + '/' : (browsePath.startsWith('/') ? '/' + prefix : prefix);
    return { name: seg, path: p };
  }) : []);

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
    if (!d) { suggestErr = '请选择数据目录（可从下方真实候选中选择，或点「浏览…」）'; return; }
    // 选中后校验：存在性/可读/含csv/占用冲突/危险路径/外部目录（未校验或已变更则先校验）
    if (!validation || validation.dir !== d) {
      try {
        const vr = await validateSelfModelDir(k, d);
        validation = (vr && vr.ok && vr.data) ? vr.data : null;
      } catch (e) { validation = null; }
    }
    if (validation && validation.blocked) {
      suggestErr = '数据目录校验未通过：' + validation.problems.join('；');
      return;
    }
    // 未匹配到已注册目录 → 显式提示「新目录」，需用户勾选确认（不静默回退到写死值）
    if (validation && validation.match === 'none' && !newDirOk) {
      suggestErr = '该目录未匹配到已注册目录（这是一个新目录）。请勾选「确认使用新目录」后再继续。';
      return;
    }
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
        // ③ 层生建议（类层次 + 实体定义）：逐条带依据，默认收起、待人工确认
        hierarchy = Array.isArray(dd.hierarchy) ? dd.hierarchy : [];
        definitions = Array.isArray(dd.definitions) ? dd.definitions : [];
        hierOpen = false;
        hierConfirmed = false;
        // ③ 扩展描述项建议（具名子类）：同层次/定义，逐条带依据、默认收起、待人工确认
        subclasses = Array.isArray(dd.subclasses) ? dd.subclasses : [];
        extOpen = false;
        extConfirmed = false;
        // 把建议的定义即时落到编辑态实体上（人可在下节改；改的就是最终写库文本）
        const defMap = {};
        for (const d of definitions) defMap[d.entity] = d.definition;
        for (const e of schema.entities) {
          if (defMap[e.id]) e.definition = defMap[e.id];
        }
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
    // ③ 人在环硬门槛：层次与定义必须人工确认后才允许落库（后端同样硬校验，双保险）
    if (!hierConfirmed) {
      confirmErr = '请先在「层次与定义确认」中核对并勾选确认，未确认不许落库。';
      hierOpen = true;
      return;
    }
    const noParent = schema.entities.filter(e => !((e.parent || '').trim())).map(e => e.id);
    if (noParent.length) {
      confirmErr = '以下实体未挂父类（类层次不完整），请先确认/修改：' + noParent.join('、');
      hierOpen = true;
      return;
    }
    // ③ 扩展描述项（具名子类）：有建议则必须显式确认（后端同样硬校验，双保险）
    if (subclasses.length && !extConfirmed) {
      confirmErr = '检测到具名子类(扩展描述项)建议：请在「扩展描述项确认」中核对并勾选确认，未确认不许落库。';
      extOpen = true;
      return;
    }
    confirmBusy = true; confirmErr = '';
    try {
      // 深拷贝回传，确保 attributes（含 role/required）一个字段都不丢；
      // 层次节点同样原样回传（后端以人确认的边为准回写 parent 并落 class_hierarchy）。
      const payload = JSON.parse(JSON.stringify({
        entities: schema.entities,
        relations: schema.relations,
        constraints: schema.constraints,
        version: schema.version || '1.0',
        name: schema.name || 'auto-inferred-ontology',
        industry: k,
        hierarchy: hierarchy,
        subclasses: subclasses,
      }));
      const res = await confirmSchema(k, payload, dataDir.trim(), true, extConfirmed === true);
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
    hierarchy = []; definitions = []; hierOpen = false; hierConfirmed = false;
    subclasses = []; extOpen = false; extConfirmed = false;
    newDirOk = false;
    loadCandidates();   // 重新枚举真实候选并重推默认值
  }
  // 回到第2步继续编辑（已建议过才可用）
  function backToEdit() {
    if (!schema) { restart(); return; }
    step = 2; confirmErr = '';
  }
  function toggleAttrs(id) { openAttrs[id] = !openAttrs[id]; }

  // ── ③ 层次与定义确认：编辑/删除/视图辅助（建议 → 人拍板）──
  const KIND_CN = { root: '根/一级类', domain: '业务域类', stem: '命名词干子类', entity: '实体' };
  const kindCn = (k) => KIND_CN[k] || k || '—';
  const classNodes = $derived(hierarchy.filter(n => n && n.kind && n.kind !== 'entity'));
  const entityNodes = $derived(hierarchy.filter(n => n && n.kind === 'entity'));
  const readyEntityCount = $derived(schema && schema.entities
    ? schema.entities.filter(e => ((e.parent || '').trim())).length : 0);
  const defFilledCount = $derived(schema && schema.entities
    ? schema.entities.filter(e => ((e.definition || '').trim())).length : 0);
  // 扩展描述项：将获得子类的实体数（父实体去重）
  const subParentCount = $derived(new Set(subclasses.map(s => s.parent)).size);

  function entityById(id) {
    return (schema && schema.entities) ? schema.entities.find(e => e.id === id) : null;
  }
  // 修改某实体的父类：同时更新层次边与实体 parent（人改了就是最终写库值）
  function setParent(node, val) {
    node.parent = val;
    const e = entityById(node.name);
    if (e) e.parent = val;
  }
  // 删除一条层次边：类节点 → 子节点上提到该节点的父；实体节点 → 置空（后端会拦下并要求补齐）
  function removeNode(node) {
    if (!node || !node.name) return;
    if (node.kind === 'entity') {
      const e = entityById(node.name);
      if (e) e.parent = '';
      hierarchy = hierarchy.filter(n => n !== node);
      return;
    }
    const up = node.parent;
    for (const n of hierarchy) {
      if (n.parent === node.name) n.parent = up;
    }
    for (const e of (schema && schema.entities) || []) {
      if (e.parent === node.name) e.parent = up || '';
    }
    hierarchy = hierarchy.filter(n => n !== node);
  }
  function resetHierarchyEdits() {
    // 重新拉建议（按钮）；这里只把确认勾选复位，防手滑（未重新建议时提示先重新建议）
    hierConfirmed = false;
  }
  // 人工修改实体定义（改的就是最终写入本体的 definition）
  function setDefinition(eid, val) {
    const e = entityById(eid);
    if (e) e.definition = val;
  }
  const defOf = (eid) => {
    const e = entityById(eid);
    return e ? (e.definition || '') : '';
  };

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

  // 候选是否与当前 kb 冲突（该目录已被「别的」kb 注册）
  function confirmConflict(c) {
    const cur = formKb.trim().toLowerCase();
    if (!cur) return false;
    const regs = (c.registered_by || []).map(x => String(x).toLowerCase());
    return regs.length > 0 && !regs.includes(cur);
  }
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

    <!-- 真实候选（从数据枚举，非写死）：默认收起为一行，点开展开（不再常驻平铺） -->
    <div class="cand-collapse">
      <button class="cand-toggle" class:expanded={candOpen} onclick={() => (candOpen = !candOpen)} type="button">
        <span class="cand-title">候选数据目录（真实枚举）</span>
        <span class="title-sub">
          {#if candLoading}读取中…
          {:else if candidates.length}{candidates.length} 个候选 · 当前：{dataDir || '未选'}
          {:else}无候选{/if}
        </span>
        <span class="chevron">{candOpen ? '▾' : '▸'}</span>
      </button>
      {#if candOpen}
      <div class="cand-body">
        <div class="cand-head">
          <button class="btn-mini" onclick={loadCandidates} disabled={candLoading} type="button">刷新候选</button>
          {#if candLoading}<span class="busy-hint">读取中…</span>{/if}
        </div>
        {#if candErr}<div class="msg msg-err">{candErr}</div>{/if}
        {#if !candLoading && candidates.length === 0 && !candErr}
          <div class="empty-line">未枚举到任何候选数据目录。可点「浏览…」手动选择。</div>
        {/if}
        {#if candidates.length}
          <div class="cand-list">
            {#each candidates as c (c.path)}
              <button
                class="cand" class:cand-on={dataDir === c.path} class:cand-bad={!c.exists || !c.has_csv}
                onclick={() => selectDir(c.path)} type="button"
              >
                <span class="cand-dir">{c.path}</span>
                <span class="chips">
                  <span class="chip" class:chip-bad={!c.exists}>{c.exists ? '存在' : '不存在'}</span>
                  <span class="chip" class:chip-bad={!c.has_csv}>{c.csv_count} 个 CSV</span>
                  {#if c.registered_by && c.registered_by.length}
                    <span class="chip chip-ok">已被 {c.registered_by.join('、')} 注册</span>
                  {:else}
                    <span class="chip">未注册</span>
                  {/if}
                  {#if c.suggested_kb}<span class="chip chip-key">建议 kb：{c.suggested_kb}</span>{/if}
                  {#if c.external}<span class="chip chip-warn">外部数据目录</span>{/if}
                  {#if confirmConflict(c)}<span class="chip chip-warn">与当前 kb 冲突</span>{/if}
                </span>
              </button>
            {/each}
          </div>
        {/if}
        <div class="form-hint">候选来源：kbs.json 已注册 data_dir + 仓库内真实 data* 目录；默认值由当前 kb 自动匹配推导，不再写死。</div>
      </div>
      {/if}
    </div>

    <div class="form-row">
      <label class="form-label" for="sm-kb">知识库名称</label>
      <input
        id="sm-kb" class="input" type="text" placeholder="如 valve3"
        bind:value={formKb}
        oninput={onKbInput}
        onkeydown={(e) => { if (e.key === 'Enter') doSuggest(); }}
      />
      <span class="form-hint">输入后自动匹配 data_&lt;kb&gt; 候选</span>
    </div>
    <div class="form-row">
      <label class="form-label" for="sm-dir">数据目录</label>
      <input
        id="sm-dir" class="input" type="text" placeholder="从候选选择，或点「浏览…」"
        bind:value={dataDir}
        oninput={onDirInput}
        onkeydown={(e) => { if (e.key === 'Enter') doSuggest(); }}
      />
      <button class="btn" onclick={openBrowser} type="button">浏览…</button>
      <span class="form-hint">相对 codes/（如 data_valve）或仓库外绝对路径</span>
    </div>

    <!-- 选中校验证据 -->
    {#if validation}
      <div class="val" class:val-bad={validation.blocked}>
        <div class="val-line">
          <span class="chip" class:chip-bad={!validation.exists}>{validation.exists ? '✔ 存在' : '✘ 不存在'}</span>
          <span class="chip" class:chip-bad={!validation.readable}>可读：{validation.readable ? '是' : '否'}</span>
          <span class="chip" class:chip-bad={!validation.has_csv}>CSV：{validation.csv_count} 个</span>
          {#if validation.registered_by && validation.registered_by.length}
            <span class="chip chip-ok">已注册：{validation.registered_by.join('、')}</span>
          {:else}<span class="chip">未注册</span>{/if}
          {#if validation.occupied_by && validation.occupied_by.length}
            <span class="chip chip-bad">冲突：被 {validation.occupied_by.join('、')} 占用</span>
          {/if}
          {#if validation.suggested_kb}<span class="chip chip-key">建议 kb：{validation.suggested_kb}</span>{/if}
          {#if validation.external}<span class="chip chip-warn">外部数据目录</span>{/if}
          <span class="chip">匹配：{validation.match}</span>
        </div>
        {#if validation.problems && validation.problems.length}
          <div class="msg msg-err">校验问题：{validation.problems.join('；')}</div>
        {/if}
        {#if validation.match === 'none'}
          <div class="msg msg-warn">未匹配到已注册目录，这是一个新目录。请确认后继续。</div>
          <label class="ack"><input type="checkbox" bind:checked={newDirOk} /> 确认使用新目录（{validation.dir}）</label>
        {/if}
      </div>
    {/if}
    {#if dataDir && !validation}
      <div class="msg msg-warn">该目录尚未校验：点「AI 建议」时会自动校验 存在性/可读性/是否含 CSV/是否被占用/危险路径。</div>
    {/if}

    <div class="actions">
      <button class="btn btn-primary" onclick={doSuggest} disabled={busy}>
        {busy ? '正在分析数据…' : 'AI 建议'}
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

  <!-- ═══ ③ 层次与定义确认（人在环；默认收起，一行汇总，点开才展开）═══ -->
  <div class="card card-hier">
    <div class="cand-head">
      <span class="card-title">层次与定义确认（建议）</span>
      <span class="title-sub">类层次 {classNodes.length} 个类 · 实体挂载 {readyEntityCount}/{entityCount} · 定义 {defFilledCount}/{entityCount}</span>
      {#if hierConfirmed}
        <span class="chip chip-ok">已确认</span>
      {:else}
        <span class="chip chip-warn">未确认（不落库）</span>
      {/if}
      <button class="btn-mini" onclick={() => (hierOpen = !hierOpen)} type="button">
        {hierOpen ? '▾ 收起' : '▸ 展开'}
      </button>
    </div>
    {#if !hierOpen}
      <div class="note">
        系统按业务域归属 + 命名词干派生类层次，并按字段/取值生成实体定义；每条都带依据。展开核对后再确认。
      </div>
    {:else}
      <div class="card-desc">
        以下为**建议**（纯规则派生，同输入同输出），逐条附依据。可改名/改用父类/删除；<b>勾选确认后才会写入本体</b>，
        未确认不许落库。
      </div>

      <div class="cand-head"><span class="cand-title">类层次（根 / 业务域 / 命名词干子类）</span></div>
      {#if classNodes.length === 0}
        <div class="empty-line">无类节点（异常：至少应有根类）。</div>
      {:else}
        <table class="tbl">
          <thead>
            <tr><th class="w-kind">类型</th><th>类名</th><th>上属类</th><th>规则</th><th>依据</th><th class="w-op">操作</th></tr>
          </thead>
          <tbody>
            {#each classNodes as n (n.name)}
              <tr>
                <td><span class="chip">{kindCn(n.kind)}</span></td>
                <td class="td-mono">{n.name}</td>
                <td class="td-mono">{n.parent || '—（根）'}</td>
                <td class="td-mono">{n.rule || '—'}</td>
                <td class="ev">{n.evidence || '—'}</td>
                <td><button class="btn-mini" onclick={() => removeNode(n)} type="button">删除</button></td>
              </tr>
            {/each}
          </tbody>
        </table>
      {/if}

      <div class="cand-head"><span class="cand-title">实体挂载（→ 上属类）</span></div>
      {#if entityNodes.length === 0}
        <div class="empty-line">无实体层次建议。</div>
      {:else}
        <table class="tbl">
          <thead>
            <tr><th>实体</th><th class="w-label">中文标签</th><th class="w-parent">上属类（可改）</th><th>规则</th><th>依据</th><th class="w-op">操作</th></tr>
          </thead>
          <tbody>
            {#each entityNodes as n (n.name)}
              <tr>
                <td class="td-mono">{n.name}</td>
                <td>{n.label || '—'}</td>
                <td>
                  <input class="cell cell-mono" type="text" placeholder="上属类"
                         value={n.parent || ''} oninput={(e) => setParent(n, e.currentTarget.value)} />
                </td>
                <td class="td-mono">{n.rule || '—'}</td>
                <td class="ev">{n.evidence || '—'}</td>
                <td><button class="btn-mini" onclick={() => removeNode(n)} type="button">删除</button></td>
              </tr>
            {/each}
          </tbody>
        </table>
      {/if}

      <div class="cand-head"><span class="cand-title">实体 Definition（可改）</span></div>
      {#if definitions.length === 0}
        <div class="empty-line">无定义建议。</div>
      {:else}
        <table class="tbl">
          <thead>
            <tr><th>实体</th><th>定义（可改）</th><th>依据</th></tr>
          </thead>
          <tbody>
            {#each definitions as d (d.entity)}
              <tr>
                <td class="td-mono">{d.entity}</td>
                <td>
                  <input class="cell" type="text" placeholder="定义"
                         value={defOf(d.entity)} oninput={(e) => setDefinition(d.entity, e.currentTarget.value)} />
                </td>
                <td class="ev">[{d.rule}] {d.evidence}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      {/if}

      <label class="ack">
        <input type="checkbox" bind:checked={hierConfirmed} />
        我已核对/修改以上层次与定义，确认写入本体（未确认则「确认生效」会被拒绝）
      </label>
      {#if !hierConfirmed}
        <div class="msg msg-warn">未确认：点「确认生效」将不会落库（不静默通过）。</div>
      {/if}
    {/if}
  </div>

  <!-- ═══ ③ 扩展描述项确认（具名子类 HasSubclass；人在环，默认收起，一行汇总）═══ -->
  <div class="card card-hier">
    <div class="cand-head">
      <span class="card-title">扩展描述项确认（具名子类）</span>
      <span class="title-sub">{subclasses.length} 条子类建议 · {subParentCount} 个实体将获得子类</span>
      {#if extConfirmed}
        <span class="chip chip-ok">已确认</span>
      {:else}
        <span class="chip chip-warn">未确认（不落库）</span>
      {/if}
      <button class="btn-mini" onclick={() => (extOpen = !extOpen)} type="button">
        {extOpen ? '▾ 收起' : '▸ 展开'}
      </button>
    </div>
    {#if !extOpen}
      <div class="note">
        系统按**真实分类列**（type/category/device_type/…）的**真实取值**派生具名子类(HasSubclass)，逐条带依据。展开核对后再确认。
      </div>
    {:else}
      <div class="card-desc">
        以下为**建议**（纯规则派生，同输入同输出），逐条附依据：依据哪张表、哪个字段、哪个真实取值、命中多少行。
        <b>勾选确认后才会写入本体</b>；不猜同义、不伪造关系；未确认不许落库。
      </div>
      {#if subclasses.length === 0}
        <div class="empty-line">未派生出具名子类（数据中没有可枚举的分类列，或实体本身已是子类）。</div>
      {:else}
        <table class="tbl">
          <thead>
            <tr><th>子类</th><th class="w-label">中文标签</th><th>父实体</th><th>分类列</th><th>取值</th><th>命中</th><th>依据</th></tr>
          </thead>
          <tbody>
            {#each subclasses as s (s.name)}
              <tr>
                <td class="td-mono">{s.name}</td>
                <td>{s.label || '—'}</td>
                <td class="td-mono">{s.parent}</td>
                <td class="td-mono">{s.column || '—'}</td>
                <td>{s.value || '—'}</td>
                <td class="td-mono">{s.count}</td>
                <td class="ev">[{s.rule}] {s.evidence}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      {/if}
      <label class="ack">
        <input type="checkbox" bind:checked={extConfirmed} />
        我已核对以上具名子类（扩展描述项），确认写入本体（未确认则「确认生效」会被拒绝）
      </label>
      {#if !extConfirmed}
        <div class="msg msg-warn">未确认：点「确认生效」将不会落库（不静默通过）。</div>
      {/if}
    {/if}
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
        {confirmBusy ? '正在生成本体与词典…' : '确认生效'}
      </button>
      <button class="btn" onclick={restart} disabled={confirmBusy}>换数据源重来</button>
      <button class="btn" onclick={doSuggest} disabled={confirmBusy || busy}>
        {busy ? '重新建议…' : '重新获取 AI 建议'}
      </button>
    </div>
    {#if confirmErr}<div class="msg msg-err">{confirmErr}</div>{/if}
  </div>
  {/if}

  <!-- ═══ 第3步：已生效 ═══ -->
  {#if step === 3 && result}
  <div class="card card-done">
    <div class="card-title">已确认生效</div>
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
      <button class="btn btn-primary" onclick={restart}>再建一个知识库</button>
      <button class="btn" onclick={backToEdit}>回到编辑继续调整</button>
    </div>
  </div>
  {/if}

  <!-- ═══ 目录浏览器（仅本机可访问；只列目录名，不读文件内容）═══ -->
  {#if browserOpen}
  <div class="modal-mask" role="presentation">
    <div class="modal" role="dialog" tabindex="-1" aria-label="选择数据目录">
      <div class="modal-title">浏览目录（仅本机可访问）</div>
      <div class="drives">
        {#each browseDrives as dr (dr.root)}
          <button class="btn-mini" class:chip-key={browsePath === dr.root} onclick={() => browseTo(dr.root)} type="button">{dr.label}</button>
        {/each}
      </div>
      <div class="crumb">
        {#each breadcrumb as b (b.path)}
          <button class="crumb-seg" onclick={() => browseTo(b.path)} type="button">{b.name}</button>
          <span class="crumb-sep">/</span>
        {/each}
      </div>
      <div class="modal-actions">
        <button class="btn" onclick={() => browseTo(browseParent)} disabled={!browseParent || browseLoading} type="button">返回上级</button>
        <button class="btn btn-primary" onclick={browsePickCurrent} disabled={!browsePath} type="button">选择当前目录</button>
        <button class="btn" onclick={() => (browserOpen = false)} type="button">关闭</button>
      </div>
      {#if browseErr}<div class="msg msg-err">{browseErr}</div>{/if}
      <div class="modal-list">
        {#if browseLoading}<div class="empty-line">读取中…</div>{/if}
        {#if !browseLoading && browseDirs.length === 0}<div class="empty-line">该目录下没有子目录</div>{/if}
        {#each browseDirs as d (d.path)}
          <div class="bdir" role="button" tabindex="0" ondblclick={() => browseEnter(d)} onkeydown={(e) => { if (e.key === 'Enter') browseEnter(d); }}>
            <button class="bdir-name" onclick={() => browseEnter(d)} type="button">{d.name}</button>
            <span class="chips">
              {#if d.has_csv}<span class="chip chip-ok">{d.csv_count} 个 CSV</span>{/if}
              {#if d.registered_by && d.registered_by.length}<span class="chip">已注册：{d.registered_by.join('、')}</span>{/if}
              {#if d.suggested_kb}<span class="chip chip-key">建议 kb：{d.suggested_kb}</span>{/if}
            </span>
            <button class="btn-mini" onclick={() => browsePick(d)} type="button">选此目录</button>
          </div>
        {/each}
      </div>
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
  .card-hier { border-color: var(--brand-line); }
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
  .w-kind { width: 96px; } .w-op { width: 64px; } .w-parent { width: 16%; }
  /* 依据列：小字、可换行，不喧宾夺主 */
  .ev { font-size: 11px; color: var(--text-muted); line-height: 1.5; }

  /* 语义角色标记 */
  .chip {
    display: inline-block; padding: 2px 8px; font-size: 11px;
    border-radius: var(--radius-sm); color: var(--text-secondary);
    background: var(--bg-elevated); border: 1px solid var(--border); white-space: nowrap;
  }
  .chip-key { color: var(--brand); background: var(--brand-soft); border-color: var(--brand-line); font-weight: 600; }
  .chip-ok { color: var(--success-fg); background: var(--success-bg); border-color: var(--border); }
  .chip-bad { color: var(--danger-fg); background: var(--danger-bg); border-color: var(--border); }
  .chip-warn { color: #8a5a00; background: #fff7e0; border-color: #f0d68a; }
  .chips { display: inline-flex; flex-wrap: wrap; gap: 4px; align-items: center; }

  /* ── 候选目录列表（真实枚举；默认收起为一行，点开展开）── */
  .cand-collapse { border: 1px solid var(--border); border-radius: var(--radius-sm); overflow: hidden; }
  .cand-toggle {
    width: 100%; display: flex; align-items: center; gap: 10px; text-align: left;
    padding: 8px 10px; background: var(--bg-elevated); border: none;
    font: inherit; cursor: pointer; transition: background 150ms ease-out;
  }
  .cand-toggle:hover { background: var(--bg-hover); }
  .cand-toggle.expanded { box-shadow: inset 3px 0 0 0 var(--brand); }
  .cand-body { padding: 8px 10px; display: flex; flex-direction: column; gap: 8px; border-top: 1px solid var(--border); }
  .chevron { margin-left: auto; color: var(--text-muted); font-size: 12px; }
  .cand-head { display: flex; align-items: center; gap: 8px; }
  .cand-title { font-size: 12px; font-weight: 600; color: var(--text-primary); }
  .cand-list {
    display: flex; flex-direction: column; gap: 6px; max-height: 260px; overflow: auto;
    border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 6px; background: var(--bg-elevated);
  }
  .cand {
    display: flex; align-items: center; justify-content: space-between; gap: 10px; width: 100%;
    text-align: left; padding: 6px 10px; background: var(--bg-card); border: 1px solid var(--border);
    border-radius: var(--radius-sm); cursor: pointer; flex-wrap: wrap;
  }
  .cand:hover { border-color: var(--brand-line); }
  .cand-on { border-color: var(--brand); box-shadow: 0 0 0 2px var(--brand-soft); }
  .cand-bad .cand-dir { color: var(--text-muted); text-decoration: line-through; }
  .cand-dir { font-family: ui-monospace, 'SF Mono', Menlo, Consolas, monospace; font-size: 12px; color: var(--text-primary); }

  /* ── 选中校验证据 ── */
  .val { display: flex; flex-direction: column; gap: 6px; padding: 8px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--bg-elevated); }
  .val-bad { border-color: var(--danger-fg); }
  .val-line { display: flex; flex-wrap: wrap; gap: 6px; }
  .msg-warn { color: #8a5a00; background: #fff7e0; border-color: #f0d68a; }
  .ack { font-size: 12px; color: var(--text-primary); display: flex; align-items: center; gap: 6px; }

  /* ── 目录浏览器弹窗（仅本机）── */
  .modal-mask { position: fixed; inset: 0; background: rgba(0,0,0,0.35); display: flex; align-items: center; justify-content: center; z-index: 50; }
  .modal {
    width: min(760px, 94vw); max-height: 80vh; overflow: auto; background: var(--bg-card);
    border: 1px solid var(--border); border-radius: var(--radius-md); box-shadow: var(--shadow-card);
    padding: 14px; display: flex; flex-direction: column; gap: 10px;
  }
  .modal-title { font-size: 13px; font-weight: 700; color: var(--text-primary); }
  .drives { display: flex; flex-wrap: wrap; gap: 6px; }
  .crumb {
    display: flex; flex-wrap: wrap; align-items: center; gap: 2px; font-size: 12px;
    background: var(--bg-elevated); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 4px 8px;
  }
  .crumb-seg { background: none; border: none; color: var(--brand); cursor: pointer; font-size: 12px; padding: 1px 3px; }
  .crumb-seg:hover { text-decoration: underline; }
  .crumb-sep { color: var(--text-muted); }
  .modal-actions { display: flex; flex-wrap: wrap; gap: 8px; }
  .modal-list {
    display: flex; flex-direction: column; gap: 4px; border: 1px solid var(--border);
    border-radius: var(--radius-sm); padding: 6px; max-height: 44vh; overflow: auto;
  }
  .bdir { display: flex; align-items: center; gap: 8px; padding: 4px 6px; border-radius: var(--radius-sm); flex-wrap: wrap; }
  .bdir:hover { background: var(--bg-hover); }
  .bdir-name { background: none; border: none; cursor: pointer; font-size: 13px; color: var(--text-primary); text-align: left; padding: 2px 4px; }
  .bdir-name:hover { color: var(--brand); }

  @media (max-width: 720px) {
    .form-row { align-items: flex-start; }
    .form-label { width: 100%; }
    .input { flex: 1 1 100%; }
    .w-id, .w-label, .w-domain, .w-key, .w-table, .w-attr { width: auto; }
  }
</style>
