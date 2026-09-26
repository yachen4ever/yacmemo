> [English](en/01-architecture.md) | 简体中文

# yacmemo 精简记忆层设计（v2 终形态）

> 2026-09-13 设计定稿；2026-09-14 更新（HTTP 多机部署模型，P0–P2 已实现）
> v1 三层架构的设计文档在 `legacy/docs/`，保留作为决策记录。本目录其余文档为 v2 现行技术文档：02 工具规格、03 存储与检索、04 一致性、05 部署、06 评测、07 WebUI 控制台；00 为用户使用手册（入口）。

---

## 一、决策过程：为什么走到这里

### 1.1 起点：OpenViking 的两个痛点

- **提取阻塞**：OV 用 VLM 对整个 session 历史做提取，每条 90 秒同步阻塞，写入被卡。
- **资源争抢**：VLM 与主模型（Qwen3.8 Flash Next）在同一台 M2 Ultra 上抢显存和带宽。

### 1.2 第一版 yacmemo：判断对了，实现做重了

第一版三层架构（Agent 写 .md → 独立小模型拆分提取 → 独立小模型一致性校验）有两项正确的核心判断：

1. **Agent 写结论，不存原文**——提炼发生在对话内，独立 LLM 不该重做提取。
2. **一致性需要专门对待**——跨 session 失忆的 Agent 不会回头修正旧事实。

但实现上有三个错误：

- 把"维护者"角色交给了 1.3B 激活参数的小模型，且让它**静默自动失效**记忆——错杀的危害远大于漏报；
- Layer 2 的拆分文件机制催生了整套文件保护子系统（hash 校验/冲突备份/完整性扫描），复杂度花在了保护 LLM 生成的文件上；
- 文档说"提取后逐节点实时校验"，实际实现是"每次写入触发全量扫描"，O(全部记忆)/次。

### 1.3 Basic Memory 评估与放弃

BM（basicmachines-co/basic-memory，AGPL-3.0，~3.9k star）与本项目理念同源：markdown 为 source of truth、SQLite 为派生索引、索引路径零 LLM。它的关键启发是：**笔记有稳定地址，事实更新 = edit_note 就地改写**，从数据模型上消灭"同一事实多版本并存"。

放弃自建、采用 BM + 外挂的组合，最后也放弃了。原因：

- 中文向量（FastEmbed 默认模型）与中文 FTS 质量存疑，外挂承担了检索主力后，BM 残余价值（写路径工具 + 图谱）缩水——**外挂方案自己把自己掏空了**；
- 锁版本 = 冻结已知 bug、文档与社区漂移、0.x 升级破坏性变更的达摩克利斯之剑；
- BM 的 `write_note` 无法被拦截，**一致性只能做到"检测 + 标注"，永远到不了"API 级强制"**；
- fork 扩展的合并税高于自拥有 ~1k 行代码。

### 1.4 结论：自建轻量记忆层

自建不是回到第一版，而是只保留验证过有价值的部分，并且获得两个 BM+外挂做不到的能力：

1. **统一的中文一等公民检索**：FTS5 trigram + Qwen3-Embedding 单一混合搜索，不再有两套质量不一致的检索工具；
2. **API 级一致性强制**：`memory_write` 拒绝近重名、`memory_edit` 强制锚点唯一——约定是概率的，API 拒绝是确定的。

---

## 二、核心原则

### 原则 1：markdown 文件是 source of truth

SQLite（FTS/元数据/冲突记录）与 LanceDB（向量）全部是派生索引，删掉可从文件全量重建。人可直接阅读编辑、可 git、可 Obsidian。

### 原则 2：记忆读写主路径零生成式 LLM

store 读写链路（检索/写入/守卫/审计）唯一的模型调用是 embedding（Qwen3-Embedding-0.6B，单次 ~50ms，<1GB 常驻）。提取、拆分、裁决、摘要等一切生成式工作要么不发生（结构靠约定），要么由主模型在对话内完成（裁决发生在读取时刻）。唯一的生成式环节是 **curator 深度审查**——但它独立于读写主路径之外：离线运行（每周 timer）、只读、只提案、绝不执行；裁决归人，落地永远走有守卫的 store 写路径。这直接消灭 OV 痛点 2。

### 原则 3：结构靠约定产生，一致性靠 API 强制

不提取结构，约定产生结构（一篇一主题、observation/`[[链接]]` 语法鼓励不强制）。一致性防线按强度递增分三层：

```
第 1 层  API 强制（确定）    write 拒绝近重名；edit 强制锚点唯一
第 2 层  确定性检测（确定）  D1 标题重复 / D2 observation 撞车 / D3 悬空链接
第 3 层  主模型裁决（读取时）检索结果内联 ⚠ 标注，agent 顺手 edit_note 合并
```

### 原则 4：失效语义优于检测语义

系统**从不删除、从不隐藏**任何记忆。冲突的两条事实都可见、都返回，只加标注。错杀在架构上不可能发生，git 承载真实历史。

### 原则 5：写入同步，无后台补偿

唯一的常驻进程是数据所在机器上的 yacmemo-server（HTTP 多用户）与每周一次的 curator timer（只读审查 + 提案落盘，Type=oneshot）。索引与 git 快照在写入时同步完成（毫秒级），没有后台扫描、没有 webhook、没有 APScheduler、没有队列。这直接消灭第一版"每次写入触发全量扫描"的回归。

（演进记录：初版为 stdio 按会话启停、无常驻服务；P3 转 HTTP 多用户后"无状态会话、客户端零进程"不变，服务端常驻成为部署形态的一部分。）

---

## 三、架构总览

```
你的电脑们（任意 agent：Claude Code / Codex / Cursor / 自研 runtime…）
  │  各端只添加一个远程 MCP URL，客户端零安装、零进程
  │      http://debsvc.local:9721/yachen/mcp
  │      http://debsvc.local:9721/user2/mcp
  ▼
yacmemo-server（debsvc，单进程，streamable HTTP，无状态会话）
  ├── /yachen/mcp → Store(root=/srv/yacmemo/yachen/memory)
  ├── /user2/mcp   → Store(root=/srv/yacmemo/user2/memory)
  │     ├── store.py      markdown CRUD + 写路径守卫 + 同步索引 + git 快照
  │     ├── search.py     FTS5(trigram) + 向量 RRF 融合
  │     ├── detectors.py  D1/D3 确定性检测器
  │     ├── index_db.py   SQLite: 元数据/FTS/冲突记录/守卫事件/向量缓存
  │     ├── vector.py     LanceDB: note_vectors + obs_vectors
  │     └── embedding.py  omlx /v1/embeddings（唯一的模型调用）
  ├── /ui/          → WebUI 控制台（笔记/搜索/审计/使用记录/健康）
  └── GET /health
  ▼
yacmemo-curator（systemd timer，每周）——读注册表/主题卡/审计 → 产出质量提案报告（只提案，绝不执行）
  │
  ▼
磁盘 (source of truth，单点存放)
  /srv/yacmemo/yachen/memory/  ← git 仓库（每次变更自动 commit，永远 git-clean）
  /srv/yacmemo/user2/memory/    ← git 仓库
  各 memory/.index/            ← 可随时删除重建，不进 git
```

数据模型从第一版的 nodes/edges/events 三表塌缩为 **notes + observations** 两级：笔记是主体，observation（若 agent 使用语法）是笔记内的事实行，用于更细粒度的检索与撞车检测。没有实体表、没有边表、没有事件表。

两个用户的内存完全独立：各自的 Store 在构造时绑定各自 root（边界固定，不存在懒解析导致的串目录）；HTTP 传输用无状态会话，任意 MCP 客户端无需会话亲和。13 个工具在 `yacmemo/tools.py` 注册一次，stdio（`yacmemo-mcp`）与 HTTP（`yacmemo-server`）两个入口共享同一工具面。

---

## 四、存储设计

### 4.1 目录约定

```
memory_root/
├── TOPICS.md         主题注册表（人机共维，活跃/已归档）
├── PROFILE.md        用户画像与偏好（记忆层功能文件：不注册、不游离检测、context 前置）
├── topics/<主题>/     每个主题一个目录：abstract.md（agent 维护的现状手册）
│                     + agent 可按模块自由增设的详细 md——目录即归属
├── archive/<主题>/   已归档主题（免注册区，检索仍可用，context 不再注入）
├── journal/          时间线流水（免注册区，豁免重名拦截）
├── agents/<agent>/          identity 专属区：shared/ 子树 = agent 层（同 agent
│                            跨设备共享，如 shared/必读.md）；<device>/ 子树 = 本机
│                            专属；不同 identity 互相不可见（2026-09-24 起，见 4.5）
└── .index/           派生索引（SQLite + LanceDB，可随时删除重建，不进 git）
```

目录划分服务人类浏览与主题归属判定（目录即归属，见十三）；检索不依赖目录。OV 时代的分类目录（infra/knowledge/projects/work/preferences/people）已于 2026-09-16 restructure 全部并入主题目录后移除。

### 4.2 笔记格式

- 一篇一主题，文件名 = 标题（允许中文），首行 `# 标题`；
- 正文自由格式，**无 frontmatter 硬要求**；
- 事实行鼓励使用 observation 语法：`- [类别] 事实内容 #标签`；
- 关联鼓励使用 `[[wiki-link]]`；
- 不使用语法的笔记功能完整降级：检索走 note 级向量 + FTS，D2 检测粒度变粗但可用。

### 4.3 索引结构（全部可重建）

```sql
-- index_db (SQLite)
notes(path TEXT PRIMARY KEY, title TEXT, content_hash TEXT, updated_at TEXT)
fts   (FTS5: title, body, tokenize='trigram')      -- 内容派生自 notes
collisions(id TEXT PRIMARY KEY, kind TEXT,          -- open / resolved / dismissed
           a_path TEXT, b_path TEXT, a_text TEXT, b_text TEXT,
           score REAL, detected_at TEXT, status TEXT)
guard_events(id TEXT PRIMARY KEY, ts TEXT, kind TEXT,        -- refused / forced
             attempted_title TEXT, matched_path TEXT)
vec_cache(content_hash TEXT PRIMARY KEY, vector BLOB)
```

`guard_events` 是设计时 4 张表之外的补充：force 越过守卫必须可数（P4 违约率指标的直接来源），refused 事件用于调标题相似度阈值。

LanceDB 两张向量表（沿用现有 `vector.py`，三表改两表，维度 1024 不变）：

| 表 | 内容 | 写入时机 |
|---|---|---|
| `note_vectors` | 整篇笔记向量（title + 正文） | `memory_write` / `memory_edit` 同步 |
| `obs_vectors` | 逐条 observation 向量 | 同上（一篇通常 0–10 行，~50ms/行） |

写入顺序：**先写文件，再更新索引**。进程崩溃最坏情况是索引滞后（下次写入或 `reindex` 修复），文件永不损坏。重建命令：删除 `.index/` 后调 `reindex` 工具。

### 4.4 git 快照（source of truth 的历史层）

每次 store 变更成功后自动产生一个 git commit（write/edit/edit_section/move/save/delete/topic_register/topic_unregister 各有 message 格式），不变式：**记忆仓库永远 git-clean**。

- 首次写入自动 `git init`（写 `.gitignore` 忽略 `.index/`，并设置 repo-local 身份）；已有身份绝不覆盖；
- 身份可配置：`[[users].git_user_name/git_user_email]` > `[memory].git_user_name/git_user_email]` > 默认 `<id>` / `<id>@yacmemo.com`；
- 外部直接改文件（Obsidian/vim）由 `memory_audit` 自愈时统一以 `external:` 快照收编；
- 降级语义：git 不可用/调用失败只跳过快照（warning 日志 + audit 输出 `== git ==` 行显示最近失败原因），**绝不阻塞记忆写入**；
- 部署注意：systemd 服务默认无 HOME → git 读不到 global gitconfig 的 safe.directory 豁免 → dubious ownership 静默降级（2026-09-16 实测踩坑）；unit 需 `Environment=HOME=/root`，代码层另有 pwd 回填兑底；
- 无远程：记忆仓库纯本地，远程备份（私有 remote / 定期 bundle）列为后续功能。

### 4.5 identity 层级与专属记忆（2026-09-24）

MCP 请求携带 identity token（`Authorization: Bearer <device>_<agent>`，stdio 走 `YACMEMO_TOKEN`）即表明"哪个 agent 在哪台机器"。token 是确定性拼接、无需注册存储；一个 user 下可挂多个 identity：

- **user 层（共享）**：`topics/`、`journal/`、`TOPICS.md`、`PROFILE.md`——所有 identity 完全共享；
- **agent 层**：`agents/<agent>/shared/` 子树——同 agent 跨设备共享（角色纪律、必读）；
- **identity 层**：`agents/<agent>/<device>/` 子树——仅本 identity 可见（本机环境、设备差异）。

专属隔离由服务端强制（`identity.py` 的 `visible`/`writable` 是唯一权威，store/search/tools 共用）：读、检索（scoped search）、列表、写全链路过滤，`memory_context` 冷启动自动注入自己 agent 层 + 本机层的 `必读.md`。约定：`agents/<agent>/` 第一层只有两类子目录——`shared/` 与 `<device>/`（shared 为保留目录名，不能用作设备名），可见性因此是纯字符串逻辑；历史平铺文件只读兼容，写入一律收敛到两类子树；必读只放指针与纪律，事实进 `topics/` 与所有 agent 共享。未携带 token 的旧配置照常可用 user 层，`agents/` 区不可见不可写。WebUI 侧为人类管理员视角（全库可见），「身份」页提供 identity 清单与 token 生成；WebUI 本身的访问密码见 `[webui].password`。

---

## 五、检索设计

### 5.1 混合检索

`memory_search` 默认双路召回、RRF 融合：

```
score(d) = Σ_channels 1 / (rrf_k + rank_channel(d))    # rrf_k = 60
通道 A：FTS5 trigram 全文检索（BM25 排序）
通道 B：note_vectors 余弦近邻（Qwen3-Embedding）
```

RRF 只用名次不用分数，避免两路分数量纲对齐问题。`kind` 参数可强制单通道（`fts` / `vector`），用于 P4 阶段的对比评测。

### 5.2 中文细节（第一验证点）

- trigram 分词器要求 SQLite ≥ 3.34，按 ≥3 字子串命中；**2 字短查询在 FTS 通道会落空**，由向量通道兜住（短查询恰是向量强项）；
- 若 P0 实测 trigram 对真实中文查询召回不足，升级路径：jieba 分词后写入 FTS 影子列（`fts_seg`），双列同时检索；
- 大小写不敏感按 trigram 默认配置，中文无影响，英文标题受益。

### 5.3 撞车标注内联

`memory_search` 结果中，凡命中笔记涉及 `collisions` 表中 status=open 的记录，在结果行内追加：

```
1. yacmemo部署配置 (score 0.91)
   "服务端口为 9721，LLM 指向 m2ultra:11234"
   ⚠ 与 [[yacmemo部署记录0910]] 疑似重复 — 建议读两篇后用 memory_edit 合并
```

**读取的时刻就是修复的时刻**：agent 合并后冲突对自然消失（两侧内容 hash 变化，stale 记录被清除）。

### 5.4 1-hop 关联

`memory_read` 返回正文之外，附加"相关笔记"：正文中的 `[[链接]]` 目标（标题 + 首条 observation）+ 该笔记向量的 top-2 近邻。单跳遍历 ~50 行代码实现，覆盖 90% 的图谱需求；多跳遍历若有朝一日成为真实需求，再评估接入现成产品（届时才是图数据库类工具的正确入场时机）。

---

## 六、MCP 工具面（17 个）

| 工具 | 签名 | 关键行为 |
|---|---|---|
| `memory_search` | `query, limit=10, kind="hybrid"\|"fts"\|"vector"` | 双路 RRF 融合 + 撞车标注内联；<3 字查询走 LIKE 回退，向量通道故障附降级提示 |
| `memory_read` | `path_or_title` | 正文 + 1-hop 相关笔记 |
| `memory_write` | `title, content, force=false, force_confirm=false` | **主题硬拦截**（未注册主题覆盖的路径拒写，force 不豁免，见 6.1）+ **近重名拦截**（含两级 force 确认）；写入即同步索引 |
| `memory_edit` | `path, old_string, new_string` | **锚点唯一性强制**：找不到/命中多处 → 拒绝并列出候选位置 |
| `memory_edit_section` | `path, heading, new_content` | 按 `##` 标题段替换 |
| `memory_move` | `path, new_path` | 移动 + 全库索引随路径更新（[[链接]] 按标题解析，移动不改标题故无需改写链接）；**目标路径同样受主题硬拦截**（移入免注册区放行） |
| `memory_delete` | `path` | **仅用户明确要求时调用**；删文件 + 全部索引行；git 快照保留历史；删的是最近审计快照时联动清 last_audit 缓存 |
| `memory_audit` | — | 自愈（外部改动/删除 hash 级重算与清理、缺向量笔记重试 embedding）+ D1/D3/D4/D5 扫描 + collisions 报告 + 守卫统计 + 空白处置行自清 + git 快照状态行 |
| `memory_list` | `path="", sort="name"\|"mtime"` | 目录树 / 最近变更 |
| `memory_context` | — | **会话开始先调**：PROFILE 前置 + 注册表 + 活跃主题 abstract 摘要头（冷启动回顾） |
| `topic_list` | `tag` | 列出活跃/已归档主题（分组，含标签；可按标签过滤） |
| `topic_register` | `title, description, related` | 注册新主题（**仅用户明确要求**），创建 topics/<主题>/abstract.md |
| `topic_tag` | `title, add, remove` | 主题标签增删（轻量可逆；响应带全库清单引导复用） |
| `topic_unregister` | `title` | 注销主题（**仅用户明确要求**；仅移出注册表，笔记不动，游离后裁决） |
| `archive_topic` | `title` | 归档主题（**仅用户明确要求**）：整个主题目录移入 archive/（卡路径同步改写），检索可用、context 不注入 |
| `get_user_preference` | `section=""` | 读画像/偏好全文或指定小节（PROFILE.md 功能层） |
| `update_user_preference` | `section, content` | 创建/替换画像/偏好的一个小节（agent 维护） |

完整规格见 [02-mcp-tools.md](02-mcp-tools.md)。

### 6.1 写路径守卫（本设计的一致性核心）

```
memory_write(title, content):
    rel = title_to_path(title)
    # 第一道：主题注册制硬拦截（2026-09-17 增补，force 不豁免）
    if rel in {TOPICS.md, PROFILE.md}:
        return 拒绝: "系统文件请用 topic_register / update_user_preference 专用工具。"
    if rel 不在免注册区 且 不被任何注册主题覆盖
            （不在注册主题卡/相关文件所在目录下）:
        记 uncovered 守卫事件
        return 拒绝:
          "写入被拦截: {rel} 不属于任何注册主题。
           新主题先 topic_register 注册（仅用户明确要求），
           模块笔记写入 topics/<主题>/ 下；免注册区不受限。"

    # 第二道：近似标题守卫
    normalized = normalize(title)          # 小写、去标点空白、
                                           # 剥离日期串、"-2"/"(新)"/"更新" 等后缀
    for existing in all_titles:
        if fuzzy_ratio(normalized, normalize(existing)) >= 0.85:
            return 拒绝:
              "已存在近似标题笔记 [[{existing}]]。
               更新内容请用 memory_edit / memory_edit_section。
               确属新主题请 force=true。"

    write file → embed note+obs → 更新 fts/vectors/collisions
    return "已写入并索引"
```

- 拒绝是**确定性行为**，不依赖模型自觉；`force=true` 是模型显式越过**标题守卫**的唯一通道；
- **主题硬拦截不可越过**：`force` 只作用于近似标题冲突——"写入必须有对应主题"是注册制的结构约束，允许 force 绕过等于绕穿注册制（`_path_covered` 与 D4 游离检测共用同一覆盖判定，两套消费方永远同口径）；
- **store.save（WebUI 编辑器/curator 报告/审计快照）只拦新建**：覆盖已有文件不受限，防止 save 成为绕过口；`memory_move` 目标路径同受约束（archive_topic 移入 archive/ 走免注册区豁免）；
- **force 两级确认**：24 小时内 forced 事件达到 `force_confirm_threshold`（默认 3）后，光 `force=true` 会被拒绝，必须同时传 `force_confirm=true`（显式人工确认语义）；拒绝信息列出候选已有笔记，全过程可数；
- **force 调用次数就是违约率的可数指标**（P4 核心度量），audit 汇总报告；uncovered 拦截同样入守卫统计；
- journal/ 目录不参与拦截；
- 阈值 `title_similarity_threshold`（默认 0.85）可配，拒绝事件全量落日志用于调阈值。

### 6.2 索引与快照同步性

所有写工具（write/edit/edit_section/move/delete）在返回成功前同步完成：文件写入 → hash → embedding（仅变更部分）→ FTS/向量/冲突表更新 → git 快照。单次调用总开销 < 300ms（典型笔记）。无懒索引、无后台补偿。

embedding 端点故障时写入**不失败**：文件与 FTS 正常落库，向量缺失以 `notes.vector_ok=0` 标记（2026-09-18 增补），由 `memory_audit` 点名并重试 embedding 自愈——端点恢复后下次审计自动收敛。

---

## 七、一致性机制细则

### 7.1 D1：标题/主题重复（写时拦截 + 审计兜底）

- 写时：6.1 的守卫拦截；
- 审计兜底：对存量笔记（守卫上线前写入的、或 force 越过的）做全量归一化标题两两比对；
- 同时比对共享 tag（≥2 个共同 tag 且标题相似度 ≥ 0.7 也列为候选）。

### 7.2 D2：observation 语义撞车（写时增量检测）

```
memory_write / memory_edit 完成 embedding 后：
    for obs_vector in 新写入的 obs_vectors:
        top-k = obs_vectors.search(obs_vector, k=5)     # 排除同 path
        for hit in top-k where cosine >= 0.86:
            insert collisions(kind="obs", a, b, score, status="open")
```

- 阈值可配（`collision_cosine_threshold`，默认 0.86）；
- **刻意不判断是否矛盾、不裁决谁有效**——只标记"疑似在说同一件事"；
- **机器产物区不进 obs 空间**：journal/audit/ 审计快照与 curator/ 提案报告是系统派生输出（处置行 `- [时间] ...` 是伪 observation，标题剥日期后同构），不参与 obs 索引、D1/D2 候选——系统自己的产物不制造一致性噪声（详见 04-consistency.md）；
- **stale 清理与自愈**：`memory_audit` 比对磁盘文件 hash 与 `notes.content_hash`——外部修改的笔记自动重建索引（embedding 走 vec_cache，未变行零调用），其涉及 collisions 随之重算；外部删除的笔记清理全部索引并列入 `missing` 报告。审计即自愈，无后台进程；
- 局限（接受）：措辞距离远但逻辑矛盾的不会命中——残余风险由第 3 层（主模型在检索到可疑对时判断）覆盖，不做基建。

### 7.3 D3：悬空引用

`[[链接]]` 指向不存在笔记的清单，audit 输出。`memory_move` 的链接改写能预防大部分，剩余靠 agent 顺手修复。

### 7.4 违约率指标（P4 度量）

| 指标 | 来源 | 含义 |
|---|---|---|
| force 使用率 | 写工具日志 | 模型越过守卫的频率 = 约定失效频率 |
| D1/D2 命中数与误报数 | collisions 表 | 检测器质量，用于调阈值 |
| 检索命中率 | 20 条真实查询的个人评测集 | top-3 内含目标笔记的比例，分通道对比 |
| 合并动作数 | audit 前后 collisions status 变化 | 修复闭环是否真的在转 |

---

## 八、Agent 使用约定（贴入任意 agent 的系统提示）

```text
# 记忆使用约定（yacmemo）
会话开始：
0. 先调 memory_context 回顾画像/偏好与主题体系；需要时用 topic_list 查看主题清单。
写入前：
1. 先查后写。写任何记忆前，先用 memory_search 查是否已有同主题笔记。
2. 已有同主题笔记 → memory_edit / memory_edit_section 增量修改，绝不新建重复笔记。
   old_string 从 memory_read 返回的 [正文开始]/[正文结束] 块内逐字复制（勿凭记忆重打）；
   "相关笔记"等标记之后的内容是工具附加信息，不是文件内容，不可作锚点。
3. 新建时标题 = 主题名（如"yacmemo部署配置"），禁止日期后缀和"-2"/"新"等尾巴
   （时间线流水放 journal/ 目录）。
写入时：
4. 写提炼后的结论，不贴对话原文；一篇笔记一个主题。状态/部署/选型类信息**就地更新已有笔记**，不新建带日期的快照（标题守卫会拦截同名新笔记）；过程性记录（调研/评估/排查）放 journal/ 或不存。
5. 事实行用 observation 语法：- [配置] 服务端口为 9721
6. 与其他笔记相关时写关系：- 部署于 [[debsvc]]
检索时：
7. memory_search 结果带 ⚠ 标注时，先读两篇，用 memory_edit 合并，然后才回答用户。
8. 探索一个主题用 memory_read 的相关笔记链路，不要只凭单条搜索结果下结论。
主题：
9. 主题的注册、注销与归档都只在用户明确要求时操作（"把 X 加入长期记忆" / "X 不用长期记录了" / "X 归档吧"）→ topic_register / topic_unregister / archive_topic；主题现状写入 abstract（topics/<主题>/abstract.md）并就地更新，目录内可按模块增设详细 md。
10. 只在注册主题内写笔记（**已代码化为写路径硬拦截**，见 6.1）；journal/、archive/、curator/、agents/ 之外发现游离文件时提示用户归位。
11. 专属必读写自己的 identity 区：agents/<agent>/shared/必读.md（同 agent 跨设备共享）或 agents/<agent>/<device>/必读.md（本机专属）；必读只放指针与纪律，事实一律进 topics/。携带 identity token 时 memory_context 自动注入，注入即视为已读，无需提示词提醒。引用其他层路径必须代入真实设备名（agents/teleagent/r9000x/必读.md），模板占位一律写尖括号形式（agents/<agent>/<device>/…），禁止留空段——agents/teleagent//必读.md 会被当成真实路径、检索必然失败。
12. memory_search 只返回 user 层 + 你的专属区——搜不到别人的专属内容是设计使然，不是索引坏了。
删除：
13. memory_delete 仅在用户明确要求时调用（"删掉 X"/"X 不用记了"）；每次删除自动产生 git 快照，历史可恢复。
审计与提案：
14. 执行审计问题（memory_audit 发现的、或 WebUI 执行指令派下的）时用 memory_audit_update 汇报：executing 接手 → progress 过程 → executed 完成（附摘要）/ blocked 受阻；复审由审计自动确认，不要声称"已验证"、不要代替人忽略。
15. memory_search 默认不返回已结案提案（curator/ 报告全部条目执行/忽略后系统自动打标）——不要执行已结案提案里的条目；memory_read 按路径仍可读。
```

约定仍会写进提示（第 1、2、4 条减少无效往返），但系统不再**依赖**模型守约——守卫与检测器兜底，这正是本设计与第一版的本质区别。

---

## 九、部署与多用户

**一个服务，所有机器，所有 agent**。yacmemo-server 跑在数据所在的 debsvc 上，以 streamable HTTP 暴露 MCP；任何电脑上的任何 MCP 客户端只需添加 URL（`http://debsvc.local:9721/{user}/mcp`），客户端零安装、零进程。用户隔离 = URL 路径 = 磁盘目录，构造时固定边界。部署细节（systemd、各客户端配置示例、备份）见 `05-deployment.md`。

- git：每个 memory_root 一个仓库，`.index/` 入 `.gitignore`；每次变更自动快照（见 4.4），事实历史由 git 承载，仓库永远 git-clean；
- 可选：Syncthing 同步 memory 目录到 Mac 用 Obsidian 浏览；
- 依赖：`mcp`（FastMCP，锁 `<2`，2.x 的 MCPServer 迁移列为评估项）、`lancedb`、`pyarrow`、`numpy`、`rapidfuzz`、`httpx`、可选 `jieba`。服务端单进程；客户端零依赖。

配置示例：

```toml
[memory]
root = "/srv/yacmemo/yachen/memory"
journal_dir = "journal"

[embedding]
base_url = "http://m2ultra:11235/v1"
model = "Qwen3-Embedding-0.6B"
dimensions = 1024

[search]
rrf_k = 60
fts_seg_fallback = false        # jieba 影子列开关

[guard]
title_similarity_threshold = 0.85
collision_cosine_threshold = 0.86
obs_topk = 5
```

---

## 十、现有代码资产去向

| 现有文件 | 去向 |
|---|---|
| `embedding.py` | 原样复用 |
| `vector.py` | 复用改造：node/event/edge 三表 → note/obs 两表 |
| `fs_utils.py` | 复用 `content_hash`/`file_hash`；CRUD 重写为 `store.py`（含守卫） |
| `consistency.py` | 前半段（embed→向量搜索→距离过滤）改写为 `detectors.py` 的 D2；LLM 裁决与自动失效退役 |
| `mcp_server.py` | 骨架模板（FastMCP 结构、启动参数） |
| `db.py` | 退役；`index_db.py` 重写（4 张表） |
| `extractor.py`、`enhancer.py`、`webui/`、`config.py` 大部 | 退役，从运行路径移除（代码可留档） |
| `docs/01–05` | 保留为决策记录 |

净新代码估计 600–800 行（含测试），复用约 400 行。

---

## 十一、分阶段落地

> **实现状态（2026-09-14）**：P0–P2 已实现并提交（51 个测试通过；三通道基线 fts 6/10 → hybrid 9/10；HTTP 多用户回环测试通过）。P3（部署 + 约定块进 agent 系统提示）与 P4（两周实测）待执行。

| 阶段 | 内容 | 工作量 | 验收标准 |
|---|---|---|---|
| P0 | 仓库转型（旧模块移出运行路径）、骨架、**trigram 中文实测** | 0.5 天 | 用 10 条真实中文查询记录 fts/vector/hybrid 三通道召回基线 |
| P1 | `search/read/write/edit` + 同步索引 | 1–2 天 | 写入 < 300ms；评测集上 hybrid ≥ 单通道最优 |
| P2 | 守卫完善（force 两级确认）、`edit_section/move`、D1–D3、`audit` 自愈、碰撞标注 | 1 天 | 人造 5 组重复/矛盾样本全被拦截或标出，误报 ≤ 2 |
| P3 | 部署 yacmemo-server + 约定块进各 agent 系统提示 | 0.5 天 | 多机多客户端隔离运行一天无串数据 |
| P4 | 两周实测 | — | 7.4 全部指标产出；据数据调阈值 |

每阶段可独立回滚：P1 完成后系统已可用（无守卫），守卫是纯增量。

---

## 十二、风险与开放问题

| # | 风险 | 缓解 |
|---|---|---|
| 1 | trigram 对 2 字中文短查询落空 | 向量通道兜底；P0 实测后决定是否启用 jieba 影子列 |
| 2 | 模型不守约定、高频 force | force 是显式显眼动作，次数可数；超阈值则收紧：force 需人工确认（MCP 返回待确认标记） |
| 3 | observation 语法采纳率低 | D2 自动降级到 note 级向量比对（粒度粗但可用）；P4 统计采纳率 |
| 4 | 标题归一化误拦（确属不同的两个主题相似命名） | force 通道 + 拒绝事件日志驱动调阈值 |
| 5 | `[[链接]]` 改写只支持精确标题 | 不支持别名是已知简化，未改写引用在 audit 中报告 |
| 6 | D2 漏掉措辞距离远的逻辑矛盾 | 接受的残余风险，主模型读取时裁决；不做全量 LLM 扫描（第一版教训） |
| 7 | 双索引（FTS/LanceDB）与文件的一致性 | 先文件后索引的写序 + 索引可全量重建，最坏降级不损坏 |

---

## 十三、主题注册制与质量策展（2026-09-15 增补）

> 背景：历史记忆迁移后发现"纷繁而无主次"——所有笔记在系统里平权，冷启动失忆，同主题快照群靠人工偶然发现。解决方案是把"主次"从**检索排序的运气**变成**显式声明的结构**。

### 主题注册制（2026-09-16 目录化改造）

- **主题由用户显式声明**（"把 X 加入长期记忆"），agent 调用 `topic_register` 注册——工具调用即用户授权的凭证；agent 平时只能提案，不能自行注册；注册同时是**写入的前置条件**（6.1 主题硬拦截：未注册主题覆盖的路径一律拒写，force 不豁免）；
- **每个主题一个目录**：`abstract.md`（现状手册，agent 维护、就地更新）+ 主题内详细记忆的模块 md（agent 可按需增设）——目录即归属，取代注册表手工维护路径列表；
- TOPICS.md 为注册表与目录；**主题生命周期**：注册 → 活跃（context 注入摘要）→ **归档**（`archive_topic`，注册表加`状态: archived` 并改写卡路径，整个主题目录移入 archive/，检索仍可用、context 不再注入、不计游离）→ 注销（topic_unregister，仅移出注册表，笔记变游离走 D4 裁决）——全程无静默数据损失；
- **画像/偏好是记忆层功能，不是主题**：`PROFILE.md` 单文件分小节，agent 用 `get_user_preference` / `update_user_preference` 维护（元信息与领域知识分层：前者是"怎么和用户协作"，后者是"知道什么"）；`memory_context` 将 PROFILE 前置注入；
- **游离文件检测（D4）双保险**：写路径已硬拦截（工具面不可能制造游离），D4 转为兜底——管 Obsidian 手建、注销后遗等工具面之外的游离（免注册区：journal/、archive/、curator/；TOPICS.md/PROFILE.md 豁免）；
- 设计立场：**主次是被声明的，不是被算出来的**——不做重要度打分/衰减函数/自动摘要。

### curator 质量策展

- `yacmemo-curator` CLI + systemd timer（默认每周六 04:00）；LLM 用主模型端点（`[curator]` 配置节）；
- 流程：读注册表 + 主题卡 + 审计结果 → LLM 审查 → **提案报告笔记**（`curator/提案-<日期>.md`，状态"待裁决"）；同日重跑不新建文件，结果以"复审（HH:MM）"小节追加进当天报告（标题保持每日唯一，不触发 D1）；
- 审查维度：duplicate / outdated / stray / stale-card / merge / forget；
- **铁律：只提案，绝不执行**——这是 v1"自动失效不问人"教训的最终形态：维护者 LLM 回来了，但被剥夺了一切写权力（唯一例外：顺手按 `audit_retention_days`（默认 7 天）清理过期的 journal/audit/ 审计快照——处置在 audit_actions 表、完整历史在 git，快照文件只是近期工作集视图）；
- 批准的提案由 agent 或人工执行，执行后在报告笔记中留痕。

### 工具面（累计 17 个）

主题生命周期四件套 `topic_list` / `topic_register` / `topic_unregister` / `archive_topic` + 冷启动 `memory_context`，画像/偏好功能对 `get_user_preference` / `update_user_preference`，规格见 [02-mcp-tools.md](02-mcp-tools.md)。注销只移出注册表、不动笔记（注销后笔记成游离文件，由 D4 点名走裁决），归档保留检索可用性但退出注入——保证主题生命周期全程无静默数据损失。

## 十四、被否决的备选方案（决策记录）

| 方案 | 否决原因 |
|---|---|
| 继续用 OpenViking | 提取阻塞 90s/条；VLM 与主模型抢资源 |
| 第一版 yacmemo（三层） | 小模型自动失效静默错杀；拆分文件保护子系统复杂度倒挂；每次写入触发全量扫描 |
| Mem0 / Letta / Graphiti / Cognee / MemOS | 提取路径需要生成式 LLM / 图数据库 / 常驻服务栈，违反核心原则 2、5；详见 2026-09 调研记录 |
| Basic Memory 直接采用 | 锁版本风险；中文向量/FTS 弱；write_note 不可拦截，一致性到不了 API 强制层 |
| BM + yacmemo 外挂 | 外挂承担检索主力后 BM 残余价值缩水；双检索工具质量不一致；写路径仍不可守卫 |

## 十五、非目标与设计议题（2026-09-19 增补）

agent-memory-atlas 对本项目的独立评审（分析 commit 2440aa5，7 项机制 rubric）提供了外部
视角的对照：有些"缺失"是刻意取舍（非目标），有些是真实空白（设计议题）。逐项写明如下，
避免后续被当成遗漏反复提出。

### 非目标（刻意不做）

- **显式信任状态门控（trust state）**：不为笔记/观察行加"可采信 / 待验证"等离散状态，
  也不按信任过滤检索结果。矛盾以 ⚠ 两边呈现、判断交给读取时的主模型——
  "永不隐藏任何记忆"（§二）与信任门控相悖；
- **双时间轴有效性（bi-temporal）**：不追踪"事实为真的时间段 vs 系统记录时间"。
  单人场景下"现状变化就地更新 abstract + observation 行的即时性"已覆盖需求，
  为此给每条事实加元数据税不成比例；
- **检索作用域（scope）按构造保证，不加库内 scope key**：每用户独立 store（§九），
  隔离在挂载层完成，读路径无需过滤器。评审 rubric 的 "Scope enforced" 判 "—" 属
  定义口径差异（它只认"库内 scope key 作为读路径过滤"这一种实现形态）。

### 威胁模型说明

git 快照与 usage.db 都在部署机本地：**持有 memory 目录或服务器文件系统权限者可以改写
git 历史**。单人 homelab 场景（记忆主人 = 服务器管理员）这是可接受的取舍——本系统防的是
"agent 写错/删错"，不防"服务器管理员作恶"。多用户互不信任应使用不同 memory root
（每用户独立 store + 独立 git 仓库），而非指望 git 充当防篡改审计。

### 设计议题（0.2.x 待研，欢迎讨论）

- **按"值"键控的拒绝值墓碑**（评审 rubric 指出的真实空白）：用户明示"X 不对 / 别再记 X"
  时，把拒绝值登记为系统记录（键 = 规范化值的 hash，附原文、时间、来源）；此后
  `memory_write` / `memory_edit` 解析出的观察行命中墓碑值即拦截（force 不豁免），
  解除须用户明示（走 WebUI 处置面）。开放问题：键控粒度（精确值 / 行级 / 笔记级）、
  与既有标题键守卫的关系（标题近似已有守卫，值键补的是"换个标题重写同一错误值"）、
  墓碑只拦"值重现"，不拦无关的新笔记。
| fork BM 扩展 | 数万行 0.x 代码库的合并税 > 自拥有 1k 行 |