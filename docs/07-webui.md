> [English](en/07-webui.md) | 简体中文

# WebUI 控制台

> 服务端自带，浏览器打开 `http://<host>:9721/ui/` 即用。与 MCP 同进程同端口，无独立部署。
> 前端为 Vue 3 + Naive UI 工程（`frontend/`），需构建：`scripts/build_webui.sh` → 产物落 `yacmemo/webui/dist/`（`app.py` 直接 serve）。**未构建时服务照常启动**，`/ui/` 返回 503 构建指引，MCP/API 不受影响。

## 一、总体说明

- **界面语言**：中文 / English 头部切换器（记忆在 localStorage），naive-ui 组件语言同步联动；后端返回的业务消息仍为中文（服务端消息目录暂未国际化）；

- WebUI 与 MCP 端点共享同一 Starlette 应用和同一批用户上下文（Store/Searcher/IndexDB 实例）——**网页上看到和改到的就是 agent 在用的那份数据**，没有第二条数据路径；
- 路由顺序：`/api/*`、`/ui/*` 先于用户 MCP 挂载注册；配置层同时把 `api`/`ui`/`health` 设为用户 id 保留字，杜绝遮蔽；
- 错误约定：业务性拒绝（如守卫拦截）返回 HTTP 200 + `{"ok": false, "error": "..."}`；未知用户返回 404；
- 无独立鉴权，遵循"内网自用"信任边界（见 [05-deployment.md](05-deployment.md) 安全边界）。

## 二、页面导览

顶部选择器切换 memory 用户（config.toml 里的 `[[users]]`），左侧菜单七页：**仪表盘（默认落地） / 主题 / 搜索 / 审计 / 画像 / 身份 / 设置**。

### 2.1 主题浏览

左侧**主题树**——存储里的任何 markdown 都可见、无重复展示：

- **活跃主题**：每主题一个目录（`abstract.md` 与 agent 增设的模块 md）逐层展开；
- **已归档**：展开 `archive/<主题>/` 目录下的全部文件；
- **免注册区**：journal / curator 两个系统目录；
- **游离文件**：与后端 D4 同口径点名（免注册区 + 系统文件 + 注册覆盖之外）——主题硬拦截下工具面不会产生新游离，这里出现的只会是 Obsidian 手建或注销后遗，让 agent 归位或删除；
- **系统文件**：TOPICS.md（注册表）与 PROFILE.md（画像）。

右侧选中即看，合并了旧"笔记"页的全部能力：

- **查看**：正文默认 markdown 渲染，`编辑` 切到源码 textarea，`预览` 切回渲染；
- **编辑**：`保存` 走 `store.save`——整篇覆盖，标题跟随首行 `# 标题`，索引同步重建（FTS/向量/冲突重算）；
- **删除**：需 confirm 确认；删除文件 + 清理全部索引。git 里仍可找回。abstract.md 不可从 UI 删除；
- **单篇归档**：选中活跃主题内的模块笔记可直接「归档」（移入 archive/<主题名>/，WebUI 树「单篇归档」分组可见、检索保留），归档笔记可「取消归档」移回主题；
- **主题标签**：树工具栏可按标签过滤主题，主题节点带标签后缀（〔工作·开发〕样式）；「标签管理」弹层支持标签重命名/删除（全库批量改写注册表）与按主题增删标签（输入时自动补全已有标签）；
- **新建笔记无 UI 入口**：新建走 MCP `memory_write`（守卫更严，防重复标题），或 Obsidian 手建后由审计自愈入索引。

### 2.2 搜索

手动验证检索效果的页面。`hybrid`（默认）/`fts`/`vector` 三通道切换，结果含得分、通道来源与 ⚠ 撞车标注；向量通道故障或短查询未命中时页顶显示提示条。使用建议同 MCP：关键词式查询走 fts 更稳，自然语句靠 vector。

### 2.3 审计

审计页为**双 Tab** 结构，两种审计引擎各占一个完整工作流。页面的角色分工是设计核心：**人只做判断（忽略误报 / 把问题派给 agent），agent 只做执行（经 `memory_audit_update` 汇报过程），系统只做验证（复审由审计自动确认）**——WebUI 是判断台与观测板，不是执行器。

**Tab 1「确定性审计」**——快速、零 LLM、含自愈，等价 `memory_audit`：

- 顶部「立即审计」；页面加载即显示最近一次审计结果（内存缓存挂在 Store 上，与 MCP memory_audit 互通；服务重启后需重新审计；快照被删除——手动或 curator 过期清理——时缓存联动清除，页面对缺失的快照文件降级提示而非报错）；
- **工作流条**：待处理 → 执行中 → 已执行待复审 → 复审通过，各阶段实时计数，一眼看出每类问题卡在谁手里；右上角按状态筛选（只列出有内容的项）；
- **问题列表与提案页同一套两级语言**：一行一个问题——状态标签（待处理·等你决定 / 执行中·agent 正在做 / 已执行·待复审 / 受阻·需要你介入 / 复发·重新处理）+ 问题类型 + 一句话描述，行内直接「复制执行指令 / 忽略」；按关注优先级排序，默认展开需要你关注的，执行中的折叠；展开可见解释性详情、问题 id 与执行时间线；
- 「忽略」是人的判断（误报/不处理）：处置行追加进当天快照《处置记录》节并持久化到 `audit_actions` 表（重跑不重放；D2 同步撞车状态）；「复制执行指令」内嵌 `memory_audit_update` 汇报约定，粘贴给任意 agent 即可开工；
- 每条问题可展开**执行时间线**：agent 的每次汇报（开始执行 / 过程 / 完成 / 受阻）带 identity 与时间逐条展示；
- **复审通过**：agent 已执行、且本轮审计不再报告的问题自动进入此处（系统追加 `verified` 封口事件）——验证是确定性的，不需要人点头；已通过的问题若再次出现会标「复发」；
- 自愈类卡片（新增/外部修改/外部删除/**缺向量笔记**）只展示不带处置按钮——缺向量是 embedding 端点故障期写入的笔记，审计已自动重试补齐；
- 删除审计快照文件（手动或 curator 过期清理）时最近审计缓存联动清除，页面降级提示而不报错；
- **判断与执行记录**：人的处置（`audit_actions` 表）与 agent 的执行汇报（`audit_exec_events` 表）按时间合并倒序——判断与执行两条线都留痕；
- **历史审计快照**：左右两列——左列快照清单（`journal/audit/<日期>.md`，每日一份、同日重跑以「复审」小节追加，按时间倒序），右列渲染选中快照的 markdown 内容，不再上下堆叠；过期由 curator timer 按 `audit_retention_days`（默认 7 天）清理。

各问题的处置口径：

| 输出 | 含义 | 处置 |
|---|---|---|
| 新发现文件 / 外部修改 / 外部删除 | 自愈结果 | 无需操作 |
| 标题重复（D1） | 归一化后近似标题的两篇 | 人工合并后由 agent 汇报 `executed`，或「忽略」 |
| 语义撞车（D2） | 跨笔记相似 observation 对（含双方文本与分数） | **人工裁决**：合并交给 agent 执行，误报则「忽略」（同步撞车状态） |
| 悬空链接（D3） | `[[目标]]` 标题与路径都解析不到 | 「复制执行指令」派给 agent 补齐；非笔记引用则「忽略」 |
| 悬空主题卡（D5） | 注册表指向的 abstract 不存在 | 派给 agent 修注册表或重建卡 |
| 游离文件（D4） | 未归入任何注册主题的散笔记 | 派给 agent 归位，或「忽略」 |

> 免注册区（journal/archive/curator）不判游离。守卫统计（refused / forced 次数）在卡片底部。

**语义撞车的判定**：1）两篇是同一主题的两份拷贝 → 内容并进保留篇、删除另一篇（agent 执行后汇报 `executed`）；2）两篇主题不同、恰好都有这条事实 → 点「忽略」；3）一篇是另一篇的旧版本 → 新内容并入保留篇。撞车检测只对 `- [类别] 内容` 形态的 observation 行做；GFM 任务清单（`- [x]`）是勾选框，不参与（见 [03-storage-and-search.md](03-storage-and-search.md)）。

**Tab 2「质量提案」**——curator 深度审查工作流：

- 「立即深度审查」把注册表、主题卡、模块文件小节标题、PROFILE.md、agents/ 强制注入必读标题与审计结果交给 `[curator]` 配置的 LLM（约 1–3 分钟；每周 timer 自动执行）；报告落盘 `curator/提案-<日期>.md`，同日重跑以"复审"小节追加（标题保持每日唯一，不触发 D1）；
- **两级结构**：第一层是提案列表，每份提案带**提案级状态**（受阻·需要你介入 / 待处理·等你决定 / 执行中·agent 正在做 / 已结案·全部完成，按此优先级排序置顶）与各状态条目计数；点开才是**条目级**明细（severity / 类型 / 复审徽标 / 条目状态 / 建议 / 执行时间线）——默认展开需要你关注的提案，已结案的折叠收起；统计行走完整状态机（待处理 → 执行中 → 已执行 / 受阻 / 已忽略），右上角可按状态筛选；
- **没有「采纳」按钮——派发即采纳**：待处理条目直接「复制执行指令」粘给任意 agent（指令内嵌 `memory_audit_update` 汇报约定），误报/不做点「忽略」（写入 `audit_actions` 表 P 类条目 + 提案笔记「裁决记录」节，git 自动快照）；agent 执行进度以事件时间线展示在条目下方；
- 历史遗留的「已采纳·未执行」条目（旧口径的派发意图）显式标出，仍可派发或忽略收口；
- **自动结案 + 复审联动**：提案全部条目执行完成或忽略后，文件头部自动打「> 状态：已结案」标记（并把正文「状态：待裁决」改为「已结案」，git 快照）——`memory_search` 默认不再返回已结案提案（显式 `memory_read` 仍可读），执行类工作不会被重复派发；卡片标题随之出现「（已结案）」。agent 也可在确认全部条目完成后用 `memory_edit` 手工打标（无汇报通道的旧会话 agent 照此收尾，审计会为缺事件的条目补记「已执行」）。**同日复审追加新条目时旧标记由 curator 即时撤销**（新条目以「复审」徽标进入列表，处理完毕后再次自动打标）。系统与 WebUI 只记录判断、绝不直接改动笔记内容——这是"curator 只提案"铁律的延伸：变更永远走有守卫、有 git 快照的 store 工具语义。

「全量重建索引」属危险维护操作（运行指标一并清零），收纳在 **设置 → 健康总览 → 维护** 卡片，不在审计页。

### 2.4 画像与偏好

PROFILE.md 的可视化编辑：左侧列出全部小节（身份 / 沟通风格 / 材料与文档偏好……），点击查看，`编辑` 后整段保存；`+ 新建小节` 输入小节名创建。等价 MCP 的 `get_user_preference` / `update_user_preference`，同样走写路径（自动 git 快照）。

### 2.5 身份管理

`agents/<agent>/` 专属记忆区管理：按 agent+设备铸造确定性 identity token，管理 agent 层共享子树（shared/必读.md）与本机专属子树；详见 [09-agent-onboarding.md](09-agent-onboarding.md)。建 identity 时自动预创建 shared/必读.md 占位模板。

### 2.6 仪表盘（默认落地页）

- **系统状态卡**：服务运行状态、Embedding 配置与模型、Curator 启用状态与模型、今日调用数；
- **用户卡片**：每用户的笔记数 / 活跃主题 / 审计待办 / 提案数，附「审计」「笔记」快捷入口（携带用户切换直达对应页）；「最近审计」在服务重启后如实显示"重启后未审计"；
- **最近活动**：跨用户最近 8 条 MCP 调用流（工具 / 摘要 / 状态 / 时间）。

### 2.7 设置

四个 tab：**用户**（结构化增删改）、**服务配置**（embedding/curator 表单）、**config.toml（高级）**、**使用记录**。

- **用户**：用户列表（id / 记忆根 / git 身份 / 挂载状态）+ 新增 / 编辑 / 删除——程序化编辑 config.toml 的 `[[users]]`（先完整校验再备份写回，注释与顺序保留）。新增自动创建记忆目录；root 变更与新用户需重启才挂载 MCP（页面明确提示）；删除需输入用户 id 确认，默认仅移出配置（记忆目录与 git 历史保留），可勾选"同时删除记忆目录"（不可恢复）；
- **服务配置**：[embedding] 与 [curator] 表单化（端点 / 模型 / 密钥 / 维度 / 超时 / 保留天数），**「测试连接」实调端点**（embedding 回显维度与延迟、curator 回显模型回复）——改错当场发现，不用等重启；保存只改动表单字段（文本手术保注释），config.toml 原文编辑保留为「高级」模式；
- 使用记录 / 维护（全量重建索引）沿用原设计；

- **使用记录**：每次 MCP 工具调用的留痕——顶部卡片（近 14 天调用/错误/客户端数）+ 表格（时间/用户/工具/摘要/客户端 UA/IP/耗时），可按工具过滤；守卫拒绝算正常业务结果不记错误；
- **健康总览**：embedding 配置状态（未配置 = FTS-only 模式）、各用户笔记数/open 撞车数/守卫统计/主题数；
- **config.toml 在线编辑**：保存前自动做 TOML 语法 + 结构校验（非法配置直接拒绝）、备份原文件为 `config.toml.bak-<时间戳>`、保持 600 权限；可选"保存并重启服务"（systemd 重启，约 3 秒离线）。删除用户属危险操作，不提供按钮，请 SSH 手工处理。

## 三、API 参考

所有响应为 JSON，业务失败返回 `{"ok": false, "error": "..."}`（HTTP 200），未知用户 404。

| 方法 | 路径 | 参数 | 说明 |
|---|---|---|---|
| GET | `/api/overview` | — | 用户列表（笔记数/撞车数/守卫统计）+ embedding 状态 + 今日调用数 |
| GET | `/api/usage` | `limit` `user` `tool` | 调用日志（默认 100 条，按时间倒序） |
| GET | `/api/usage/clients` | — | 客户端汇总（UA + IP + 调用数 + 最近活跃） |
| GET | `/api/usage/days` | — | 近 14 天每日调用/错误数 |
| GET | `/api/{user}/notes` | `path` `sort` | 笔记列表（含 mtime/size） |
| POST | `/api/{user}/notes` | `{title, content, force, force_confirm}` | 新建（走写路径守卫） |
| GET | `/api/{user}/note` | `path` | 读取单篇（path 或标题） |
| PUT | `/api/{user}/note` | `{path, content}` | 整篇保存（标题随首行标题） |
| DELETE | `/api/{user}/note` | `path` | 删除（文件 + 全部索引） |
| GET | `/api/{user}/search` | `q` `limit` `kind` | 检索（含 ⚠ warnings） |
| POST | `/api/{user}/audit` | — | 运行审计（含自愈，会改动索引）；返回 `audit_file`（本次快照路径） |
| GET | `/api/{user}/audit/last` | — | 最近一次审计结果（内存缓存，重启即失效） |
| GET | `/api/{user}/audit/runs` | — | 历史审计快照列表（journal/audit/*.md，时间倒序） |
| GET | `/api/{user}/audit/actions` | — | 处置/裁决历史全量（audit_actions 表） |
| GET | `/api/{user}/audit/exec` | — | agent 执行时间线全量（audit_exec_events 表，新在前） |
| POST | `/api/{user}/audit/action` | `{file, id, action, label, note?}` | 记录人的处置（追加快照处置记录；D2 同步撞车状态） |
| POST | `/api/{user}/proposal/action` | `{file, index, action, type?, reason?, note?}` | 裁决提案条目（表持久化 + 提案笔记「裁决记录」留痕） |
| POST | `/api/{user}/collision` | `{id, status}` | 撞车裁决：`resolved` / `dismissed` |
| POST | `/api/{user}/curator` | — | 触发深度审查（同步等待，约 1-2 分钟），返回报告 markdown |
| GET | `/api/{user}/proposals` | — | 列出 curator 提案报告 |
| GET | `/api/config` | — | 读取 config.toml 原文（含密钥，仅内网管理用途） |
| POST | `/api/config` | `{content, restart?}` | 校验（TOML + load_config）→ 备份 → 保存；`restart=true` 时延迟重启服务 |

`{user}` 为 config.toml 中的用户 id。脚本化示例：

```bash
curl -s http://debsvc.local:9721/api/yachen/search?q=端口 | python -m json.tool
curl -s -X POST http://debsvc.local:9721/api/yachen/audit
```

## 四、使用日志（usage.db）

- 位置：`[server].data_dir/usage.db`（默认 `data/usage.db`，相对服务启动目录）；
- 表 `call_log`：`id / ts / user_id / client / ip / tool / summary / duration_ms / ok / error`；
- **滚动保留最近 2 万条**，无需维护；
- 写入方：`tools.py` 在每次 MCP 工具调用后记录（stdio 调用 client 记为 `stdio`；HTTP 调用取请求 UA 与远端 IP）；
- `ok` 语义：意外异常 = 0；守卫拒绝等业务结果 = 1（守卫行为另有 `guard_events` 表可查）。

## 五、构建与实现说明

- **前端工程**：Vue 3 + Naive UI + Vite（`frontend/`），源码 `src/App.vue` + `src/components/`（五页组件）+ `src/composables/api.js`（统一 fetch 封装）；
- **构建**：`scripts/build_webui.sh`（npm ci + vite build）→ 产物落 `yacmemo/webui/dist/`，与 `app.py` 的 `STATIC_DIR` 一致；vite outDir 用 `new URL('../yacmemo/webui/dist', import.meta.url)`，相对 `frontend/vite.config.js` 解析（上一级即仓库根），别改成 `../../yacmemo/webui/dist`（跑到仓库外面去了）；
- **分包**：manualChunks 把 `vue` 与 `naive-ui` 各自成 chunk——业务代码迭代不会使大依赖缓存失效；
- **开发模式**：`cd frontend && npm run dev`（Vite dev server 端口 5173，`/api` 代理到本机 9721）；
- **未构建行为**：`dist/` 不存在时服务正常启动，`/ui/` 返回 503 + 构建指引（PlainTextResponse），MCP/API 全功能可用；
- **服务器无 npm**：在开发机构建后 scp：`scp -r yacmemo/webui/dist <server>:/srv/yacmemo/yacmemo/webui/`（dist 不进 git）；
- 后端 handler 为 async，Store 的阻塞操作经 `run_in_threadpool` 执行，不会阻塞 MCP 事件循环；跨线程安全由 IndexDB/VectorStore/Store 的实例锁保证；
- 静态资源仅挂载 `/ui/assets`（Vite 产物）；`/ui/` 由 handler 直接回 `index.html`。

## 六、常见操作

- **看 agent 这两周干了什么**：设置页 → 使用记录区按工具过滤 `memory_write`，summary 列就是写入标题清单；
- **裁决一条撞车**：审计页 → 读双方文本 → 已合并则派给 agent 执行（复制执行指令）；确认是误报点"忽略"；裁决前可点路径跳到主题页核对；
- **排查"搜不到"**：搜索页切 `fts` / `vector` 分别试 → 设置页健康总览确认 embedding 是否配置 → 审计页看文件是否入索引；
- **手动改了文件**：审计页点一下运行，索引即对齐；
- **升级前端依赖/改组件**：开发机 `scripts/build_webui.sh` → scp dist → 刷新即生效（无需重启服务）。