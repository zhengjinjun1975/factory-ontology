# Changelog

## [0.4.0] - 2026-09-25

### 商业化鉴权加固 + 自助建模与建模质量（多租户 / 严格鉴权 / 令牌 / SSO / 向导 / 类层次）
> 本版为一次功能级发布：累计工作区 57 个文件改动。八道自检门全绿（`python scripts/run_all_gates.py` → 全部通过（8 个门）），问答评测回归门命中率 **62.8%**、误答率 **18.4%**、编造 **0**。逐项证据见 `docs/实验/` 各报告。

**新增 · 多租户与行级隔离**（`docs/实验/多租户与行级隔离-20260924.md`）
- 新增 `codes/tenant.py`（租户解析：凭据声明 > `X-Tenant-Id` > 默认租户 `default`；`contextvars` 请求级上下文，线程/协程安全）+ `codes/config/tenants.json`（配置驱动租户注册表，缺省只含 `default` 且 `kbs:"*"`，老部署行为逐字段不变）。
- 行级隔离单点：`codes/db_dialect.py` 新增 `tenant_clause` / `select_all_scoped`（缺租户上下文 fail-closed → `TenantContextError`）；`codes/db_loader.py` SQLite 读路径 PRAGMA 探测租户列并强制加租户条件（无租户列的历史表 SQL 与改前逐字节一致）。
- KB 可见性：`codes/kb_registry.py` `list_kbs` / `get_kb(tenant=)` 按租户过滤，未知/越权抛 `TenantDenied`（拒绝而非空列表）。
- 审计双通道带 `tenant`：`codes/audit_chain.py` 每条账本记录与 JSONL 访问日志必带租户。
- 新端点：`GET /api/kbs`（当前租户可见 KB）；`/api/admin/kbs` 增加 `tenant` / `total`；令牌 `fotk2` 携带租户声明。
- 自检 `scripts/verify_tenant.py`：**87/87 通过**（真实 HTTP，两套凭据各打一遍）。

**新增 · 严格鉴权灰度开关 + 上传体积上限**（`docs/实验/严格鉴权开关与上传上限-20260924.md`）
- `FOOD_STRICT_AUTH=1/true/yes/on` 时，本体结构/图/图 SVG/app-config/flows/metrics 等匿名只读端点要求凭据（无/无效 401、`/admin` 非 admin 403）；**默认关，行为逐字节不变**。
- 上传上限 `FOOD_MAX_UPLOAD_MB`（默认 50MB）：`POST /api/admin/upload` 与 `POST /api/knowledge/ingest` 超限返回 **413 且不落盘**，读入改分块累计（AST 扫描 0 处无界 `await file.read()`）。
- 自检 `scripts/verify_strict_auth.py`：**47/47 通过**。

**新增 · 令牌吊销 / 刷新轮替 + 限速防爆破**（`docs/实验/令牌吊销与限速-20260924.md`）
- 新增 `codes/token_guard.py`（零依赖，纯标准库 SQLite；状态目录有界，默认 `FOOD_AUTH_STATE_DIR=<repo>/var/auth_state`）。
- 令牌格式扩展：`fotk3.<role>.<tenant|->.<exp>.<jti>.<sig>`（可吊销访问令牌）、`fotkr1.*`（刷新令牌）；**默认仍签发 `fotk1`，老令牌照旧可用**。
- 新端点：`POST /api/auth/refresh`（刷新即轮替、旧刷新令牌用后即废、复用留审计 `refresh_reuse`）、`POST /api/auth/revoke`（按 `jti` / 按主体批量，吊销后**立即 401**）、`POST /api/auth/ban` / `POST /api/auth/unban`、`GET /api/auth/state`。
- 限速：按「主体」与「来源 IP」两路计数，失败累计超阈值短时封禁（**429** + `Retry-After`），`ban`/`throttle`/`unban` 全留审计；正常凭据在封禁期间不被误伤。
- 自检 `scripts/verify_token_guard.py`：**39/39 通过**。

**新增 · SSO / OIDC(JWT) / LDAP 适配器**（`docs/实验/SSO接入适配-20260924.md`）
- 新增 `codes/sso.py`：适配器注册表（注册/启用/停用）+ `OIDCJWTProvider`（**HS256 + 纯标准库 RS256 公钥验签**）+ `LDAPProvider`（**接口占位，未实现**）。
- `codes/api_server.py` 仅**追加** SSO 入口（`_principal` 末尾）与 `GET /api/auth/sso/status`（admin，只报非敏感配置）；**本地静态 key / 租户 key / 本地令牌永远优先**，SSO 只作兜底。
- 新增配置样例 `codes/config/sso.example.json`（`enabled:false`，全部示例值）；真实密钥配置 `codes/config/sso.json` 已 gitignore。
- 自检 `scripts/verify_sso.py`：**56/56 通过**（篡改签名/过期/issuer/audience 不匹配/缺声明全拒）。

**新增 · 自助建模：目录浏览 + 自动匹配推导**（`docs/实验/自助建模目录浏览与自动匹配-20260925.md`）
- 后端/BFF：`web/server/ontology.js` 新增只读目录枚举与校验（`listDrives`/`listDirs`/`selfmodelCandidates`/`validateDataDir`/`isDangerousPath`，**只返回目录名与元信息，不返回文件内容**）；`web/server/index.js` 新增 `GET /api/fs/drives`、`GET /api/fs/dirs`（**仅本机**，非 127.0.0.1/::1 → 403）、`GET /api/ontology/selfmodel/candidates`、`POST /api/ontology/selfmodel/validate`、`POST /api/ontology/self-onboard`。
- 后端补齐此前缺失的 `POST /api/ontology/self-onboard`（修反向后端断链）；`_resolve_src` / `confirm` 支持仓库外绝对路径并新增危险路径（盘符根/系统目录）拦截。
- 前端：移除 `SelfModelPanel.svelte` 里写死的 `data_valve` 默认，改为从真实数据枚举推导（kb↔目录双向匹配、逐项证据 chip、目录选择器弹窗）；新增封装 `web/src/lib/api.js`。
- 自检 `scripts/verify_selfmodel_browse.py`：**33/33 通过**；`check_chainbreak.py` 反向断链归零。

**新增 · 激活态单一真相源 + 服务/文案修复**（`docs/实验/修A_C组-状态单一真相源与服务-20260925.md`）
- 新增运行态 `codes/config/active_ontology.json`（已 gitignore）作为「当前生效本体」唯一写入点（建库/切库写入，服务启动读取）；`GET /api/kb/active`、`POST /api/kb/active`。
- `/api/standard/compliance`、`/api/standard/export`、`roundtrip`、`quality`、`/api/ask` 缺省 kb 一律跟随当前激活库（不再写死全局 `config/ontology_schema.json`）；`confirm` / `self-onboard` 新增 `data_dir` 占用冲突校验（4091 拒绝）。
- 鉴权 key 来源改为「环境变量优先 → `codes/config/api_keys.json`」，一处未配时返回 503 + 指引（仍 fail-closed）。
- 自检 `scripts/verify_active_ontology.py`：**32/32 通过**（含「重启后端后激活态仍是切换后的库」实测）。

**新增 · 建模质量：类层次 / 实体定义 / 具名子类**（`docs/实验/建模质量-类层次与定义-20260925.md`、`docs/实验/建模质量-扩展描述项-20260925.md`）
- `codes/schema_ontology.py` 新增确定性派生内核：`derive_class_hierarchy` / `apply_class_hierarchy` / `derive_definitions` / `derive_named_subclasses` / `apply_named_subclasses`（纯规则、零 token、同输入同输出）。
- `/api/ontology/suggest` 增返 `hierarchy` / `definitions` / `subclasses`（只读预览，逐条带 `rule` / `evidence`）；`/api/ontology/confirm` 增 `hierarchy_confirmed` / `extensions_confirmed` 硬门（未确认 / 缺依据 / 父实体不存在 → 拒绝落库，**模型只建议、人来确认**）。
- 实测（同源 `ontology_check._check_standard`）：valve 合规度 84.2→**100.0**、实体核心描述项 83.3→**100.0**、类层次 0→**8/38**；food_co 84.2→**100.0**、类层次 0→**7/31**；实体扩展描述项齐备率 75.0→**77.0**。
- 自检 `scripts/verify_standard_hierarchy.py`（39/39）、`scripts/verify_standard_extended.py`（41/41）。

**修复**
- 反向后端断链：补齐 `POST /api/ontology/self-onboard` 后端路由（3.4/第四节）。
- BFF 未代理 `/api/standard/`：合规面板此前拿到首页 HTML → 显示空；`web/server/index.js` 新增该前缀转发并纳入登录门禁（C1）。
- 错误文案骗人：`apiFetch` 对框架级 ≥400 如实透出后端原因（401 → 「未登录或会话已过期，请重新登录」；403/429/5xx → 后端 `detail`），不再被「后端建模失败」兜底掩盖（C2）。
- 漏带 key 即整站 401：key 支持落配置文件 + 未配置时明确指引（C4）。
- `codes/run.py` 新增 `_rel_or_abs()` 替换 `os.path.relpath`：修「自助建模选异盘/仓库外数据目录时 `ValueError: path is on mount C:, start on mount D:`」被掩盖成「按确认 schema 建本体失败」的真 bug。
- `scripts/verify_kb_registry.py` 去掉「恰好 33 个 KB」的脆弱硬编码断言，改为「未缩水 ≥33 且无 `verify_*` 残渣」（D2）。
- `codes/tests/test_api.py` 食品库问答用例显式传 `kb="food"`，与全局激活态解耦，防测试随切库漂移。
- 建模「实体平铺」：自动派生类层次 + 补实体 Definition，消除合规面板两条 major 提示（D1）。
- 前端 `SelfModelPanel.svelte`：候选数据目录由常驻长列表改为**下拉一行**（默认收起、点开展开）；按钮去除 emoji/图标字形，统一站内既有 `.btn` 样式（B1/B2）。

**安全**
- 多租户行级隔离 fail-closed（缺租户上下文即拒，越权拒绝而非返回空）。
- 严格鉴权灰度开关（**默认关闭**）保护 8 条匿名只读业务端点；`/health` 保持匿名探活，`/metrics` 严格模式下需 read 凭据。
- 上传体积上限（默认 50MB），超限 413 且不落盘。
- 令牌吊销下一请求即生效；刷新令牌轮替、复用被拒并留审计；失败限速 429 防爆破。
- SSO 篡改签名 / 过期 / issuer / audience 不匹配 / 缺声明一律拒绝；SSO 开启时本地凭据仍可用。
- `.gitignore` 追加：`var/`（鉴权运行时状态）、`codes/config/sso.json`、`codes/config/active_ontology.json`、`codes/config/api_keys.json`（后两者含运行态/真实 key）。
- 自检脚本脱敏：4 个新增自检脚本内的本地测试 admin/read key 常量由 `devkey-*` 改为中性的 `verify-admin-key` / `verify-read-key`（`scripts/check_boundary.py` A1/A2 违规 0）。

**变更**
- 版本号 `0.3.2 → 0.4.0`，统一 6 处落点（`codes/run.py` `__version__`、`web/package.json`、`web/package-lock.json`（版本字段）、`codes/e2e_test.py` 断言、`web/server/index.js` 兜底版本、`README.md` 徽章与版本节）。
- `codes/export/ontology.ttl` / `ontology.jsonld` / `shapes.ttl`：标准导出物重新生成（含类层次与命名空间治理），为验证时的生成物。
- `codes/config/lexicon_valve.json`：被 `/api/ontology/confirm` 既有流程重生成（新增行业层键，`synonym_map` 有增有减），**非本轮新增逻辑**（见类层次报告 §6.4）。
- `codes/config/ontology_schema_valve.json` / `ontology_schema_food_co.json`：落库类层次、实体 Definition、具名子类（valve 30 条 / food_co 24 条）。
- `codes/config/kbs.json`：新增 KB `food_co`；`food_co.data_dir` 登记为 `data_food_co`（原指向共享目录 `data`，会误扩实体，已纠正）。
- `POST /api/knowledge/ingest` 超限响应码由 200（信封 code=4001）改为 **413**（信封体结构保留）。
- `scripts/eval_qa_baseline.json`：回归基线同步至当前真值（命中率 0.568→**0.628**、误答率 0.296→**0.184**、编造 4→**0**），与已发布的问答信封改造一致。
- `/health` 增加 `apiKeyConfigured` / `backend` 字段（BFF 侧，便于排障，不暴露 key）。
- 前端构建产物重新生成：`web/public/index.html` + `web/public/assets/*`（旧 hash 文件删除，新 hash 文件新增）。
- 令牌格式扩展（`fotk3` / `fotkr1`）为**附加**，默认输出与旧版逐字节一致。

**文档**
- 新增实验报告：`docs/实验/多租户与行级隔离-20260924.md`、`严格鉴权开关与上传上限-20260924.md`、`令牌吊销与限速-20260924.md`、`SSO接入适配-20260924.md`、`商业化鉴权4轮总账-20260925.md`、`自助建模目录浏览与自动匹配-20260925.md`、`修A_C组-状态单一真相源与服务-20260925.md`、`建模质量-类层次与定义-20260925.md`、`建模质量-扩展描述项-20260925.md`、`待修-自助建模前端-20260925.md`（及 `_4轮工作计划-20260924.md`）。
- 新增发布准备：`docs/发布/发布准备-20260925.md`。
- 更新 `CHANGELOG.md`、`README.md`（能力清单 / 快速开始 / 评测基线与自检门 / 已知限制）。

**补记：本版本线内此前已提交、未记入 CHANGELOG 的批次**（仅列事实，SHA 可在 `git log` 核对）
- `9a2c343` 改造第一批：问答评测基线 + KB 注册表与词典分层 + 商用加固。
- `e197585` 改造第二批：问答统一信封 + 编排上半截。
- `c922d17` 改造第三批：模型建议层 + 检查工具与 CI（`scripts/check_boundary.py` / `check_chainbreak.py` / `.github/workflows/ci.yml` self-check job）。
- `b5e8b29` / `5dd7f5b`：`scripts/run_all_gates.py`（一把跑全部门）+ 修其 A2 本机路径。
- `5c3e482` 批7：开源净化 —— 甲方痕迹脱敏（A1/A2 归零）+ 报告。

**本版已知限制 / 未完成项**（据各报告「没做到」章节如实汇总，未粉饰）
1. 严格鉴权开关 `FOOD_STRICT_AUTH` **默认关闭** —— 默认部署下本体结构与图仍可匿名读；上线前需决定是否默认开；且该开关是**路径白名单**（新增匿名端点需手工加进 `_STRICT_READ_PATHS` 才受保护）；`/admin` HTML 在严格模式下浏览器无法直接打开（顶层导航不带请求头）。
2. **多进程/多实例部署下，令牌吊销名单与限速计数是本地状态文件，多实例不共享**（要共享需引入 Redis/数据库，本版未做）。
3. **MySQL / PostgreSQL 的行级隔离只有代码，无真库验证**（本机未起真库，驱动未安装）；且 mysql/pg 侧不做租户列自动探测（需配置显式 `tenant_col`）。
4. **LDAP 只做接口占位，未实现、未实测**。
5. **SSO 只落地 JWT 直验（HS256 + RS256）**；完整 OAuth2 授权码流程（跳转 / 换 token / JWKS 在线轮换）未做。
6. **等效类（EquivalentClass）两库均不可提升** —— 词典/别名表无「被建模成实体」的真同义实体，故不伪造；实体扩展描述项齐备率因此封顶 77%（有真实依据下的可达值，数学上限约 81%，不填假关系）。
7. **具名子类未做实例级归类**（未生成 `:Valve_products_闸阀` 的实例 `rdf:type`）；且 `--kb` 只落 valve / food_co，历史测试残渣库（valve2/valve3/valve9）未处理。
8. **`codes/data_loader.py:57` 有一处未加租户过滤的 SQL 站点**（不在允许改动的文件范围内，已登记未改）。
9. **`X-Tenant-Id` 头本身不鉴权**：无凭据的既有路由仍可用该头自选租户；真实部署建议前置网关鉴权或只信凭据声明。
10. **default 租户等同超管**（`kbs:"*"` 可见全部 KB）—— 这是「老部署行为不变」的锚点，真实多租户部署须收口其 scope。
11. **外部绝对路径的「建本体」未端到端实测**；`self-onboard` 后端路由未做真实上传建模端到端实测（避免在真机跑真实建模写产物）；`/api/fs/dirs` 不拦危险路径（仅 `selfmodel/validate` 拦）。
12. **BFF 门禁「单企业收敛」语义未改**：经 BFF 的请求会把激活库拉回登录用户绑定的 kb（一企业一库的既有产品语义）；仅通过后端 API 切库而不同步 `user.kb` 时，下次经 BFF 的请求会回退。
13. **`/api/stats` 分组统计 tie 顺序既有非确定性**（同版本两进程 raw 结果不同）—— 与本版改动无关，但影响「逐字段相等」类断言。
14. **超大 body 的 ASGI 层 spool 不在本改动可控范围**（Starlette 进入端点前先落 `SpooledTemporaryFile`）；未做自定义 ASGI 中间件级流式拒绝。
15. 限速阈值、上传上限、吊销名单上限均为**保守默认值**，属需业务侧复核的参数。

## [0.3.2] - 2026-09-16

### 词典资产闭环：行业积累真正转起来（数据资产可导出、可导入、可按独立来源沉淀）

> 三层资产（工厂/行业/公共）+ 进出口 + 独立来源判据。验证：四库关系 9/9、闭环端到端 21/21、端点 HTTP 18/18、问答回归 48/48。

**修复（此前是断链）**
- 行业层写了没人读：`_load_public` 默认只合并 `00_basis.json`，`01_valve_pump/02_fine_chem/03_geophysics` 从未被消费
  → 新增 `industry_for_kb` / `load_industry_files`，`merge_industrial_dict(..., industry=)`；实测泵阀库 type 23→45 词、pump 0→20、part 0→18
- 合并白名单与行业层键位不匹配：`_MERGE_KEYS` 由 4 类扩到 12 类（fault/material_synonyms/pump/part/process/product_type/safety/method）
- 建模生成的工厂词典不按行业合并：`_build_lexicon(schema, data, industry=None)` 自动解析行业，行业层特有词键原样带进工厂词典
- 问答加载按 lexicon 文件名解析 kb → 行业，行业层被问答消费

**新增（资产积累机制）**
- `absorb_public_dict`：`source_clusters`（词集合 Jaccard ≥0.9 判同源，模板复制只计 1 个来源）、
  `independent_source_counter`、候选池 `industrial_dict/_candidates.json`、`learn_from_kb`
  吸收判据由「文件计数」改为「独立来源计数」（阈值 3）；实测 ≥3 来源的词 61 → 15，挤掉模板复制的水分
- 聚类前剔除公共层已有词（否则人人含公共词 → Jaccard 虚高 → 全体误判同源）
- `dict_asset.py`（新模块，零依赖）：工厂词典导出 / 导入（merge|replace、dry_run、差异报告、落盘前备份）/
  整包 zip（lexicon+schema+nt+meta）/ 恢复
- 端点：`GET /api/kb/{kb}/lexicon/export`（`bundle=1` 打包整包）、`POST /api/kb/{kb}/lexicon/import`；
  `POST /api/industry/absorb` 改走独立来源判据（返回 promoted / candidates）

**前端入口（闭环最后一段）**
- BFF（`web/server/index.js`）新增五条转发（均在既有登录门禁内）：`lexicon-export` / `lexicon-import` /
  `industry-list` / `industry-candidates` / `industry-absorb`
- `web/src/lib/api.js` 新增五个封装（导出走 blob 下载，必须带 Authorization 头）；
  新组件 `LexiconAssetPanel.svelte`（导出词典/整包、导入+差异预览+确认、吸收进候选池、
  公共词典规模、候选池明细）；`App.svelte` 新增「词典资产」标签页；`npm run build` 通过
- 后端新增 `GET /api/industry/candidates`（候选池状态）

**集成修复（BFF 端到端实测验出）**
- 导出误判成功：后端业务失败用 HTTP 200 + `{ok:false}`，BFF 只看状态码会把错误 JSON 当词典下载
  （实测"不存在的 kb"→ 500 `Invalid character in header content`）→ 改为解析响应体判 `ok`
- 非 ASCII 文件名：kb 含中文时 `Content-Disposition` 让 Node 抛错 → ASCII 兜底 + RFC 5987 `filename*`

**实测与消融（附带发现，均为既有问题）**
- `scripts/ablation_public_layer.py`：公共/行业层对基本盘问法贡献 **+0.0 个百分点**（44 句同口径同判法）；
  纯工厂词典 synonym_map 0 条 → 合并后 49 条（别名能力完全依赖公共层）
- 既有 P0：类型词作主语的问法（"试压设备有多少台"→"有 10 台设备"，真实 2）答成实体总数，
  摘掉全部词典后同样错 → 与词典无关，是问答实现的语序判定；
  而 `eval_hit_rate` 的 5 类问句均由数据字段名生成，未覆盖该语序（评测盲区）

**修复（既有 P0：类型词作主语的问法被实体总数抢答，见 docs 第 7.1 节）**
- `ontology_qa_v3.answer` 实体总数分支的跨行业守卫：原实现 `q.replace(实体词,"")` 后再找类型枚举词，
  类型词本身含实体词时（"试压设备" ⊃ "设备"）被破坏成"试压" → 守卫失效 → "试压设备有多少台"答成实体总数。
  改为用完整问句判定，并排除"命中枚举词 == 实体词本身"（两个方向的风险都挡）
- 断言门 `scripts/verify_qa_word_order.py`（9 项：3 类语序 + 6 项对照）：修前 6/9 → 修后 9/9
- 回归：既有问答 48/48、命中率 44/44（同口径 n=12）、四库词典关系 9/9
- `eval_hit_rate.py` 的 key 改读 `FACTORY_READ_KEY`（原硬编码 test-read-key，与其它脚本不一致）

**验证脚本**
- `scripts/verify_ontology_lexicon_relation.py` 扩到 9 项（新增行业层消费、向后兼容断言）
- `scripts/verify_dict_asset_loop.py`（新）端到端 21 项：导出→导入 round-trip、同源/异构判定、
  阈值闸门（<3 不升级、≥3 升级）、沉淀词被新企业建模消费、候选池留痕

## [0.3.1] - 2026-09-15

### 问答解析层修复（四库命中率 48.4% → 100%，"答 0" 假答 6 例 → 0 例）

> 本轮全部改动都落在**解析层**，未动数据与建模规则；domain 无关，四库（valve/food/chem/auto_parts）同一套代码。

**修复**
- `_find_attr` 剔除实体名：实体名混进属性词典后，同长词按插入序抢命中，导致极值/聚合整条链失效
  （"原料中库存最大是多少" 解析出的属性竟是实体名"原料"）
- `_find_enum` 三处剔除实体名 + 候选词必须出现在问句里：
  - 路径1（枚举词典直接命中）早前已剔；本轮补路径2 第一段（规范词）与第二段（候选词）
  - valve 的 `synonym_map` 把状态词并进了实体词组（`'运行中'` 的规范词竟是 `'设备'`），
    导致 `_find_enum(q,'type')` 返回 `('equipment','设备')` → 抢走"状态+类型"组合分支
    → "运行中的设备有多少台" 答"有 0 台运行中的设备"
- `_field` 容忍 `None` 与**空列表**别名：`.get(k, default)` 对"键存在但值为 `[]`"不返回 default
  （valve `field_aliases` 曾出现 `'status': []`）→ 取空值 → 状态匹配全 False
- status 分支让路：`"合格"` 同属 `qc_result` 的值却被 status 值表误收，致"原料中质检结果为合格的有多少条"答 0
- `_entity_subset` 认 `X中/里` 结构：`"订单中客户编号为C003"` 里 X 才是限定的表
  （"订单"与"客户"同为 2 字时按插入序会选错表）
- `_attr_val` 后缀匹配归一化：`orders_customer_id` 这类带表前缀的 snake 键取不到值

**保真与净化**
- 润色层数字按**独立数字**比对（原 `"2" in "2026"` 子串包含会蒙混过关）+ 单位原样保留 + 汉英不加空格
- 多引擎 LLM 出口统一净化 `_strip_reasoning_leak`：模型思维链不得进入面向用户的答案
- 无依据时改确定性话术 `no_basis_reply()`，不再调本地小模型（其独白会被当答案返回）

**词典**
- 补 14 项未译属性（food/chem/auto_parts），改前备份 `.bak` 可回滚
- 附 `docs/lexicon-mismatch-2026-09-15.md`：15 条错配清单（只读扫描）+ 复核命令

**版本号统一**
- 改为**单一事实源**：`codes/run.py` 的 `__version__`，后端 `api_server.py`（3 处硬编码）与
  前端 `web/server/index.js` 都读它
- 修此前"前端显示 0.2.1、后端 /health 显示 0.2.2"的漂移；0.3.0 已由插件框架那批占用，本期为 0.3.1

**工程**
- 新增 `scripts/regression_qa.py`（50 条断言，每类根因一条）
- 新增 `.gitattributes` 统一 LF（防编辑工具把整文件转 CRLF 造成"每行都变"的假 diff，实测曾把 308 行改动显示成 3933 行）

## [0.3.0] - 2026-08-13

### 生态插件基础框架（第三方可开发插件扩展系统）

> 不改主程序，第三方即可通过「插件」为系统新增能力。核心框架纯标准库零依赖，完全离线可跑。

- **插件加载器**（`codes/plugin_framework.py`）：扫描 `codes/plugins/` 目录 → 解析 `manifest.json`（`name/kind/version/entry/provides`）→ 按 `load → register → run → unload` 生命周期调度。清单缺字段/kind 非法/name 与目录名不符/入口缺失时逐个容错报告，不中断整体扫描
- **扩展点注册表**（`ExtensionRegistry`）：四类扩展点 `decision`（决策规则）/ `data_source`（数据源）/ `push`（推送通道）/ `template`（模板渲染），按 `(kind, id)` 注册、调用、注销，重复占用抛冲突；卸载插件自动注销其扩展点
- **CLI**（`run.py plugin`）：`plugin list [kind]` / `plugin run <名> ['<json>']` / `plugin ext <kind> <id> ['<json>']` / `plugin install <目录|zip|tar.gz> [--name 别名] [--force]` / `plugin remove <名>`；安装支持本地目录、zip、tar.gz 归档，别名安装自动改写 manifest
- **示例插件**（`codes/plugins/example_decision/`）：决策类插件，按温度/磨损/转速阈值输出设备维护优先级（正常/关注/预警/紧急），登记 `decision/maintenance_priority` 与 `decision/failure_alert` 两个扩展点；提供独立运行自测（`python plugin.py`）
- **测试**：`tests/test_plugin_framework.py` 6 项（扫描/生命周期/注册表/冲突/安装移除/zip 安装）；全量 pytest **35 passed**
- **文档**：`docs/插件框架.md` 第三方开发指南（目录结构/manifest 字段/生命周期/扩展点/CLI/写插件步骤）

## [0.2.1] - 2026-08-14

### 新增
- 事件驱动无死角：改行业自动重建本体（saveEnterprise 自动 buildIndustry），行业下拉从 kbs.json 动态加载
- 数据建模右栏"显示本体模型"按钮（默认欢迎界面，点击显示本体力导向图）
- 本体建模关系发现器升级：显式外键 + 隐式外键 + 同域值域重叠 + LLM 兜底关系发现

### 修复
- seismic 串台 bug（本体/词典被写成 chem，强制重建修正）
- 行业→kb 联动（改行业 kb 跟随，不再串台）
- 数据看板故障率与异常设备数口径一致（含中文状态词）
- 行业下拉过滤无中文名的测试残渣 kb
- 本地文件建模 kb 为空时 fallback 到行业 kb

# Changelog

## [0.2.0] - 2026-08-13

### 功能累积升级（检索/评测/上传/前端全面增强）

- **多租户企业绑定修复**：`getCurrentKb` 移除 `keys[0]`/`food` 兜底（A 企业不再被 B 企业数据污染）、`resetKb` 不再拦 `food`、onboarding 企业名/行业正确贯穿、顶部标签按企业行业识别
- **检索增强**：咨询/建议型开放问题（"有什么安全问题/风险/注意"）走专业 LLM 兜底生成建议；极值语义陷阱拦截（"最大的安全问题"不再误答"容量最大"）；评测路径友好兜底 + hit 判定补非答案词（不虚高命中率）
- **文档上传增强**：超长章节按 size 二次切分（修 PDF embed 超 token 失败）、入库时间 `ingested_at` 字段 + 前端格式化、会话持久化（node 重启不掉线）
- **前端**：资产面板空态也显示"创建快照"入口（修死锁）、企业行业识别、UI 审美调优
- **验证**：pytest 29 passed、前端 vite build 通过

## [0.1.5] - 2026-08-11

### 综合方案：9 大行业泛化建模 + 向量混合检索（命中率 100%）

> 基于 9 大行业（阀门/机械/食品/化工/地震/精加工/波纹管/环保/造船）横向验证，落地"两阶段泛化建模"方法论：建库自动生成查询映射 + 向量语义混合检索，实现"换任何行业数据即用、命中率 100%"。

- **基础重构（建库自动生成映射，替代硬编码）**：`_build_lexicon` 自动生成 `entity_cn2en`（实体计数映射，词干+中文label双源）+ `numeric_fields`（极值字段，data profiling 数值列识别），任意新行业实体（测线/炮点/项目/船/船坞/订单）自动可计数、极值查询自动命中
- **向量语义混合检索**（`vector_retrieval.py`，本地 nomic-embed-text 768维 纯标准库）：BM25 稀疏 + 向量语义 融合，接入 run.py 主链路 + api_server，语义模糊查询（"最贵的产品"/"油轮有几艘"）命中；embedding 失败回落不阻塞
- **极值词映射**：`_EXTREME_WORD_FIELDS` + `_extreme_field`，"最贵/最便宜"→price、"大/小"→容量/"高/低"→温度功率，通用极值词自动推断字段
- **模型配置增加向量模型**：model_config.json 加 `embedding` 配置（默认本地 nomic-embed-text），`get_embedding_config()` + vector_retrieval 读配置
- **9 行业数据**（`data_valve/data_machining/data_food_co/data_chem/data_seismic/data_precision/data_bellows/data_eco/data_ship`）
- **验证**：9 行业 40/40 = 100% 命中（实体计数/极值/类型/材质全泛化）；pytest 29 passed；CI verify ok:True
- **方法论**（`docs/方法论-两阶段泛化建模.md`）：两阶段（规则+LLM 自动建模 → 人工辅助精细化）+ 分层架构（结构层通用 + 映射层随行业）+ 混合检索

## [0.1.4] - 2026-08-11

### 厂区数据真实化 + 检索容错

> 检索全网真实阀门制造数据特征，重构示例数据为接近真实（产品BOM/工艺/传感器/噪声），并增强检索容错。

- **真实化示例数据**（`data_valve/`，基于研究《阀门制造工厂数据特征-真实化.md》）：产品用 GB/T 32808 型号编码（Z41H-16C/Q641F-40P）+ 材料牌号（WCB/CF8/CF8M）+ 标准号/温度范围；设备含传感器（振动/温度/电流）；质检用 API 598 试压矩阵（壳体/密封压力、保压、泄漏率气泡/min）；含真实噪声（材质别名 1Cr18Ni9Ti≈304≈CF8、缺失值、泄漏超标异常）
- **schema 更新**（`ontology_schema.json`）：匹配真实 BOM 字段（model_code/pressure_grade/connection/seal_material/body_material/standard_no/temp_range）+ 设备传感器 + 质检试压字段
- **检索容错**（`graph_rag.py`）：材质/单位/类型同义词扩展，`_expand_synonyms` + `_SYNONYM_GROUPS`，查询"不锈钢"能命中 CF8/CF8M/304/1Cr18Ni9Ti（子串匹配兼容 CF8(304) 带括号格式）；缺失值/异常值检索不崩
- **验证**：真实化数据建模 1066 行 NT（142 节点/173 边）；检索容错 8/8（"不锈钢"命中 CF8 产品 P004）；pytest 29 passed；CI verify ok:True
- **修复（同日）**：过滤计数模板优先级：属性名含"故障"时被状态模板劫持（"机器故障标签=0 的数量"答成"有 339 故障的"），模板前置后 ai4i benchmark 82% → **61/61 = 100%**（四领域全 100% 复现）；极值回答显示修正（"最扭矩的记录" → "扭矩最大的记录"）；移除 run.py 对已删除 ontology_depth.py 的失效调用

## [0.1.3] - 2026-08-11

### 安全边界 + 核心程序加固 + 编码正确性

> 审查重构后本体建模的安全边界、加固核心程序、确保检索无编码错误。

- **本体建模失败报告**（`schema_ontology.py` / `run.py setup-schema`）：建模各步骤失败时报告清晰原因（`[建模失败] 数据目录不存在` / `schema JSON 非法` / `实体id重复` / `关系引用不存在`），不裸抛异常。`load_schema` 的 assert 改为显式 ValueError（报告具体错误项）
- **SQL 注入加固**（`db_loader.py`）：表名白名单校验（`re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*")`），拦截 `products; DROP TABLE x` 类注入，与 data_loader 一致
- **编码正确性验证**：核心文件全 UTF-8；中文 label 无乱码、BM25 中文检索正常、find_seeds 中文匹配、parse_nt 中文 label 正确保留
- **验证**：错误报告 6/6 + 编码正确性 7/7 + pytest 29 passed + CI verify ok:True

## [0.1.2] - 2026-08-11

### 本体驱动增强（基于 2025-2026 最新技术研究）

> 检索全网企业本体建模/知识图谱最新技术讨论（OntoRAG、OG-RAG、本体约束减幻觉、LLM驱动本体构建），方法论升级为「schema 驱动建模 + 本体驱动混合检索 + 本体约束减幻觉」。

- **本体引导 GraphRAG 种子**（`graph_rag.find_seeds(ontology=...)`）：问题匹配本体关系 label 时，沿关系路径扩展种子，提升多跳/关系查询召回（依据 OG-RAG EMNLP2025 / ORT ACL2025）
- **本体约束减幻觉**（`graph_rag`/`logical_qa` LLM prompt 注入）：只依据子图事实回答，不编造图中不存在的关系/实体（依据临床 QA 98% vs GPT-4 37%）
- **schema 自动推断**（`schema_ontology.suggest_schema(data)`）：从多表数据自动推断实体/关系/约束，无手写 schema 也能 schema 驱动建模（schema-free 范式，依据 LLM/规则驱动本体构建）
- **方法论升级**：`docs/泛化方法论.md` 加入 2026 本体驱动增强表，引用三篇研究笔记
- **研究笔记**：`knowledge/AI/articles/` 新增《企业本体建模最新技术讨论-2025》《知识图谱最新技术演进-2025》《本体建模输入输出与作用-2025》
- **验证**：本体增强 9/9（suggest_schema 推断 8 实体 8 关系建 403 行 NT + find_seeds 本体引导 + prompt 本体约束）；pytest 29 passed；valve_demo 溯源命中 + benchmark 13/13

## [0.1.1] - 2026-08-11

### 激进重构：schema 驱动统一建模 + 融入 sme 本体重构精髓

> 从 v0.1.0 起点，按「复用优先·极简落地」方法论全面重构本体建模路径。

- **版本降维**：v2.9.6 → **v0.1.0 → v0.1.1**，从 0.1 重新开始（融入 sme 本体重构精髓后作为新起点）
- **schema 驱动统一建模**（`schema_ontology.py`）：移植 sme-decision-ontology 本体重构精髓，schema 驱动（ontology.json 显式声明实体/关系/约束）、属性语义角色（identifier/reference/measure/category/timestamp）、类型体系（Enterprise→BusinessObject→域类→实体）、validate 约束校验、traverse 跨域图遍历、build_graph 跨表统一实例图
- **`to_nt()` N-Triples 统一输出**（激进重构核心）：schema 驱动建模结果输出标准 N-Triples，替代 csv_to_owl/multi_table 的多表建本体职责；类名表名风格（Valve_products）+ 对象属性英文 id（usesRawMaterial），下游 ontology_qa_v3/graph_rag 无缝消费。已拆 4 子函数（类声明/属性声明/类别层级/实例）降低复杂度
- **`run.py setup-schema` 命令**：多表数据目录 + ontology_schema.json → 统一本体（约束校验 + 类型体系 + 语义域）
- **调用点统一**：valve_demo/mcp_server 建本体改为 schema 驱动优先（无 schema 回退 multi_table，向后兼容）；单表 benchmark 保留 csv_to_owl（正确工具）
- **`config/ontology_schema.json`**：阀门工厂示例 schema（8 实体 / 6 关系 / 溯源链 usesRawMaterial+belongsToBatch+checkedBy）
- **验证**：valve_demo 反向溯源命中（RM03→VB02）+ benchmark 13/13=100%；pytest 28 passed；setup-schema 端到端 8 表→383 行 N-Triples→43 节点/36 边；CodeAgent 审查通过

## [2.9.6] - 2026-08-07

### 本体深化：类别类层级(Is-A) + 企业与客户关系 + 企业本体大图（参考 sme-decision-ontology）
- **本体层次深入**：multi_table.py 新增类别类层级，自动检测 `type/category` 列，生成 `<表名>Category_值 rdfs:subClassOf <表名>`（产品 Is-A 类别、设备 Is-A 类别）+ 实例 `hasType` 链接
- **FK 检测增强**：支持领域前缀（valve_/food_/factory_）+ 单复数匹配（product_id → products），自动识别跨表关系
- **企业与客户关系**：新增 `valve_customers.csv` + `valve_sales.csv`，产品 --销售--> 客户（hasValve_products / hasValve_customers）
- **企业本体大图**：`docs/diagrams/ontology-大图.svg` 展示企业与客户关系 + 本体层次 Is-A
- **跨行业验证**：阀门 8 subClassOf + 食品 11 subClassOf，FK 前缀/单复数均适配
- 版本 2.9.5 → 2.9.6

## [2.9.5] - 2026-08-07

### 优化：lexicon_agent._build_full_lexicon（方案A）
- 圈复杂度 **46 → 2**（数据驱动查表 + 抽子方法，行为不变）
- 抽：`_build_attr_mapping`/`_build_enum_mapping`/`_build_field_aliases`/`_build_relations_cn2en`/`_is_single_letter_grade`/`_is_binary_flag`/`_camelize`
- 关键词查表：STATUS_KEYWORDS/TYPE_KEYWORDS/ZONE_KEYWORDS
- **修复潜在 bug**：`_infer_cn_from_name(f, {})` 传2参但函数只收1参（attr_map 空时崩溃）→ `(f)`
- 验证：pytest 29 + e2e 17/17 全过，行为一致

## [2.9.4] - 2026-08-07

### BM25 混合检索 + MCP server（AI 原生）
- **BM25 混合检索**（`bm25_retrieval.py`，纯标准库零依赖）：中文 unigram+bigram 分词、倒排索引、BM25 打分；接入 API 问答链路（规则→逻辑桥→GraphRAG→BM25→miss），提升模糊/自然语言查询召回，零 token；`min_score` 阈值过滤噪音
- **MCP server**（`mcp_server.py`，纯标准库 stdio JSON-RPC）：暴露知识库给任意 MCP-native AI agent，工具=ask/trace_forward/trace_reverse/stats；AI 原生，agent 可调用问答/溯源/统计
- 测试：`tests/test_bm25_mcp.py` 5 项（BM25 检索/排序 + MCP 握手/工具/溯源）；pytest 29 项全过
- 鲁棒性：`benchmark_logical.py` 自动构建缺失的 NT（干净检出也可跑，e2e 17/17）

## [2.9.3] - 2026-08-06

### 内部使用方案（示例）
- `docs/内部使用方案.md`（示例）：设备/合同知识库场景 + 落地步骤 + ROI

## [2.9.2] - 2026-08-06

### 阀门行业示例（交叉佐证）
- **阀门行业 demo**（`valve_demo.py` + `data_valve/*.csv` 合成示例）：实证框架对石油/阀门领域"换领域即用"
  - 规则问答（数量/极值）、逻辑桥（自然语言）、反向溯源（不合格密封圈→批次→阀门，质量召回）、benchmark 13/13=100%
- `config/lexicon_valve.json` 阀门词典；README 加"阀门行业示例"章节
- 实证框架对设备/阀门台账类结构化数据"换领域即用"

## [2.9.1] - 2026-08-06

### 文档/图表
- **系统级架构设计图**、**数据走向逻辑图**、**工厂落地路线图**（`docs/diagrams/*.svg`，暗色玻璃拟态）
- README 加"系统架构与落地路线"章节（嵌入 3 图，GitHub 自动渲染）+ 过程说明
- **端到端测试** `e2e_test.py`：CodeAgent 驱动，问答/溯源/导出/管理/多源/一致性 17 项全过

## [2.9.0] - 2026-08-06

### 平台化 + 多源 + 定位（A+B+D1）
- **逻辑桥评测**（`benchmark_logical.py`）：命中率 5/5=100%；`logical_qa` 补实体名解析（极值/排序返回中文名）
- **ERP 多源接入**（`db_loader.py`）：直连 MySQL/PostgreSQL，缺驱动清晰报错
- **Web 管理后台**：`/admin` 页面 + `POST /api/admin/upload`（CSV 上传 → 重建本体）+ 统计/词典/审计视图
- **溯源导出**：`GET /api/export/reverse`（CSV/TXT，可读名：原料→批次→产品→日期）
- **定位与横向对比**：README 加生态定位表（vs Dify/RAGFlow/GraphRAG/KAG）

## [2.8.0] - 2026-08-06

### 逻辑推理 + 可解释 + 全本地化（A+B+C，多 Agent 实现）
- **逻辑推理桥**（`logical_qa.py`）：LLM 转逻辑查询 → 确定性执行器（借鉴 KAG logical-form 模式）。规则引擎 miss 后先走逻辑桥，覆盖更多开放式问题而不失确定性
- **答案溯源/可解释**（`evidence.py`）：提取命中实体/属性/值证据，`/api/ask` 返回 `evidence`，APP 展示"答为什么"
- **全本地化**（Ollama）：`model_config` 加 `local_ollama`，`model_llm` 支持 base_url 离线，数据不出厂
- ask 流程：规则 → 逻辑桥 → GraphRAG → 引导（pytest 10→24 项）

## [2.7.2] - 2026-08-06

### 交付测试修复
- **多 Agent 交付测试**：3 Agent 并行测 L1-L5（数据/问答/API/交付/一致性），全过
- **修复增量缓存 bug**：`_ensure_food_ontology` 复用缓存前校验跨表对象属性完整（`_has_required_relations`），缺失强制重建，避免污染本体致溯源静默失效
- 新增 `docs/测试方案.md` + `docs/交付测试报告.md`

## [2.7.1] - 2026-08-06

### 落地问题修复（用户视角）
- **APP/API 通用化**：`GET /api/app-config` 返回当前 KB 品牌/图标/示例；APP 动态加载（去食品硬编码），miss 引导读 KB 示例，任何工厂换数据即换 APP 文案
- **新知识库引导**（`new_kb.py`）：一键搭建企业知识库骨架（kbs.json 注册 + 数据目录 + 词典模板 + 表结构说明）
- **README** 加"添加你的工厂数据（新企业落地）"快速指南

## [2.7.0] - 2026-08-06

### 精炼化（去除冗余能力）
- **删除早期研究框架的冗余层**（10 个模块）：`pipeline` / `factory_agent` / `aggregate` / `analysis` / `model_schema` / `ontology_depth` + 4 个死 agent（enhance/ingest/ops/query）
- 保留 `agents/lexicon_agent`（run.py 自动词典用）+ `core/base_agent`（其依赖）+ `csv_to_owl`（单表建本体）
- 根目录收敛为**精炼核心路径**：data_loader → multi_table → ontology_qa_v3 + graph_rag → api_server
- README 核心组件表更新，定位为"本体在中小工厂的具体实施"参考

## [2.6.2] - 2026-08-06

### 短板推进（测试加固 + 实证）
- **测试加固**：`tests/test_api.py` 新增 API/多租户/graph_store/data_import 测试，pytest 6→10 项
- **LLM 兜底评测**（`benchmark_graphrag.py`）：GraphRAG 开放式问题命中率 **8/8 = 100%**（实证）
- **规模实证**：20 万实体合成图，SQLite 图持久化 1.58s / 加载 1.76s / 67MB（规模化路径实测可行）

## [2.6.1] - 2026-08-06

### 规模化/产品（T-D 剩余）
- **多租户隔离**：`config/kbs.json` 注册多知识库，每企业独立数据/词典；`FOOD_KB` 切换；`GET /api/admin/kbs`
- **实时数据同步**：`POST /api/admin/sync`（admin）重读数据 + 可选外部源导入 + 重建
- **图数据库路径**：`graph_store.py`（SQLite 图持久化，10万-100万实体过渡）+ `docs/规模化.md`（内存图→SQLite→Neo4j 三档 + 迁移要点）

## [2.6.0] - 2026-08-06

### 重构 + 能力增强 + 规模化
- **删除 4 个弃用 QA 引擎**（ontology_qa/v2/query/relation_qa，~771 行，含 eval/exec 隐患），全部迁移到 canonical v3 + GraphRAG 兜底，run.py/factory_agent/query_agent 一致
- **GraphRAG 实体链接增强**（T-C）：`find_seeds` 支持词典引导，问题提到属性/类型时加权有该字段的实体（归一化下划线匹配驼峰）
- **多知识库支持**（T-D）：`FOOD_DATA_DIR` / `FOOD_KB` 环境变量切换，一套部署服务多个企业知识库

## [2.5.0] - 2026-08-06

### 规模化/合规（T3）
- **审计日志**：每次 API 请求记录(时间/方法/路径/来源IP/状态码/耗时)落盘，`GET /api/admin/audit` 可查
- **监控告警**（`monitor.py`）：健康检查 + 指标看门狗，异常告警
- **食品合规文档**（`docs/合规.md`）：GB 溯源标准对齐、一物一码、召回场景、诚实边界

## [2.4.0] - 2026-08-06

### 可用性提升（T2）
- **数据接入自动化**（`data_import.py`）：Excel/DB/CSV → 知识库，列映射 + 定时同步（`--schedule`）
- **数据质量反馈环**（`data_quality.py`）：自动校验空值/重复ID/悬空引用/数值越界 + 报告
- **APP 升级 PWA**：manifest + service worker + 图标，可安装、离线缓存核心页面

## [2.3.0] - 2026-08-06

### 工程化硬化（M1 + 三跳板）
- **角色化鉴权**：`FOOD_ADMIN_KEY`（管理）/ `FOOD_READ_KEY`（只读），/api/* 需 `X-API-Key` 头；未配置则内网开放
- **增量重建**：数据文件 hash 检测，数据未变复用缓存本体，变了才重建（接新数据自动生效）
- **管理端点** `POST /api/admin/rebuild`（admin 权限），接真实数据后强制重建
- **结构化日志 + 指标** `/metrics`：请求计数 + 统一日志格式
- **Docker 一键部署**：`Dockerfile` + `docker-compose.yml` + `nginx.conf`（HTTPS 反代示例）

## [2.2.0] - 2026-08-06

### 新增
- **REST API 层**（`api_server.py`）：FastAPI 服务，统一入口供 APP/语音/Web 调用，自然语言问答 + 正/反向溯源 + 扫码溯源 + 统计
- **食品企业知识库示例**：`data/food_*.csv`（产品/原料/批次/质检/设备 + 溯源 join 表），可跑规则问答 + GraphRAG 溯源
- **品类计数模板**：规则引擎支持"X 的数量"（食品品类计数场景）

### 修复（multi_table 图一致性命中）
- **join 表实例 ID 去重**：id 列非唯一时追加行号，避免 URI 碰撞丢关系
- **id 列同时是外键**：id_col 若在 relations 里则作为对象属性发出（否则 join 表丢失跨表关系）

## [2.1.0] - 2026-08-06

### 新增
- **GraphRAG-lite 层**（`graph_rag.py`）：本体建图 + 图遍历检索（BFS 正反向邻域）+ LLM 生成，补开放式/关系问题路径
- **GitHub Actions CI**（`ci.yml`）：自动跑 pytest 单测 + 对照评测
- **持久化 pytest 单测**（`tests/test_core.py`）：数据加载/本体生成/规则问答/GraphRAG 检索，5 项全过
- **图查询接口**：graph_rag 提供 build_graph / find_seeds / extract_subgraph / serialize（图查询即接口）

### 重构
- 定 `ontology_qa_v3.py` 为唯一 canonical 问答引擎；`ontology_qa.py` / v2 / ontology_query / relation_qa 标注 DEPRECATED（保留供回退，不硬删）

## [2.0.2] - 2026-08-06

### 新增
- **多数据源支持**：`data_loader.py` 统一读取 CSV / JSON / SQLite / Excel（前三种标准库零依赖，Excel 可选 openpyxl）；`csv_to_owl.py` 与 `multi_table.py` 均支持

### 修复（CodeAgent 代码审查发现）
- **data_loader.py**：SQLite 表名拼接前加合法标识符校验，消除 SQL 注入风险（表名来自库内，校验后安全）
- **multi_table.py**：移除未使用的 `csv` / `json` import（改 data_loader 后的残留）
- **csv_to_owl.py**：移除未使用的 `os` import

## [2.0.1] - 2026-08-06

### 新增
- **Web 前端**（`web/`）：Svelte5 + Vite + Node 的完整问答应用，CSV 上传 → 建模 → 自然语言问答 → 知识图谱/分析看板。已移除硬编码私有路径，指向仓库内 `codes/` 套件

## [2.0.0] - 2026-08-06

### 新增
- **benchmark.py**：本体问答 vs 纯 LLM 命中率对照评测（可复现，标准答案从源数据确定性计算）
- **多领域泛化验证**：新增图书库存、能源电站 2 个不同领域示例数据集，三个领域 benchmark 均 **100%** 命中
- **multi_table.py**：多表自动关联建本体（自动外键检测 + 跨表对象属性），无需手写 relations.json
- **新问答模板**：过滤计数（`属性=N 的数量`）、总数（`一共有多少条记录`）
- **方法论文档升级**：`docs/泛化方法论.md`（含 benchmark 实证）

### 修复
- 本体问答引擎补全过滤计数、总数模板，结构化查询命中率 74% → **100%**

### 基础设施
- 引入 `__version__`（主入口 run.py）

## [1.0.0] - 2026-08-06（初始开源发布）

- CSV → 本体（N-Triples，类型自动推断）
- 词典驱动通用问答引擎（规则 + LLM 兜底）
- 交付方法论 / 白皮书 / 开源调研文档
