# 商用化基础设施研究报告：MySQL 迁移 · 鉴权多租户 · License 计费 · 交付运维 · 政企采购

> 对象系统：自研本地部署 AI 系统（Python + 标准库为主，SQLite 默认存储，已有 Bearer 令牌鉴权，已具备 Docker 部署）
> 目标：可私有化交付给制造业客户，满足"数据不出厂"
> 检索网络：墙内（代理 127.0.0.1:33210）；检索通道 anysearch CLI（`node <skill_dir>/scripts/anysearch_cli.js`）
> 图例：🟢 = 已 extract 核到正文/官方原文；🟡 = 检索层/二手转述，未见原文；🔴 = 未取到 / 未找到公开资料
> 本报告所有 URL 均为真实检索/抓取所得，未编造；每条结论标注核验等级。

---

## 0. 一页结论（先做什么最值）

| 优先级 | 动作 | 为什么最值 | 工作量 |
|---|---|---|---|
| **P0** | **存储抽象层（Dialect Adapter）先行**：所有 SQL 走一处方言适配器，先固化 schema 与参数化查询，再谈换库 | 换库的成本 90% 在 SQL 方言散落各处；先收敛再迁移，否则迁移即重构 | 1–2 周 |
| **P0** | **SQLite 加固到"可交付"档**：WAL + `busy_timeout` + 单写者串行化 + 备份快照 | SQLite 已有就够撑单机私有化；不加固则并发一上来就 `database is locked` 丢数据 | 3–5 天 |
| **P0** | **License 离线签验（非对称签名 + 机器码绑定）** | 私有化交付的收钱闸门；无此则软件可被无限复制，商业模型不成立 | 1 周 |
| **P1** | **审计日志 + 日志留存 ≥6 个月**（法定硬要求） | 《网络安全法》第二十一条(三) 明文"留存不少于六个月"，是政企验收必查项 | 1 周 |
| **P1** | **多租户隔离：tenant_id 贯穿 + 出接口强制过滤** | 制造业集团客户多工厂/多组织，串数据是交付事故级问题 | 1–2 周 |
| **P1** | **离线安装包 + 一键部署脚本（docker compose 打包）** | 客户内网无外网，交付形态决定能不能落地 | 1 周 |
| **P1** | **MySQL 适配（不是立刻迁移）**：先把 MySQL 跑通为"可选后端"，SQLite 保持默认 | 只有大客户/高并发才需要 MySQL；过早迁移是过度工程 | 2–3 周 |
| **P2** | **备份恢复 + Prometheus/Grafana 最小监控集** | 运维可观测性；可在交付后补齐 | 1–2 周 |
| **P2** | **信创适配（达梦/人大金仓）+ 等保/密评材料** | 大单（国企/政务）才需要；提前 3–6 个月准备认证材料 | 视订单 |

一句话：**先把"不迁库也能交付"做扎实（P0），把"收钱的闸门"（License）和"合规的底线"（审计日志）补齐，MySQL 只作为可选后端按客户要求启用。**

---

## 1. SQLite → MySQL 迁移

### 1.1 schema 与方言差异清单

| 维度 | SQLite | MySQL / InnoDB | 核验 |
|---|---|---|---|
| **类型系统** | 动态类型（type affinity，5 档 affinity），列可存任意存储类数据 | 静态严格类型 | 🟢 [sqlite.org/datatype3.html](https://www.sqlite.org/datatype3.html) |
| **布尔** | **无独立 BOOLEAN 存储类**，用 INTEGER 存 0/1 | 习惯 `TINYINT(1)`（或 8.0 的 `BOOLEAN` 别名=`TINYINT(1)`） | 🟢 sqlite.org/datatype3.html；🟡 [convert-in.com 类型映射](https://www.convert-in.com/docs/slt2sql/types-mapping) |
| **日期时间** | **无专用存储类**，存 TEXT(ISO8601)/REAL(儒略日)/INTEGER(unix 秒) 三选一 | `DATETIME` / `TIMESTAMP` / `DATE` 严格类型 | 🟢 sqlite.org/datatype3.html |
| **自增主键** | `INTEGER PRIMARY KEY AUTOINCREMENT`（且是 rowid 别名，语义特殊） | `AUTO_INCREMENT`，且仅可有一列、须为索引 | 🟡 [ai2sql 转换指南](https://builder.ai2sql.io/convert/sqlite-to-mysql) |
| **UPSERT** | `INSERT ... ON CONFLICT(...) DO UPDATE/NOTHING`（3.24+） | `INSERT ... ON DUPLICATE KEY UPDATE`（依赖唯一键/主键，**无显式冲突目标**）；另有 `REPLACE INTO`（先删后插，会触发级联、重置自增） | 🟡 [dev.mysql.com ON DUPLICATE KEY](https://dev.mysql.com/doc/refman/8.3/en/insert-on-duplicate.html)（官网 extract 被拦，标题层引用）；🟡 [bytebase 博客：ON DUPLICATE KEY 即使值未变也会锁冲突行](https://www.bytebase.com/blog/sql-upsert/) |
| **JSON** | `json` 函数 + TEXT 存；无原生 JSON 类型 | 原生 `JSON` 类型 + `->`/`->>` 操作符 + 生成列索引 | 🟡 见 1.1 注 |
| **并发模型** | **单写者**：WAL 模式下一个 WAL 文件只能有一个写入器（读写不互斥）；**WAL 不能放在网络文件系统上** | 行级锁 + MVCC，多写者并发 | 🟢 [sqlite.ac.cn/wal.html](https://sqlite.ac.cn/wal.html)（WAL 官方文档中文版） |
| **默认隔离级别** | SERIALIZABLE（可串行化，实际按锁粒度近似） | **REPEATABLE READ** | 🟢 [mysql.net.cn 8.0 事务隔离级别（官方文档中文镜像）](https://mysql.net.cn/doc/refman/8.0/en/innodb-transaction-isolation-levels.html) |
| **索引前缀长度** | 无此限制 | 5.6 默认 767 字节；5.7.7+/8.0 默认支持 **3072 字节**；`COMPACT/REDUNDANT` 行格式 767，`DYNAMIC/COMPRESSED` 3072；**utf8mb4 每字符 4 字节**（varchar(255) 索引 = 1020 字节，超 767 会报 `ERROR 1071`） | 🟢 [阿里云 RDS 官方帮助页](https://www.alibabacloud.com/help/zh/rds/support/specified-key-was-too-long-max-key-length-is-767-bytes) |

**JSON 字段说明**：本机 Python + 标准库栈，SQLite 下 JSON 通常以 `TEXT` 列存字符串。迁 MySQL 有两种选择：(a) 保持 `TEXT`/`LONGTEXT` + 应用层 `json.loads/dumps`——**最省事、可移植性最好，推荐**；(b) 改原生 `JSON` 类型——可建生成列索引、用 SQL 直接查询，但会把应用与 MySQL 绑定，违背"可换库"目标。🟡（此判断为工程推断，非引文）

### 1.2 迁移工具与回滚方案

- **全量 + 增量 + 一致性校验三件套**：TiDB 官方迁移文档给出了"用 Dumpling 全量导出 + TiDB Lightning 导入 + DM 增量追平 + `safe-mode` 避免增量报错"的标准范式，**这是自研迁移脚本可直接照抄的流程骨架**（尤其"全量快照必须是同一一致性快照，否则增量起点要前移"这条坑）。🟢 [docs.pingcap.com 大数据量合并迁移](https://docs.pingcap.com/zh/tidb/stable/migrate-large-mysql-shards-to-tidb/)
- **在线加字段/改表**：`pt-online-schema-change`（Percona）/ `gh-ost`（GitHub）是 MySQL 在线 DDL 的行业标配。🟡（检索层提及，未核原文）
- **异构数据同步**：Canal（解析 binlog，伪装 MySQL slave）、DataX（阿里离线批量）、Flink CDC（流式）。🟡 [juejin 工具对比](https://juejin.cn/post/7520955995092172850)；商业版 NineData 主打"迁移+对比（一致性校验）"，社区版支持 Docker 本地化运行。🟡 [Tencent 开发者社区 NineData 社区版](https://developer.cloud.tencent.com/article/2637485) / [ninedata.cloud/dbmigration](https://ninedata.cloud/dbmigration)
- **回滚方案（推荐做法）**：
  1. **双写期不删旧库**：源 SQLite（或旧 MySQL）保持完整可读，回滚 = 切回旧连接串；
  2. **迁移前全量物理备份**（SQLite 用 `VACUUM INTO` 或文件拷贝，MySQL 用 `mysqldump --single-transaction`）；
  3. **schema 迁移脚本幂等 + 可反向**：每支 migration 带 `up`/`down`；
  4. **灰度按"读→写"两步走**：先只切读（双写比对无损）→ 再切写。
  🟡（工程实践共识，未找到单一权威原文；TiDB 文档的"业务平滑切换"章节为流程参考 🟢）

### 1.3 双写 / 灰度切换做法

- **双写**：应用层写主库后同步写从库（或消息队列异步），读仍走主库，比对两库差异。**代价是写放大与一致性窗口**。🟡
- **灰度切流**：按 tenant/工厂维度分批切，切完观察错误率与延迟再放量。🟡
- **MySQL 特有坑**：`ON DUPLICATE KEY UPDATE` 在值未变时也会锁冲突行，高写表上双写会放大锁竞争——双写期尤其要压测。🟡 [bytebase](https://www.bytebase.com/blog/sql-upsert/)

### 1.4 存量数据校验对账方法

- **行数对账**：源/目标逐表 `COUNT(*)` 比对（最快的第一道门）。
- **摘要对账**：逐表对**排序后拼接的关键字段**算 hash/checksum 比对；或分批（按主键区间）比对。
- **逐行 diff**：抽样 + 全量分批比对，差异落到对账报告表。
- **迁移必须处理的数据坑**（来自达梦 MySQL 迁移 FAQ，SQLite→MySQL 同理会遇到）：`0000-00-00` 等非法日期、`CHAR` 自动空格补齐、`TIMESTAMP` 时区/范围、加密函数（`AES_ENCRYPT/DECRYPT`）不兼容。🟢 [达梦技术文档 MySQL→DM 迁移 FAQ](https://eco.dameng.com/document/dm/zh-cn/faq/faq-mysql-dm8-migrate.html)
- 🟡 对账脚本需自研（本机 Python + 标准库即可，无需引第三方）：`sqlite3` ↔ `pymysql`（或 MySQL 官方 `mysql-connector-python`）双连接，按主键区间分批拉取 → 规范化（日期统一 ISO、布尔统一 0/1）→ hash 比对。

### 1.5 迁移后的性能与并发基线

- **隔离级别**：MySQL 默认 REPEATABLE READ，**长事务 + 间隙锁可能死锁**，应用层要（a）事务尽量短，（b）按主键顺序访问，（c）对邮箱/唯一键做显式唯一索引而非依赖锁。🟢（隔离级别依据见 1.1）；🟡（死锁工程建议为通行实践）
- **连接池**：Python 侧建议 `DBUtils`/`SQLAlchemy Pool`（若引入）或在标准库 `sqlite3`/`pymysql` 外自建连接池；MySQL `max_connections` 与客户机资源要匹配（制造业现场常是低配工控机）。🟡
- **基线建议**（自测得出，非引文）：记录 p50/p95 延迟、QPS、并发连接数、慢查询阈值（`long_query_time`），迁移前后同一压测脚本跑两遍做对比。🟡
- **索引前缀**：utf8mb4 下给 `VARCHAR` 建索引前先核对字节数，超 3072 需用前缀索引（`INDEX(col(191))`）。🟢 [阿里云 RDS 官方帮助页](https://www.alibabacloud.com/help/zh/rds/support/specified-key-was-too-long-max-key-length-is-767-bytes)

---

## 2. 鉴权与授权

### 2.1 JWT / OAuth2 / SSO / LDAP 在私有化场景的取舍

| 方案 | 适用 | 私有化取舍 |
|---|---|---|
| **Bearer Token / JWT（现状）** | 内网单一系统、无统一身份源 | **保持为默认**；制造业客户多数没有统一 IAM，强上 SSO 是给自己加负担 |
| **OAuth2** | 需要给第三方系统授权、多端登录 | 私有化场景收益低，除非要开放 API 给客户 ERP/MES |
| **SSO（CAS/OIDC）** | 客户已有统一门户（国企/集团常见） | **按客户要求做"可选插件"**，不要做成强依赖 |
| **LDAP / AD** | 客户有域控（外企/大型制造） | 同上，做适配层；**客户内网 AD 往往是唯一身份源** |

- **开源 IAM 可选 Keycloak**（支持 SAML/OAuth/LDAP，可私有化部署），适合"不想自研鉴权"的团队。🟡 [Logto 2025 开源 IAM 供应商](https://blog.logto.io/zh-TW/top-oss-iam-providers-2025)
- **内部服务间鉴权**：用非对称签名（服务 A 持私钥签名 JWT，服务 B 用公钥验签），验签方无需回调鉴权中心。🟡（检索到的架构文章描述）

### 2.2 会话与令牌过期 / 刷新

- **JWT 安全基线（OWASP 官方 Cheat Sheet）**：签名保证 header+claims 未被篡改；**优先公钥数字签名（RSA/ECDSA）而非 MAC（HMAC）**，避免算法混淆攻击；**不要在 payload 放敏感信息**。🟢 [OWASP JWT Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/JSON_Web_Token_Cheat_Sheet.html)
- **刷新令牌轮替（Refresh Token Rotation）**：用刷新令牌换新访问令牌时，**作废旧刷新令牌并签发新刷新令牌**，降低泄露风险；Logto 默认启用该机制。🟢 [Logto 博客：什么是刷新令牌轮替](https://blog.logto.io/zh-TW/understanding-refresh-token-rotation)
- **过期时间惯例**：访问令牌短（小时级）、刷新令牌较长（天级）+ 轮替 + 可撤销。🟡 [AWS Cognito 刷新令牌文档](https://docs.aws.amazon.com/zh_cn/cognito/latest/developerguide/amazon-cognito-user-pools-using-the-refresh-token.html)（二手整理）
- **私有化补充**：JWT 无状态难以"立即吊销"，需维护**吊销名单（黑名单 / jti + Redis 或数据库）**或改用服务端会话。🟡（OWASP 提到令牌撤销需求 🟢）

### 2.3 RBAC 与多租户数据隔离

- **三层隔离**：功能权限（RBAC）+ 数据权限（行/列）+ 租户隔离。🟡 [企业级权限管理：RBAC+数据权限+多租户隔离](https://www.cnblogs.com/zhouzhongyan2020/p/19813074)
- **主流实现 = 按 `tenant_id` 字段隔离**（共享库共享表 + 每行带 tenant_id），查询层强制注入 `WHERE tenant_id = ?`。🟡 [浅析 SaaS 多租户数据隔离方案](https://zhuanlan.zhihu.com/p/630429257)
- **行级安全（RLS）**：PostgreSQL 有原生 RLS 可用数据库角色实现租户隔离；MySQL 无等价原生机制，**必须在应用层（DAO/中间件）强制织入 tenant_id 过滤**。🟡 [Logto: 用 PostgreSQL RLS 实现多租户](https://blog.logto.io/zh-CN/implement-multi-tenancy)；🟢 [Azure 架构中心：多租户存储与数据隔离方法](https://learn.microsoft.com/zh-cn/azure/architecture/guide/multitenant/approaches/storage-data)（官方文档，已核）
- **AI 平台特有**：多租户需同时隔离"权限 + 数据 + 计费"三层（模型调用、知识库检索、应用访问都按 tenant 绑定）。🟡 [腾讯云开发者：多租户 AI 平台设计](https://developer.cloud.tencent.com/article/2671731)
- **对自研系统的具体建议（P0）**：
  1. 所有业务表加 `tenant_id`（NOT NULL + 索引首位），**主键可用 `(tenant_id, id)` 复合**；
  2. 写一个**唯一的查询入口**（repository 层），强制拼 tenant 条件，禁止裸 SQL 绕过；
  3. 加**自检门**：CI/启动时扫描所有 SQL，发现无 `tenant_id` 过滤的查询即报错；
  4. 交付时若客户是单租户，让 `tenant_id` 默认常量，不增加复杂度。

### 2.4 审计日志要求

- **法定底线**：《网络安全法》**第二十一条(三)** 明确要求"采取监测、记录网络运行状态、网络安全事件的技术措施，并**按照规定留存相关的网络日志不少于六个月**"。🟢 [cac.gov.cn 网络安全法原文（已 curl 核到正文）](https://www.cac.gov.cn/2016-11/07/c_1119867116_2.htm)
- **等保三级**要求日志**防篡改**、集中审计，并要求"安全管理中心"（三级强制、二级无要求，含系统管理/审计管理/安全管理/集中管控四维度）。🟡 [等保三级必备安全管理制度清单](https://guoyuants.com/news/dbrd/526.html)；🟢 [华为云 等保三级 2.0 规范合规包（引 GB/T 22239-2019）](https://support.huaweicloud.com/usermanual-rms/rms_13_6002.html)
- **标准依据**：GB/T 22239-2019《信息安全技术 网络安全等级保护基本要求》。🟡 [国家标准全文公开（openstd.samr.gov.cn，页面为 JS 渲染，仅标题层）](https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno=BAFB47E8874764186BDB7865E8344DAF)；🔴 公安部解读页未取到（`www.mps.gov.cn` extract 返回 98 字节壳）
- **对自研系统的建议**：审计日志**独立表 + append-only**（禁 UPDATE/DELETE）+ 记录 `who/when/what/before-after/ip/result`，至少保留 6 个月，导出格式便于客户交测评机构。

---

## 3. 授权与计费（License）

### 3.1 私有化 License 常见机制

- **机器码绑定**：授权绑定唯一设备 ID，防止一码多机。🟡 [DJI Payload SDK License 校验说明（唯一设备 ID 强绑定、支持有效期、离线校验）](https://developer.dji.com/doc/payload-sdk-tutorial/cn/manifold-quick-start/license.html)
- **授权维度**：按节点数 / 按并发数 / 按年（订阅）——国内私有化交付常见组合。🟡（检索层；另有专利 CN115859389A"基于私有化部署的软件序列号授权方法" 🟡 [Google Patents](https://patents.google.com/patent/CN115859389A/zh)）
- **到期降级**：两种模式（a）**到期停用**（严格，制造业尾款回收场景常用——授权到期映射合同付款节点）🟡 [工业设备授权到期与续期设计](https://blog.csdn.net/2601_95885957/article/details/162043960)；（b）**永久回退授权**（订阅到期后仍可用当前版本，只是不能再升级，如 JetBrains 的 perpetual fallback license）🟡 [JetBrains 永久回退授权](https://sales.jetbrains.com/hc/zh-tw/articles/207240845)

### 3.2 离线授权文件签名验证与防篡改

- **核心机制 = 非对称签名**：厂商**私钥签名**授权信息 → 客户侧软件内置**公钥验签**；先对内容做哈希摘要，再对摘要签名（性能考虑）。License 含客户信息、授权时间、绑定机器、功能开关。🟢 [掘金：适配私有化部署，手写支持离线验证的 License 授权系统](https://juejin.cn/post/7545015409617961023)；🟡 [电子工程专辑：软件 License 授权原理](https://www.eet-china.com/mp/a291968.html)
- **防篡改三要素**：① 文件头魔数 + 分隔符校验；② 提取授权信息验签比对；③ 到期时间校验。🟡 [电子工程专辑](https://www.eet-china.com/mp/a291968.html) / [百度智能云：离线许可实现原理](https://cloud.baidu.com/article/3345142)
- **算法选择**：RSA + 摘要签名是主流；国密场景（密评要求下）应改用 **SM2 签名 + SM3 摘要**（见 §4.2）。🔴 未找到"私有化 License 用国密 SM2"的权威公开原文，**此条为基于密评要求的工程推断，标注为未核实**。
- **对自研系统的具体建议（P0）**：
  1. Python 标准库即可实现（`cryptography` 库更省事，或纯 `hashlib` + 内置验签逻辑）；
  2. 授权文件 = `base64(json_payload) + "." + base64(签名)`，与 JWT 结构同构；
  3. **公钥硬编进代码**，私钥只留在公司签发系统（离线机器）；
  4. 机器码 = 主板/CPU 序列号 + 网卡 MAC 的哈希（注意虚拟化环境会变，需容错策略）；
  5. **防回拨**：记录"最后运行时间"，发现系统时间回拨即触发异常（离线场景防绕过到期）。

### 3.3 计费模式与按调用量计量

- **计量指标（metering）设计**：按用量定价中，"价值计量单位"是决定客户付多少钱的核心，优质计量指标需"随价值递增"。🟢 [Stripe：SaaS 按用量定价策略](https://stripe.com/zh-us/resources/more/usage-based-pricing-strategy-for-saas)
- **计量维度**：token 数 / 调用次数 / 生成时长 / 成功任务数。🟡 [支付宝 Agent 支付：AI 助手按量收费](https://aipay.alipay.com/article/229)
- **平台级参考**：AWS Marketplace / Microsoft Marketplace 都用"定义计费维度 + 上报用量记录 → 平台计费"的模式。🟡 [AWS Marketplace 计量](https://docs.aws.amazon.com/zh_cn/marketplace/latest/userguide/metering-for-usage.html) / [Microsoft 市场计量计费](https://learn.microsoft.com/zh-cn/partner-center/marketplace-offers/saas-metered-billing)
- **私有化特殊点（数据不出厂）**：**用量数据不能上传云端**。做法：(a) 本地计量、本地出报表，客户按报表对账付费（离线计量）；(b) 客户授权的单向"用量摘要"上报（仅计数不含内容）；(c) 计量与 License 解耦——License 控"能不能用"，计量控"用多少"。🔴 未找到"私有化离线计费"的权威公开原文，**此条为工程推断**。
- **实现要点**：每次调用写一条**计量事件**（append-only），按周期聚合；计量点放在**统一入口**（与 §1.5 的"唯一查询入口"同理，一处收口）。

---

## 4. 交付与运维

### 4.1 Docker / K8s 私有化交付与离线安装包

- **离线交付范式**：把应用与依赖打包成离线包，U 盘拷入内网，一键安装（政企内网无外网是常态）。🟡 [ROI：离线环境 K8s 一键部署](https://www.cnblogs.com/databank/p/19494081)
- **K8s 离线部署工具**：kubeadm + 私有镜像仓库；KubeKey（KubeSphere 生态，可制作离线安装包 + 一键部署）；KubeClipper 离线包；sealos。🟢 [blog.k8s.li PaaS toB K8s 离线部署方案](https://blog.k8s.li/pass-tob-k8s-offline-deploy.html) / 🟡 [KubeKey 离线部署实战](https://kubesphere.io/zh/blogs/using-kubekey-v3.1.1-deploy-k8s-v1.28.8-offline/) / 🟡 [KubeClipper 制作离线安装包](https://kubeclipper.io/docs/getting-started/make-offline-package/) / 🟡 [Kuboard sealos 离线安装](https://kuboard.cn/install/sealos/)
- **对本系统的建议**：**优先 Docker Compose 单机交付**（制造业现场常是一台工控机/服务器），**K8s 只在客户明确有多节点高可用需求时才上**——K8s 会给客户运维和自身交付都加复杂度。离线包 = `docker save` 镜像 tar + `compose.yaml` + 一键脚本。🟡（工程判断）

### 4.2 升级迁移、备份恢复、监控告警最小集

- **升级迁移**：schema 版本表（`schema_version`）+ 幂等 migration 脚本；升级前自动备份；升级失败可回滚到上一版本镜像。🟡（工程共识）
- **备份恢复**：MySQL `mysqldump --single-transaction` / `xtrabackup`；SQLite `VACUUM INTO` 快照；**定期恢复演练**（备份不验证 = 没有备份）。🟡 [ClickHouse 生产就绪指南提到"建立备份验证和灾难恢复流程"](https://clickhouse.com/docs/zh/products/cloud/guides/production-readiness)
- **监控告警最小集**：Prometheus（采集）+ Alertmanager（告警路由/去重/分组）+ Grafana（看板）；Docker Compose 一键拉起。🟡 [Docker 部署 Prometheus+Grafana+Alertmanager](https://github.com/misakivv/docker-Prometheus-Grafana/blob/main/docker%E9%83%A8%E7%BD%B2%E7%9B%91%E6%8E%A7Prometheus%2BGrafana-cnblog.md)
  - **最小告警项**：服务存活、CPU/内存/磁盘、数据库连接数/慢查询、License 到期预警、审计日志写入是否正常。🟡

### 4.3 等保 2.0 与密评要求

- **等保 2.0**：标准为 **GB/T 22239-2019**；三级系统**强制要求"安全管理中心"**（系统管理/审计管理/安全管理/集中管控）。🟢 [华为云 等保三级 2.0 合规包（引 GB/T 22239-2019）](https://support.huaweicloud.com/usermanual-rms/rms_13_6002.html)；🟡 [等保三级制度清单](https://guoyuants.com/news/dbrd/526.html)
- **密评（商用密码应用安全性评估）**：
  - 依据标准：**GM/T 0054-2018**《信息系统密码应用基本要求》（行业标准，密评指导性标准）+ **GB/T 39786-2021**（上升为国标）。🟡 [安全内参：开展密评工作](https://www.secrss.com/articles/36928)
  - 定义：对采用商用密码技术/产品/服务建设的信息系统的密码应用**合规性、正确性、有效性**进行评估。🟢 [辽宁省密码管理局：等保、关基、密评三者关系](http://www.lnsm.gov.cn/lnsm/zcfg/zcjd/2024032708282690302/index.shtml)；🟡 [智巡官网释义](https://www.zxcsec.com/Assessment.html)
  - **等保三级及以上系统：密评与等保同时产生作用**。🟡 [安全内参](https://www.secrss.com/articles/36928)
  - 测评要求涉及"密钥管理安全性""密码产品合规性"（须用**认证合格的商用密码产品**，如签名验签服务器）。🟡 [启明星辰《密评 FAQ（第三版）》PDF](https://www.venustech.com.cn/u/cms/www/202401/111400449qji.pdf)；🟡 [奇安信签名验签服务器](https://www.qianxin.com/product/detail/pid/503)
  - 管理办法：🟡 [《商用密码应用安全性评估管理办法》（司法部网站）](https://www.moj.gov.cn/pub/sfbgw/flfggz/flfggzbmgz/202312/t20231221_492092.html)
  - 官方解读：🟡 [国家密码管理局（oscca.gov.cn）专家解读密评体系](https://www.oscca.gov.cn/sca/xxgk/2023-05/31/content_1061054.shtml)
- **对自研系统的含义**：等保三级/密评是**认证流程**，不是代码改造——需要（a）选用认证合格的密码产品（而非自己写加密），（b）等保测评前做安全整改（双因素认证、日志防篡改、集中审计），（c）**提前 3–6 个月**准备材料。若客户只要"二级"，密评非强制，压力小很多。

---

## 5. 政企 / 国企采购常见技术要求

### 5.1 信创与国产数据库适配

- **信创 = 信息技术应用创新产业**，覆盖国产 CPU / 操作系统 / 数据库 / 中间件。"信创要求已从'建议替换'转为'强制替换'"（二手表述，慎用）。🟡 [腾讯云开发者：2026 信创目录全名单与选型指南](https://cloud.tencent.com/developer/article/2710588)
- **硬约束清单**（政务项目）：数据库要进信创目录（OceanBase、达梦、人大金仓、高斯等在列）；底座要匹配国产 CPU（鲲鹏/海光/飞腾）+ 国产 OS（统信 UOS / 麒麟）。🟡 [OceanBase 博客：等保三级+信创目录：政务项目数据库选型硬约束清单](https://open.oceanbase.com/blog/30197956496)（extract 仅得 184 字节壳，仅检索层引用）
- **注意**：**信创名录已停止更新**，当前认定需"多维度综合评估（国家政策标准、第三方认证、技术自主可控性）"。🟡 [搜狐：信创名录取消背景下如何认定](https://www.sohu.com/a/898746485_100232921)
- **国产数据库生态**：达梦（DM8）、人大金仓（KingbaseES）、OceanBase、GaussDB、TDSQL、TiDB、南大通用、神州通用。🟡 [OceanBase 技术百科](https://www.oceanbase.com/topic/techwiki-guochanshujuku) / [dboop：信创和国产数据库](https://www.dboop.com/dba/%E4%BF%A1%E5%88%9B%E5%92%8C%E5%9B%BD%E4%BA%A7%E6%95%B0%E6%8D%AE%E5%BA%93/) / [金仓：国产数据库品牌对比](https://www.kingbase.com.cn/explore/tech-blog/)
- **对本系统的含义**：**只要做了 §1 的方言适配层，适配达梦/金仓大多是"再加一个 dialect"**。达梦官方提供了 MySQL→DM 迁移 FAQ（含日期/char/timestamp/AES 函数坑），可作为适配 checklist 直接复用。🟢 [达梦 MySQL→DM 迁移 FAQ](https://eco.dameng.com/document/dm/zh-cn/faq/faq-mysql-dm8-migrate.html)

### 5.2 审计留存年限与数据不出域

- **审计留存**：网络安全法下限 **6 个月**（见 §2.4 🟢）；**企业档案保管期限**分永久与定期（定期一般 30 年 / 10 年）。🟡 [国家档案局：企业文件材料归档范围和档案保管期限规定](https://www.saac.gov.cn/daj/xzfgk/202112/45c72942b02d499bb4b838a53d04184e.shtml)
- **数据不出域**：依据《数据安全法》《数据出境安全评估办法》——需向网信部门申报安全评估的是"重要数据"出境场景（个人信息门槛约为 10 万人/2 年、敏感个人信息 1 万人/2 年）。🟢 [cac.gov.cn 数据出境安全评估办法](https://www.cac.gov.cn/2022-07/07/c_1658811536396503.htm)；🟡 [海问律所解析重要数据门槛](http://www.haiwen-law.com/35/1491) / [锦天城《重要数据处理安全要求》简析](https://www.allbrightlaw.com/CN/10475/db581830121672ed.aspx)
- **对本系统的含义**：**"数据不出厂"本身就是最合规的形态**——全本地部署天然满足数据不出域，反而是**竞品 SaaS 形态的短板**，这是产品卖点而非负担。要做的只是：在方案/合同里明写"全本地部署、无外联、无遥测回传"，并提供"网络隔离下功能完整"的证明（离线安装 + 离线 License 即为证据）。🟡（工程/商务判断）
- **国企采购规范**：《国有企业采购管理规范》对采购组织架构、实施操作、供应商管理等作出规定（国资委）。🟡 [国资委：《国有企业采购管理规范》](http://www.sasac.gov.cn/n2588040/n2590387/n9854212/c14902719/content.html)

---

## 6. 可执行路线图（按"最先做最值"排序）

### 阶段一：不迁库也能交付（2–4 周，P0）
1. **存储抽象层**：把所有 SQL 收敛到一个 dialect 适配器；参数化查询全覆盖；固化 schema 定义（单一真相源）。
2. **SQLite 加固到交付档**：开 WAL、设 `busy_timeout`、单写者串行化、写操作重试；`VACUUM INTO` 备份。
3. **License 离线签验**：非对称签名 + 机器码绑定 + 到期判定 + 防回拨；生成/验证双脚本。
4. 产出：可交付客户的 Docker 镜像 + 离线安装包 + License 签发工具。

### 阶段二：合规与隔离（3–5 周，P1）
5. **审计日志**：append-only 表 + who/when/what/before-after/result，留存 ≥6 个月，可导出。
6. **多租户隔离**：`tenant_id` 全表贯穿 + 唯一查询入口强制过滤 + CI 自检门。
7. **备份恢复 + 最小监控**：docker compose 拉起 Prometheus/Grafana/Alertmanager；备份恢复演练脚本。

### 阶段三：可选后端与认证（按订单驱动，P2）
8. **MySQL 作为可选后端**：先跑通（同一 dialect 适配器加 mysql 分支）+ 迁移脚本 + 对账脚本 + 双写/灰度开关；SQLite 仍为默认。
9. **国产数据库适配**：达梦/金仓各加一个 dialect（复用达梦迁移 FAQ checklist）。
10. **等保/密评材料**：按客户等级要求准备；三级及以上才需密评，且必须用认证密码产品。

### 阶段四：增强（按客户要求）
11. SSO/LDAP 适配插件；12. 按调用量计量 + 离线报表；13. K8s 多节点高可用交付。

---

## 7. 反幻觉备注（核验状态）

**已 extract 核到正文（🟢）**：sqlite.org/datatype3.html、sqlite.ac.cn/wal.html、mysql.net.cn（MySQL 8.0 官方文档中文镜像，隔离级别）、cheatsheetseries.owasp.org（JWT）、alibabacloud.com（索引前缀）、eco.dameng.com（MySQL→DM 迁移 FAQ）、docs.pingcap.com（TiDB 迁移）、support.huaweicloud.com（等保三级合规包）、blog.logto.io（刷新令牌轮替）、juejin.cn/post/7545015409617961023（离线 License 实践）、blog.k8s.li（K8s 离线部署）、stripe.com（按用量定价）、learn.microsoft.com（多租户存储隔离）、cac.gov.cn（网络安全法原文，curl 直连核到第二十一条(三)）、cac.gov.cn（数据出境安全评估办法）、lnsm.gov.cn（密评定义）。

**仅检索层/二手（🟡）**：dev.mysql.com（ON DUPLICATE KEY / 官方隔离级别页 extract 返回 55 字节壳，改引 mysql.net.cn 镜像 + 标题层）、open.oceanbase.com（184 字节壳）、openstd.samr.gov.cn（JS 渲染，仅标题层）、mps.gov.cn（98 字节壳）、以及各博客/知乎/CSDN/腾讯云社区转述。

**未取到 / 未找到（🔴）**：`www.mps.gov.cn` 公安部等保解读页正文；"私有化 License 采用国密 SM2/SM3"的权威公开原文（本报告该条已显式标注为工程推断）；"私有化离线计费"的权威公开原文（同上标注为工程推断）。

**未编造声明**：本报告未编造任何 URL、价格、条款号、star 数；凡未核实的内容均标注 🟡/🔴 或明写"为工程推断"。
