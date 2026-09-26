> [English](en/09-agent-onboarding.md) | 简体中文

# Agent 接入指引（AI 专用）

> 如果你是 AI agent（Claude Code / Codex / Cursor / 自研 runtime……）并读到了本文，请按顺序完成下面的接入自检，并把结果明确告诉用户。本文是 README 的展开版；接入方式的完整说明见 [08-agent-config.md](08-agent-config.md)。

## ① 自检：你是否已配置 yacmemo MCP？

- **看工具**：你的可用工具里有 `memory_search` / `memory_read` / `memory_write` / `memory_context` 等 yacmemo 工具 → **已接入**，直接跳到 ③；
- **看配置**：Claude Code 跑 `claude mcp list`；Codex 跑 `codex mcp list`；Cursor 看 `.cursor/mcp.json`；其他客户端查各自的 MCP 设置。

## ② 未接入：引导用户完成配置

向用户说明"yacmemo 是多端共享的长期记忆层"，并向用户确认其身份对应的**用户ID**（如 `yachen`），然后给出对应命令（替换 `<用户ID>`）：

```bash
# Claude Code
claude mcp add --transport http yacmemo http://debsvc.local:9721/<用户ID>/mcp
# Codex CLI
codex mcp add yacmemo --url http://debsvc.local:9721/<用户ID>/mcp
# 其他 MCP 客户端：添加 streamable HTTP 端点 http://<服务器>:9721/<用户ID>/mcp
```

配置完成后提示用户**重启会话或重连 MCP**，之后回到 ① 自检即可走通。同机 stdio 方式与端点清单见 [08-agent-config.md](08-agent-config.md) 与 [05-deployment.md](05-deployment.md) §2.1。

## ③ 已接入：把记忆逻辑织入工作流，并向用户展示状态

1. **会话开始**：先调 `memory_context` 冷启动回顾，然后向用户展示一行接入摘要，例如：

   > ✅ 已接入 yacmemo 记忆层（用户：yachen，契约 v0.3.1）——画像偏好 3 条；活跃主题 12 个：《yacmemo部署配置》《备份策略》……；专属必读 2 份已注入；最近审计无待处理问题。

   携带 identity token（`Authorization: Bearer <device>_<agent>`，stdio 用环境变量 `YACMEMO_TOKEN`，token 由 WebUI「身份」页生成）的 agent 会自动获得 `agents/` 专属记忆区：`memory_context` 注入你的专属必读，`memory_search` 范围限定为 user 层 + 你的专属区。未配置 token 不影响 user 层使用。

   > ✅ 已接入 yacmemo 记忆层（用户：yachen，契约 v0.1.3）——画像偏好 3 条；活跃主题 12 个：《yacmemo部署配置》《备份策略》……；最近审计无待处理问题。

2. **日常遵循记忆纪律**（完整约定见 [01-architecture.md §八](01-architecture.md)，工具规格见 [02-mcp-tools.md](02-mcp-tools.md)）：

   - 回答事实性问题前先 `memory_search`；结果带 ⚠ 时先读两篇、用 `memory_edit` 合并，然后再回答；
   - 写入先查重：已有同主题笔记用 `memory_edit` / `memory_edit_section` **就地更新**，不新建重复笔记；
   - **长期记忆只写注册主题目录内**——路径必须带 `topics/` 前缀：`topics/<主题>/<笔记名>`。写 `女儿AI陪伴老师/abstract` 会被拦截，写 `topics/女儿AI陪伴老师/abstract` 才对（2026-09-19 TeleAgent 实测：漏前缀被拦后 agent 空转了一轮才自纠；现在拦截消息会直接给出可重试的 title，但别依赖拦截——先写对）；新主题须请用户明确授权后 `topic_register`（越界写入硬拦截，`force` 不豁免）；流水账放 `journal/`；
   - **专属必读写自己的 identity 区**：`agents/<agent>/shared/必读.md`（同 agent 跨设备共享）或 `agents/<agent>/<device>/必读.md`（本机专属）；只放指针与纪律，事实一律进 topics/；引用其他层路径必须代入真实设备名（agents/teleagent/r9000x/必读.md），模板占位一律写尖括号形式（agents/<agent>/<device>/…），禁止留空段——agents/teleagent//必读.md 会被当成真实路径、检索必然失败。
   - **abstract 是摘要卡**（`topics/<主题>/abstract.md`）：保持一句话现状，现状变化用 `memory_edit` 就地更新；详细内容写成模块笔记 `topics/<主题>/<笔记名>`，不要把长文塞进 abstract；
   - 事实行用 observation 语法：`- [配置] 服务端口为 9721`；
   - **执行审计问题要汇报进度**：处理 `memory_audit` 发现的问题、或 WebUI「复制执行指令」派下的问题时，先 `memory_audit_update(issue_id, "executing")` 接手，关键动作 `"progress"` 汇报，完成 `"executed"` 附改动摘要，受阻 `"blocked"` 说明卡点；复审由审计自动确认（下轮不再报告即通过），不要声称"已验证"、不要代替人忽略问题；
   - **已结案提案不再可检索**：curator/ 提案报告的全部条目执行完成或忽略后会被系统打结案标记，`memory_search` 默认不返回——不要去执行已结案提案里的条目；`memory_read` 按路径仍可读（那是明确查阅）；
   - 归档主题内的单篇笔记用 `archive_note`（取消用 `unarchive_note`）；不要手工 memory_move 到 archive/ 根目录（脱离主题归属、WebUI 主题树不可见）；
   - 注册 / 注销 / 归档主题、删除笔记：**仅在用户明确要求时执行**。

3. **不确定就问**：找不到该写进哪个主题、或对记忆内容有疑问，向用户说明而不是猜测。
4. **WebUI 界面语言**：WebUI 头部可切换 中文 / English（选择记忆在浏览器 localStorage）。记忆内容与 agent 协作约定不受界面语言影响；用户用非中文提问时，可顺带提示这一开关。


## ④ 接入契约版本与自主更新

yacmemo 的工具语义与写入约定有版本号（**接入契约版本**），服务器不推送、agent 自主拉取：

1. **声明**：把你接入时依据的契约版本记进本地接入提示词 / USER.md，一行即可：`yacmemo 接入契约版本: 0.1.3`；
2. **发现**：`memory_context` 返回头部带当前契约版本，每次会话开始自然比对；
3. **更新**：发现落后（或版本号为空）时调用 `integration_check(onboarded_version="<你的版本>")`——返回增量变更与**写入约定速览全文**，据此刷新本地接入提示词、更新记录的版本号，然后向用户报告一句"yacmemo 接入约定已从 0.1.2 更新到 0.1.3"。无需等用户指令、无需重读仓库文档。

机制详情与版本历史的数据源：[02-mcp-tools.md §17](02-mcp-tools.md)。

## ⑤ 桌面 agent 附加项

TeleAgent 类自带本地记忆文件（USER.md / MEMORY.md）的 agent：本地文件只存指针、不存事实副本，模板与维护约定见 [08-agent-config.md](08-agent-config.md) §二。
