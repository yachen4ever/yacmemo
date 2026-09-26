> [English](README.en.md) | 简体中文

# yacmemo

带 API 级一致性守卫的个人记忆层 —— markdown 为本、全本地、agent 无关。

## 它是什么

yacmemo 让**你所有电脑上的所有 AI agent** 共享同一份长期记忆，且这份记忆是自洽的：

- **markdown 是唯一真相**——笔记就是你服务器上的普通文件：人可读、可 git、可 Obsidian。SQLite + LanceDB 只是派生索引，删掉随时可重建。
- **一个服务，所有设备**——唯一的服务进程跑在数据所在的机器上（streamable HTTP）。Claude Code、Codex、Cursor、自研 runtime……任何 MCP 客户端只需添加一个 URL，客户端零安装、零进程。
- **记忆读写主路径零生成式 LLM**——常驻的模型调用只有 0.6B 的 embedding（~50ms）。结构靠约定产生，一致性靠确定性 API 守卫强制，模糊判断交给你的主模型在读取时完成；唯一的生成式环节是 curator 深度审查，而它离线运行、只读、只提案、绝不执行——落地永远走人工派发 + 写路径守卫。
- **主题注册制**——长期记忆的主题由你显式声明（"把 X 加入长期记忆"），注册表 + 主题卡让主次分明；**写入被硬性限定在已注册主题内**（未注册主题的路径一律拒写，force 不豁免——先注册、后写入）；agent 会话开始先回顾记忆体系，冷启动不再失忆；
- **curator 质量策展**——可配置的 LLM 定期审查记忆质量，产出**提案报告**：只提案、绝不自动执行；**判断/执行/验证三权分立**——人在 WebUI 只做判断（忽略误报 / 复制执行指令派给任意 agent），agent 执行并经 `memory_audit_update` 汇报过程，复审由审计自动确认（已执行且下轮不再报告即通过）；提案全部条目收口后自动**结案**，不再出现在 agent 的检索结果里；
- **WebUI 控制台**——浏览器打开 `/ui/`（Vue 3 + Naive UI，`scripts/build_webui.sh` 构建）：主题树内浏览/编辑笔记（**存储中任何 markdown 皆可见**：注册主题、已归档、免注册区、游离文件、系统文件）、在线搜索、**审计双模式**（确定性规则 + curator LLM 深度审查提案，工作流条 + 状态标签 + 执行时间线 + 状态筛选）、**审计历史快照与判断/执行记录**（journal/audit/ + audit_actions/audit_exec_events，可回看、可追溯）、画像/偏好编辑、使用留痕与健康总览、**config.toml 在线配置**；
- **一致性是被强制的，不是被希望的**——`memory_write` 先做主题覆盖拦截再做近似重复标题拒绝，`memory_edit` 强制锚点唯一，`memory_move`/`save` 目标同受约束，矛盾在检索结果里带 ⚠ 标注并存呈现，系统永不静默删除或隐藏任何记忆。
- **记忆是被版本管理的**——每次写入/编辑/移动/删除自动产生一个 git commit（`write: x.md`），记忆仓库永远 git-clean；删错可恢复，历史可回溯，agent 无需文件系统权限。

## 🤖 Agent 接入指引（AI 专用）

> **你是 AI agent 吗？请先读 [docs/09-agent-onboarding.md](docs/09-agent-onboarding.md) 并执行接入自检**：确认你是否已配置 yacmemo MCP——未接入则引导用户完成配置，已接入则把记忆逻辑织入工作流并向用户展示接入状态。桌面 agent 的本地文件（USER.md/MEMORY.md）维护约定见 [docs/08-agent-config.md](docs/08-agent-config.md)。

## 架构

```
你的电脑们（任意 MCP agent）
   │  各端只加一个 URL，什么都不用装：
   │  http://debsvc.local:9721/yachen/mcp
   ▼
yacmemo-server（单进程，streamable HTTP，无状态会话）
   ├── /yachen/mcp → Store(root=.../yachen/memory)
   └── /user2/mcp   → Store(root=.../user2/memory)
         store.py      CRUD + 写路径守卫 + 同步索引 + 主题注册表 + git 快照
         search.py     FTS5 trigram + 向量，RRF 融合
         detectors.py  确定性 D1/D3 检测
         index_db.py   SQLite：元数据/FTS/冲突/守卫事件
         vector.py     LanceDB：笔记 + observation 向量
         embedding.py  embedding 调用（0.6B，~50ms）
    ├── /ui/ → WebUI 控制台（Vue 3 构建：主题/搜索/审计/画像/设置）
   ▼
yacmemo-curator（systemd timer，每周）→ 质量提案报告，只提案不执行
   ▼
memory_root（git 仓库，每次变更自动 commit，永远 git-clean）
  ├── TOPICS.md                  主题注册表（活跃/已归档）
  ├── PROFILE.md                 画像与偏好（功能层，context 前置注入）
  ├── topics/<主题>/              abstract.md + agent 增设的模块 md
  ├── archive/ journal/ curator/  免注册区（归档主题 / 流水 / 提案）
  ├── agents/<agent>/shared/      identity 专属记忆：agent 层跨设备共享 + <device>/
  │                               本机专属；token = <device>_<agent>，互相不可见
  └── .index/                    派生索引（可重建，不进 git）
```

## 快速开始

### 服务端（数据所在机器）

```bash
git clone <your-repo> yacmemo && cd yacmemo
uv sync
cp config.example.toml config.toml   # 填 embedding 端点与各用户 root
uv run yacmemo-server --config config.toml
curl http://127.0.0.1:9721/health    # → {"status":"ok","users":["user2","yachen"]}
```

浏览器打开 `http://debsvc.local:9721/ui/` 就是自带的管理控制台（主题 / 搜索 / 审计 / 画像 / 设置，侧边栏底部展示版本号与 commit），详见 [docs/07-webui.md](docs/07-webui.md)。前端需先构建：本机一条命令 `scripts/deploy_webui.sh`（构建 + scp 到 debsvc，需 Node 18+），或分步 `scripts/build_webui.sh` 后手动 scp（见部署文档）。

### 客户端（你的每台电脑、每个 agent）

```
http://debsvc.local:9721/yachen/mcp
http://debsvc.local:9721/user2/mcp
```

```bash
# Claude Code
claude mcp add --transport http yacmemo http://debsvc.local:9721/yachen/mcp
# Codex CLI
codex mcp add yacmemo --url http://debsvc.local:9721/yachen/mcp
```

同机 agent 也可用 stdio：`uv run yacmemo-mcp --root /path/to/memory`。

**第一次用？**请先读 [用户使用手册](docs/00-user-guide.md)——上手、日常用法、常见问题都在里面。

## MCP 工具（19 个）

| 工具 | 用途 |
|---|---|
| `memory_search` | 混合检索（FTS trigram + 向量，RRF 融合）；疑似重复/矛盾内联 ⚠ 标注；向量通道故障或短查询未命中时附提示（<3 字查询走 LIKE 回退） |
| `memory_read` | 笔记全文（[正文开始/结束] 块内逐字原文）+ 相关笔记（工具附加信息，wiki-links + 语义近邻） |
| `memory_write` | 新建笔记（**带 `topics/` 前缀写 `topics/<主题>/笔记名`**）；**未注册主题路径直接拒绝**，拦截消息自带近失诊断（缺前缀/拼错目录会给出可重试的 title）+ 近似重复标题拒绝（force 需两级确认） |
| `memory_edit` | 就地更新，文本锚点必须唯一；未命中时附可自纠诊断（点破锚点混入附加信息/空白差异还原文/最接近行）；消解语义撞车时返回自动清除的冲突对计数 |
| `memory_edit_section` | 按小节整段替换 |
| `memory_move` | 移动文件，索引跟随；目标路径同样受主题注册制约束 |
| `memory_delete` | 删除笔记（**仅用户明确要求时**，git 历史可恢复） |
| `memory_audit` | 自愈式一致性审计（外部改动/删除自愈、D1–D5 一致性问题、缺向量笔记点名+自愈重试、守卫统计、过期冲突对清除计数、审计快照路径）；输出含「执行进度」与「复审通过」节 |
| `memory_audit_update` | 执行审计问题修复时向 server 汇报进度（executing/progress/executed/blocked），identity 自动入时间线；复审由审计自动确认 |
| `memory_list` | 目录树 / 最近变更 |
| `memory_context` | **会话开始先调**：返回接入契约版本头 + 主题注册表 + 各主题卡摘要头（冷启动回顾） |
| `topic_list` | 列出长期记忆主题（活跃/已归档分组，含标签；`tag` 参数按标签过滤） |
| `topic_tag` | 为主题增删标签（轻量可逆元数据；响应带全库标签清单引导复用，避免同义词蔓延） |
| `topic_register` | 注册新主题（**仅在用户明确要求时调用**，如"把 X 加入长期记忆"），创建 topics/<主题>/abstract.md；成功返回可复制的写入模板 |
| `topic_unregister` | 注销主题（**仅用户明示**，仅移出注册表，笔记不动，游离后裁决） |
| `archive_topic` | 归档主题（**仅用户明示**）：整个主题目录移入 archive/，检索保留、context 退出 |
| `get_user_preference` | 读画像/偏好（PROFILE.md 功能层，全文或指定小节） |
| `update_user_preference` | 创建/替换画像/偏好的一个小节（agent 维护） |
| `integration_check` | **Agent 接入契约版本核对**：agent 汇报本地记录的契约版本，落后时返回增量变更与写入约定速览——agent 据此自主更新本地提示词（docs/09 §四） |

完整规格：[docs/02-mcp-tools.md](docs/02-mcp-tools.md)；使用约定（贴进 agent 系统提示）：[docs/01-architecture.md](docs/01-architecture.md) 第八节。

## 文档

| 文档 | 内容 |
|---|---|
| [00-user-guide.md](docs/00-user-guide.md) | **用户使用手册（从这里开始）** |
| [01-architecture.md](docs/01-architecture.md) | 设计、决策记录、原则 |
| [02-mcp-tools.md](docs/02-mcp-tools.md) | 工具规格 |
| [03-storage-and-search.md](docs/03-storage-and-search.md) | 文件格式、索引、混合检索、自愈 |
| [04-consistency.md](docs/04-consistency.md) | 三层防线、force 阶梯、指标 |
| [05-deployment.md](docs/05-deployment.md) | systemd、客户端配置、备份、安全 |
| [06-evaluation.md](docs/06-evaluation.md) | 检索基线与复测方法 |
| [07-webui.md](docs/07-webui.md) | WebUI 控制台：页面与 API 参考 |
| [08-agent-config.md](docs/08-agent-config.md) | **各 agent 记忆接入与本地 USER.md/MEMORY.md 配置维护** |
| [09-agent-onboarding.md](docs/09-agent-onboarding.md) | **Agent 接入自检与记忆逻辑织入指引（AI 读这篇）** |

v1（三层提取架构）冻结在 [`legacy/`](legacy/)，仅作决策记录。

## 技术栈

Python 3.11+ · mcp SDK（FastMCP）· SQLite（FTS5 trigram，WAL）· LanceDB · Qwen3-Embedding-0.6B（任意 OpenAI 兼容端点）· rapidfuzz；WebUI 前端 Vue 3 + Naive UI + Vite。服务端单进程；无队列、无图数据库；读写主路径零生成式 LLM（curator 深度审查为唯一生成式环节：离线、只读、只提案不执行）。

## License

MIT