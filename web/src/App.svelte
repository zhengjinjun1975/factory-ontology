<script>
  // 工厂智能体 · 本体问答 — 独立 Web 应用（工业软件浅色风格）
  import { onMount } from 'svelte';
  import { setupOntologyMulti, dbSetup, askOntology, analyzeOntology, setModel, getModels, saveModels, fetchVersion, browseFiles, readDataFile, fetchExample, fetchKbs, setKb, fetchEnterprise, saveEnterprise, authLogin, authRegister, authMe, authLogout, onboardEnterprise, resetEnterprise, getToken, setToken, fetchIndustries, switchIndustry } from './lib/api.js';
  import DashboardPanel from './components/DashboardPanel.svelte';
  import ModelGraph from './components/ModelGraph.svelte';
  import WelcomeModel from './components/WelcomeModel.svelte';
  import AnalysisResult from './components/AnalysisResult.svelte';
  import EvalPanel from './components/EvalPanel.svelte';
  import KnowledgePanel from './components/KnowledgePanel.svelte';
  import AssetPanel from './components/AssetPanel.svelte';

  // ─── 企业用户登录态 ───
  let user = $state(null);        // {username, enterpriseName, logo, industry, kb, onboarded}
  let authLoading = $state(true); // 启动时校验会话中
  let loginMode = $state('login'); // login | register
  let loginUser = $state('');     // 登录表单
  let loginPass = $state('');
  let loginErr = $state('');
  let loginBusy = $state(false);

  // 是否已登录
  const isAuthed = $derived(!!user);
  // 是否已引导配置（未配置 → 引导 onboarding）
  const needsOnboard = $derived(!!user && !user.onboarded);
  // 单企业收敛：当前企业唯一 kb = 登录用户的 kb
  const currentKb = $derived((user && user.kb) || '');
  // 本地文件建模的目标 kb: currentKb 优先; 为空(如重置后/未绑定)则用用户 industry 对应 kb
  function resolveTargetKb() {
    if (currentKb) return currentKb;
    if (user && user.industry) {
      const ind = INDUSTRIES.find(i => i.name === user.industry);
      if (ind && ind.kb) return ind.kb;
    }
    return '';
  }

  // 登录
  async function doLogin() {
    if (loginBusy) return;
    const u = loginUser.trim(), p = loginPass;
    if (!u || !p) { loginErr = '请输入用户名和密码'; return; }
    loginBusy = true; loginErr = '';
    try {
      const res = await authLogin(u, p);
      if (!res.ok) { loginErr = res.error || '登录失败'; }
      else { if (await loadSessionUser()) await initAppData(); }
    } catch (e) { loginErr = String(e && e.message ? e.message : e); }
    finally { loginBusy = false; }
  }
  // 注册企业用户
  async function doRegister() {
    if (loginBusy) return;
    const u = loginUser.trim(), p = loginPass;
    if (!u || !p) { loginErr = '请输入用户名和密码'; return; }
    loginBusy = true; loginErr = '';
    try {
      const res = await authRegister({ username: u, password: p });
      if (!res.ok) { loginErr = res.error || '注册失败'; }
      else { if (await loadSessionUser()) await initAppData(); }
    } catch (e) { loginErr = String(e && e.message ? e.message : e); }
    finally { loginBusy = false; }
  }
  // 退出登录
  async function doLogout() {
    await authLogout();
    user = null; loginUser = ''; loginPass = ''; loginErr = '';
    activeTab = 'model'; answer = ''; analysis = null; modelResult = null;
  }
  // 从会话读取当前用户
  async function loadSessionUser() {
    try {
      const res = await authMe();
      if (res.ok && res.user) {
        user = res.user;
        return true;
      }
      user = null;
    } catch (e) { user = null; }
    return false;
  }

  // ─── 引导 onboarding（新企业未配置时）───
  // 步骤: 1 确认企业(名/logo/行业) → 2 用所选行业示例建本体 → 3 完成解锁
  let onboardStep = $state(1);          // 1 企业信息 | 2 建本体 | 3 完成
  let onboardForm = $state({ name: '', logo: '', industry: '' });
  let onboardBusy = $state(false);
  let onboardErr = $state('');
  // 一企业一行业一数据唯一性：企业/行业在步骤1已锁定，步骤2不再重复选择
  // ─── 行业清单（数据驱动，事件驱动无死角）───
  // 行业下拉/建模目标不再硬编码 7/9 个，而是从后端 kbs.json 全量动态读取 {kb,name,icon,dir}。
  let industries = $state([]);        // 全部可建模行业 [{kb,name,icon,dir}]
  let industriesLoaded = $state(false);
  async function loadIndustries() {
    try {
      const res = await fetchIndustries();
      if (res && res.ok && Array.isArray(res.industries)) {
        industries = res.industries;
        industriesLoaded = true;
      }
    } catch (e) { /* 后端不可达则降级为空，行业下拉暂空 */ }
  }
  // 行业名(中文) → 示例数据目录(data_xxx)；找不到时兜底默认阀门
  const industryDirByName = $derived((() => {
    const m = {};
    for (const i of industries) m[i.name] = i.dir;
    return m;
  })());
  const onboardDataDir = $derived(industryDirByName[onboardForm.industry] || 'data_valve');
  const onboardInd = $derived(industries.find(i => i.dir === onboardDataDir));
  // 行业名列表（下拉选项，全量动态）
  const INDUSTRY_OPTIONS = $derived(industries.map(i => i.name));

  // 进入 onboarding 时预填当前用户企业信息
  function initOnboard() {
    onboardForm = { name: (user && user.enterpriseName) || '', logo: (user && user.logo) || '🏭', industry: (user && user.industry) || '' };
    onboardStep = 1; onboardErr = '';
  }

  // 步骤1：保存企业信息（确认企业名/logo/行业）
  async function onboardSaveEnterprise() {
    if (onboardBusy) return;
    const name = onboardForm.name.trim();
    if (!name) { onboardErr = '请输入企业名称'; return; }
    onboardBusy = true; onboardErr = '';
    try {
      const res = await saveEnterprise({ name, logo: onboardForm.logo, industry: onboardForm.industry });
      if (res && res.ok && res.data) {
        user = { ...user, enterpriseName: res.data.enterpriseName, logo: res.data.logo, industry: res.data.industry };
        onboardStep = 2;
      } else {
        onboardErr = (res && res.error) || '保存失败';
      }
    } catch (e) { onboardErr = String(e && e.message ? e.message : e); }
    finally { onboardBusy = false; }
  }

  // 步骤2：用步骤1锁定的行业 → 自动建模（后端读该行业数据目录建本体，事件驱动无死角）
  async function onboardBuild() {
    if (onboardBusy) return;
    onboardBusy = true; onboardErr = '';
    setStatus('info', '正在为当前企业建本体…');
    try {
      const industry = onboardForm.industry;
      if (!industry) { onboardErr = '请选择行业'; onboardBusy = false; return; }
      const ind = industries.find(i => i.name === industry);
      if (!ind) { onboardErr = `未知行业：${industry}`; onboardBusy = false; return; }
      // 显式行业切换：后端用该行业数据自动建模并联动企业 kb/激活 kb
      const res = await switchIndustry(industry);
      if (!res.ok) {
        const e = res && res.error;
        onboardErr = typeof e === 'string' ? e : (e && (e.message || e.error)) || '建本体失败';
        onboardBusy = false;
        return;
      }
      else {
        modelResult = { table: res.table || ind.kb, attrs: res.attrs || [], tables: 0, ts: Date.now() };
        status = 'ready';
        // 一企业一行业一数据：企业唯一 kb 用实际建成的行业 kb
        const builtKb = res.industry && res.industry.kb ? res.industry.kb : ind.kb;
        const ob = await onboardEnterprise({ name: (user && user.enterpriseName) || '', logo: (user && user.logo) || '', industry, kb: builtKb });
        if (ob.ok && ob.data) user = ob.data; else user = { ...user, kb: builtKb, industry };
        await setKb(builtKb);
        await loadKbs();
        onboardStep = 3;
      }
    } catch (e) { onboardErr = String(e && e.message ? e.message : e); }
    finally { onboardBusy = false; }
  }

  // 完成 onboarding → 解锁进入系统
  function onboardFinish() {
    user = { ...user, onboarded: true };
    activeTab = 'model';
  }

  // 企业重置：清空当前企业数据 → 重新 onboarding
  let resetOpen = $state(false);
  let resetBusy = $state(false);
  async function doResetEnterprise() {
    if (resetBusy) return;
    resetBusy = true;
    try {
      const res = await resetEnterprise();
      if (res && res.ok && res.data) {
        user = res.data;   // 变为未配置 → 前端进入引导 onboarding
        initOnboard();
        setStatus('ok', '企业数据已重置，请重新引导配置');
      } else {
        setStatus('err', (res && res.error) || '重置失败');
      }
    } catch (e) { setStatus('err', String(e && e.message ? e.message : e)); }
    finally { resetBusy = false; resetOpen = false; }
  }

  // ─── 状态 ───
  let activeTab = $state('model');   // model | query | dashboard | eval | knowledge | assets
  let showModel = $state(false);      // 数据建模右栏：默认欢迎界面，点"显示本体模型"才显示模型图
  // ─── 文件浏览框（学习 solo-agent-kit /api/browse，默认 data_valve 示例目录）───
  let browseDir = $state('data_valve');    // 当前目录（相对 codes/）
  let browseParent = $state('');           // 上级目录（空=根）
  let browseDirs = $state([]);             // [{path,name}] 子目录
  let browseFileList = $state([]);         // [{path,name}] 数据文件
  let browseLoading = $state(false);
  let browseChecked = $state([]);          // [{path,name}] 已选文件
  let browseBusy = $state(false);
  let modelResult = $state(null);   // {table, attrs}
  // 本地文件多选建模
  let localFiles = $state([]);       // [{name, content}] 本地选择文件
  let localBusy = $state(false);
  let defaultBusy = $state(false);   // 默认示例建模中
  // 行业示例：动态来自后端 kbs.json（事件驱动无死角，不再硬编码 9 个）。
  // 行业列表 = industries（[kb,name,icon,dir]）；兼容旧引用：INDUSTRIES 统一指向 industries。
  const INDUSTRIES = $derived(industries);
  let defaultIndustry = $state('data_valve'); // 当前选中的行业示例
  // 本地文件建模折叠区（默认收起，保持左侧干净大功能卡排队）
  let localModelOpen = $state(false);
  // 数据库接入
  let dbOpen = $state(false);
  let dbBusy = $state(false);
  let dbResult = $state(null);      // {ok, table?, output?, error?}
  let dbForm = $state({ db_type: 'mysql', host: '127.0.0.1', port: '3306', user: '', password: '', database: '', tables: '' });
  let question = $state('');
  let asking = $state(false);
  let answer = $state('');
  let answerHTML = $state(null);   // 结构化答案 HTML（列表/表格）；null 则退回 <pre>
  let evidence = $state(null);     // 问答证据溯源
  let evidenceOpen = $state(false);
  let analysis = $state(null);       // {report, stats} 智能分析结果
  let modelList = $state([]);        // 可用模型
  let activeModel = $state('');      // 当前生效模型 key
  let modelOpen = $state(false);     // 模型管理折叠区
  let modelEditBusy = $state(false);
  let editModels = $state([]);       // 可编辑模型配置 [{key,name,type,base_url,model,api_key}]
  let editActive = $state('');       // 编辑态 active
  let editEmbedding = $state({ name: '', type: 'ollama', base_url: 'http://127.0.0.1:11434', model: 'nomic-embed-text' }); // 向量检索(embedding)模型
  let appVersion = $state('');       // 代码版本(读后端)
  let status = $state('idle');       // idle | modeling | ready | asking
  let statusMsg = $state('等待数据导入');
  let statusType = $state('info');   // info | ok | err
  let answerBox = $state(null);
  // 单企业收敛：不保留可切换的多 kb 列表，只跟随登录企业唯一 kb。
  let kbList = $state([]);        // 仅含当前企业 kb（[{key, name, icon, examples}]）
  let kbsLoaded = $state(false);

  // ─── 企业设置（单企业：品牌来自登录用户 enterpriseName/logo/industry）───
  let entOpen = $state(false);              // 企业设置面板开关
  let entForm = $state({ name: '', logo: '', industry: '' });  // 编辑态表单
  let entBusy = $state(false);
  let entErr = $state('');
  let entOk = $state('');
  // 可选的 logo emoji 预设（企业 logo 快速选择）
  const LOGO_EMOJIS = ['🏭', '🏢', '💼', '🔧', '🧪', '⚙️', '🔩', '🛠️', '🌍', '🌿', '🚢', '🌀', '🎯', '🤖', '⚡', '📚', '🥛', '🏗️'];

  // logo 是否图片（dataURL/URL/相对路径）→ 渲染 <img>；否则按 emoji 文本渲染
  const isImgLogo = (l) => !!l && /^(data:image\/|https?:\/\/|\/)/i.test(l);
  // 顶部品牌名/logo：来自登录企业用户（单企业唯一）
  const brandName = $derived(user && user.enterpriseName ? `${user.enterpriseName} · 本体问答` : '工厂智能体 · 本体问答');
  const brandLogo = $derived((user && user.logo) || '🏭');

  // 从后端读取企业配置（onMount 时调用）—— 现在来自 /api/auth/me 的 user
  async function loadEnterprise() {
    try {
      const res = await fetchEnterprise();
      if (res && res.ok && res.data) {
        const d = res.data;
        entForm = { name: d.name || '', logo: d.logo || '', industry: d.industry || '' };
      }
    } catch (e) { /* 后端不可达则回退默认 */ }
  }

  function openEnterprise() {
    entForm = { name: (user && user.enterpriseName) || '', logo: (user && user.logo) || '', industry: (user && user.industry) || '' };
    entErr = ''; entOk = '';
    entOpen = true;
  }
  function closeEnterprise() { entOpen = false; }

  // 选择预设 emoji 作 logo
  function pickLogoEmoji(e) {
    entForm.logo = e.target.value;
    entErr = '';
  }
  // 上传本地图片作 logo（转 base64 dataURL，前端预览 + 后端存储）
  // form=目标表单（entForm 或 onboardForm），errSetter=错误提示写入函数
  function uploadLogo(e, form, errSetter) {
    const f = e.target.files && e.target.files[0];
    if (!f) return;
    if (!/^image\/(png|jpe?g|gif|webp|svg\+xml)$/i.test(f.type)) {
      errSetter('仅支持图片文件（png/jpg/gif/webp/svg）');
      e.target.value = ''; return;
    }
    if (f.size > 1024 * 1024) { errSetter('图片过大，请控制在 1MB 以内'); e.target.value = ''; return; }
    const reader = new FileReader();
    reader.onload = () => { form.logo = String(reader.result); errSetter(''); };
    reader.readAsDataURL(f);
    e.target.value = '';
  }
  // 企业设置弹窗 → entForm/entErr
  const onLogoUpload = (e) => uploadLogo(e, entForm, (m) => (entErr = m));
  // onboarding 引导 → onboardForm/onboardErr
  const onOnboardLogoUpload = (e) => uploadLogo(e, onboardForm, (m) => (onboardErr = m));
  function clearLogo() { entForm.logo = ''; }

  // 保存企业设置 → 后端持久化 → 顶部品牌即时跟随
  // 事件驱动无死角：若改行业，后端已自动用新行业数据建模并联动企业 kb/激活 kb，
  // 前端这里只需把用户状态/激活 kb/看板跟随刷新到新行业。
  async function doSaveEnterprise() {
    if (entBusy) return;
    const name = entForm.name.trim();
    if (!name) { entErr = '请输入企业名称'; return; }
    entBusy = true; entErr = ''; entOk = '';
    const prevIndustry = (user && user.industry) || '';
    try {
      const res = await saveEnterprise({ name, logo: entForm.logo, industry: entForm.industry });
      if (res && res.ok && res.data) {
        user = { ...user, enterpriseName: res.data.enterpriseName, logo: res.data.logo, industry: res.data.industry };
        // 改行业成功 → 后端已自动建模并返回新行业信息，前端同步 kb + 刷新看板/问答
        if (res.industry && res.industry.kb) {
          user = { ...user, kb: res.industry.kb };
          await setKb(res.industry.kb);
          await loadKbs();
          entOk = `已保存并自动切换至「${res.industry.name}」，模型已重建`;
          setStatus('ok', `行业已切换至「${res.industry.name}」，本体已自动重建`);
        } else if (res.data.industry && res.data.industry !== prevIndustry) {
          // 兜底：后端没返回行业映射（非注册行业名）时也刷新 kb 列表
          await loadKbs();
          entOk = '已保存，顶部品牌已更新';
        } else {
          entOk = '已保存，顶部品牌已更新';
        }
      } else {
        entErr = (res && res.error) || '保存失败';
      }
    } catch (err) {
      entErr = String(err && err.message ? err.message : err);
    } finally {
      entBusy = false;
    }
  }

  // 快速问题：跟随当前激活 kb 的 examples（不锁死 food）
  const quickQuestions = $derived(
    (kbList.find(k => k.key === currentKb)?.examples) || [
      '有多少台运行中的设备',
      '车间A的设备有哪些',
      '功率最大的设备',
      '有多少台报警的',
      'L1产线的设备有哪些',
    ]
  );

  function setStatus(type, msg) {
    statusType = type; statusMsg = msg;
  }

  // 错误分类加固：SoGuru 5 族分类法 + 语义/输入策略 guardrail（fail-open）。
  // 判定优先级：HTTP 状态码(429→限流,5xx→基础设施) → res.error 关键词（模型配置/语义/输入/数据）→ 原始错误。
  // fetch 抛异常(err) → 基础设施（网络）。返回值始终为文本，经 setStatus 由文本插值转义，防 XSS。
  function formatError(res, err) {
    const status = res && res.status;
    const msg = (res && res.error && String(res.error).trim()) ? String(res.error) : '';
    // 基础设施-限流：429 配额/频率超限 → 提示退避重试
    if (status === 429) return `请求过于频繁，请稍后再试：${msg}`;
    // 基础设施-服务：5xx 服务端异常
    if (status >= 500) return `服务异常，请重试：${msg}`;
    if (msg) {
      // 模型配置问题（保留现有）
      if (/(key|api_key|模型|Ollama|未配置|无模型)/i.test(msg)) return `模型配置问题：${msg}`;
      // 语义：幻觉 / schema 不匹配 / 拒答 / 空回答
      if (/(幻觉|拒答|拒绝回答|schema不匹配|schema 不匹配|空回答|无法回答|不能回答|没有结果|无结果|纯噪声)/i.test(msg)) return `模型输出异常：${msg}`;
      // 输入策略：不安全 / 注入 / PII / 敏感内容
      if (/(不安全|注入|PII|敏感内容|非法输入|不被允许|不允许)/i.test(msg)) return `输入不被允许：${msg}`;
      // 数据词典问题（保留现有）
      if (/(词典|建模|读取|数据)/.test(msg)) return `数据问题：${msg}`;
      // 未知族 → 显示原始错误
      return msg;
    }
    if (err) return '服务异常，请重试（网络错误：服务未启动或连接失败，请确认后端已运行）';
    return '';
  }

  function switchTab(tab) {
    activeTab = tab;
  }

  // ─── 单企业 kb 加载（无切换）───
  // 从后端读取当前企业唯一 kb（/api/ontology/kbs → 只含登录企业 kb），供查询示例问题。
  async function loadKbs() {
    try {
      const res = await fetchKbs();
      if (res && res.ok && Array.isArray(res.kbs)) {
        kbList = res.kbs;
        kbsLoaded = true;
        const cur = kbList.find(k => k.key === currentKb);
        if (cur) setStatus('info', `${cur.icon} ${cur.name} 已就绪`);
        // 已注册 kb 可直接查询（无需先本地建模），放行查询输入
        if (status === 'idle') { status = 'ready'; }
        return true;
      }
    } catch (e) { /* 忽略，前端降级用默认 */ }
    return false;
  }

  // ─── 登录成功后/会话恢复时初始化数据（kb/模型/企业设置）───
  // P0 修复：首次登录(非刷新)后也必须触发此初始化，否则 status 停留在"等待数据导入"，
  // 问答输入框/按钮 disabled。刷新之所以正常，是因为 onMount 带会话 cookie 走了同一路径。
  async function initAppData() {
    if (!user) return;
    // 行业下拉动态化：从后端 kbs.json 全量读取（事件驱动无死角，不依赖前端硬编码）
    await loadIndustries();
    // 新企业未配置 → 初始化引导 onboarding
    if (needsOnboard) initOnboard();
    // 加载当前企业唯一 kb（决定查询/看板/本体图检索哪个）→ 注册 kb 放行问答输入（status → ready）
    await loadKbs();
    // 企业设置：读取登录企业用户的信息（顶部品牌跟随）
    await loadEnterprise();
    try {
      const res = await getModels();
      if (res.ok) {
        modelList = res.models.map(m => ({ key: m.key, name: m.name }));
        activeModel = res.active || '';
        loadEditModels(res);
      }
    } catch (e) { /* 忽略 */ }
    try {
      const v = await fetchVersion();
      if (v.ok && v.version) appVersion = v.version;
    } catch (e) { /* 忽略 */ }
  }

  // ─── 模型配置加载与切换 ───
  onMount(async () => {
    // 单企业：先校验登录会话（未登录/失效 → 跳登录页）
    await loadSessionUser();
    if (!user) { authLoading = false; return; }
    authLoading = false;
    await initAppData();
  });

  // ─── 用行业示例自动建模（事件驱动无死角，走后端全量行业清单）───
  // 欢迎页示例传的是行业 dir，这里解析为行业名后调用 switchIndustry 自动建模并联动 kb。
  async function doDefaultExample(dirArg) {
    if (defaultBusy) return;
    const dir = dirArg || defaultIndustry || 'data_valve';
    defaultIndustry = dir;   // 同步左侧行业下拉（点击欢迎页示例时跟随）
    const ind = INDUSTRIES.find(i => i.dir === dir);
    const industry = (ind && ind.name) || dir;
    defaultBusy = true;
    setStatus('info', `正在用「${industry}」示例数据自动建模…`);
    try {
      const res = await switchIndustry(industry);
      if (!res.ok) {
        setStatus('err', formatError(res, null) || '建模失败');
      } else {
        const kb = (res.industry && res.industry.kb) || currentKb;
        modelResult = { table: kb, attrs: res.attrs || [], tables: 0, ts: Date.now() };
        status = 'ready';
        if (user) user = { ...user, kb, industry: (res.industry && res.industry.name) || industry };
        await setKb(kb);
        await loadKbs();
        setStatus('ok', `「${industry}」建模完成：${kb}，共 ${(res.attrs || []).length} 个字段`);
      }
    } catch (err) {
      setStatus('err', formatError(null, err));
    } finally { defaultBusy = false; }
  }

  // ─── 本地文件多选（系统浏览框）───
  function onPickFiles(e) {
    const files = Array.from(e.target.files || []);
    if (!files.length) return;
    const invalid = files.find(f => !/\.(csv|json)$/i.test(f.name));
    if (invalid) {
      setStatus('err', '仅支持 .csv / .json 文本文件');
      e.target.value = ''; return;
    }
    localFiles = files.map(f => ({ name: f.name, content: null, size: f.size, file: f }));
    setStatus('info', `已选 ${localFiles.length} 个本地文件`);
  }

  // 本地文件 → 读取内容 → 多文件建模
  async function doLocalModel() {
    if (!localFiles.length || localBusy) return;
    localBusy = true;
    setStatus('info', `正在读取 ${localFiles.length} 个本地文件…`);
    try {
      const files = [];
      for (const lf of localFiles) {
        const content = await readFileAsText(lf.file);
        files.push({ name: lf.name, content });
      }
      const res = await setupOntologyMulti(files, resolveTargetKb());
      if (!res.ok) {
        setStatus('err', formatError(res, null) || '建模失败');
      } else {
        modelResult = { table: res.table, attrs: res.attrs || [], tables: localFiles.length, ts: Date.now() };
        status = 'ready';
        setStatus('ok', `建模完成：${res.table}，共 ${localFiles.length} 张表、${(res.attrs || []).length} 个字段`);
      }
    } catch (err) {
      setStatus('err', formatError(null, err));
    } finally { localBusy = false; }
  }

  function readFileAsText(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(reader.error);
      reader.readAsText(file, 'utf-8');
    });
  }

  // 从完整配置装载可编辑列表（api_key 用脱敏占位，保存时后端保留原值）
  function loadEditModels(res) {
    editActive = res.active || '';
    editModels = (res.models || []).map(m => ({
      key: m.key, name: m.name, type: m.type, base_url: m.base_url || '',
      model: m.model || '', api_key: m.has_key ? (m.api_key_status || '已配置') : '',
    }));
    if (res.embedding) editEmbedding = { name: res.embedding.name || '', type: res.embedding.type || 'ollama', base_url: res.embedding.base_url || '', model: res.embedding.model || 'nomic-embed-text' };
  }

  async function refreshModels() {
    const res = await getModels();
    if (res.ok) {
      modelList = res.models.map(m => ({ key: m.key, name: m.name }));
      activeModel = res.active || '';
      loadEditModels(res);
      return true;
    }
    return false;
  }

  function addModel() {
    const key = 'model_' + Date.now();
    editModels = [...editModels, { key, name: '新模型', type: 'ollama', base_url: 'http://127.0.0.1:11434', model: '', api_key: '' }];
  }

  function removeModel(i) {
    if (editModels.length <= 1) { setStatus('err', '至少保留一个模型'); return; }
    editModels = editModels.filter((_, idx) => idx !== i);
  }

  function setEditActive(key) {
    editActive = key;
  }

  async function saveModelConfig() {
    if (modelEditBusy) return;
    const list = editModels.map(m => ({
      key: m.key, name: m.name, type: m.type, base_url: m.base_url, model: m.model, api_key: m.api_key,
    }));
    if (list.length === 0) { setStatus('err', '至少保留一个模型'); return; }
    modelEditBusy = true;
    try {
      const res = await saveModels({ models: list, active: editActive, embedding: editEmbedding });
      if (res.ok) {
        setStatus('ok', `模型配置已保存，当前：${res.active}`);
        await refreshModels();
      } else {
        setStatus('err', formatError(res, null) || '保存失败');
      }
    } catch (err) {
      setStatus('err', formatError(null, err));
    } finally { modelEditBusy = false; }
  }

  async function switchModel(e) {
    const key = e.target.value;
    try {
      const res = await setModel(key);
      if (res.ok) {
        activeModel = res.active;
        setStatus('ok', `模型已切换：${res.active}`);
      } else {
        setStatus('err', formatError(res, null) || '切换失败');
      }
    } catch (err) {
      setStatus('err', formatError(null, err));
    }
  }

  // ─── 数据库接入建模（本地厂区局域网）───
  function onDbTypeChange() {
    dbForm.port = dbForm.db_type === 'postgres' ? '5432' : '3306';
  }

  async function doDbSetup() {
    if (dbBusy) return;
    const f = dbForm;
    if (!f.host.trim() || !f.user.trim() || !f.database.trim() || !f.tables.trim()) {
      setStatus('err', '请填写主机、用户、库名和表名'); return;
    }
    dbBusy = true;
    setStatus('info', '正在连接数据库并建模…');
    try {
      const cfg = {
        db_type: f.db_type,
        host: f.host.trim(),
        port: String(f.port || ''),
        user: f.user.trim(),
        password: f.password,
        database: f.database.trim(),
        tables: f.tables.split(/[,，]/).map(s => s.trim()).filter(Boolean),
      };
      const res = await dbSetup(cfg);
      if (!res.ok) {
        const emsg = formatError(res, null) || '数据库建模失败';
        setStatus('err', emsg);
        dbResult = { ok: false, error: emsg };
      } else {
        dbResult = { ok: true, table: res.table, output: res.output || '' };
        modelResult = { table: res.table, attrs: [], ts: Date.now() };
        status = 'ready';
        setStatus('ok', `数据库建模完成：${res.table}`);
      }
    } catch (err) {
      const emsg = formatError(null, err);
      setStatus('err', emsg);
      dbResult = { ok: false, error: emsg };
    } finally { dbBusy = false; }
  }

  // ─── 提问（普通问答 + 智能分析路由）───
  // 分析意图关键词
  const ANALYZE_KEYWORDS = ['分析', '比较', '对比', '关注', '趋势', '整体', '状况', '健康', '产能', '评估', '总结', '分布', '建议', '异常情况'];

  function isAnalyzeQuestion(q) {
    return ANALYZE_KEYWORDS.some(k => q.includes(k));
  }

  async function doAsk(text) {
    const q = (text ?? question).trim();
    if (!q || asking) return;
    // 输入 guardrail（fail-open）：长度上限，超限直接拦截提示
    if (q.length > 200) { setStatus('err', '问题过长，请控制在 200 字以内'); return; }
    question = ''; asking = true; status = 'asking';
    answer = ''; answerHTML = null; evidence = null; evidenceOpen = false; analysis = null;
    setStatus('info', `查询：${q}`);
    try {
      if (isAnalyzeQuestion(q)) {
        // 智能分析：统计摘要 + LLM 洞察（前端画图 + 报告）
        const res = await analyzeOntology(q, currentKb);
        if (!res.ok) {
          setStatus('err', formatError(res, null) || '分析失败'); status = 'ready';
        } else {
          analysis = { report: res.report, stats: res.stats };
          evidence = res.evidence || null;   // 分析答案带文档RAG溯源则一并展示
          status = 'ready';
          setStatus('ok', `分析完成：${q}`);
        }
      } else {
        // 普通问答
        const res = await askOntology(q, currentKb);
        if (!res.ok) {
          setStatus('err', formatError(res, null) || '问答失败'); status = 'ready';
        } else {
          // 统一 answer 为字符串, 避免后端返回对象(error/structured)被渲染成 [object Object]
          const a = typeof res.answer === 'string' ? res.answer
                   : (res.answer && typeof res.answer === 'object' && res.answer.message) ? res.answer.message
                   : (res.answer && typeof res.answer === 'object' && res.answer.text) ? res.answer.text
                   : String(res.answer ?? '');
          answer = a || '（无结果）';
          answerHTML = renderAnswerHTML(a);
          evidence = res.evidence || null;
          status = 'ready';
          setStatus('ok', `查询完成：${q}`);
        }
      }
    } catch (err) {
      setStatus('err', formatError(null, err)); status = 'ready';
    } finally {
      asking = false;
      if (answerBox) answerBox.scrollTop = answerBox.scrollHeight;
    }
  }

  function onKeydown(e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); doAsk(); }
  }

  // ─── 结构化答案渲染（极简，无第三方库）───
  function escapeHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  // 返回 HTML；若不含表格/列表结构则返回 null（调用方退回 <pre>）
  function renderAnswerHTML(text) {
    const lines = String(text || '').split('\n');
    let html = ''; let changed = false; let i = 0;
    while (i < lines.length) {
      const line = lines[i]; const t = line.trim();
      // 表格：连续两行以上含 |
      if (line.includes('|')) {
        const rows = [];
        while (i < lines.length && lines[i].includes('|')) {
          const cells = lines[i].split('|').map(c => c.trim());
          while (cells.length && cells[0] === '') cells.shift();
          while (cells.length && cells[cells.length - 1] === '') cells.pop();
          rows.push(cells); i++;
        }
        if (rows.length >= 2) {
          changed = true;
          html += '<table class="ans-table"><thead><tr>' +
            rows[0].map(c => '<th>' + escapeHtml(c) + '</th>').join('') +
            '</tr></thead><tbody>' +
            rows.slice(1).map(r => '<tr>' + r.map(c => '<td>' + escapeHtml(c) + '</td>').join('') + '</tr>').join('') +
            '</tbody></table>';
        } else {
          for (const r of rows) html += escapeHtml(r.join(' | ')) + '<br>';
        }
        continue;
      }
      // 列表：`信息(N):`/`字段信息(N):` 后跟 `- xxx`
      const m = t.match(/^(.*?信息\s*[（(]?\d+[）)]?:?)$/);
      if (m) {
        let j = i + 1; const items = [];
        while (j < lines.length && /^\s*[-•]\s/.test(lines[j])) {
          items.push(lines[j].replace(/^\s*[-•]\s*/, '')); j++;
        }
        if (items.length > 0) {
          changed = true;
          html += '<div class="ans-head">' + escapeHtml(t) + '</div><ul class="ans-list">' +
            items.map(it => '<li>' + escapeHtml(it) + '</li>').join('') + '</ul>';
          i = j; continue;
        }
      }
      html += escapeHtml(line) + '<br>'; i++;
    }
    return changed ? html : null;
  }
  // 证据溯源渲染（entities / 对象 / 数组，通用展示）
  function renderEvidence(ev) {
    if (!ev) return '';
    if (typeof ev === 'string') return '<div class="ev-text">' + escapeHtml(ev) + '</div>';
    const list = Array.isArray(ev) ? ev : (ev.entities || ev.rows || []);
    const items = [];
    if (Array.isArray(list) && list.length) {
      for (const it of list) {
        if (typeof it === 'string') items.push('<li>' + escapeHtml(it) + '</li>');
        else if (it && typeof it === 'object')
          items.push('<li>' + Object.entries(it)
            .map(([k, v]) => '<span class="ev-k">' + escapeHtml(k) + '</span>：' + escapeHtml(typeof v === 'string' ? v : JSON.stringify(v)))
            .join('；') + '</li>');
      }
    } else if (ev && typeof ev === 'object') {
      for (const [k, v] of Object.entries(ev))
        items.push('<li><span class="ev-k">' + escapeHtml(k) + '</span>：' + escapeHtml(typeof v === 'string' ? v : JSON.stringify(v)) + '</li>');
    }
    return items.length ? '<ul class="ev-list">' + items.join('') + '</ul>' : '';
  }
</script>

<div class="app">
  {#if authLoading}
    <!-- 启动校验会话中 -->
    <div class="auth-loading">
      <span class="load-dot"></span><span class="load-dot"></span><span class="load-dot"></span>
      正在校验登录…
    </div>
  {:else if !isAuthed}
    <!-- ═══ 登录页 ═══ -->
    <div class="login-page">
      <div class="login-card">
        <div class="login-logo">🏭</div>
        <h1 class="login-title"><span class="acc">工厂智能体</span> · 本体问答</h1>
        <p class="login-sub">企业专属知识库 · 数据本地处理不出厂</p>
        <div class="login-tabs">
          <button class="login-tab" class:on={loginMode === 'login'} onclick={() => { loginMode = 'login'; loginErr = ''; }}>登录</button>
          <button class="login-tab" class:on={loginMode === 'register'} onclick={() => { loginMode = 'register'; loginErr = ''; }}>注册企业</button>
        </div>
        <input class="login-input" type="text" placeholder="用户名" bind:value={loginUser} onkeydown={(e) => { if (e.key === 'Enter') loginMode === 'login' ? doLogin() : doRegister(); }} />
        <input class="login-input" type="password" placeholder="密码" bind:value={loginPass} onkeydown={(e) => { if (e.key === 'Enter') loginMode === 'login' ? doLogin() : doRegister(); }} />
        {#if loginErr}<div class="login-err">✗ {loginErr}</div>{/if}
        <button class="login-btn" onclick={loginMode === 'login' ? doLogin : doRegister} disabled={loginBusy}>
          {loginBusy ? '请稍候…' : (loginMode === 'login' ? '登 录' : '注册并登录')}
        </button>
        <p class="login-hint">{loginMode === 'login' ? '演示账号：admin / admin123' : '注册后自动创建企业空间，进入引导配置'}</p>
      </div>
    </div>
  {:else if needsOnboard}
    <!-- ═══ 引导 onboarding（新企业未配置）═══ -->
    <div class="onboard-page">
      <div class="onboard-card">
        <div class="onboard-progress">
          <span class="ob-step" class:done={onboardStep >= 1} class:cur={onboardStep === 1}>1 确认企业</span>
          <span class="ob-arrow">→</span>
          <span class="ob-step" class:done={onboardStep >= 2} class:cur={onboardStep === 2}>2 建本体</span>
          <span class="ob-arrow">→</span>
          <span class="ob-step" class:done={onboardStep >= 3} class:cur={onboardStep === 3}>3 完成</span>
        </div>

        {#if onboardStep === 1}
          <h2 class="onboard-title">🏢 确认企业信息</h2>
          <p class="onboard-sub">填写企业名称、选择 Logo 与所属行业，用于品牌展示与本体建模。</p>
          <div class="ent-logo-row">
            {#if isImgLogo(onboardForm.logo)}
              <img class="ent-logo-preview" src={onboardForm.logo} alt="企业logo" />
            {:else}
              <span class="ent-logo-preview ent-logo-emoji">{onboardForm.logo || '🏭'}</span>
            {/if}
            <div class="ent-logo-actions">
              <span class="form-label">企业 Logo</span>
              <div class="ent-logo-btns">
                <label class="ent-upload">
                  <input type="file" accept="image/*" onchange={onOnboardLogoUpload} />
                  📤 上传图片
                </label>
                <button class="ent-mini" onclick={() => (onboardForm.logo = '')}>清除</button>
              </div>
            </div>
          </div>
          <div class="ent-emoji-wrap">
            <span class="form-label">或选择 Logo（emoji）</span>
            <div class="ent-emoji-grid">
              {#each LOGO_EMOJIS as em}
                <button class="ent-emoji" class:sel={onboardForm.logo === em} onclick={() => { onboardForm.logo = em; onboardErr = ''; }}>{em}</button>
              {/each}
            </div>
          </div>
          <div class="form-group">
            <label class="form-label" for="ob-name">企业名称</label>
            <input id="ob-name" class="db-input" placeholder="如：示例制造公司" bind:value={onboardForm.name} />
          </div>
          <div class="form-group">
            <label class="form-label" for="ob-industry">所属行业</label>
            <select id="ob-industry" class="db-input" bind:value={onboardForm.industry}>
              <option value="">请选择行业</option>
              {#each INDUSTRY_OPTIONS as ind}
                <option value={ind}>{ind}</option>
              {/each}
            </select>
          </div>
          {#if onboardErr}<div class="login-err">✗ {onboardErr}</div>{/if}
          <button class="login-btn" onclick={onboardSaveEnterprise} disabled={onboardBusy}>
            {onboardBusy ? '保存中…' : '下一步：选择行业并建本体 →'}
          </button>
        {:else if onboardStep === 2}
          <h2 class="onboard-title">🧪 为「{user && user.enterpriseName}」建本体</h2>
          <p class="onboard-sub">行业「{onboardForm.industry}」已确定，系统将用该行业示例数据为您的企业建本体（唯一知识库，一企业一行业一数据）。</p>
          <div class="onboard-industries">
            <button class="ob-industry sel">
              <span class="ob-ind-icon">{onboardInd && onboardInd.icon}</span>
              <span class="ob-ind-name">{onboardForm.industry}</span>
            </button>
          </div>
          {#if onboardErr}<div class="login-err">✗ {onboardErr}</div>{/if}
          <button class="login-btn" onclick={onboardBuild} disabled={onboardBusy}>
            {onboardBusy ? '正在建本体…' : `为「${onboardForm.industry}」企业建本体 🚀`}
          </button>
        {:else}
          <h2 class="onboard-title">🎉 配置完成</h2>
          <p class="onboard-sub">「{user && user.enterpriseName}」的本体已建好，问答 / 知识库 / 评测 / 资产 / 看板 / 模型图功能已解锁。</p>
          <button class="login-btn" onclick={onboardFinish}>进入系统 →</button>
        {/if}
      </div>
    </div>
  {:else}
  <!-- ═══ 顶部工具栏 ═══ -->
  <header class="toolbar">
    <div class="toolbar-left">
      {#if isImgLogo(brandLogo)}
        <img class="logo-img" src={brandLogo} alt="企业logo" />
      {:else}
        <span class="logo">{brandLogo}</span>
      {/if}
      <div class="brand">
        <div class="brand-name">{brandName}</div>
        <div class="brand-sub">Factory Ontology QA System{#if appVersion} · v{appVersion}{/if}</div>
      </div>
    </div>
    <div class="toolbar-right">
      {#if kbsLoaded && kbList.length > 0}
        <!-- 一企业一行业一数据：顶部标签显示企业所属行业(industry)，而非专属kb名(ent_xxx)。
             企业绑专属kb，用kb名会显示下划线乱码；用企业行业属性定位行业图标/名称。 -->
        <span class="cur-kb-tag" title="当前企业知识库">
          <span class="kb-tag-icon">{INDUSTRIES.find(i => i.name === (user && user.industry))?.icon || kbList.find(k => k.key === currentKb)?.icon || '🗂️'}</span>
          {(user && user.industry) || kbList.find(k => k.key === currentKb)?.name || currentKb}
        </span>
      {/if}
      {#if modelList.length > 0}
        <label class="model-select">
          <span class="model-label">模型</span>
          <select value={activeModel} onchange={switchModel}>
            {#each modelList as m}
              <option value={m.key}>{m.name}</option>
            {/each}
          </select>
        </label>
      {/if}
      <span class="status-indicator" class:st-ok={statusType === 'ok'} class:st-err={statusType === 'err'} class:st-info={statusType === 'info'}>
        <span class="status-dot"></span>
        <span class="status-text">{statusMsg}</span>
      </span>
      <button class="ent-btn" onclick={openEnterprise} title="企业设置">
        <span class="btn-icon">⚙️</span> 企业设置
      </button>
      <button class="ent-btn ent-btn-danger" onclick={() => (resetOpen = true)} title="重置当前企业数据">
        <span class="btn-icon">🔄</span> 重置企业
      </button>
      <button class="ent-btn" onclick={doLogout} title="退出登录">
        <span class="btn-icon">⎋</span> 退出
      </button>
    </div>
  </header>

  <!-- ═══ 标签栏 ═══ -->
  <nav class="tabbar">
    <button class="tab" class:active={activeTab === 'model'} onclick={() => switchTab('model')}>
      <span class="tab-icon">📊</span> 数据建模
    </button>
    <button class="tab" class:active={activeTab === 'query'} onclick={() => switchTab('query')}>
      <span class="tab-icon">💬</span> 查询分析
    </button>
    <button class="tab" class:active={activeTab === 'dashboard'} onclick={() => switchTab('dashboard')}>
      <span class="tab-icon">📈</span> 数据看板
    </button>
    <button class="tab" class:active={activeTab === 'eval'} onclick={() => switchTab('eval')}>
      <span class="tab-icon">🧪</span> 评测
    </button>
    <button class="tab" class:active={activeTab === 'knowledge'} onclick={() => switchTab('knowledge')}>
      <span class="tab-icon">📚</span> 知识库
    </button>
    <button class="tab" class:active={activeTab === 'assets'} onclick={() => switchTab('assets')}>
      <span class="tab-icon">📦</span> 资产
    </button>
  </nav>

  <!-- ═══ 主区域 ═══ -->
  <main class="workspace">
    {#if activeTab === 'model'}
    <!-- ─── 左栏：数据建模 ─── -->
    <section class="pane pane-left">
      <div class="pane-title">数据建模<span class="pane-sub">上传数据、连接数据库或配置模型，构建企业专属本体</span></div>

      {#if defaultBusy || localBusy || dbBusy}
        <div class="model-busy-overlay">
          <span class="model-busy-spin"></span>
          <span class="model-busy-text">
            {defaultBusy ? '正在用示例数据建本体，请稍候…' : localBusy ? '正在读取本地文件并建本体…' : '正在连接数据库并建模…'}
          </span>
        </div>
      {/if}

      <!-- ─── 卡片① 本地文件建模（折叠式，对齐模型配置管理）─── -->
      <div class="card">
        <button class="card-head card-head-btn" class:expanded={localModelOpen} onclick={() => (localModelOpen = !localModelOpen)}>
          <span class="card-icon ic-file">📄</span>
          <div class="card-titles">
            <div class="card-title">本地文件建模</div>
            <div class="card-desc">{localModelOpen ? '点击收起' : '点击展开'}上传 CSV / JSON 文件建模</div>
          </div>
          <span class="chevron">{localModelOpen ? '▾' : '▸'}</span>
        </button>
        {#if localModelOpen}
        <div class="card-body">
          <div class="file-upload-zone">
            <label class="file-input">
              <input type="file" multiple accept=".csv,.json" onchange={onPickFiles} />
              <span class="file-upload-icon">📁</span>
              <span class="file-upload-main">
                <span class="file-upload-title">{localFiles.length ? `已选 ${localFiles.length} 个文件` : '选择 CSV / JSON 文件'}</span>
                <span class="file-upload-hint">可多选，支持 .csv / .json</span>
              </span>
              <span class="file-upload-btn">浏览选择文件</span>
            </label>
            {#if localFiles.length}
              <div class="file-selected">
                <div class="file-selected-title">待建模文件</div>
                <div class="file-selected-list">
                  {#each localFiles as f}
                    <span class="file-selected-chip">📄 {f.name}</span>
                  {/each}
                </div>
              </div>
              <button class="btn-action" onclick={doLocalModel} disabled={localBusy}>
                <span class="btn-icon">{localBusy ? '⏳' : '⚙'}</span>
                {localBusy ? '建模进行中…' : `确认并建模（${localFiles.length} 个文件）`}
              </button>
            {/if}
          </div>
        </div>
        {/if}
      </div>

      {#if modelResult}
        <div class="card">
          <div class="card-head">
            <span class="card-icon">✅</span>
            <div class="card-titles">
              <div class="card-title">建模结果</div>
              <div class="card-desc">知识库 {modelResult.table}</div>
            </div>
          </div>
          <div class="card-body">
            <div class="model-summary">
              <span class="sum-tables">共 {modelResult.tables || localFiles.length || 1} 张表</span>
              <span class="sum-sep">·</span>
              <span class="sum-fields">{(modelResult.attrs || []).length} 个字段</span>
              <span class="sum-ok">✓ 建模完成</span>
            </div>
            {#if localFiles.length > 0}
              <div class="model-files">
                <div class="model-files-title">已建模文件</div>
                <div class="model-files-list">
                  {#each localFiles as f}
                    <span class="model-file-chip">📄 {f.name}</span>
                  {/each}
                </div>
              </div>
            {/if}
            <div class="attr-chips">
              {#each modelResult.attrs as a}
                <span class="attr-chip">{typeof a === 'object' && a !== null ? (a.cn || a.field || JSON.stringify(a)) : a}</span>
              {/each}
            </div>
            <div class="model-actions">
              <span class="model-hint">右栏已生成本体结构力导向图，可拖动缩放查看。</span>
              <button class="btn-action btn-small" onclick={() => switchTab('query')}>💬 进入问答</button>
            </div>
          </div>
        </div>
      {/if}

      <!-- ─── 卡片③ 数据库接入（折叠式，对齐模型配置管理）─── -->
      <div class="card">
        <button class="card-head card-head-btn" class:expanded={dbOpen} onclick={() => (dbOpen = !dbOpen)}>
          <span class="card-icon ic-db">🗄️</span>
          <div class="card-titles">
            <div class="card-title">数据库接入</div>
            <div class="card-desc">{dbOpen ? '点击收起' : '点击展开'}连接 MES / ERP / 台账数据库</div>
          </div>
          <span class="chevron">{dbOpen ? '▾' : '▸'}</span>
        </button>
        {#if dbOpen}
        <div class="card-body">
          <div class="form-group">
            <label class="form-label" for="db-type">数据库类型</label>
            <select id="db-type" class="db-input" bind:value={dbForm.db_type} onchange={onDbTypeChange}>
              <option value="mysql">MySQL</option>
              <option value="postgres">PostgreSQL</option>
            </select>
          </div>

          <div class="db-row">
            <div class="form-group">
              <label class="form-label" for="db-host">主机</label>
              <input id="db-host" class="db-input" placeholder="127.0.0.1" bind:value={dbForm.host} />
            </div>
            <div class="form-group db-port">
              <label class="form-label" for="db-port">端口</label>
              <input id="db-port" class="db-input" placeholder="3306" bind:value={dbForm.port} />
            </div>
          </div>

          <div class="db-row">
            <div class="form-group">
              <label class="form-label" for="db-user">用户</label>
              <input id="db-user" class="db-input" placeholder="root" bind:value={dbForm.user} />
            </div>
            <div class="form-group">
              <label class="form-label" for="db-pass">密码</label>
              <input id="db-pass" class="db-input" type="password" placeholder="••••••" bind:value={dbForm.password} />
            </div>
          </div>

          <div class="form-group">
            <label class="form-label" for="db-database">库名</label>
            <input id="db-database" class="db-input" placeholder="factory" bind:value={dbForm.database} />
          </div>
          <div class="form-group">
            <label class="form-label" for="db-tables">表名（多个用逗号分隔）</label>
            <input id="db-tables" class="db-input" placeholder="equipment, devices" bind:value={dbForm.tables} />
          </div>

          <button class="btn-action" onclick={doDbSetup} disabled={dbBusy}>
            <span class="btn-icon">{dbBusy ? '⏳' : '🔗'}</span>
            {dbBusy ? '建模进行中…' : '确认并建模'}
          </button>

          {#if dbResult}
            <div class="db-result" class:db-err={!dbResult.ok}>
              {#if dbResult.ok}
                <div class="model-head">
                  <span class="model-table">数据表：{dbResult.table}</span>
                  <span class="model-count">✓ 建模完成</span>
                </div>
              {:else}
                <div class="db-err-text">✗ {dbResult.error}</div>
              {/if}
            </div>
          {/if}
        </div>
        {/if}
      </div>

      <!-- ─── 卡片④ 模型配置管理（查看/编辑/增删/设 active，api_key 脱敏）─── -->
      <div class="card">
        <button class="card-head card-head-btn" class:expanded={modelOpen} onclick={() => (modelOpen = !modelOpen)}>
          <span class="card-icon ic-model">⚙️</span>
          <div class="card-titles">
            <div class="card-title">模型配置管理</div>
            <div class="card-desc">{modelOpen ? '点击收起' : '点击展开'}模型/向量配置</div>
          </div>
          <span class="chevron">{modelOpen ? '▾' : '▸'}</span>
        </button>
        {#if modelOpen}
          <div class="card-body">
            <div class="db-hint">模型配置持久化到 model_config.json。api_key 仅在输入新值时更新，留空/不改则保留原值。本地优先：默认 ornith/qwen 可直连 Ollama。</div>
            {#each editModels as m, i}
              <div class="m-edit">
                <div class="m-edit-head">
                  <label class="m-radio">
                    <input type="radio" checked={editActive === m.key} onclick={() => setEditActive(m.key)} />
                    <span class="m-active-tag">{editActive === m.key ? '✓ 生效' : '设为生效'}</span>
                  </label>
                  <span class="m-key">key: {m.key}</span>
                  <button class="file-remove" onclick={() => removeModel(i)} aria-label="删除">✕</button>
                </div>
                <div class="db-row">
                  <div class="form-group">
                    <span class="form-label">名称</span>
                    <input class="db-input" placeholder="模型名称" bind:value={m.name} />
                  </div>
                  <div class="form-group">
                    <span class="form-label">类型</span>
                    <select class="db-input" bind:value={m.type}>
                      <option value="ollama">ollama</option>
                      <option value="openai">openai</option>
                    </select>
                  </div>
                </div>
                <div class="db-row">
                  <div class="form-group">
                    <span class="form-label">Base URL</span>
                    <input class="db-input" placeholder="http://127.0.0.1:11434" bind:value={m.base_url} />
                  </div>
                  <div class="form-group">
                    <span class="form-label">Model</span>
                    <input class="db-input" placeholder="ornith:latest" bind:value={m.model} />
                  </div>
                </div>
                <div class="form-group">
                  <span class="form-label">API Key（{m.api_key ? '已配置，输入新值可更新' : '未配置'}{m.api_key ? '：' + m.api_key : ''}）</span>
                  <input class="db-input" type="password" placeholder="留空 = 保留原值" bind:value={m.api_key} />
                </div>
              </div>
            {/each}
            <div class="m-embed">
              <div class="m-embed-title">🔎 向量检索模型（embedding，默认本地）</div>
              <div class="db-row">
                <div class="form-group">
                  <span class="form-label">Base URL</span>
                  <input class="db-input" placeholder="http://127.0.0.1:11434" bind:value={editEmbedding.base_url} />
                </div>
                <div class="form-group">
                  <span class="form-label">Model</span>
                  <input class="db-input" placeholder="nomic-embed-text" bind:value={editEmbedding.model} />
                </div>
              </div>
              <div class="db-hint">向量语义检索用本地 embedding，零 API 成本。改这里可切换向量模型/地址。</div>
            </div>
            <div class="m-actions">
              <button class="btn-action btn-default" onclick={addModel}>＋ 新增模型</button>
              <button class="btn-action" onclick={saveModelConfig} disabled={modelEditBusy}>
                {modelEditBusy ? '保存中…' : '💾 保存配置'}
              </button>
            </div>
          </div>
        {/if}
      </div>
    </section>

    <!-- ─── 右栏：模型结构图（默认欢迎界面，点"显示本体模型"才显示模型图）─── -->
    <section class="pane pane-right">
      <div class="pane-title">
        {showModel ? (modelResult ? '模型结构' : (user && user.enterpriseName ? `${user.enterpriseName} · 本体问答系统` : '本体问答系统')) : (user && user.enterpriseName ? `${user.enterpriseName} · 本体问答系统` : '本体问答系统')}
        <button class="btn-model-toggle" onclick={() => (showModel = !showModel)}>
          {showModel ? '← 返回欢迎界面' : '显示本体模型'}
        </button>
      </div>
      <div class="graph-body">
        {#if showModel}
          <!-- refreshKey=ts：重新建模 ts 变 → ModelGraph 的 $effect 触发重新加载；kb=当前激活知识库，本体图跟随该 kb（非 food） -->
          <ModelGraph refreshKey={modelResult ? modelResult.ts : Date.now()} kb={currentKb} />
        {:else}
          <!-- 未点显示模型：科幻风格 SVG 企业欢迎页（企业欢迎词 + 建模示例），按企业所属行业识别 -->
          <WelcomeModel kb={currentKb} kbList={kbList} industries={INDUSTRIES} industry={user && user.industry} onModel={(dir) => doDefaultExample(dir)} />
        {/if}
      </div>
    </section>
    {:else if activeTab === 'query'}
    <!-- ─── 查询分析（独立标签页）─── -->
    <section class="pane pane-full">
      <div class="pane-title">查询分析<span class="pane-sub">用自然语言查询企业知识库，每个答案带证据溯源</span></div>
      <div class="query-body">
        <div class="query-bar">
          <input
            type="text"
            class="query-input"
            placeholder="输入中文查询语句…"
            bind:value={question}
            onkeydown={onKeydown}
            disabled={asking || status !== 'ready'}
          />
          <button class="btn-query" onclick={() => doAsk()} disabled={asking || !question.trim() || status !== 'ready'}>
            {asking ? '查询中…' : '查 询'}
          </button>
        </div>

        <div class="quick-bar">
          {#each quickQuestions as q}
            <button class="quick-btn" onclick={() => doAsk(q)} disabled={asking || status !== 'ready'}>{q}</button>
          {/each}
        </div>

        <div class="result-panel">
          {#if status === 'asking'}
            <div class="skel-block">
              <div class="result-head">
                <span class="result-label">正在查询本体</span>
              </div>
              <div class="skel-line-md"></div>
              <div class="skel-line-lg"></div>
              <div class="skel-line-lg"></div>
              <div class="skel-line-md"></div>
              <div class="query-thinking">
                <span class="load-dot"></span><span class="load-dot"></span><span class="load-dot"></span>
                正在检索知识库并生成回答…
              </div>
            </div>
          {:else if analysis}
            <div class="analysis-body">
              <AnalysisResult stats={analysis.stats} report={analysis.report} />
            </div>
            {#if evidence}
              <div class="evidence-wrap">
                <button class="evidence-toggle" onclick={() => (evidenceOpen = !evidenceOpen)}>
                  📎 证据溯源
                  <span class="chevron">{evidenceOpen ? '▾' : '▸'}</span>
                </button>
                {#if evidenceOpen}
                  <div class="evidence-body">{@html renderEvidence(evidence)}</div>
                {/if}
              </div>
            {/if}
          {:else if answer}
            <div class="result-head">
              <span class="result-label">查询结果</span>
            </div>
            <div class="result-scroll result-fade" bind:this={answerBox}>
              {#if answerHTML}
                <div class="ans-body">{@html answerHTML}</div>
              {:else}
                <pre class="result-text">{answer}</pre>
              {/if}
            </div>
            {#if evidence}
              <div class="evidence-wrap">
                <button class="evidence-toggle" onclick={() => (evidenceOpen = !evidenceOpen)}>
                  📎 证据溯源
                  <span class="chevron">{evidenceOpen ? '▾' : '▸'}</span>
                </button>
                {#if evidenceOpen}
                  <div class="evidence-body">{@html renderEvidence(evidence)}</div>
                {/if}
              </div>
            {/if}
          {:else}
            <div class="result-empty">{#if kbsLoaded && currentKb}当前知识库「{kbList.find(k => k.key === currentKb)?.name || currentKb}」已就绪，输入中文问题开始查询。{/if}</div>
          {/if}
        </div>
      </div>
    </section>
    {:else if activeTab === 'dashboard'}
    <section class="pane pane-full">
      <div class="pane-title">数据看板<span class="pane-sub">企业关键数据的可视化概览</span></div>
      <div class="dashboard-body">
        <DashboardPanel kb={currentKb} />
      </div>
    </section>
    {:else if activeTab === 'eval'}
    <!-- ─── 评测（问答命中率基线）─── -->
    <section class="pane pane-full">
      <div class="pane-title">评测基线<span class="pane-sub">用示例题目评测知识库的问答命中率</span></div>
      <div class="dashboard-body">
        <EvalPanel kb={currentKb} />
      </div>
    </section>
    {:else if activeTab === 'knowledge'}
    <!-- ─── 知识库管理（文档列表）─── -->
    <section class="pane pane-full">
      <div class="pane-title">知识库管理<span class="pane-sub">上传文档，构建企业知识库</span></div>
      <div class="dashboard-body">
        <KnowledgePanel kb={currentKb} />
      </div>
    </section>
    {:else if activeTab === 'assets'}
    <!-- ─── 资产版本（版本链）─── -->
    <section class="pane pane-full">
      <div class="pane-title">资产版本<span class="pane-sub">管理语义资产快照，支持版本回滚与交付</span></div>
      <div class="dashboard-body">
        <AssetPanel kb={currentKb} />
      </div>
    </section>
    {/if}
  </main>

  <!-- ═══ 企业设置弹窗 ═══ -->
  {#if entOpen}
  <div class="ent-overlay" role="presentation" onclick={(e) => { if (e.target === e.currentTarget) closeEnterprise(); }}>
    <div class="ent-modal" role="dialog" aria-modal="true" aria-label="企业设置">
      <div class="ent-head">
        <span class="card-icon">⚙️</span>
        <span class="ent-title">企业设置</span>
        <button class="ent-close" onclick={closeEnterprise} aria-label="关闭">✕</button>
      </div>
      <div class="ent-body">
        <div class="ent-logo-row">
          {#if isImgLogo(entForm.logo)}
            <img class="ent-logo-preview" src={entForm.logo} alt="企业logo" />
          {:else}
            <span class="ent-logo-preview ent-logo-emoji">{entForm.logo || '🏭'}</span>
          {/if}
          <div class="ent-logo-actions">
            <span class="form-label">企业 Logo</span>
            <div class="ent-logo-btns">
              <label class="ent-upload">
                <input type="file" accept="image/*" onchange={onLogoUpload} />
                📤 上传图片
              </label>
              <button class="ent-mini" onclick={clearLogo}>清除</button>
            </div>
          </div>
        </div>
        <div class="ent-emoji-wrap">
          <span class="form-label">或选择 Logo（emoji）</span>
          <div class="ent-emoji-grid">
            {#each LOGO_EMOJIS as em}
              <button
                class="ent-emoji" class:sel={entForm.logo === em}
                onclick={() => { entForm.logo = em; entErr = ''; }}
              >{em}</button>
            {/each}
          </div>
        </div>
        <div class="form-group">
          <label class="form-label" for="ent-name">企业名称</label>
          <input id="ent-name" class="db-input" placeholder="请输入企业名称（如：示例制造公司）" bind:value={entForm.name} />
        </div>
        <div class="form-group">
          <label class="form-label" for="ent-industry">所属行业</label>
          <select id="ent-industry" class="db-input" bind:value={entForm.industry}>
            <option value="">请选择行业</option>
            {#each INDUSTRY_OPTIONS as ind}
              <option value={ind}>{ind}</option>
            {/each}
          </select>
        </div>
        {#if entErr}<div class="ent-err">✗ {entErr}</div>{/if}
        {#if entOk}<div class="ent-ok">✓ {entOk}</div>{/if}
      </div>
      <div class="ent-foot">
        <button class="btn-action btn-default" onclick={closeEnterprise}>取消</button>
        <button class="btn-action" onclick={doSaveEnterprise} disabled={entBusy}>
          {entBusy ? '保存中…' : '💾 保存设置'}
        </button>
      </div>
    </div>
  </div>
  {/if}

  <!-- ═══ 企业重置确认弹窗 ═══ -->
  {#if resetOpen}
  <div class="ent-overlay" role="presentation" onclick={(e) => { if (e.target === e.currentTarget) resetOpen = false; }}>
    <div class="ent-modal reset-modal" role="dialog" aria-modal="true" aria-label="重置企业">
      <div class="ent-head">
        <span class="card-icon">🔄</span>
        <span class="ent-title">重置企业数据</span>
        <button class="ent-close" onclick={() => (resetOpen = false)} aria-label="关闭">✕</button>
      </div>
      <div class="ent-body">
        <p class="reset-warn">确认重置「{(user && user.enterpriseName) || '当前企业'}」的全部数据？</p>
        <p class="reset-desc">将清空本企业的本体、知识库、资产与设置，随后重新进入引导配置（onboarding）。此操作不可恢复。</p>
        {#if resetBusy}<div class="ent-err">正在重置…</div>{/if}
      </div>
      <div class="ent-foot">
        <button class="btn-action btn-default" onclick={() => (resetOpen = false)} disabled={resetBusy}>取消</button>
        <button class="btn-action btn-danger" onclick={doResetEnterprise} disabled={resetBusy}>
          {resetBusy ? '重置中…' : '确认重置'}
        </button>
      </div>
    </div>
  </div>
  {/if}

  <footer class="statusbar">
    <span class="sb-left">{(user && user.enterpriseName) ? `${user.enterpriseName} · 本体问答系统` : '工厂智能体 · 本体问答系统'}</span>
    <span class="sb-right">数据本地处理 ｜ 运行时：Node.js + Python</span>
  </footer>
  {/if}
</div>

<style>
  /* ─── 企业品牌色（统一视觉系统）─── */
  :root {
    /* ── 中性阶（暖灰白，方案A） ── */
    --bg-page:      #F8FAFC;   /* 页面底（暖灰白） */
    --bg-elevated:  #F1F5F9;   /* 次级块 / 表头 / 折叠卡底 */
    --bg-card:      #FFFFFF;   /* 卡片 */
    --bg-hover:     #F8FAFC;   /* 行 hover */
    --bg-selected:  #CCFBF1;   /* 选中态（浅青绿） */

    /* ── 边框 ── */
    --border:       #E5E7EB;   /* 卡片/表分隔 */
    --border-strong:#D1D5DB;   /* 强调分隔（标签栏下、工具栏底） */

    /* ── 文字 ── */
    --text-primary:   #1F2937; /* gray-800 */
    --text-secondary: #6B7280;
    --text-muted:     #9CA3AF;

    /* ── 品牌与焦点（青绿主色） ── */
    --brand:       #0D9488;
    --brand-dark:  #0F766E;
    --brand-soft:  #F0FDFA;   /* 选中/激活浅青绿底 */
    --brand-line:  #99F6E4;
    --ring:        #5EEAD4;   /* focus-visible 焦点环 */

    /* ── 紫罗兰强调（决策/行动/重点/链接） ── */
    --accent:       #7C3AED;
    --accent-dark:  #6D28D9;
    --accent-soft:  #F3E8FF;
    --accent-line:  #DDD6FE;

    /* ── 语义色（只给状态） ── */
    --success: #10B981; --success-bg: #F0FDF4; --success-fg: #059669;
    --warning: #F59E0B; --warning-bg: #FFFBEB; --warning-fg: #B45309;
    --danger:  #EF4444; --danger-bg:  #FEF2F2; --danger-fg:  #B91C1C;
    --info:    #0D9488; --info-bg:    #F0FDFA; --info-fg:    #0F766E;

    /* ── 圆角与阴影 ── */
    --radius-sm: 6px;  --radius-md: 8px;  --radius-lg: 10px;  --radius-xl: 12px;
    --shadow-card:    0 1px 2px rgba(15,23,42,.05);
    --shadow-hover:   0 2px 8px  rgba(15,23,42,.07);
    --shadow-popover: 0 4px 16px rgba(15,23,42,.12);

    /* 兼容旧变量名（迁移过渡期） */
    --txt: var(--text-primary);
    --txt-sub: var(--text-secondary);
    --txt-muted: var(--text-muted);
    --line: var(--border);
    --line-soft: var(--bg-elevated);
    --card: var(--bg-card);
    --bg: var(--bg-page);
    --brand-soft-bd: var(--brand-line);
    --ok: var(--success); --warn: var(--warning); --err: var(--danger);
  }
  :global(*) { box-sizing: border-box; }
  :global(body) {
    margin: 0;
    font-family: 'Segoe UI', 'Microsoft YaHei', 'PingFang SC', sans-serif;
    background: var(--bg-page);
    color: var(--text-primary);
    min-height: 100vh;
  }
  :global(:focus-visible) { outline: 2px solid var(--ring); outline-offset: 1px; }


  .app { min-height: 100vh; display: flex; flex-direction: column; }

  /* ─── 启动会话校验 ─── */
  .auth-loading {
    min-height: 100vh; display: flex; align-items: center; justify-content: center; gap: 8px;
    color: var(--txt-sub); font-size: 13px; font-weight: 600;
  }

  /* ─── 登录页（品牌落地页）─── */
  .login-page {
    min-height: 100vh; display: flex; align-items: center; justify-content: center;
    padding: 24px; position: relative; overflow: hidden;
    background: linear-gradient(160deg, #f0fdfa 0%, #e6f7f5 55%, #f3eefe 100%);
  }
  /* 品牌光斑装饰 */
  .login-page::before, .login-page::after {
    content: ''; position: absolute; border-radius: 50%; filter: blur(60px); opacity: .5;
  }
  .login-page::before { width: 420px; height: 420px; top: -140px; right: -100px; background: #99f6e4; }
  .login-page::after  { width: 360px; height: 360px; bottom: -140px; left: -90px; background: #ddd6fe; }
  .login-card {
    position: relative; z-index: 1; width: 100%; max-width: 400px;
    background: var(--card); border: 1px solid var(--line);
    border-radius: 14px; box-shadow: 0 18px 50px rgba(15,23,42,.12);
    padding: 32px 30px 26px; display: flex; flex-direction: column;
    animation: fadeInUp .4s ease both;
  }
  .login-logo {
    width: 56px; height: 56px; margin: 0 auto 6px; display: flex; align-items: center; justify-content: center;
    font-size: 30px; background: var(--brand-soft); border: 1px solid var(--brand-soft-bd);
    border-radius: 14px;
  }
  .login-title { margin: 8px 0 2px; text-align: center; font-size: 22px; font-weight: 800; color: var(--txt); letter-spacing: .3px; }
  .login-title .acc { color: var(--brand); }
  .login-sub { margin: 0 0 20px; text-align: center; font-size: 12px; color: var(--txt-sub); }
  .login-tabs { display: flex; gap: 4px; margin-bottom: 16px; background: #f1f5f9; border: 1px solid var(--line-soft); border-radius: 8px; padding: 4px; }
  .login-tab {
    flex: 1; padding: 8px; border: none; border-radius: 6px; font-size: 13px; font-weight: 600;
    background: transparent; color: var(--txt-sub); cursor: pointer; transition: all .15s;
  }
  .login-tab:hover { color: var(--txt); }
  .login-tab.on { background: var(--brand); color: #fff; box-shadow: 0 2px 6px rgba(13,148,136,.3); }
  .login-input {
    width: 100%; margin-bottom: 12px; padding: 11px 13px; font-size: 14px;
    background: #fff; border: 1px solid #cbd5e1; border-radius: 8px; color: var(--txt); outline: none;
    transition: border-color .15s, box-shadow .15s;
  }
  .login-input:focus { border-color: var(--brand); box-shadow: 0 0 0 3px rgba(13,148,136,.12); }
  .login-err { margin-bottom: 10px; font-size: 12px; color: var(--err); background: #fef2f2; border: 1px solid #fecaca; border-radius: 6px; padding: 7px 10px; }
  .login-btn {
    width: 100%; padding: 12px; border: none; border-radius: 8px;
    background: var(--brand); color: #fff; font-size: 14px; font-weight: 700; letter-spacing: 2px;
    cursor: pointer; transition: background .15s, transform .05s;
  }
  .login-btn:hover:not(:disabled) { background: var(--brand-dark); }
  .login-btn:active:not(:disabled) { transform: translateY(1px); }
  .login-btn:disabled { background: #94a3b8; cursor: not-allowed; }
  .login-hint { margin: 14px 0 0; text-align: center; font-size: 11px; color: var(--txt-muted); }

  /* ─── 引导 onboarding ─── */
  .onboard-page {
    min-height: 100vh; display: flex; align-items: flex-start; justify-content: center;
    padding: 40px 20px; background: linear-gradient(160deg, #f0fdfa 0%, #f5f3ff 100%);
  }
  .onboard-card {
    width: 100%; max-width: 560px; background: var(--card); border: 1px solid var(--line);
    border-radius: 14px; box-shadow: 0 14px 44px rgba(15,23,42,.10); padding: 28px;
    animation: fadeInUp .4s ease both;
  }
  .onboard-progress { display: flex; align-items: center; gap: 6px; margin-bottom: 22px; }
  .ob-step { font-size: 12px; font-weight: 600; color: var(--txt-muted); padding: 4px 10px; border-radius: 999px; background: #f1f5f9; }
  .ob-step.done { color: var(--ok); background: #f0fdf4; }
  .ob-step.cur { color: var(--brand); background: var(--brand-soft); border: 1px solid var(--brand-soft-bd); }
  .ob-arrow { color: #cbd5e1; font-size: 12px; }
  .onboard-title { margin: 0 0 6px; font-size: 19px; font-weight: 800; color: var(--txt); }
  .onboard-sub { margin: 0 0 18px; font-size: 13px; color: var(--txt-sub); line-height: 1.6; }
  .onboard-industries { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-bottom: 16px; }
  .ob-industry {
    display: flex; flex-direction: column; align-items: center; gap: 6px; padding: 14px 8px;
    background: #fff; border: 1px solid var(--line-soft); border-radius: 8px; cursor: pointer;
    transition: all .15s;
  }
  .ob-industry:hover { border-color: var(--brand); background: var(--brand-soft); }
  .ob-industry.sel { border-color: var(--brand); background: var(--brand-soft); box-shadow: 0 0 0 2px rgba(13,148,136,.15); }
  .ob-ind-icon { font-size: 24px; }
  .ob-ind-name { font-size: 12px; font-weight: 600; color: var(--txt); }

  /* ─── 顶部工具栏 ─── */
  .toolbar {
    display: flex; align-items: center; justify-content: space-between;
    padding: 10px 18px;
    background: #ffffff;
    border-bottom: 1px solid #d5dbe3;
    box-shadow: 0 1px 2px rgba(0,0,0,0.04);
    flex-shrink: 0;
  }
  .toolbar-left { display: flex; align-items: center; gap: 12px; }
  .toolbar-right { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .model-select { display: flex; align-items: center; gap: 6px; font-size: 11px; color: #64748b; }
  .model-label { font-weight: 600; }
  .model-select select {
    background: #fff; border: 1px solid #d5dbe3; border-radius: 4px;
    padding: 4px 8px; font-size: 12px; color: #1e293b; cursor: pointer;
  }
  .model-select select:focus { outline: none; border-color: var(--brand); }
  .logo {
    font-size: 22px; line-height: 1;
    width: 34px; height: 34px; display: flex; align-items: center; justify-content: center;
    background: var(--brand-soft); border: 1px solid var(--brand-soft-bd); border-radius: 8px;
  }
  .logo-img {
    width: 32px; height: 32px; border-radius: 8px; object-fit: contain;
    background: var(--brand-soft); border: 1px solid var(--line-soft); padding: 2px;
  }
  .brand-name {
    font-size: 15px; font-weight: 800; color: var(--txt); letter-spacing: 0.2px;
    display: flex; align-items: center; gap: 7px;
  }
  .brand-name::before {
    content: ''; width: 4px; height: 14px; border-radius: 2px; background: var(--brand);
  }
  .brand-sub { font-size: 11px; color: #8892a4; letter-spacing: 0.4px; }

  /* ─── 企业设置按钮 ─── */
  .ent-btn {
    display: inline-flex; align-items: center; gap: 5px;
    background: #fff; color: #475569; border: 1px solid #d5dbe3; border-radius: 8px;
    padding: 6px 12px; font-size: 12px; font-weight: 600; cursor: pointer;
    transition: all 0.15s; white-space: nowrap;
  }
  .ent-btn .btn-icon { font-size: 13px; opacity: 0.85; }
  .ent-btn:hover { background: var(--brand-soft); border-color: var(--brand-line); color: var(--brand); }
  .ent-btn:hover .btn-icon { opacity: 1; }
  /* 危险操作(重置)：柔和深红而非刺眼亮红，商用软件的低调危险色 */
  .ent-btn-danger { color: #9f1239; border-color: #fecdd3; background: #fff5f6; }
  .ent-btn-danger:hover { background: #ffe4e6; border-color: #fda4af; color: #be123c; }

  /* ─── 卡片化功能分区（建模 tab）─── */
  .card {
    background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius-lg);
    box-shadow: var(--shadow-card);
    overflow: hidden; transition: box-shadow 0.2s;
  }
  .card:hover { box-shadow: var(--shadow-hover); }
  .card-head {
    display: flex; align-items: center; gap: 10px;
    padding: 12px 14px; background: var(--bg-elevated);
    border-bottom: 1px solid var(--bg-hover);
  }
  .card-head-btn {
    width: 100%; border: none; text-align: left; cursor: pointer;
    background: var(--bg-elevated); font: inherit;
    transition: background 0.15s;
  }
  .card-head-btn:hover { background: var(--bg-hover); }
  .card-head-btn:hover .card-title { color: var(--brand); }
  /* 折叠卡展开态：左侧品牌色指示条 */
  .card-head-btn.expanded { box-shadow: inset 3px 0 0 0 var(--brand); }
  .card-icon {
    font-size: 18px; flex-shrink: 0;
    width: 34px; height: 34px; border-radius: 9px;
    display: flex; align-items: center; justify-content: center;
    background: var(--brand-soft); border: 1px solid var(--brand-line);
  }
  .card-icon.ic-file  { background: var(--info-bg); border-color: var(--brand-line); }
  .card-icon.ic-db    { background: #f0f9ff; border-color: #e0f2fe; }
  .card-icon.ic-model { background: #f5f3ff; border-color: #ede9fe; }
  .card-titles { flex: 1; display: flex; flex-direction: column; gap: 1px; }
  .card-title { font-size: 13px; font-weight: 700; color: var(--text-primary); }
  .card-desc { font-size: 11px; color: var(--text-muted); }
  .card-body { padding: 12px 14px; display: flex; flex-direction: column; gap: 10px; }
  .btn-example { background: var(--text-primary); }
  .btn-example:hover:not(:disabled) { background: #0f172a; }

  .status-indicator {
    display: flex; align-items: center; gap: 8px;
    font-size: 12px; font-weight: 500;
    padding: 5px 14px; border-radius: 4px;
    border: 1px solid transparent;
    background: #f1f5f9;
  }
  .status-dot { width: 8px; height: 8px; border-radius: 50%; background: #94a3b8; }
  .st-info .status-dot { background: var(--brand); }
  .st-ok   .status-dot { background: #16a34a; }
  .st-err  .status-dot { background: #dc2626; }
  .st-info .status-text { color: var(--brand); }
  .st-ok   .status-text { color: #16a34a; }
  .st-err  .status-text { color: #dc2626; }
  .st-ok  { background: #f0fdf4; border-color: #bbf7d0; }
  .st-err { background: #fef2f2; border-color: #fecaca; }
  .st-info{ background: var(--brand-soft); border-color: var(--brand-line); }

  /* ─── 标签栏 ─── */
  .tabbar {
    display: flex; gap: 4px; padding: 10px 18px 0;
    flex-shrink: 0; border-bottom: 1px solid var(--border-strong);
  }
  .tab {
    display: flex; align-items: center; gap: 6px;
    background: transparent; border: none;
    border-radius: 8px 8px 0 0;
    padding: 9px 18px; font-size: 13px; font-weight: 600; color: var(--text-secondary);
    cursor: pointer; transition: color 0.15s, background 0.15s; position: relative;
  }
  .tab:hover { color: var(--text-primary); background: var(--bg-hover); }
  .tab.active {
    background: var(--bg-card); color: var(--brand); font-weight: 700;
    box-shadow: 0 -2px 8px rgba(0,0,0,0.05);
  }
  .tab.active::after {
    content: ''; position: absolute; left: 14px; right: 14px; bottom: 0;
    height: 3px; border-radius: 3px 3px 0 0; background: var(--brand);
  }
  .tab-icon { font-size: 14px; }

  /* ─── 主区域 ─── */
  .workspace {
    flex: 1; display: grid;
    grid-template-columns: minmax(200px, 240px) 1fr;
    gap: 14px; padding: 14px 18px;
    min-height: 0;
  }
  .pane-full {
    grid-column: 1 / -1;
    background: #ffffff;
    border: 1px solid #d5dbe3; border-radius: 4px;
    display: flex; flex-direction: column; min-height: 0;
  }
  .dashboard-body { padding: 14px; overflow-y: auto; }
  .graph-body { padding: 14px; overflow-y: auto; }
  .graph-empty { color: #94a3b8; font-size: 13px; text-align: center; padding: 60px 20px; }
  .query-body { padding: 14px; display: flex; flex-direction: column; gap: 12px; }

  .pane {
    background: #ffffff;
    border: 1px solid #d5dbe3;
    border-radius: 4px;
    display: flex; flex-direction: column;
    min-height: 0;
  }
  /* ─── 页头（商用惯例：20px 标题 + 13px 副标题说明） ─── */
  .pane-title {
    padding: 14px 18px;
    font-size: 16px; font-weight: 700; color: var(--text-primary);
    background: var(--bg-elevated);
    border-bottom: 1px solid var(--border);
    letter-spacing: 0.3px;
    flex-shrink: 0;
    display: flex; flex-direction: column; gap: 3px;
  }
  .pane-title .pane-sub {
    font-size: 12px; font-weight: 400; color: var(--text-muted); letter-spacing: 0;
  }
  .pane-title .btn-model-toggle {
    margin-left: auto; font-size: 12px; font-weight: 600;
    padding: 4px 12px; border-radius: 6px; border: 1px solid var(--brand-line);
    background: var(--brand-soft); color: var(--brand); cursor: pointer;
    transition: background 0.15s, color 0.15s;
  }
  .pane-title .btn-model-toggle:hover { background: var(--brand); color: #fff; }

  /* ─── 左栏表单 ─── */
  .pane-left { padding: 14px; gap: 14px; overflow-y: auto; }
  .form-group { display: flex; flex-direction: column; gap: 6px; }
  .form-label { font-size: 12px; color: #64748b; font-weight: 600; }

  /* ─── 多文件列表 ─── */
  .file-list {
    display: flex; flex-direction: column; gap: 6px;
    border: 1px solid #e2e8f0; border-radius: 4px; padding: 8px;
    background: #f8fafc; max-height: 180px; overflow-y: auto;
  }
  .file-remove {
    flex-shrink: 0; width: 18px; height: 18px; border: none; border-radius: 3px;
    background: #fef2f2; color: #dc2626; cursor: pointer; font-size: 11px; line-height: 1;
    transition: background 0.15s;
  }
  .file-remove:hover { background: #fee2e2; }

  /* ─── 默认示例：次级按钮（模型配置等）─── */
  .btn-default { background: #1e293b; }
  .btn-default:hover:not(:disabled) { background: #0f172a; }

  /* ─── 数据文件选择器（单一入口，默认示例目录，目录导航 + Ctrl/Shift 多选）─── */
  .example-empty { font-size: 12px; color: #94a3b8; padding: 8px 2px; }
  .example-item {
    display: flex; align-items: center; gap: 6px; padding: 4px 6px;
    border-radius: 3px; cursor: pointer; transition: background 0.15s;
    border: 1px solid transparent;
  }
  .example-item:hover { background: #f1f5f9; }
  .example-item.selected { background: var(--brand-soft); border-color: var(--brand); }
  .example-item.selected .example-item-name { color: var(--brand); font-weight: 600; }
  .example-item-name {
    flex: 1; color: #1e293b; white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis; font-size: 12px;
  }
  .browse-count { font-size: 12px; color: var(--accent); font-weight: 600; }
  .browse-toggle { justify-content: flex-start; }
  .browse-panel {
    display: flex; flex-direction: column; gap: 8px;
    border: 1px solid #e2e8f0; border-radius: 4px; padding: 10px;
    background: #fbfcfe;
  }
  .browse-hint { font-size: 11px; color: #64748b; }
  .browse-actions {
    display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-top: 2px;
  }
  .browse-current {
    font-size: 11px; color: #1e293b; font-weight: 600;
    background: var(--brand-soft); border: 1px solid var(--brand-line); border-radius: 3px;
    padding: 4px 8px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .browse-nav { display: flex; flex-direction: column; gap: 4px; }
  .browse-up {
    align-self: flex-start; border: 1px solid #cbd5e1; border-radius: 3px;
    background: #fff; color: var(--accent); font-size: 12px; cursor: pointer;
    padding: 3px 8px; transition: background 0.15s;
  }
  .browse-up:hover { background: var(--brand-soft); }
  .browse-dirs { display: flex; flex-wrap: wrap; gap: 4px; }
  .browse-dir {
    border: 1px solid #cbd5e1; border-radius: 3px; background: #f1f5f9;
    color: #1e293b; font-size: 12px; cursor: pointer; padding: 2px 8px;
    transition: background 0.15s;
  }
  .browse-dir:hover { background: #e2e8f0; }
  .browse-files { max-height: 200px; }
  .browse-file-path {
    flex-shrink: 0; max-width: 40%; color: #94a3b8; font-size: 10px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }

  .attr-chips { display: flex; flex-wrap: wrap; gap: 6px; padding: 10px 12px; }
  .attr-chip {
    background: var(--brand-soft); color: var(--brand); border: 1px solid var(--brand-line);
    border-radius: 3px; padding: 3px 8px; font-size: 11px;
    font-family: 'Consolas', monospace;
  }

  /* ─── 数据库接入折叠区 ─── */
  .db-collapse {
    border: 1px solid #e2e8f0; border-radius: 4px; overflow: hidden;
  }
  .db-toggle {
    width: 100%; display: flex; align-items: center; gap: 8px;
    padding: 10px 12px; background: #f8fafc; border: none;
    font-size: 12px; font-weight: 700; color: #1e293b; cursor: pointer;
    text-align: left; transition: background 0.15s;
  }
  .db-toggle:hover { background: #f1f5f9; color: var(--accent); }
  .db-body { padding: 12px; display: flex; flex-direction: column; gap: 10px; border-top: 1px solid #e2e8f0; }
  .db-hint { font-size: 11px; color: #64748b; background: var(--brand-soft); border: 1px solid var(--brand-line); border-radius: 4px; padding: 6px 10px; }
  .db-row { display: flex; gap: 10px; }
  .db-row .form-group { flex: 1; }
  .db-port { flex: 0 0 90px; }
  .db-input {
    width: 100%; background: #fff; border: 1px solid #cbd5e1;
    border-radius: 4px; padding: 7px 10px; font-size: 12px; color: #1e293b;
    outline: none; transition: border-color 0.15s;
  }
  .db-input:focus { border-color: var(--brand); }
  .db-result { border: 1px solid #e2e8f0; border-radius: 4px; }
  .db-result .model-head { border-bottom: none; }
  .db-result.db-err { border-color: #fecaca; background: #fef2f2; }
  .db-err-text { padding: 8px 12px; font-size: 12px; color: #b91c1c; line-height: 1.5; }

  /* ─── 模型配置管理 ─── */
  .m-edit {
    border: 1px solid #e2e8f0; border-radius: 4px; padding: 10px;
    display: flex; flex-direction: column; gap: 8px; background: #fbfcfe;
  }
  .m-edit-head { display: flex; align-items: center; gap: 10px; }
  .m-radio { display: flex; align-items: center; gap: 4px; cursor: pointer; font-size: 12px; }
  .m-active-tag {
    font-size: 11px; font-weight: 700; color: #16a34a;
    background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 3px; padding: 2px 8px;
  }
  .m-key { flex: 1; font-size: 11px; color: #94a3b8; font-family: 'Consolas', monospace; }
  .m-actions { display: flex; gap: 8px; }

  /* ─── 向量检索(embedding)模型 ─── */
  .m-embed {
    border: 1px dashed var(--brand-line); border-radius: 4px; padding: 10px;
    display: flex; flex-direction: column; gap: 8px; background: var(--brand-soft);
  }
  .m-embed-title { font-size: 12px; font-weight: 700; color: var(--brand-dark); }

  .file-icon { font-size: 16px; }

  /* ─── 本地文件建模：上传区（对齐成熟商业软件的上传体验） ─── */
  .file-upload-zone { display: flex; flex-direction: column; gap: 10px; }
  .file-input {
    display: flex; align-items: center; gap: 12px;
    border: 1.5px dashed #cbd5e1; border-radius: 8px;
    padding: 14px 16px; cursor: pointer; background: #f8fafc;
    transition: border-color 0.15s, background 0.15s;
  }
  .file-input:hover { border-color: var(--brand); background: var(--brand-soft); }
  .file-input input[type="file"] { display: none; }
  .file-upload-icon { font-size: 24px; flex-shrink: 0; }
  .file-upload-main { flex: 1; display: flex; flex-direction: column; gap: 2px; }
  .file-upload-title { font-size: 13px; font-weight: 600; color: #1e293b; }
  .file-upload-hint { font-size: 11px; color: #64748b; }
  .file-upload-btn {
    font-size: 12px; font-weight: 600; color: var(--brand);
    border: 1px solid var(--brand); border-radius: 4px; padding: 5px 12px;
    white-space: nowrap; transition: background 0.15s;
  }
  .file-input:hover .file-upload-btn { background: var(--brand); color: #fff; }
  .file-selected { display: flex; flex-direction: column; gap: 6px; }
  .file-selected-title { font-size: 11px; font-weight: 600; color: #64748b; }
  .file-selected-list { display: flex; flex-wrap: wrap; gap: 6px; }
  .file-selected-chip {
    display: inline-flex; align-items: center; gap: 4px;
    background: var(--brand-soft); color: var(--brand-dark); border: 1px solid var(--brand-line);
    border-radius: 4px; padding: 3px 8px; font-size: 12px;
  }

  .btn-action {
    display: flex; align-items: center; justify-content: center; gap: 8px;
    background: var(--brand); color: #fff;
    border: none; border-radius: 4px;
    padding: 10px 16px; font-size: 13px; font-weight: 600;
    cursor: pointer; transition: background 0.15s;
  }
  .btn-action:hover:not(:disabled) { background: var(--brand-dark); }
  .btn-action:disabled { background: #94a3b8; cursor: not-allowed; }
  .btn-small { padding: 6px 12px; font-size: 12px; border-radius: 4px; }

  /* ─── 建模结果摘要 ─── */
  .model-summary { display: flex; align-items: center; gap: 8px; padding: 4px 12px 0; font-size: 13px; }
  .sum-tables { font-weight: 700; color: #1e293b; }
  .sum-fields { color: var(--brand); font-weight: 600; }
  .sum-ok { margin-left: auto; color: #16a34a; font-size: 12px; font-weight: 600; }
  .sum-sep { color: #94a3b8; }
  .model-files { padding: 8px 12px 0; }
  .model-files-title { font-size: 11px; color: #64748b; font-weight: 600; margin-bottom: 5px; }
  .model-files-list { display: flex; flex-wrap: wrap; gap: 6px; }
  .model-file-chip {
    background: #f8fafc; color: #334155; border: 1px solid #e2e8f0;
    border-radius: 3px; padding: 2px 8px; font-size: 11px;
    font-family: 'Consolas', monospace;
  }
  .model-actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; padding: 10px 12px; border-top: 1px dashed #e2e8f0; margin-top: 6px; }
  .model-hint { font-size: 11px; color: #94a3b8; flex: 1; }
  .example-link {
    display: inline-flex; align-items: center; gap: 4px;
    background: none; border: none; padding: 2px 4px;
    font-size: 12px; color: #64748b; cursor: pointer;
  }
  .example-link:hover:not(:disabled) { color: var(--accent); text-decoration: underline; }
  .example-link:disabled { color: #94a3b8; cursor: not-allowed; }
  .ind-select-row { display: flex; align-items: center; gap: 8px; margin-top: 8px; flex-wrap: wrap; }
  .ind-select { display: flex; align-items: center; gap: 6px; font-size: 11px; color: #64748b; }
  .ind-label { font-weight: 600; white-space: nowrap; }
  .ind-select select {
    background: #fff; border: 1px solid #cbd5e1; border-radius: 4px;
    padding: 5px 8px; font-size: 12px; color: #1e293b; cursor: pointer; outline: none;
  }
  .ind-select select:focus { border-color: var(--brand); }
  .btn-icon { font-size: 14px; }

  /* ─── 字段表 ─── */
  .model-panel { border: 1px solid #e2e8f0; border-radius: 4px; overflow: hidden; }
  .model-head {
    display: flex; justify-content: space-between; align-items: center;
    padding: 8px 12px; background: #f8fafc;
    border-bottom: 1px solid #e2e8f0;
  }
  .model-table { font-size: 12px; font-weight: 700; color: #1e293b; }
  .model-count { font-size: 11px; color: #64748b; }

  /* ─── 右栏 ─── */
  .pane-right { padding: 14px; gap: 12px; }
  .query-bar { display: flex; gap: 8px; }
  .query-input {
    flex: 1; background: #fff; border: 1px solid #cbd5e1;
    border-radius: 4px; padding: 9px 12px;
    font-size: 14px; color: #1e293b; outline: none;
    transition: border-color 0.15s;
  }
  .query-input:focus { border-color: var(--brand); }
  .query-input:disabled { background: #f8fafc; }

  .btn-query {
    background: #1e293b; color: #fff; border: none; border-radius: 4px;
    padding: 9px 20px; font-size: 13px; font-weight: 600; cursor: pointer;
    transition: background 0.15s;
  }
  .btn-query:hover:not(:disabled) { background: #0f172a; }
  .btn-query:disabled { background: #94a3b8; cursor: not-allowed; }

  .quick-bar { display: flex; gap: 6px; flex-wrap: wrap; }
  .quick-btn {
    background: #f1f5f9; color: #334155; border: 1px solid #e2e8f0;
    font-size: 11px; padding: 4px 10px; border-radius: 3px;
    cursor: pointer; transition: all 0.15s;
  }
  .quick-btn:hover:not(:disabled) { background: #e2e8f0; border-color: #cbd5e1; }
  .quick-btn:disabled { opacity: 0.5; cursor: not-allowed; }

  /* ─── 结果区 ─── */
  .result-panel {
    flex: 1; border: 1px solid #e2e8f0; border-radius: 4px;
    background: #fbfcfe; min-height: 220px;
    display: flex; flex-direction: column; overflow: hidden;
  }
  .result-head {
    padding: 7px 12px; background: #f8fafc;
    border-bottom: 1px solid #e2e8f0;
  }
  .result-label { font-size: 11px; font-weight: 700; color: #64748b; letter-spacing: 0.5px; }
  .result-scroll { flex: 1; overflow-y: auto; }
  .result-text {
    margin: 0; padding: 14px;
    font-family: 'Consolas', 'Menlo', monospace;
    font-size: 13px; line-height: 1.7; color: #1e293b;
    white-space: pre-wrap; word-break: break-word;
  }
  .ans-body { padding: 14px; font-size: 13px; line-height: 1.8; color: #1e293b; }
  :global(.ans-head) { font-weight: 700; color: #1e293b; margin: 6px 0 4px; }
  :global(.ans-list) { margin: 0 0 8px; padding-left: 20px; }
  :global(.ans-list li) { margin: 2px 0; }
  :global(.ans-table) { width: 100%; border-collapse: collapse; margin: 6px 0 10px; }
  :global(.ans-table th), :global(.ans-table td) {
    border: 1px solid #e2e8f0; padding: 6px 10px; text-align: left;
    font-size: 12px; color: #334155;
  }
  :global(.ans-table th) { background: #f1f5f9; color: #475569; font-weight: 600; }

  /* ─── 证据溯源 ─── */
  .evidence-wrap { border-top: 1px solid #e2e8f0; background: #f8fafc; }
  .evidence-toggle {
    width: 100%; display: flex; align-items: center; gap: 6px;
    padding: 8px 12px; background: transparent; border: none;
    font-size: 12px; font-weight: 600; color: #475569; cursor: pointer;
    text-align: left; transition: background 0.15s;
  }
  .evidence-toggle:hover { background: #f1f5f9; color: var(--accent); }
  .chevron { margin-left: auto; font-size: 11px; color: #94a3b8; }
  .evidence-body { padding: 4px 12px 12px; border-top: 1px dashed #e2e8f0; }
  :global(.ev-list) { margin: 6px 0 0; padding-left: 18px; font-size: 12px; color: #334155; }
  :global(.ev-list li) { margin: 3px 0; line-height: 1.6; }
  :global(.ev-k) { color: var(--accent); font-weight: 600; }
  :global(.ev-text) { padding: 8px 0; font-size: 12px; color: #334155; }
  .result-empty {
    flex: 1; display: flex; align-items: center; justify-content: center;
    color: #94a3b8; font-size: 12px;
  }
  .analysis-body { flex: 1; overflow-y: auto; padding: 14px; }
  .loading { gap: 6px; }
  .load-dot {
    width: 6px; height: 6px; border-radius: 50%; background: var(--brand);
    animation: bounce 1.2s infinite;
  }
  .load-dot:nth-child(2) { animation-delay: 0.15s; }
  .load-dot:nth-child(3) { animation-delay: 0.3s; }
  @keyframes bounce { 0%, 60%, 100% { transform: translateY(0); } 30% { transform: translateY(-4px); } }
  @keyframes fadeInUp { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: none; } }
  @keyframes shimmer { 0% { background-position: -360px 0; } 100% { background-position: 360px 0; } }

  /* ─── 骨架屏（问答/看板/评测/知识库加载占位）─── */
  .skel {
    border-radius: 6px;
    background: linear-gradient(90deg, #eef2f7 25%, #f8fafc 40%, #eef2f7 55%);
    background-size: 720px 100%; animation: shimmer 1.4s infinite linear;
  }
  .skel-block { display: flex; flex-direction: column; gap: 12px; padding: 16px; }
  .skel-line-sm { height: 12px; width: 30%; }
  .skel-line-md { height: 14px; width: 70%; }
  .skel-line-lg { height: 14px; width: 100%; }
  .skel-card {
    border: 1px solid var(--line-soft); border-radius: 6px; background: #fff;
    padding: 14px; display: flex; flex-direction: column; gap: 12px;
  }
  .skel-row { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
  .skel-kpi { height: 74px; }

  /* ─── 建模进行中骨架（本地/示例/数据库建模 busy 时展示）─── */
  .model-busy-overlay {
    position: relative; overflow: hidden;
    background: var(--brand-soft); border: 1px solid var(--brand-soft-bd);
    border-radius: 8px; padding: 12px 14px; display: flex; align-items: center; gap: 10px;
  }
  .model-busy-spin {
    width: 16px; height: 16px; flex-shrink: 0; border-radius: 50%;
    border: 2px solid var(--brand-soft-bd); border-top-color: var(--brand);
    animation: bounce 1.2s infinite;
  }
  .model-busy-text { font-size: 13px; font-weight: 600; color: var(--brand); }

  /* ─── 问答结果反馈动效（进入动画 + 结果头脉冲）─── */
  .result-fade { animation: fadeInUp .35s ease both; }
  .query-thinking {
    display: flex; align-items: center; gap: 7px; margin-top: 6px;
    color: var(--txt-muted); font-size: 12px; font-weight: 500;
  }
  .ans-body, .result-text { animation: fadeInUp .4s ease both; }
  .result-label::after {
    content: ''; display: inline-block; width: 6px; height: 6px; margin-left: 7px;
    border-radius: 50%; background: var(--ok); animation: pulse 1.6s ease infinite;
  }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: .25; } }


  /* ─── 底部状态栏 ─── */
  .statusbar {
    display: flex; justify-content: space-between;
    padding: 5px 18px; background: #ffffff;
    border-top: 1px solid #d5dbe3;
    font-size: 11px; color: #8892a4;
    flex-shrink: 0;
  }

  /* ─── 企业设置弹窗 ─── */
  .ent-overlay {
    position: fixed; inset: 0; z-index: 1000;
    background: rgba(15, 23, 42, 0.42);
    display: flex; align-items: center; justify-content: center;
    padding: 16px;
  }
  .ent-modal {
    width: 100%; max-width: 460px; max-height: 90vh; overflow-y: auto;
    background: #ffffff; border-radius: 12px;
    box-shadow: 0 12px 40px rgba(15, 23, 42, 0.25);
    display: flex; flex-direction: column;
  }
  .ent-head {
    display: flex; align-items: center; gap: 8px;
    padding: 14px 16px; background: #f8fafc;
    border-bottom: 1px solid #eef1f6;
  }
  .ent-title { flex: 1; font-size: 15px; font-weight: 700; color: #1e293b; }
  .ent-close {
    width: 26px; height: 26px; border: none; border-radius: 6px;
    background: #eef1f6; color: #64748b; font-size: 13px; cursor: pointer;
    transition: background 0.15s;
  }
  .ent-close:hover { background: #e2e8f0; color: #1e293b; }
  .ent-body { padding: 16px; display: flex; flex-direction: column; gap: 14px; }
  .ent-logo-row { display: flex; align-items: center; gap: 14px; }
  .ent-logo-preview {
    width: 58px; height: 58px; border-radius: 12px;
    background: #f1f5f9; border: 1px solid #e2e8f0;
    object-fit: contain; display: flex; align-items: center; justify-content: center;
    flex-shrink: 0;
  }
  .ent-logo-emoji { font-size: 32px; }
  .ent-logo-actions { display: flex; flex-direction: column; gap: 6px; }
  .ent-logo-btns { display: flex; gap: 8px; align-items: center; }
  .ent-upload {
    display: inline-flex; align-items: center; gap: 4px;
    background: var(--brand); color: #fff; border-radius: 6px;
    padding: 5px 10px; font-size: 12px; font-weight: 600; cursor: pointer;
    transition: background 0.15s;
  }
  .ent-upload:hover { background: var(--brand-dark); }
  .ent-upload input { display: none; }
  .ent-mini {
    background: #f1f5f9; color: #64748b; border: 1px solid #e2e8f0;
    border-radius: 6px; padding: 5px 10px; font-size: 12px; cursor: pointer;
    transition: background 0.15s;
  }
  .ent-mini:hover { background: #e2e8f0; color: #1e293b; }
  .ent-emoji-wrap { display: flex; flex-direction: column; gap: 6px; }
  .ent-emoji-grid { display: grid; grid-template-columns: repeat(9, 1fr); gap: 4px; }
  .ent-emoji {
    aspect-ratio: 1; border: 1px solid #e4e9f0; border-radius: 8px;
    background: #fbfcfe; font-size: 18px; cursor: pointer;
    display: flex; align-items: center; justify-content: center;
    transition: all 0.12s;
  }
  .ent-emoji:hover { background: var(--brand-soft); border-color: var(--brand); }
  .ent-emoji.sel { background: var(--brand-soft); border-color: var(--brand); box-shadow: 0 0 0 2px rgba(13,148,136,.2); }
  .ent-err { font-size: 12px; color: #b91c1c; background: #fef2f2; border: 1px solid #fecaca; border-radius: 6px; padding: 7px 10px; }
  .ent-ok { font-size: 12px; color: #16a34a; background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 6px; padding: 7px 10px; }
  .ent-foot {
    display: flex; justify-content: flex-end; gap: 10px;
    padding: 13px 16px; background: #f8fafc;
    border-top: 1px solid #eef1f6;
  }
  .reset-modal { max-width: 420px; }
  .reset-warn { font-size: 14px; font-weight: 700; color: #1e293b; }
  .reset-desc { font-size: 12px; color: #64748b; line-height: 1.7; }
  .btn-danger { background: #dc2626; }
  .btn-danger:hover:not(:disabled) { background: #b91c1c; }

  ::-webkit-scrollbar { width: 6px; height: 6px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 3px; }
  ::-webkit-scrollbar-thumb:hover { background: #94a3b8; }

  /* ═══ 响应式断点（移动端适配）═══ */
  @media (max-width: 1024px) {
    .workspace { grid-template-columns: minmax(280px, 330px) 1fr; gap: 12px; }
    .onboard-industries { grid-template-columns: repeat(3, 1fr); }
  }

  @media (max-width: 860px) {
    .workspace { grid-template-columns: 1fr; }          /* 左/右栏上下堆叠 */
    .pane-left, .pane-right { padding: 12px; }
    .db-row { flex-direction: column; }                  /* 表单项纵向排列 */
    .db-port { flex: 1; }
    .toolbar-right { justify-content: flex-end; }
    .status-indicator { max-width: 100%; }
  }

  @media (max-width: 720px) {
    .toolbar { flex-direction: column; align-items: stretch; gap: 8px; padding: 10px 14px; }
    .toolbar-left { justify-content: space-between; }
    .toolbar-right { flex-wrap: wrap; gap: 8px; justify-content: flex-start; }
    .brand-sub { display: none; }                        /* 窄屏隐藏副标题 */
    .model-select { flex-wrap: wrap; }
    .tabbar {
      padding: 8px 10px 0; gap: 6px; overflow-x: auto;
      -webkit-overflow-scrolling: touch; scrollbar-width: none;
    }
    .tabbar::-webkit-scrollbar { display: none; }
    .tab { flex: 0 0 auto; padding: 8px 13px; font-size: 12px; white-space: nowrap; }
    .workspace { padding: 10px 12px; }
    .query-bar { flex-direction: column; }               /* 查询输入+按钮纵向 */
    .btn-query { width: 100%; padding: 11px; }
    .onboard-industries { grid-template-columns: repeat(2, 1fr); }
    .ent-emoji-grid { grid-template-columns: repeat(6, 1fr); }
    .kpi-row { grid-template-columns: repeat(2, 1fr); }
  }

  @media (max-width: 480px) {
    .login-page, .onboard-page { padding: 16px; }
    .login-card { padding: 26px 20px 20px; }
    .onboard-card { padding: 20px 16px; }
    .onboard-progress { flex-wrap: wrap; }
    .onboard-industries { grid-template-columns: repeat(2, 1fr); }
    .ent-overlay { padding: 10px; }
    .ent-modal { max-height: 94vh; }
    .ent-logo-row { flex-direction: column; align-items: flex-start; }
    .skel-row { grid-template-columns: 1fr; }
    .statusbar { flex-direction: column; gap: 3px; align-items: flex-start; }
    .tab { padding: 7px 10px; }
  }

  /* ─── 统一动效（Linear/Vercel 标准：120-200ms ease-out） ─── */
  :global(*) { transition-timing-function: cubic-bezier(0.16,1,0.3,1); }
  @media (prefers-reduced-motion: reduce) {
    :global(*) { transition: none !important; animation: none !important; }
  }

  /* ─── 深色模式（可选，prefers-color-scheme） ─── */
  @media (prefers-color-scheme: dark) {
    :root {
      --bg-page: #16181d; --bg-elevated: #1e2128; --bg-card: #22262e;
      --bg-hover: #2a2e37; --bg-selected: #1e2a45;
      --border: #2e333d; --border-strong: #3a404c;
      --text-primary: #e8eaed; --text-secondary: #a6aeb9; --text-muted: #6b7380;
      --brand-soft: #1e2a45; --brand-line: #33507a;
      --success-bg: #14301f; --success-fg: #4ade80;
      --warning-bg: #33290f; --warning-fg: #fbbf24;
      --danger-bg: #33131a;  --danger-fg: #f87171;
      --info-bg: #1e2a45;    --info-fg: #60a5fa;
      --shadow-card: 0 1px 2px rgba(0,0,0,.4);
      --shadow-hover: 0 2px 8px rgba(0,0,0,.5);
      --shadow-popover: 0 4px 16px rgba(0,0,0,.6);
    }
  }
</style>
