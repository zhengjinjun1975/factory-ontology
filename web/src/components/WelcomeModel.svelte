<script>
  // WelcomeModel — 建模区域科幻风格 SVG 企业欢迎页（替代空白空态）
  // 视觉：深色渐变背景 + 星海/粒子 + 光效 + HUD 科幻元素；中文科幻排版。
  // 跟随当前激活 kb：切换行业（化工→阀门）→ 企业欢迎词/建模示例跟随。
  // 建模示例可点击 → 复用 doDefaultExample 直接建模。
  import { onMount } from 'svelte';

  let { kb = '', kbList = [], industries = [], industry = '', onModel = () => {} } = $props();

  // 当前行业：优先按企业所属行业(中文名)识别；其次按激活 kb 推断；兜底阀门制造。
  // 一企业一行业一数据：企业绑定的是专属 kb(ent_xxx)，用 kb 匹配不到行业列表，
  // 必须用企业行业属性(industry)定位，否则永远兜底到第一个(阀门制造)。
  const cur = $derived(
    industries.find(i => i.name === industry) ||
    industries.find(i => i.kb === kb) ||
    industries[0] ||
    { icon: '🔧', name: '阀门制造', dir: 'data_valve' }
  );
  // 行业 → 欢迎语/说明
  const WELCOME = $derived(cur.name.endsWith('企业') ? cur.name : cur.name + '企业');

  // 建模示例（该行业可建模方向；点击即建模）
  const EXAMPLES = $derived([
    { label: cur.name + '本体', sub: '一键构建领域知识图谱', dir: cur.dir },
    { label: '产品 × 设备 × 客户', sub: '多表关联建模', dir: cur.dir },
    { label: '全量示例数据', sub: '读取示例数据直接建模', dir: cur.dir },
  ]);

  // ── 确定性星海（固定种子，避免刷新闪烁）──
  let stars = $state([]);
  let particles = $state([]);
  let nebula = $state([]);
  onMount(() => {
    let s = 42; const rnd = () => (s = (s * 1103515245 + 12345) % 2147483648) / 2147483648;
    stars = Array.from({ length: 120 }, () => ({ x: 20 + rnd() * 1240, y: 20 + rnd() * 600, r: 0.6 + rnd() * 1.8, a: 0.25 + rnd() * 0.6, d: 1 + rnd() * 3 }));
    particles = Array.from({ length: 22 }, () => ({ cx: rnd() * 1280, cy: rnd() * 640, r: 2 + rnd() * 5, g: 2 + rnd() * 4 }));
    nebula = Array.from({ length: 6 }, () => ({ cx: rnd() * 1280, cy: rnd() * 640, r: 90 + rnd() * 150, o: 0.05 + rnd() * 0.06 }));
  });

  function onExample(dir) {
    if (dir) onModel(dir);
  }
</script>

<div class="welcome-wrap">
  <svg class="welcome-svg" viewBox="0 0 1280 640" preserveAspectRatio="xMidYMid meet" role="img" aria-label="科幻风格企业欢迎页">
    <defs>
      <linearGradient id="wm-bg" x1="0" y1="0" x2="1" y2="1">
        <stop offset="0%" stop-color="#050816"/>
        <stop offset="45%" stop-color="#0b1030"/>
        <stop offset="100%" stop-color="#111c4e"/>
      </linearGradient>
      <linearGradient id="wm-glow" x1="0" y1="0" x2="1" y2="1">
        <stop offset="0%" stop-color="#22d3ee"/>
        <stop offset="100%" stop-color="#818cf8"/>
      </linearGradient>
      <linearGradient id="wm-chip" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="#1e2a5a"/>
        <stop offset="100%" stop-color="#0e1533"/>
      </linearGradient>
      <radialGradient id="wm-cta" cx="50%" cy="40%" r="80%">
        <stop offset="0%" stop-color="#38bdf8"/>
        <stop offset="100%" stop-color="#4f46e5"/>
      </radialGradient>
      <filter id="wm-blur" x="-60%" y="-60%" width="220%" height="220%">
        <feGaussianBlur stdDeviation="3"/>
      </filter>
      <filter id="wm-glowbig" x="-80%" y="-80%" width="260%" height="260%">
        <feGaussianBlur stdDeviation="12" result="b"/>
        <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
      </filter>
      <filter id="wm-glowtxt" x="-30%" y="-30%" width="160%" height="160%">
        <feGaussianBlur stdDeviation="1.5"/>
      </filter>
    </defs>

    <!-- 背景 -->
    <rect width="1280" height="640" fill="url(#wm-bg)"/>

    <!-- 星云光斑 -->
    {#each nebula as n}
      <circle cx={n.cx} cy={n.cy} r={n.r} fill="#6366f1" opacity={n.o} filter="url(#wm-blur)"/>
    {/each}

    <!-- 星海 -->
    {#each stars as st}
      <circle cx={st.x} cy={st.y} r={st.r} fill="#e2e8f0" opacity={st.a} class="wm-star" style="animation-duration:{st.d}s"/>
    {/each}

    <!-- HUD 装饰弧线 -->
    <g opacity="0.5" fill="none" stroke="#38bdf8" stroke-width="1">
      <circle cx="640" cy="300" r="470" stroke-dasharray="2 8"/>
      <circle cx="640" cy="300" r="360" stroke-dasharray="1 12" opacity="0.4"/>
    </g>
    <g opacity="0.35" stroke="#22d3ee" stroke-width="1" stroke-dasharray="6 6">
      <line x1="0" y1="18" x2="1280" y2="18"/>
      <line x1="0" y1="622" x2="1280" y2="622"/>
    </g>

    <!-- 角落 HUD 标签 -->
    <g font-family="'Consolas','Courier New',monospace" font-size="12" fill="#67e8f9" opacity="0.7">
      <text x="40" y="52" letter-spacing="3">ENTERPRISE KNOWLEDGE CORE · v2.0</text>
      <text x="40" y="70" font-size="10" fill="#94a3b8" letter-spacing="2">FACTORY ONTOLOGY // {cur.name.toUpperCase()}</text>
      <text x="1208" y="52" text-anchor="end" letter-spacing="2">SYSTEM ONLINE</text>
    </g>

    <!-- 光粒子 -->
    {#each particles as p}
      <circle cx={p.cx} cy={p.cy} r={p.r} fill="#67e8f9" opacity="0.6" filter="url(#wm-glowbig)" class="wm-particle"/>
    {/each}

    <!-- 中心企业徽标环 -->
    <g transform="translate(640,230)">
      <circle r="86" fill="none" stroke="url(#wm-glow)" stroke-width="2" class="wm-ring" opacity="0.9"/>
      <circle r="98" fill="none" stroke="#38bdf8" stroke-width="1" stroke-dasharray="4 6" opacity="0.5" class="wm-ring2"/>
      <text y="26" text-anchor="middle" font-size="60" filter="url(#wm-glowtxt)">{cur.icon}</text>
    </g>

    <!-- 企业欢迎词 -->
    <text x="640" y="360" text-anchor="middle" font-family="'Microsoft YaHei','PingFang SC',sans-serif" font-size="46" font-weight="700" fill="#f8fafc" letter-spacing="4" class="wm-title">
      {WELCOME}
    </text>
    <text x="640" y="404" text-anchor="middle" font-family="'Microsoft YaHei','PingFang SC',sans-serif" font-size="20" fill="#67e8f9" letter-spacing="6" class="wm-sub">
      欢迎使用企业知识中枢
    </text>
    <text x="640" y="432" text-anchor="middle" font-family="'Microsoft YaHei','PingFang SC',sans-serif" font-size="13" fill="#94a3b8" letter-spacing="2">
      构建您的领域本体，驱动智能问答与数据分析 —— 数据本地处理，安全不出厂
    </text>

    <!-- 建模示例 -->
    <text x="640" y="476" text-anchor="middle" font-family="'Microsoft YaHei','PingFang SC',sans-serif" font-size="15" fill="#c7d2fe" letter-spacing="3">
      ◆ 快速建模示例 · 点击即刻生成企业知识本体 ◆
    </text>
    {#each EXAMPLES as ex, i}
      {@const cx = 640 + (i - 1) * 250}
      <g class="wm-example" onclick={() => onExample(ex.dir)} onkeydown={(e) => (e.key === 'Enter' || e.key === ' ') && onExample(ex.dir)} role="button" tabindex="0" style="cursor:pointer">
        <rect x={cx - 108} y="496" width="216" height="62" rx="12" fill="url(#wm-chip)" stroke="#4f46e5" stroke-width="1.2" class="wm-chip-bg"/>
        <rect x={cx - 108} y="496" width="216" height="62" rx="12" fill="none" stroke="#818cf8" stroke-width="0" opacity="0"/>
        <text x={cx} y="524" text-anchor="middle" font-family="'Microsoft YaHei','PingFang SC',sans-serif" font-size="15" fill="#e0e7ff" class="wm-example-label">{ex.label}</text>
        <text x={cx} y="543" text-anchor="middle" font-family="'Microsoft YaHei','PingFang SC',sans-serif" font-size="10" fill="#818cf8">{ex.sub}</text>
        <circle cx={cx - 92} cy="512" r="5" fill="#22d3ee" opacity="0.8" class="wm-particle"/>
      </g>
    {/each}

    <!-- 主 CTA -->
    <g class="wm-cta" onclick={() => onExample(cur.dir)} onkeydown={(e) => (e.key === 'Enter' || e.key === ' ') && onExample(cur.dir)} role="button" tabindex="0" style="cursor:pointer">
      <rect x="540" y="572" width="200" height="42" rx="21" fill="url(#wm-cta)" filter="url(#wm-glowbig)"/>
      <rect x="540" y="572" width="200" height="42" rx="21" fill="none" stroke="#a5f3fc" stroke-width="1" opacity="0.7"/>
      <text x="640" y="599" text-anchor="middle" font-family="'Microsoft YaHei','PingFang SC',sans-serif" font-size="15" font-weight="600" fill="#04142a" letter-spacing="3">⚡ 立即建模</text>
    </g>

    <!-- 底部版本条 -->
    <text x="640" y="636" text-anchor="middle" font-family="'Consolas',monospace" font-size="9" fill="#475569" letter-spacing="2">FACTORY-ONTOLOGY-KIT · WEB CONSOLE</text>
  </svg>
</div>

<style>
  .welcome-wrap { display: flex; justify-content: center; padding: 6px; }
  .welcome-svg {
    width: 100%; height: auto; max-width: 1160px;
    border-radius: 10px;
    box-shadow: 0 8px 30px rgba(15, 23, 42, 0.35);
    background: #050816;
  }
  .wm-title, .wm-sub { text-shadow: 0 0 18px rgba(56, 189, 248, 0.55); }
  .wm-ring { transform-origin: center; animation: wm-spin 26s linear infinite; }
  .wm-ring2 { transform-origin: center; animation: wm-spin-rev 40s linear infinite; }
  .wm-star { animation-name: wm-twinkle; animation-iteration-count: infinite; animation-timing-function: ease-in-out; }
  .wm-particle { animation-name: wm-glowpulse; animation-iteration-count: infinite; animation-timing-function: ease-in-out; }
  .wm-example { transition: transform 0.15s ease; }
  .wm-example:hover { transform: translateY(-2px); }
  .wm-example:hover .wm-chip-bg { stroke: #22d3ee; }
  .wm-example:hover .wm-example-label { fill: #ffffff; }
  .wm-cta:hover { opacity: 0.92; }
  @keyframes wm-spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
  @keyframes wm-spin-rev { from { transform: rotate(360deg); } to { transform: rotate(0deg); } }
  @keyframes wm-twinkle {
    0%, 100% { opacity: 0.25; }
    50% { opacity: 0.9; }
  }
  @keyframes wm-glowpulse {
    0%, 100% { opacity: 0.35; }
    50% { opacity: 0.85; }
  }
</style>
