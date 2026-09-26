> [English](en/02-mcp-tools.md) | 简体中文

# MCP 工具规格（17 个）

> 适用传输：stdio（`yacmemo-mcp`）与 HTTP（`yacmemo-server`），工具面完全一致。
> 所有工具返回人类可读文本；错误以中文消息直接返回（不抛协议错误），agent 可读可自纠。

## 总则

- **路径语义**：所有 `path` 参数接受相对 memory_root 的路径或笔记标题（标题精确匹配，见 `memory_read` 的解析顺序）。
- **同步索引**：所有写工具在返回成功前完成文件写入 → hash → embedding → FTS/向量/冲突表更新，单次典型开销 < 300ms。返回成功即索引可用。
- **git 快照**：所有写/删除/主题操作在成功后自动产生一个 git commit（`{tool}: {path}` 格式），记忆仓库永远 git-clean；首次写入自动 `git init`（含 `.index/` 忽略与 repo-local 身份）；git 不可用时降级为只写不快照，绝不阻塞记忆功能。外部直接改文件（Obsidian/vim）的改动由 `memory_audit` 自愈时统一快照（`external:` 前缀）。commit 身份按 `[[users].git_user_name/email]` > `[memory].git_user_name/email]` > 默认 `<id>` / `<id>@yacmemo.com` 解析，仓库已有身份绝不覆盖；最近一次快照失败会显示在 `memory_audit` 输出的 `== git ==` 行（消灭静默降级）。
- **失败语义**：文件永远是第一位。embedding 失败时内容照常写入、FTS 照常更新，仅向量/D2 缺失（下次写入或 `memory_audit` 自愈补齐）。
- **守卫拒绝是正常返回**（不是错误）：agent 应读拒绝消息并改用建议的工具。

## 0. 身份（identity）与专属记忆

MCP 请求可携带 **identity token** 表明"哪个 agent 在哪台机器上"：

- HTTP：请求头 `Authorization: Bearer <device>_<agent>`（如 `r9000x_teleagent`；兼容 `X-Yacmemo-Token` 头）；
- stdio：环境变量 `YACMEMO_TOKEN=<device>_<agent>`。

token 是确定性拼接（`<device>_<agent>`，小写字母/数字/短横线，不含下划线），无需注册存储；WebUI「身份」页可校验名称并生成各客户端配置片段。层级模型：

| 层 | 路径 | 可见性 | 用途 |
|---|---|---|---|
| user 层 | `topics/`、`journal/`、`TOPICS.md`、`PROFILE.md` 等 | 所有 identity 共享 | 主题记忆、画像、流水账 |
| agent 层 | `agents/<agent>/shared/` 子树 | 同 agent 跨设备共享 | 角色纪律、必读 |
| identity 层 | `agents/<agent>/<device>/` 子树 | 仅本 identity | 本机环境、设备差异 |

- **专属隔离是服务端强制的**：读、检索、列表、写全链路过滤——任何 identity 只能看到 user 层 + 自己的 agent 层 + 自己的设备子树，其他 identity 的专属区不可见不可写；
- **scoped search**：`memory_search` 只返回 user 层 + 本 identity 专属区；
- **memory_context 自动注入**：携带 token 时追加 `agents/<agent>/shared/必读.md`（跨设备共享）与 `agents/<agent>/<device>/必读.md`（本机专属）两节——加上画像即会话必读三件套，注入即视为已读；未创建时给出写入模板，占位模板会在注入中持续提醒填写；
- **未携带 token 的旧配置照常可用** user 层，但 `agents/` 区不可见不可写（写入会被拦截并提示配置方式）；token 拼写错误按非法 token 拒绝并附约定说明；
- 写入约定：**必读只放指针与纪律，事实一律进 `topics/`** 与所有 agent 共享；`agents/<agent>/` 第一层只有 shared/ 与 <device>/ 两类子目录（平铺文件只读兼容，写入一律进两类子树）。引用其他层路径必须代入真实设备名（agents/teleagent/r9000x/必读.md），模板占位一律写尖括号形式（agents/<agent>/<device>/…），禁止留空段——agents/teleagent//必读.md 会被当成真实路径、检索必然失败。

## 1. memory_search

```
memory_search(query: str, limit: int = 10, kind: str = "hybrid") -> str
```

双通道检索 + Reciprocal Rank Fusion（k=60，只用名次不用分值）：

| 通道 | 机制 | 擅长 |
|---|---|---|
| fts | SQLite FTS5 trigram，BM25 排序 | 关键词式查询（子串字面匹配，token ≥ 3 字符） |
| vector | Qwen3-Embedding 最近笔记（cosine/L2） | 自然语句、同义改写 |

- `kind="fts"` / `"vector"` 强制单通道（评测用）；默认 hybrid。
- **查询措辞建议**：给 FTS 通道喂关键词（"端口 9721"、"restic 备份"），自然语句交给向量通道。混合查询（"yacmemo 端口"）两通道同时工作。
- **短查询回退**：短于 3 字的查询 trigram 无法命中，自动走 LIKE 子串扫描兜底；LIKE 也未命中时输出提示"请换更长的关键词"。
- **降级提示**：向量通道故障时输出 `⚠ 向量通道不可用（原因），本次结果仅 FTS`——"未找到相关笔记"不再被当成权威结论（2026-09-18 端点故障实测的静默降级问题）。
- **已结案提案隐去**（2026-09-25 增补）：curator/ 提案报告的全部条目都执行完成或忽略后，文件头部自动打「> 状态：已结案」标记，检索结果默认不再返回（输出提示"已隐去 N 条已结案提案"）；显式 `memory_read` 仍可读——那是明确查阅。不要再去执行已结案提案里的条目。

返回格式：

```
1. yacmemo部署配置 (score 0.0316, fts+vector)
   path: projects/yacmemo部署配置.md
   ⚠ 与 [[yacmemo部署记录0910]] 疑似重复（score 0.91）— 建议读两篇后用 memory_edit 合并。对方内容: 服务端口为 8080
```

**遇到 ⚠ 标注的处置约定**：先 `memory_read` 两篇 → `memory_edit` 合并 → 再回答用户。合并后冲突对自动消失（内容 hash 变化触发重算）。

## 2. memory_read

```
memory_read(path_or_title: str) -> str
```

解析顺序：① 相对路径精确存在 → ② 笔记标题精确匹配 → ③ 补 `.md` 后缀再试路径。

返回 = `[正文开始 | path | 锚点提示]` + **逐字正文** + `[正文结束]` + 相关笔记：

- 正文块内的文字是文件原文——`memory_edit` 的 `old_string` 必须从中**逐字复制**，勿凭记忆重打；
- `[正文结束]` 之后的"相关笔记"是**工具附加信息，非文件内容**（不再用 `##` 标题语法，避免被误认为笔记小节）：
  - `[[wiki-link]]` 目标存在的：列出标题 + 对方首条 observation（`via: link`）；
  - 目标不存在的：标注"目标不存在"（agent 可顺手创建或清理）；
  - 语义近邻 top-2（`via: vector`，需要 embedding 端点）。

## 3. memory_write

```
memory_write(title: str, content: str, force: bool = False,
             force_confirm: bool = False) -> str
```

新建笔记。`title` 可含目录前缀，**主题目录内写作 `topics/<主题>/笔记名`**（缺 `topics/` 前缀会被主题硬拦截，拦截消息会给出修正后的 title）。目录只是归档，**笔记的标题是去掉目录后的主题名**。文件名对非法字符（`\ / : * ? " < > |`）做替换清洗。

**主题硬拦截**（2026-09-17 增补，force 不豁免）：写入路径必须被某个注册主题覆盖——注册主题卡/相关笔记所在目录之下（每主题一目录，目录即归属），否则拒绝：

```
写入被拦截: 女儿AI陪伴老师/abstract.md 不属于任何注册主题（主题注册制硬约束，force 不豁免）。
⚠ 疑似路径前缀/目录名不对：主题「女儿AI陪伴老师」已注册，目录 topics/女儿AI陪伴老师/。
  改用 title="topics/女儿AI陪伴老师/abstract" 即可写入；abstract 是摘要卡，
  详细内容建议写成 topics/女儿AI陪伴老师/<笔记名>。
- 新主题：先征得用户同意后 topic_register 注册（会在 topics/<主题>/abstract.md 建卡），
  之后把笔记写入 topics/<主题>/ 目录下；
- 已有主题：写入该主题目录下的模块笔记，如 topics/<主题>/笔记名.md；
  abstract 是摘要卡（保持一句话现状），详细内容请写成模块笔记；
- journal/、archive/、curator/、agents/ 免注册区不受限（agents/ 另有 identity 专属守卫）。
当前活跃主题（共 9 个）: 《……》
```

**近失诊断**（2026-09-19 增补）：拦截前把写入路径首段与注册主题名做精确/模糊比对，命中就在错误里直接给可重试的 title——缺 `topics/` 前缀、目录名拼错这类错误一轮自纠，不用猜。活跃主题列表带总数，**被命中的主题无论排位必显示**（TeleAgent 实测中列表静默截断到 8 个恰好切掉刚注册的主题，agent 误判"注册表未同步"白烧一个推理块）。

系统文件（`TOPICS.md`/`PROFILE.md`）不允许经此工具创建/覆盖——分别走 `topic_register` / `update_user_preference`。拦截事件记入 `guard_events`（kind=`uncovered`），与 refused/forced 一样进守卫统计。

**近重名守卫**：归一化（小写、去标点空白、剥离日期串/`-2`/`(新)`/`更新`/`v3` 等后缀）后与所有既有标题做模糊比对，相似度 ≥ `title_similarity_threshold`（默认 0.85）即拒绝：

```
已存在近似标题笔记，拒绝新建：
  - [[yacmemo部署配置]] (projects/yacmemo部署配置.md, 相似度 0.93)
更新内容请用 memory_edit / memory_edit_section；确属新主题请 memory_write(force=true)。
```

**force 两级确认**（24 小时滚动窗口内 forced 事件 ≥ `force_confirm_threshold`，默认 3）：

- 裸 `force=true` 被拒，返回"需要人工确认"并列出候选笔记；
- 确认确属新主题后，`force=true, force_confirm=true` 放行；
- 全程记入 `guard_events`——refused / forced 次数即违约率指标。

**journal 豁免**：`journal/` 目录下的写入不做重名拦截（时间线流水天然按日期命名），免注册区同样不受主题硬拦截约束。

## 4. memory_edit

```
memory_edit(path: str, old_string: str, new_string: str) -> str
```

唯一文本锚点替换（与 Claude Code 的 Edit 语义一致）：

- `old_string` 未找到 → 拒绝，且附**确定性诊断**（拒绝消息是可执行的下一步指令）：
  - 锚点里混有 `memory_read` 附加信息标记（`相关笔记` / `(vector)` / `[正文开始` 等）→ 点破"这些不是文件内容"；
  - 仅空白不一致（空行数量/行尾空格）→ 交还该位置的逐字原文（优先唯一单行锚点），复制即可一轮恢复；
  - 实质差异 → 给出最接近的原文行与相似度，提示先 `memory_read` 核对；
- 命中多处 → 拒绝并列出近似行号，要求扩展锚点上下文；
- 恰好一处 → 替换、写盘、全量重索引该笔记（FTS/向量/冲突重算）。

匹配语义始终是**严格逐字唯一**，诊断只改变拒绝消息的信息量，不做模糊替换。

**这是更新事实的正确方式**——事实变更永远就地编辑，不新建笔记。

**合并清除计数**（2026-09-19 增补）：编辑消解语义撞车后（重索引重算不再命中旧冲突对），成功返回追加"（自动清除过期冲突对 N 对）"——看到它即说明这次编辑合并掉了 D2 重复；计数同样出现在审计概览/快照与 `memory_audit` 输出（见 §7）。

## 5. memory_edit_section

```
memory_edit_section(path: str, heading: str, new_content: str) -> str
```

按 `##` 及更深层标题替换整个小节：保留标题行，替换体到下一个同级/更高级标题或文末。

- 标题不存在 → 拒绝并列出**现有全部小节名**；
- 同名标题多处命中 → 拒绝并列出行号；
- 一级标题（`# 笔记标题`）不可用——那是笔记本身，请用 `memory_edit`。

适合重写一整段（如"## 部署步骤"全换），比多次 `memory_edit` 高效。

## 6. memory_move

```
memory_move(path: str, new_path: str) -> str
```

移动文件到新相对路径（自动补 `.md`）。notes/FTS/向量全部随路径更新（embedding 走 vec_cache，零 API 调用）。`[[链接]]` 按标题解析，移动不改标题，因此**不需要改写链接**。目标已存在则拒绝。

## 7. memory_audit

```
memory_audit() -> str
```

全量一致性审计，兼**自愈**：

1. **外部修改自愈**：磁盘 hash ≠ `notes.content_hash` 的笔记自动重建索引（外部编辑在 Obsidian/vim 里做的也能对齐）；
2. **外部删除清理**：文件已消失的笔记清理全部索引并列入报告；
3. D1 标题重复全量两两扫描；
4. open 状态的 D2 语义撞车清单（含双方文本与分数）；
5. D3 悬空 `[[链接]]`；
6. D5 悬空主题卡（注册表 `卡:` 指向不存在的 abstract，restructure/手工编辑 TOPICS.md 的遗留）；
7. D4 游离文件（免注册区之外、不属于任何注册主题的散文件——agent 据此提示用户归位）；
8. **缺向量笔记点名 + 自愈重试**：embedding 端点故障期间写入的笔记（vector_ok=0）审计时重试 embedding，成功即自愈、仍失败保持点名；
9. 守卫统计（refused / forced / uncovered 次数）；全空处置行自动清理；
10. **自动清除过期冲突对统计**（2026-09-19 增补）：笔记删除或重算后不再命中的 D2 旧对，概览行 + `== 自动清除过期冲突对 ==` 行 + 审计快照留痕。

修复建议都内联在输出里。发现即展示，**系统不做任何自动删除或失效**。自愈涉及的外部改动统一以 `external: self-healed N note(s)` 快照入库，保持 git-clean 不变式（输出末尾附 git 快照状态行与当次审计快照路径 `journal/audit/<日期>.md`——每日一份、同日复审追加；过期快照由 curator 按 `audit_retention_days` 清理）。

2026-09-25 增补输出：`== 执行进度 ==`（agent 经 `memory_audit_update` 汇报的执行中问题与最新动态）、`== 复审通过 ==`（已执行且本轮不再报告的问题——审计自动确认，无需人工；仅 D 类）与 `== 提案结案补记 ==`（文件已标已结案的提案，为缺执行事件的条目补记 executed——agent 手工打标与结构化汇报两条路都算数）。

## 7.5 memory_audit_update

```
memory_audit_update(issue_id: str, event: str, note: str = "") -> str
```

汇报审计问题的**执行进度**（契约 0.3.3 新增）。判断与执行分离的 agent 侧入口：WebUI「复制执行指令」派下的问题、或 `memory_audit` 自己发现的问题，执行修复时向 server 留痕。

- `issue_id`：审计报告/执行指令里的 id（`D3:<path>|<link>`、`P:<file>:<index>` 等）；
- `event`：`executing` 开始执行 / `progress` 过程汇报 / `executed` 执行完成 / `blocked` 受阻需人工；
- `note`：一句话说明（做了什么/卡在哪）；
- identity 自动记录（哪个 agent 哪台设备汇报的）；时间线只追加不改写，WebUI 审计页逐条展示；
- **复审不归 agent 管**：完成后重跑 `memory_audit`，问题不再被报告即为复审通过（系统自动追加封口事件）；不要声称"已验证"，也不要代替人做忽略。

## 8. memory_list

```
memory_list(path: str = "", sort: str = "name") -> str
```

列出 memory_root（或子目录）下全部 `.md`，`sort="mtime"` 时最近变更优先。`.index/` 永不列出。

## Agent 选择工具的决策树

```
要记一个新主题？        → memory_search 查重 → memory_write（被拒就转 edit）
要更新已有事实？        → memory_edit（锚点唯一）/ memory_edit_section（整段重写）
要找"某件事记在哪"？    → memory_search（关键词式 query）
要梳理一个主题全貌？    → memory_read（看相关笔记链路）
定期体检？              → memory_audit
执行审计问题修复？      → memory_audit_update 汇报进度（executing → progress → executed）
```


## 9. topic_list

```
topic_list() -> str
```

列出当前注册的全部长期记忆主题（标题、一句话现状、主题卡路径、标签）。免注册区（journal/、archive/、curator/）单列说明。

- `tag` 参数可按标签过滤（如 topic_list(tag="工作")）；
- 标签存于注册表 `- 标签:` 行（0-多个，视角归类用，不是状态）。

## 10. topic_register

```
topic_register(title: str, description: str = "", related: str = "", tags: str = "") -> str
```

注册新的长期记忆主题：追加到 `TOPICS.md` 注册表，并创建 `topics/<主题>/abstract.md`（或用既有笔记充当 abstract）。

- `tags` 参数：注册时即可打标签（逗号分隔，可选；优先复用已有标签）；
- **调用门槛**：仅在用户明确要求时调用（"把 X 加入长期记忆"）——这条写进约定块，注册行为本身即用户授权的凭证；
- **注册是写入的前置条件**：主题硬拦截（见 §3）下，未注册主题覆盖的路径一律拒写——`topic_register` 是新主题的唯一授权门；
- 重复主题名拒绝（提示直接编辑既有 abstract）；
- 注册后 abstract 与注册表立即入索引；主题目录内 agent 可按模块自由增设详细 md（目录即归属）；
- **成功返回含可复制的写入模板**（2026-09-19 增补），例如：

```
已注册主题「女儿AI陪伴老师」，abstract: topics/女儿AI陪伴老师/abstract.md。
后续写入约定：
- 详细笔记：memory_write(title="topics/女儿AI陪伴老师/<笔记名>", ...) ——必须带目录前缀（如 "topics/女儿AI陪伴老师/xxx"），缺前缀会被主题硬拦截；
- abstract 是摘要卡，保持一句话现状：现状变化用 memory_edit 就地更新，不要把长文塞进 abstract。
```

## 10.5 topic_tag

```
topic_tag(title: str, add: str = "", remove: str = "") -> str
```

为主题增删标签（契约 0.3.8）。标签是轻量可逆元数据：用户要求打标、或执行标签类 curator 提案时使用。

- `add` / `remove` 均为逗号分隔的标签列表（支持中文逗号），可同时使用；
- 响应自带**全库标签清单**——打标签优先复用已有标签，避免同义词蔓延；
- 用户没让就不主动批量打标；标签不影响 memory_search 的内容检索。

## 11. topic_unregister

```
topic_unregister(title: str) -> str
```

注销长期记忆主题：把该主题块从 `TOPICS.md` 移除（其余内容逐字保留）。

- **调用门槛与注册相同**：仅在用户明确要求时调用（"X 不用长期记录了"）；
- **只动注册表，笔记文件一律不动**——注销后相关笔记成为游离文件（D4 点名），工具返回消息会引导 agent 与用户确认后归位 `archive/` 或删除；
- 未知主题名拒绝并列出现有主题。

## 12. memory_context

```
memory_context() -> str
```

**每次会话开始先调用**。返回核心记忆上下文 = 接入契约版本头 + `TOPICS.md` 注册表全文 + 各主题卡摘要头（前 12 行）+ identity 专属必读（携带 token 时自动注入 `agents/<agent>/必读.md` 与 `agents/<agent>/<device>/必读.md`，未创建时给写入模板；见 §0）。解决冷启动失忆：agent 不必"想到去搜什么"，主题体系直接在场。

**版本头**（2026-09-19 增补）：形如 `[yacmemo 接入契约 v0.1.3——与你本地记录的版本不一致时，调用 integration_check(onboarded_version="<你的版本>") 自主更新]`。agent 把接入时依据的契约版本记在本地接入提示词里，每次会话开始比对，落后即自主更新（见 §17）。

## 13. memory_delete

```
memory_delete(path: str) -> str
```

删除笔记（移除文件 + 全部索引行 + 相关 collisions/向量），删除动作自动产生 `delete:` git 快照，历史可恢复。

- **调用门槛**：仅在用户明确要求时调用（"删掉 X"/"X 不用记了"）——与 topic_register 同级的人工确认语义；
- 未知路径/标题拒绝；
- 系统（store）自身永远不会主动删除——这是 v1"自动失效错杀"教训的边界：删除永远是被指令的。

## 14. archive_topic

```
archive_topic(title: str) -> str
```

归档主题（生命周期：注册 → 活跃 → 归档 → 注销）：**整个主题目录**移入 `archive/<主题>/`（目录即归属——主题内全部模块笔记随 abstract 一起走，不留游离），注册表块内 `- 状态: archived` 且 `- 卡:` 路径同步改写。

- **调用门槛**：仅在用户明确要求时调用（"X 归档吧"/"这个项目翻篇了"）；
- 与注销的区别：**归档不丢检索**——主题笔记仍在索引里可搜，仅 memory_context 不再注入 abstract、topic_list 分入已归档组；archive/ 是免注册区，不计游离；
- 可逆性：git 历史可回退，手工删除注册表状态行即恢复活跃。

## 15. get_user_preference

```
get_user_preference(section: str = "") -> str
```

读取用户画像与偏好（`PROFILE.md`，记忆层功能文件而非主题记忆）。

- `section` 为空返回全文；指定小节返回该小节正文；
- 文件不存在返回引导文案；小节不存在报错并提示用 update 创建；
- `memory_context` 会话开始时已将 PROFILE 前置注入，多数场景无需单独调用。

## 16. update_user_preference

```
update_user_preference(section: str, content: str) -> str
```

创建或替换画像/偏好的一个小节（agent 加以维护）：

- 小节存在 → 整段替换（edit_section 语义，标题保留）；
- 小节不存在 → 文末追加新小节；文件不存在 → 连同 `# 用户画像与偏好` 头一起创建；
- 写提炼后的结论（"- [类别] 内容" 语法），不贴对话原文；
- 每次更新自动 git 快照，可回溯。

## 17. integration_check

```
integration_check(onboarded_version: str = "") -> str
```

**Agent 接入契约版本核对**（2026-09-19 新增，"教 AI 自我更新"机制）：

```
integration_check(onboarded_version="0.1.2")
→ yacmemo 接入契约当前版本: 0.1.3
  你声明的版本: 0.1.2——有更新，请据此自主更新本地接入提示词，并记录本次核对到的新版本。

  【0.1.3】
  - 拦截错误自带近失诊断：……

  ## 写入约定速览
  - 长期记忆只写注册主题目录内：topics/<主题>/<笔记名> ……
```

工作机制（服务器不推送，agent 自主拉取）：

1. **版本声明**：agent 把接入时依据的契约版本记在本地接入提示词/USER.md 里（如 `yacmemo 接入契约版本: 0.1.3`）；
2. **发现**：`memory_context` 头部随身携带当前契约版本，每次会话开始自然比对；
3. **更新**：版本落后时调用本工具，返回**增量变更**（逐版本条目）+ **写入约定速览全文**——agent 据此刷新本地提示词并记录新版本，无需人工介入；
4. 留空 `onboarded_version` 返回当前版本 + 全部记录在案的变更；声明版本比服务端还新则提示"可能连到了旧实例"；
5. **契约版本独立于包版本**：只在 agent 可感知行为变化（工具语义、返回文案、写入约定）时前进，数据源在 `yacmemo/agent_changes.py`——改约定必须同步追加 `AGENT_CHANGELOG` 条目并前进版本号（有测试守护）。

## Agent 决策树（更新）

```
会话开始              → memory_context（契约版本头 + 画像/偏好前置 + 主题体系）
契约版本落后          → integration_check(onboarded_version=...) → 自主更新本地提示词
用户要新增长期记忆主题 → topic_register（仅用户明示时）→ 按返回模板写 topics/<主题>/ 目录内
用户不再长期记录某主题 → topic_unregister（仅用户明示）→ 引导归位/清理
用户说某项目翻篇了    → archive_topic（仅用户明示；检索保留、context 退出）
用户要删某条记忆      → memory_delete（仅用户明示；git 可恢复）
用户的画像/偏好变化了 → update_user_preference（按小节替换/追加）
要记一个新主题？      → memory_search 查重 → memory_write（被拒转 edit）
要更新已有事实？      → memory_edit / memory_edit_section
要找"某件事记在哪"？  → memory_search（关键词式 query）
要梳理一个主题全貌？  → memory_read / abstract
定期体检？            → memory_audit（+ WebUI 审计页）
不知道有哪些主题？    → topic_list
```