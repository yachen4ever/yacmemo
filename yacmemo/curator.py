"""Memory quality curator: periodic review -> proposal report. Never auto-applies.

    yacmemo-curator --config config.toml [--user yachen] [--dry-run]

Runs (typically via systemd timer, off-peak): gathers the topic registry,
topic cards, audit results and guard stats, asks a configured LLM for
quality findings, and writes a PROPOSAL report note into `curator/`. It has
no write power over memories — approved proposals are executed by the agent
or by hand, exactly like any other human decision.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from datetime import date, datetime, timedelta

import httpx

from yacmemo.config import Config, UserEntry, load_config

logger = logging.getLogger("yacmemo.curator")

_SYSTEM_PROMPT = """你是个人记忆体系的质量审查员。
你收到：主题注册表、各主题卡（现状手册）、各主题模块文件的小节标题、PROFILE.md（用户画像）、agents/ 强制注入必读的小节标题、审计结果。
你的任务是发现记忆体系的质量问题并输出"提案"——你只提案，绝不执行，也绝不修改任何记忆。

先理解记忆的分层模型（决定内容放对没放对）：
- topics/ = 按需检索层：agent 执行任务搜到才看到，不保证每次会话都被看到
- PROFILE.md + agents/<agent>/shared/必读.md = 强制注入层：每次会话开头自动注入，一定生效
- 内容该放哪一层，取决于"错过它"的代价：事实记录错过可接受；行为纪律错过就是违规

审查维度：
- duplicate：同一主题的多份拷贝/快照
  （注意：archive/ 与 OV 分片备份按设计只读保留，不要建议整理它们）
- outdated：现状笔记内容明显落后（结合文中提到的日期与"已取代"线索判断）
- stray：游离在所有注册主题之外的文件（建议归入哪个主题或删除）
- stale-card：主题卡的"现状"描述与正文明显不一致
- merge：两个主题应合并
- forget：纯过程性记录，建议遗忘（删除，git 可恢复）
- misplaced：内容放错了记忆层级——重点维度。信号：主题笔记的小节标题或
  内容是"给 agent 的行为纪律"（如 三条铁律/发版规则/每次会话必须/约定/
  禁止事项），而非事实记录。这类内容放 topics/ 意味着"被检索到才生效"，
  应提案迁移到强制注入位置：agent 纪律 → agents/<agent>/shared/必读.md；
  用户个人偏好（工具链/环境/风格）→ PROFILE.md。对照材料中的
  "模块文件小节标题"与"agents/ 强制注入必读"两个视图判断。
- profile-overlap：PROFILE.md 与主题内容重复或边界不清——画像回答
  "用户是谁、偏好什么"，主题回答"某件事的事实与现状"；明显重叠时
  建议归位（画像保留画像侧，事实留给主题，或反之）。
- tag-missing：主题没有任何标签——结合该主题卡内容建议 1-2 个标签，
  优先复用「主题标签」清单里已有的标签词汇（新标签克制）；
  标签是"视角归类"（如 工作/开发/生活），不要把状态（维护中/已归档）
  当标签提案。
- tag-duplicate：语义重复的标签（如 工作/上班/公司）——对照「主题标签」
  清单判断，建议合并：指明保留哪个标签、清理哪个，涉及的主题会自动改挂。
- tag-mismatch：标签与主题内容明显不符——该主题卡片讲的内容与所挂标签
  语义对不上，建议改挂正确标签或移除。
  （标签在注册表 `- 标签:` 行，改挂/合并由用户裁决后 agent 执行。）

输出严格 JSON（不要 markdown 代码块）：
{"summary": "总体评价（2-3 句）",
 "findings": [{"type": "duplicate|outdated|stray|stale-card|merge|forget|misplaced|profile-overlap|tag-missing|tag-duplicate|tag-mismatch|other",
               "severity": "high|medium|low",
               "paths": ["涉及笔记路径"],
               "reason": "判断依据",
               "proposal": "具体建议动作"}]}

若无发现，findings 返回空数组。severity 从严：只有确有把握才标 high。"""


def default_llm_call(config: Config):
    """Build an OpenAI-compatible chat caller from [curator] config."""
    cfg = config.curator
    if not cfg.base_url or not cfg.model:
        raise RuntimeError("curator 未配置 LLM 端点（[curator].base_url / model）")

    def call(system: str, user: str) -> str:
        headers = {"Content-Type": "application/json"}
        if cfg.api_key:
            headers["Authorization"] = f"Bearer {cfg.api_key}"
        resp = httpx.post(
            f"{cfg.base_url}/chat/completions",
            headers=headers,
            json={
                "model": cfg.model,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
                "temperature": 0.2,
                "max_tokens": cfg.max_tokens,
            },
            timeout=cfg.timeout,
        )
        resp.raise_for_status()
        try:
            return resp.json()["choices"][0]["message"]["content"]
        except Exception as e:
            # 端点 200 + 非 chat 响应（空体/错模型回显）时不指名就查不到案：
            # 错误必须带上状态码与 body 开头，把嫌疑指向 [curator] 三件套
            raise RuntimeError(
                f"LLM 响应不可用（HTTP {resp.status_code}，body 开头: "
                f"{resp.text[:200]!r}）——请检查 [curator].base_url / api_key，"
                "并确认 model 是 chat 主模型而非 embedding 等非对话模型") from e

    return call


def _headings_of(path: Path, limit: int = 10) -> str:
    """文件的小节标题串（高信号、低体量——供 LLM 判断内容性质）。"""
    try:
        heads = [ln.lstrip("# ").strip() for ln in
                 path.read_text(encoding="utf-8").splitlines()
                 if ln.lstrip().startswith("#")]
    except OSError:
        return "（读取失败）"
    return " / ".join(heads[:limit]) if heads else "（无小节标题）"


def build_material(store, max_chars_per_card: int = 2000) -> str:
    """Registry + topic cards + module headings + PROFILE + audit, for the reviewer."""
    topics = store.load_topics()
    cards = []
    hygiene = []
    for t in topics:
        p = store.root / t["card"] if t["card"] else None
        if t["card"] and p and p.is_file():
            raw = p.read_text(encoding="utf-8")
            if len(raw) > max_chars_per_card:
                # 超长卡只送摘录——必须显式声明这是摘录而非笔记末尾，
                # 否则 LLM 会把摘录边界误读成"卡片末尾截断/内容缺失"
                # （2026-09-25 实爆：一次复审 6 条误报全是这个来源）
                body = (raw[:max_chars_per_card]
                        + f"\n\n（摘录说明：本卡全文共 {len(raw)} 字符，以上只是开头摘录，"
                          "并非笔记末尾——禁止以「末尾截断/内容在'质量全'等词后被切断/"
                          "缺失 META 或结尾」为由提案）")
            else:
                body = raw
            cards.append(f"## {t['title']}（{t['card']}）\n{body}")
        # 模块文件小节标题（卫生视图）：不看模块内容就无法发现
        # "纪律/规则类内容错放在按需检索层"这类组织问题
        if not t.get("archived") and t["card"]:
            d = p.parent if p else None
            if d and d.is_dir():
                for f in sorted(d.glob("*.md")):
                    if f.name == "abstract.md":
                        continue
                    hygiene.append(f"- {f.relative_to(store.root).as_posix()}：{_headings_of(f)}")
    audit = store.audit()
    profile = store.root / "PROFILE.md"
    profile_text = (profile.read_text(encoding="utf-8")[:1500]
                    if profile.is_file() else "（尚无画像）")
    mustread = []
    for f in sorted(store.root.glob("agents/*/shared/*.md")):
        mustread.append(f"- {f.relative_to(store.root).as_posix()}：{_headings_of(f)}")
    # 主题标签清单（tag → 主题 + 未打标主题）——标签治理三稽核的数据源
    tag_map, untagged = {}, []
    for t in topics:
        tags = t.get("tags") or []
        if tags:
            for x in tags:
                tag_map.setdefault(x, []).append(t["title"])
        else:
            untagged.append(t["title"])
    tags_view = [f"- {x}：{'、'.join(ts)}" for x, ts in sorted(tag_map.items())]
    tags_view.append(f"- （未打标主题：{'、'.join(untagged) or '无'}）")
    material = (
        "### 主题注册表\n" +
        (store.topics_file().read_text(encoding="utf-8")
         if store.topics_file().is_file() else "（空）")
        + "\n\n### 各主题卡\n" + ("\n\n".join(cards) or "（无）")
        + "\n\n### 主题标签\n" + ("\n".join(tags_view) or "（无）")
        + "\n\n### 模块文件小节标题（各主题目录下的非 abstract 文件）\n"
        + ("\n".join(hygiene) or "（无模块文件）")
        + "\n\n### PROFILE.md（用户画像，强制注入）\n" + profile_text
        + "\n\n### agents/ 强制注入必读（小节标题）\n"
        + ("\n".join(mustread) or "（无）")
        + "\n\n### 审计结果\n" + json.dumps(audit, ensure_ascii=False)[:4000]
    )
    return material[:36000]


def parse_proposal(raw: str) -> dict:
    """Parse the LLM's JSON proposal (tolerates markdown code fences)."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"提案 JSON 解析失败（{e}）。LLM 原始返回开头: {raw[:200]!r}——"
            "常见原因：[curator].model 配成了非 chat 模型（如 embedding 模型）"
            "或返回被截断（调大 max_tokens）") from e


def _format_findings(findings: list) -> list[str]:
    lines = []
    for i, f in enumerate(findings, 1):
        lines.append(
            f"{i}. **[{f.get('severity', '?')}] {f.get('type', '?')}** — {f.get('reason', '')}")
        for p in f.get("paths", []):
            lines.append(f"   - 涉及: {p}")
        lines.append(f"   - 建议: {f.get('proposal', '')}")
    return lines


def render_report(proposal: dict, user_id: str) -> str:
    lines = [
        f"# 记忆质量提案（{user_id}，{date.today().isoformat()}）",
        "",
        "**状态：待裁决** —— 本报告由 curator 生成，仅含提案。"
        "批准的条目请让 agent 执行或手工处理；执行后可在审计页复核。",
        "",
        "## 总评",
        proposal.get("summary", ""),
        "",
    ]
    findings = proposal.get("findings", [])
    lines.append(f"## 提案（{len(findings)} 条）")
    if not findings:
        lines.append("无。")
    lines.extend(_format_findings(findings))
    lines.append("")
    lines.append("> 裁决后在本行下追加执行记录；被采纳并执行的条目由 agent 在对应笔记中落实。")
    return "\n".join(lines)


def render_review_section(proposal: dict) -> str:
    """Same-day re-run result, appended to the existing report note.
    One note per day keeps the fixed title unique (D1 guard friendly)."""
    lines = ["", "---", "",
             f"## 复审（{datetime.now().strftime('%Y-%m-%d %H:%M')}）", ""]
    findings = proposal.get("findings", [])
    if not findings:
        lines.append("本次复审未发现新问题（0 条），此前提案维持原状。")
    else:
        lines.append(f"本次复审发现 {len(findings)} 条新问题，待裁决：")
        lines.extend(_format_findings(findings))
    lines.append("")
    return "\n".join(lines)


def run_check(config: Config, user: UserEntry, dry_run: bool = False,
              llm_call=None) -> str:
    """One curator pass for one user. Returns the proposal report markdown."""
    from yacmemo.index_db import IndexDB
    from yacmemo.store import Store
    from yacmemo.vector import VectorStore

    root = config.user_root_abs(user)
    db = IndexDB(str(root / ".index" / "index.db"))
    emb, vectors = None, None
    if config.embedding.base_url and config.embedding.model:
        from yacmemo.embedding import EmbeddingClient

        emb = EmbeddingClient(base_url=config.embedding.base_url,
                              api_key=config.embedding.api_key,
                              model=config.embedding.model,
                              dimensions=config.embedding.dimensions,
                              timeout=config.embedding.timeout)
        vectors = VectorStore(str(root / ".index" / "lancedb"),
                              config.embedding.dimensions)
    from yacmemo.config import resolve_git_identity
    git_name, git_email = resolve_git_identity(user, config)
    store = Store(config, db, emb, vectors, root=root,
                  git_user=git_name, git_email=git_email)

    cleaned = cleanup_audit_snapshots(store, config.curator.audit_retention_days,
                                      dry_run=dry_run)

    material = build_material(store)
    call = llm_call or default_llm_call(config)
    raw = call(_SYSTEM_PROMPT, material)
    proposal = parse_proposal(raw)
    report = render_report(proposal, user.id)
    if cleaned:
        report += (f"\n\n---\n\n> 维护：{'（dry-run 未执行）' if dry_run else '已'}清理 "
                   f"{cleaned} 份过期审计快照"
                   f"（audit_retention_days={config.curator.audit_retention_days}）\n")

    if not dry_run:
        path = f"curator/提案-{date.today().isoformat()}.md"
        abs_path = store.root / path
        if abs_path.is_file():
            # 同日重跑 = 复审：追加复审小节而非新建笔记——
            # 每天一份报告、标题天然唯一，D1 标题守卫不再被重跑命中
            old = abs_path.read_text(encoding="utf-8")
            merged = old + render_review_section(proposal)
            if proposal.get("findings"):
                # 复审带来新条目 → 旧「已结案」标记失效：撤标 + 状态复位，
                # 防止带着新待办的报告对 agent 隐身（审计侧双向自愈兜底）
                from .store import PROPOSAL_SETTLED_MARKER
                if PROPOSAL_SETTLED_MARKER in merged:
                    merged = ("\n".join(
                        ln for ln in merged.splitlines()
                        if PROPOSAL_SETTLED_MARKER not in ln) + "\n"
                    ).replace("**状态：已结案**", "**状态：待裁决**", 1)
            store.save(path, merged)
        else:
            store.save(path, report)
    db.close()
    return report


def cleanup_audit_snapshots(store, retention_days: int,
                            dry_run: bool = False) -> int:
    """Delete journal/audit/ snapshots older than retention_days (0 = never).

    Day of a snapshot comes from its filename stem (both the current day-keyed
    `<YYYYMMDD>.md` and the legacy `<YYYYMMDD>-<HHMMSS>.md` parse). The files
    are a recent-working-set view only: dispositions persist in the
    audit_actions table and git keeps the full history, so nothing is lost.
    Deletion goes through the store so every removal is git-snapshotted and
    the git-clean invariant holds.
    """
    if retention_days <= 0:
        return 0
    audit_dir = store.root / store.config.memory.journal_dir / "audit"
    if not audit_dir.is_dir():
        return 0
    cutoff = date.today() - timedelta(days=retention_days)
    cleaned = 0
    for f in sorted(audit_dir.glob("*.md")):
        m = re.match(r"^(\d{8})", f.stem)
        if not m:
            continue
        try:
            file_day = datetime.strptime(m.group(1), "%Y%m%d").date()
        except ValueError:
            continue
        if file_day < cutoff:
            if not dry_run:
                store.delete_note(f.relative_to(store.root).as_posix())
            cleaned += 1
    if cleaned and not dry_run:
        logger.info("cleaned %d audit snapshot(s) older than %dd",
                    cleaned, retention_days)
    return cleaned


def main():
    parser = argparse.ArgumentParser(description="yacmemo memory quality curator")
    parser.add_argument("--config", default=None)
    parser.add_argument("--user", default=None, help="single user (default: all)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the proposal instead of saving it")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    config = load_config(args.config)
    users = config.users
    if args.user:
        users = [u for u in users if u.id == args.user]
        if not users:
            raise SystemExit(f"未知用户: {args.user}")

    for user in users:
        try:
            report = run_check(config, user, dry_run=args.dry_run)
            logger.info("[%s] curator report generated (%d chars)%s",
                        user.id, len(report), " (dry-run)" if args.dry_run else "")
            if args.dry_run:
                print(report)
        except Exception as e:
            logger.error("[%s] curator check failed: %s", user.id, e)


if __name__ == "__main__":
    main()
