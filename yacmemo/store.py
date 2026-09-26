"""Markdown store: CRUD + write-path guards + synchronous index maintenance.

Design invariants (docs/06-lean-architecture.md):
- File first, index second: a crash can only leave the index stale, never
  corrupt the source of truth.
- Every mutating call updates all indexes synchronously (<300ms typical);
  no daemons, no queues, no cron.
- The store never deletes user content. Guards refuse; force is explicit and
  logged; collisions are annotated, never auto-resolved.
- Embedding is the only model call and may fail: content is still written,
  vectors/D2 catch up on the next write or `reindex`.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import os
import posixpath
import re
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from rapidfuzz import fuzz

from .config import Config
from .detectors import (
    canonical_link_target,
    d1_scan,
    d3_scan,
    find_title_conflicts,
    parse_links,
    parse_observations,
)
from .embedding import EmbeddingClient
from .fs_utils import content_hash
from .git_snapshots import GitSnapshots
from .identity import AGENTS_PREFIX, Identity, visible, writable
from .index_db import IndexDB
from .vector import VectorStore

logger = logging.getLogger(__name__)

_ILLEGAL_FILENAME = re.compile(r'[\\/:*?"<>|]')
_HEADING_RE = re.compile(r"^(#{2,6})\s+(.+?)\s*$")
TOPICS_FILE = "TOPICS.md"
# 已结案提案的规范标记（引用行，search 据此把已结案提案从检索结果隐去；
# 显式 memory_read 仍可读——那是明确查阅）。打标时同步把「**状态：待裁决**」
# 行改为「**状态：已结案**」，人读机读一致。
PROPOSAL_SETTLED_MARKER = "> 状态：已结案"
# 用户画像与偏好：记忆层功能文件（不注册主题、不游离检测、memory_context 前置）
PROFILE_FILE = "PROFILE.md"
# 免注册区：不参与主题注册与游离检测的目录（agents/ 另有 identity 写守卫，
# 见 _require_agents_write——免注册 ≠ 任意可写）
FREE_ZONES = ("journal/", "archive/", "curator/", AGENTS_PREFIX)
_TOPIC_FIELD_RE = re.compile(r"^-\s*(卡|相关|现状|注册|状态|标签):\s*(.*)$")

# memory_read 返回值里的附加信息标记：agent 把它们当文件内容抄进 old_string
# 时，拒绝消息要能直接点破（2026-09-16 TeleAgent 连续 4 次 edit 失败的根因）
_READ_DECOR_MARKERS = ("[正文开始", "[正文结束", "相关笔记", "(path: ",
                       "(vector)", "(via: ")


def _d1_id(c: dict) -> str:
    """Stable issue id for a D1 title-duplicate pair (order-independent)."""
    return "D1:" + "|".join(sorted((c["a_title"], c["b_title"])))


def _d3_id(c: dict) -> str:
    return f"D3:{c['path']}|{c['link']}"


def _d4_id(path: str) -> str:
    return f"D4:{path}"


def _d5_id(title: str, card: str) -> str:
    return f"D5:{title}|{card}"


class StoreError(Exception):
    """Tool-facing error; the message is meant to be shown to the agent."""


class TitleConflict(StoreError):
    """Write refused because an existing note has a near-duplicate title."""

    def __init__(self, message: str, matches: list[dict]):
        super().__init__(message)
        self.matches = matches


class AnchorError(StoreError):
    """Edit refused: old_string not found or not unique."""


class Store:
    # Methods serialized under the instance lock: the HTTP server runs tools
    # in a threadpool, and mutating ops must not interleave.
    _MUTATING = ("write", "edit", "edit_section", "move", "read", "audit", "reindex")

    def __init__(self, config: Config, db: IndexDB,
                 emb: EmbeddingClient | None = None,
                 vectors: VectorStore | None = None,
                 root: str | Path | None = None,
                 git_user: str = "", git_email: str = ""):
        self.config = config
        self.db = db
        self.emb = emb
        self.vectors = vectors
        # Explicit root for multi-user servers; falls back to the single-user
        # [memory].root. Never derived lazily — the boundary must be fixed at
        # construction time or two users could share one directory.
        self.root = (Path(root).expanduser().resolve() if root
                     else config.root_abs)
        self.root.mkdir(parents=True, exist_ok=True)
        self.snapshots = GitSnapshots(self.root,
                                      enabled=config.memory.git_snapshots,
                                      user_name=git_user, user_email=git_email)
        self._lock = threading.RLock()
        # 最近一次审计结果（MCP memory_audit 与 WebUI 审计页共享，见 audit()）
        self.last_audit: dict | None = None
        # 最近一次 _index_note 自动清除的过期冲突对数（open 状态、重算后不再
        # 命中）——合并型编辑的可见信号，由 edit/edit_section 读取上报
        self._last_cleared_collisions = 0
        for name in self._MUTATING:
            fn = getattr(self, name)
            setattr(self, name,
                    functools.wraps(fn)(
                        lambda *a, _fn=fn, **kw: self._locked_call(_fn, *a, **kw)))

    def _locked_call(self, fn, *args, **kwargs):
        with self._lock:
            return fn(*args, **kwargs)

    # ------------------------------------------------------------------ paths

    def _split_title(self, title: str) -> tuple[str, str]:
        """'projects/foo' -> ('projects', 'foo'); reject traversal."""
        t = (title or "").strip().replace("\\", "/").strip("/")
        if not t:
            raise StoreError("标题不能为空。")
        if ".." in t.split("/"):
            raise StoreError(f"标题不允许包含路径穿越: {title}")
        if "/" in t:
            dirpart, name = t.rsplit("/", 1)
            return dirpart, name
        return "", t

    def title_to_path(self, title: str) -> str:
        dirpart, name = self._split_title(title)
        name = _ILLEGAL_FILENAME.sub("_", name).strip(". ")
        if not name:
            raise StoreError(f"标题无法转为合法文件名: {title}")
        rel = f"{dirpart}/{name}.md" if dirpart else f"{name}.md"
        return rel

    def topic_name_map(self) -> dict[str, str]:
        """注册主题名 → 主题卡路径。主题名是 [[链接]] 的稳定引用——
        卡的索引标题从 H1 提取、会与主题名漂移，按主题名解析必须走注册表。"""
        return {t["title"]: t["card"] for t in self.load_topics() if t.get("card")}

    def resolve(self, path_or_title: str) -> str:
        """Resolve to an existing note: path first, then exact title."""
        p = (path_or_title or "").strip()
        if not p:
            raise StoreError("空的路径/标题。")
        rel = p.replace("\\", "/").lstrip("/")
        if (self.root / rel).is_file():
            return rel
        row = self.db.get_note_by_title(p)
        if row:
            return row["path"]
        # tolerate path with/without .md
        if not rel.endswith(".md") and (self.root / (rel + ".md")).is_file():
            return rel + ".md"
        raise StoreError(f"未找到笔记: {path_or_title}（可先用 memory_list 浏览）")

    # ------------------------------------------------------------------ guard

    def check_title_conflicts(self, title: str,
                              exclude_path: str | None = None) -> list[dict]:
        return find_title_conflicts(
            title, self.db.all_titles(),
            self.config.guard.title_similarity_threshold,
            exclude_path=exclude_path,
        )

    # ------------------------------------------------------------------ write

    def write(self, title: str, content: str, force: bool = False,
              force_confirm: bool = False,
              identity: Identity | None = None) -> dict:
        rel = self.title_to_path(title)
        self._require_agents_write(rel, identity)
        self._require_covered(rel)
        _, name = self._split_title(title)
        is_journal = rel.startswith(self.config.journal_prefix)
        # agents/ 专属区的标题是文件名语义（必读/环境……），跨 identity 同构，
        # 与 journal 一样跳过全局唯一标题守卫（D1 审计侧同步排除）
        is_agent_zone = rel.startswith(AGENTS_PREFIX)

        # The stored title is the topic name without any directory prefix —
        # directories are filing, not part of the note's identity.
        conflicts: list[dict] = []
        if not (is_journal or is_agent_zone):
            conflicts = self.check_title_conflicts(name, exclude_path=rel)
            if conflicts and not force:
                self.db.add_guard_event("refused", name,
                                        conflicts[0]["path"], forced=False)
                top = "\n".join(
                    f"  - [[{c['title']}]] ({c['path']}, 相似度 {c['score']})"
                    for c in conflicts[:5]
                )
                raise TitleConflict(
                    f"已存在近似标题笔记，拒绝新建：\n{top}\n"
                    f"更新内容请用 memory_edit / memory_edit_section；"
                    f"确属新主题请 memory_write(force=true)。",
                    conflicts,
                )
        if conflicts and force:
            # Two-step confirmation ladder: frequent force usage requires an
            # explicit force_confirm=true on top of force=true (human-confirm
            # semantics, deterministic and fully counted in guard_events).
            recent = self.db.count_forced_since(hours=24)
            threshold = self.config.guard.force_confirm_threshold
            if recent >= threshold and not force_confirm:
                raise StoreError(
                    f"force 近 24 小时已被使用 {recent} 次（阈值 {threshold}），"
                    "需要人工确认。\n"
                    f"候选已有笔记：[[{conflicts[0]['title']}]] "
                    f"({conflicts[0]['path']}, 相似度 {conflicts[0]['score']})。\n"
                    "若已确认这确实是不同主题，请同时传 force=true 和 "
                    "force_confirm=true 重试。"
                )
            self.db.add_guard_event("forced", name, conflicts[0]["path"], forced=True)

        abs_path = self.root / rel
        if is_agent_zone and abs_path.is_file():
            # agents/ 区跳过标题守卫，覆盖不会像 topics/ 一样被近似同名拦截——
            # 显式拒绝：必读等专属文件更新一律就地 edit（git 可恢复，但静默
            # 覆盖是事故；WebUI 编辑器走 save() 不受影响）
            raise StoreError(
                f"写入被拦截: {rel} 已存在（agents/ 区 memory_write 只创建不覆盖）。\n"
                "更新内容用 memory_edit / memory_edit_section 就地修改；"
                "确要整篇重建请先 memory_delete 该路径（仅用户明确要求时）。")
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(content, encoding="utf-8")

        self._index_note(rel, name, content)
        # 写时撞车即时回显：D2 在 _index_note 里增量检出，若不在此处
        # 带回，agent 要等到下次审计才知道自己制造了语义撞车
        new_cols = [c for c in self.db.collisions_for(rel, "open")
                    if c["b_path"] == rel]
        self.snapshots.commit(f"write: {rel}")
        return {"path": rel, "forced": bool(conflicts and force),
                "new_collisions": [{"with_path": c["a_path"],
                                    "score": c["score"],
                                    "text": c["b_text"]} for c in new_cols]}

    # ------------------------------------------------------------------ read

    def read(self, path_or_title: str,
             identity: Identity | None = None) -> dict:
        rel = self.resolve(path_or_title)
        self._require_agents_visible(rel, identity)
        content = (self.root / rel).read_text(encoding="utf-8")
        row = self.db.get_note(rel)
        title = row["title"] if row else self._title_from_content(rel, content)

        related = []
        seen_paths = {rel}
        topic_names = self.topic_name_map()
        for link in parse_links(content):
            target = self.db.get_note_by_title(link)
            if not target:
                # 主题名解析：卡标题会随 H1 漂移，注册表里的主题名才是
                # 稳定标识（[[工作规则与开发偏好]] → 其主题卡）
                card = topic_names.get(link.strip())
                target = self.db.get_note(card) if card else None
            if not target:
                # 路径形式兜底（与 d3_scan 同规则）：agent 从 memory_list
                # 拿到的是路径，[[topics/x/abstract.md]] 这类引用按路径解析
                tpath = canonical_link_target(link)
                target = (self.db.get_note(tpath)
                          or self.db.get_note(tpath + ".md"))
            if target and target["path"] not in seen_paths:
                first_obs = self._first_observation(target["path"])
                related.append({"title": link, "path": target["path"],
                                "via": "link", "note": first_obs or ""})
                seen_paths.add(target["path"])
            elif not target:
                related.append({"title": link, "via": "link", "missing": True})

        if self.emb and self.vectors:
            try:
                qv = self._embed_cached(f"{title}\n{content}")
                for hit in self.vectors.search_note_vectors(qv, 3):
                    if hit["id"] not in seen_paths:
                        # 相关笔记同样遵守 identity 边界：其他专属区的笔记不出现
                        if not visible(hit["id"], identity):
                            continue
                        related.append({"title": self._title_of(hit["id"]),
                                        "path": hit["id"], "via": "vector",
                                        "score": round(1 - hit["_distance"] / 2, 3)})
                        seen_paths.add(hit["id"])
            except Exception as e:
                logger.warning("Related-vector lookup failed: %s", e)

        return {"path": rel, "title": title, "content": content, "related": related}

    # ------------------------------------------------------------------ edit

    def edit(self, path: str, old_string: str, new_string: str,
             identity: Identity | None = None) -> dict:
        rel = self.resolve(path)
        self._require_agents_write(rel, identity)
        abs_path = self.root / rel
        content = abs_path.read_text(encoding="utf-8")

        positions = [i + 1 for i, line in enumerate(content.splitlines())
                     if old_string in line]
        n = content.count(old_string)
        if n == 0:
            raise AnchorError(
                f"old_string 在 {rel} 中未找到。{self._edit_miss_hint(content, old_string)}")
        if n > 1:
            raise AnchorError(
                f"old_string 在 {rel} 中命中 {n} 处（约行 {positions[:5]}），需要唯一。"
                "请扩展上下文使锚点唯一。")

        new_content = content.replace(old_string, new_string, 1)
        abs_path.write_text(new_content, encoding="utf-8")
        title = self._title_of(rel, new_content)
        self._index_note(rel, title, new_content)
        self.snapshots.commit(f"edit: {rel}")
        return {"path": rel, "title": title,
                "before_hash": content_hash(content),
                "cleared_collisions": self._last_cleared_collisions}

    def _edit_miss_hint(self, content: str, old: str) -> str:
        """未命中锚点的确定性诊断：拒绝消息必须是可执行的下一步指令
        （04-consistency §一），点破原因并交还可复制的逐字原文。"""
        deco = sorted({m for m in _READ_DECOR_MARKERS if m in old})
        if deco:
            return ("old_string 里混有 memory_read 返回值的附加信息（"
                    + "、".join(deco) + "）——相关笔记/path 等标注不是文件内容，"
                    "锚点请只用 [正文开始]/[正文结束] 块内的文字。")

        def _norm(text: str) -> list[tuple[str, int]]:
            """每行 rstrip + 连续空行折叠为一行；返回 (行文本, 原始行号)。"""
            out, blanks = [], 0
            for idx, ln in enumerate(text.splitlines()):
                s = ln.rstrip()
                if not s:
                    blanks += 1
                    if blanks >= 2:
                        continue
                else:
                    blanks = 0
                out.append((s, idx))
            return out

        orig = content.splitlines()
        hay, needle = _norm(content), _norm(old)
        if not any(s for s, _ in needle):
            return "old_string 为空白，无法定位。"
        span = len(needle)
        hits = [i for i in range(len(hay) - span + 1)
                if [t for t, _ in hay[i:i + span]] == [t for t, _ in needle]]
        if len(hits) == 1:
            region = orig[hay[hits[0]][1]:hay[hits[0] + span - 1][1] + 1]
            for ln in region:  # 首选唯一单行锚点（实测单行逐字复制成功率最高）
                if ln.strip():
                    if content.count(ln) == 1 and len(ln.strip()) >= 12:
                        return ("old_string 与文件内容仅空白不一致（空行数量/行尾空格）。"
                                "可改用下面这行文件原文作锚点：\n" + ln)
                    break
            verbatim = "\n".join(region)
            if len(verbatim) > 600:
                verbatim = verbatim[:600] + "…（截断，请用 memory_read 核对全段）"
            return ("old_string 与文件内容仅空白不一致（空行数量/行尾空格）。"
                    "该位置逐字原文如下，请整段复制：\n" + verbatim)
        if len(hits) > 1:
            return (f"old_string 归一化空白后仍命中 {len(hits)} 处"
                    "——请扩展上下文使锚点唯一。")
        best_score, best_line = 0, ""
        for s, _ in hay:
            if not s:
                continue
            for nl, _ in needle:
                if nl:
                    r = fuzz.ratio(s, nl)
                    if r > best_score:
                        best_score, best_line = r, s
        if best_score >= 75:
            return ("old_string 与文件内容有实质差异。最接近的原文行（相似度 "
                    f"{best_score}%）：\n{best_line}\n请先 memory_read 核对实际内容再重试。")
        return "文件中无相似内容——该段可能尚不存在，请 memory_read 核对后决定改法。"

    def edit_section(self, path: str, heading: str, new_content: str,
                     identity: Identity | None = None) -> dict:
        """Replace the body of one `##`-level (or deeper) section, keeping the heading."""
        rel = self.resolve(path)
        self._require_agents_write(rel, identity)
        abs_path = self.root / rel
        content = abs_path.read_text(encoding="utf-8")
        lines = content.splitlines()

        wanted = (heading or "").strip()
        targets: list[tuple[int, int]] = []
        available: list[str] = []
        for i, line in enumerate(lines):
            m = _HEADING_RE.match(line)
            if not m:
                continue
            text = m.group(2).strip()
            available.append(text)
            if text == wanted:
                targets.append((i, len(m.group(1))))

        if not targets:
            raise StoreError(
                f"未找到小节标题 '{wanted}'（仅匹配 ## 及更深层标题，"
                "# 一级标题是笔记本身，请用 memory_edit）。\n"
                f"现有小节: {', '.join(available) if available else '（无）'}"
            )
        if len(targets) > 1:
            nos = [t[0] + 1 for t in targets]
            raise StoreError(
                f"小节标题 '{wanted}' 命中 {len(targets)} 处（行 {nos}），需要唯一。")

        idx, level = targets[0]
        end = len(lines)
        for j in range(idx + 1, len(lines)):
            m = _HEADING_RE.match(lines[j])
            if m and len(m.group(1)) <= level:
                end = j
                break

        body = (new_content or "").strip("\n")
        new_lines = lines[:idx + 1]
        if body:
            new_lines += ["", *body.splitlines()]
        if end < len(lines):
            new_lines += [""]
        new_lines += lines[end:]
        new_text = "\n".join(new_lines).rstrip("\n") + "\n"

        abs_path.write_text(new_text, encoding="utf-8")
        title = self._title_of(rel, new_text)
        self._index_note(rel, title, new_text)
        self.snapshots.commit(f"edit: {rel}")
        return {"path": rel, "heading": wanted,
                "before_hash": content_hash(content),
                "cleared_collisions": self._last_cleared_collisions}

    def save(self, path: str, content: str) -> dict:
        """Create-or-overwrite by exact path (WebUI editor, curator reports).
        Title follows the first `#` heading; index fully resynced."""
        rel = path.replace("\\", "/").strip("/")
        if not rel or ".." in rel.split("/"):
            raise StoreError(f"非法路径: {path}")
        if not rel.endswith(".md"):
            rel += ".md"
        abs_path = self.root / rel
        if not abs_path.is_file():
            # Overwriting an existing file is the editor/report fast path and
            # stays unrestricted; only *creating* a file must respect the
            # registry, otherwise save() becomes a write-gate bypass.
            self._require_covered(rel)
        old_title = self._title_of(rel) if abs_path.is_file() else None
        before_hash = (content_hash(abs_path.read_text(encoding="utf-8"))
                       if abs_path.is_file() else "")
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(content, encoding="utf-8")
        title = self._title_from_content(rel, content) or old_title
        self._index_note(rel, title, content)
        self.snapshots.commit(f"save: {rel}")
        return {"path": rel, "title": title, "before_hash": before_hash}

    def delete_note(self, path: str,
                    identity: Identity | None = None) -> dict:
        """User-instructed deletion (WebUI / memory_delete tool): remove the
        file and all index rows. The store never deletes on its own
        initiative — this is an explicit human/agent action, equivalent to
        deleting the file in Obsidian. Git history keeps it recoverable."""
        rel = self.resolve(path)
        self._require_agents_write(rel, identity)
        abs_path = self.root / rel
        title = self._title_of(rel)
        before_hash = ""
        if abs_path.exists():
            before_hash = content_hash(abs_path.read_text(encoding="utf-8"))
            abs_path.unlink()
        self.db.remove_note(rel)
        self.db.remove_collisions_involving(rel)
        if self.vectors:
            self.vectors.delete_by_path(rel)
        # 删的是最近一次审计的快照 → 联动清缓存，否则审计页加载时
        # 会对着已删除的文件报"未找到笔记"（curator 过期清理同理）
        if (self.last_audit
                and self.last_audit.get("audit", {}).get("audit_file") == rel):
            self.last_audit = None
        self.snapshots.commit(f"delete: {rel}")
        return {"path": rel, "title": title, "deleted": True,
                "before_hash": before_hash}

    # ------------------------------------------------------------------ move

    def move(self, path: str, new_path: str,
             identity: Identity | None = None) -> dict:
        old_rel = self.resolve(path)
        self._require_agents_write(old_rel, identity)
        new_rel = new_path.strip().replace("\\", "/").strip("/")
        if ".." in new_rel.split("/"):
            raise StoreError(f"目标路径不允许路径穿越: {new_path}")
        if not new_rel.endswith(".md"):
            new_rel += ".md"
        if new_rel == old_rel:
            raise StoreError("目标路径与原路径相同。")
        new_abs = self.root / new_rel
        if new_abs.exists():
            raise StoreError(f"目标已存在: {new_rel}")
        # A move that lands outside every registered topic is stray creation
        # by another name; archive_topic() lands in archive/ and is exempt.
        self._require_agents_write(new_rel, identity)
        self._require_covered(new_rel)

        old_abs = self.root / old_rel
        content = old_abs.read_text(encoding="utf-8")
        title = self._title_of(old_rel, content)

        new_abs.parent.mkdir(parents=True, exist_ok=True)
        os.rename(old_abs, new_abs)

        self.db.remove_collisions_involving(old_rel)
        if self.vectors:
            self.vectors.delete_by_path(old_rel)
        self.db.move_note(old_rel, new_rel)
        # re-index under the new path; embeddings come from vec_cache (content
        # unchanged), so this makes no embedding API calls.
        self._index_note(new_rel, title, content)
        self.snapshots.commit(f"move: {old_rel} -> {new_rel}")
        return {"old_path": old_rel, "new_path": new_rel, "title": title}

    # ------------------------------------------------------------------ list

    def list_notes(self, sub: str = "", sort: str = "name",
                   identity: Identity | None = None) -> list[str]:
        base = self.root / sub.strip("/").replace("\\", "/") if sub.strip() else self.root
        if not base.is_dir():
            raise StoreError(f"目录不存在: {sub}")
        entries = []
        for p in base.rglob("*.md"):
            rel = p.relative_to(self.root).as_posix()
            if "/.index/" in f"/{rel}" or rel.startswith(".index"):
                continue
            if not visible(rel, identity):
                continue
            mtime = p.stat().st_mtime
            entries.append((rel, mtime))
        if sort == "mtime":
            entries.sort(key=lambda e: e[1], reverse=True)
        else:
            entries.sort()
        return [e[0] for e in entries]

    # ------------------------------------------------------------------ topics

    def topics_file(self) -> Path:
        return self.root / TOPICS_FILE

    def load_topics(self) -> list[dict]:
        """Parse TOPICS.md registry: [{title, card, related[], status, registered}]."""
        p = self.topics_file()
        if not p.is_file():
            return []
        topics, cur = [], None
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.startswith("## "):
                if cur:
                    topics.append(cur)
                cur = {"title": line[3:].strip(), "card": "", "related": [],
                       "status": "", "archived": False, "registered": "",
                       "tags": []}
            elif cur is not None:
                m = _TOPIC_FIELD_RE.match(line)
                if m:
                    key, val = m.group(1), m.group(2).strip()
                    if key == "卡":
                        cur["card"] = val
                    elif key == "相关":
                        cur["related"] = [x.strip().rstrip("/")
                                          for x in val.split(",") if x.strip()]
                    elif key == "标签":
                        cur["tags"] = [x.strip() for x in val.split(",")
                                       if x.strip()]
                    elif key == "现状":
                        cur["status"] = val
                    elif key == "注册":
                        cur["registered"] = val
                    elif key == "状态":
                        cur["archived"] = "archived" in val
        if cur:
            topics.append(cur)
        return topics

    def topic_register(self, title: str, description: str = "",
                       related: str = "", card_path: str = "",
                       tags: str = "") -> dict:
        """Register a new topic: append to TOPICS.md and create the topic card.

        Called only on explicit user instruction (约定：用户明确要求时才注册).
        """
        title = (title or "").strip()
        if not title:
            raise StoreError("主题名不能为空。")
        if title in {t["title"] for t in self.load_topics()}:
            raise StoreError(f"主题已存在: {title}（如需更新请直接编辑主题卡）")

        if card_path:
            card_path = card_path.replace("\\", "/").lstrip("/")
            if not (self.root / card_path).is_file():
                raise StoreError(f"指定的主题 abstract 不存在: {card_path}")
        else:
            folder = f"topics/{_ILLEGAL_FILENAME.sub('_', title).strip('. ')}"
            card_path = f"{folder}/abstract.md"
            abs_card = self.root / card_path
            if abs_card.exists():
                raise StoreError(f"abstract 文件已存在: {card_path}")
            abs_card.parent.mkdir(parents=True, exist_ok=True)
            abs_card.write_text(f"# {title}\n\n{description or '（待补充现状）'}\n",
                                encoding="utf-8")

        from .fs_utils import content_hash as _ch  # noqa: F401 (kept for parity)
        now = datetime.now(UTC).isoformat(timespec="seconds")
        tlist = [x.strip() for x in tags.replace("，", ",").split(",") if x.strip()]
        tags_line = f"- 标签: {', '.join(tlist)}\n" if tlist else ""
        with open(self.topics_file(), "a", encoding="utf-8") as f:
            f.write(f"\n## {title}\n- 卡: {card_path}\n{tags_line}"
                    f"- 相关: {related}\n- 现状: {description}\n- 注册: {now}\n")

        # index the new/updated files so search sees them immediately
        self._index_note(card_path, title,
                         (self.root / card_path).read_text(encoding="utf-8"))
        self._index_note(TOPICS_FILE, "主题记忆注册表",
                         self.topics_file().read_text(encoding="utf-8"))
        self.snapshots.commit(f"topic: register {title} ({card_path})")
        return {"title": title, "card": card_path, "registered": now}

    def topic_unregister(self, title: str) -> dict:
        """Remove a topic from TOPICS.md (user-instructed). Notes are untouched:
        they become stray files (D4) pending an explicit follow-up decision."""
        p = self.topics_file()
        if not p.is_file():
            raise StoreError("尚无主题注册表。")
        lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
        start = None
        for i, ln in enumerate(lines):
            if ln.rstrip("\r\n") == f"## {title}":
                start = i
                break
        if start is None:
            known = "、".join(t["title"] for t in self.load_topics()) or "（空）"
            raise StoreError(f"注册表中没有主题: {title}。现有主题: {known}")
        end = len(lines)
        for j in range(start + 1, len(lines)):
            if lines[j].startswith("## "):
                end = j
                break
        removed_card = ""
        for ln in lines[start:end]:
            if ln.startswith("- 卡: "):
                removed_card = ln[len("- 卡: "):].strip()
        del lines[start:end]
        p.write_text("".join(lines), encoding="utf-8")
        self._index_note(TOPICS_FILE, "主题记忆注册表",
                         p.read_text(encoding="utf-8"))
        self.snapshots.commit(f"topic: unregister {title}")
        return {"title": title, "card": removed_card}

    # ---- 主题标签（注册表 `- 标签:` 行；轻量可逆元数据）----

    @staticmethod
    def _parse_tags(raw: str) -> list[str]:
        return [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]

    def _set_topic_tags(self, title: str, tags: list[str]) -> None:
        """改写注册表中该主题块的 `- 标签:` 行（空列表 = 整行移除），
        重索引注册表并快照。"""
        p = self.topics_file()
        lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
        start = next((i for i, ln in enumerate(lines)
                      if ln.rstrip("\r\n") == f"## {title}"), None)
        if start is None:
            raise StoreError(f"注册表中没有主题: {title}")
        end = next((i for i in range(start + 1, len(lines))
                    if lines[i].startswith("## ")), len(lines))
        block = [ln for ln in lines[start + 1:end]
                 if not re.match(r"^-\s*标签:", ln)]
        if tags:
            pos = next((k for k, ln in enumerate(block)
                        if ln.startswith("- 卡:")), -1)
            block.insert(pos + 1, f"- 标签: {', '.join(tags)}\n")
        p.write_text("".join(lines[:start + 1] + block + lines[end:]),
                     encoding="utf-8")
        self._index_note(TOPICS_FILE, "主题记忆注册表",
                         p.read_text(encoding="utf-8"))
        self.snapshots.commit(
            f"topic: tags {title} → {', '.join(tags) or '（清空）'}")

    def topic_tag(self, title: str, add: str = "", remove: str = "") -> dict:
        """为主题增删标签（幂等，轻量可逆元数据）。返回该主题标签与
        全库标签清单——引导 agent 优先复用已有标签，避免同义词蔓延。"""
        tmap = {t["title"]: t for t in self.load_topics()}
        if title not in tmap:
            known = "、".join(tmap) or "（空）"
            raise StoreError(f"注册表中没有主题: {title}。现有主题: {known}")
        add_l = self._parse_tags(add)
        rm_l = self._parse_tags(remove)
        new = [x for x in (tmap[title].get("tags") or []) if x not in rm_l]
        for a in add_l:
            if a not in new:
                new.append(a)
        self._set_topic_tags(title, new)
        all_tags = sorted({x for t in self.load_topics()
                           for x in t.get("tags") or []})
        return {"title": title, "tags": new, "all_tags": all_tags}

    def tag_rename(self, old: str, new: str) -> dict:
        """重命名标签（全库批量改写；重名等价于合并）。"""
        old, new = old.strip(), new.strip()
        if not old or not new:
            raise StoreError("标签名不能为空")
        affected = [t["title"] for t in self.load_topics()
                    if old in (t.get("tags") or [])]
        if not affected:
            raise StoreError(f"标签不存在: {old}")
        for title in affected:
            tags = next(t["tags"] for t in self.load_topics()
                        if t["title"] == title)
            nt = []
            for x in tags:
                if x == old:
                    if new not in nt:
                        nt.append(new)      # 重名 = 合并
                else:
                    if x not in nt:
                        nt.append(x)
            self._set_topic_tags(title, nt)
        return {"renamed": f"{old} → {new}", "topics": affected}

    def tag_delete(self, tag: str) -> dict:
        """删除标签（从所有主题的标签行移除，主题本身不动）。"""
        tag = tag.strip()
        if not tag:
            raise StoreError("标签名不能为空")
        affected = []
        for t in self.load_topics():
            if tag in (t.get("tags") or []):
                self._set_topic_tags(
                    t["title"], [x for x in t["tags"] if x != tag])
                affected.append(t["title"])
        if not affected:
            raise StoreError(f"标签不存在: {tag}")
        return {"deleted": tag, "topics": affected}

    def archive_topic(self, title: str) -> dict:
        """Archive a topic (user-instructed): the WHOLE topic directory moves
        under archive/<topic>/（目录即归属——只移 abstract 会把主题内其余模块
        笔记留在 topics/ 成为游离文件），registry entry gets 状态: archived
        and its 卡: path rewritten (2026-09-17 实爆：卡路径不改写 → D5 每次
        必点名). Notes stay searchable; archived topics never count as stray
        (archive/ is a free zone). Reversible by hand (git history +
        registry edit)."""
        topics = self.load_topics()
        active = [t for t in topics if not t.get("archived")]
        t = next((x for x in active if x["title"] == title), None)
        if t is None:
            known = "、".join(x["title"] for x in active) or "（空）"
            raise StoreError(f"没有活跃主题: {title}。现有主题: {known}")

        dest_dir = f"archive/{_ILLEGAL_FILENAME.sub('_', title).strip('. ')}"
        new_card = t["card"]
        if t["card"]:
            old_dir = posixpath.dirname(t["card"])
            moved = False
            if old_dir and (self.root / old_dir).is_dir():
                for f in sorted((self.root / old_dir).rglob("*.md")):
                    rel = f.relative_to(self.root).as_posix()
                    sub = f.relative_to(self.root / old_dir).as_posix()
                    self.move(rel, f"{dest_dir}/{sub}")  # move() 逐个快照
                    moved = True
                # 清掉因移动而空掉的主题目录（目录即归属，不留空壳）
                for d in sorted((self.root / old_dir).rglob("*"), reverse=True):
                    if d.is_dir():
                        with contextlib.suppress(OSError):
                            d.rmdir()
                with contextlib.suppress(OSError):
                    (self.root / old_dir).rmdir()
            elif (self.root / t["card"]).is_file():
                # 卡不在主题目录内（注册表手工指定路径）：单移卡文件
                self.move(t["card"], f"{dest_dir}/{posixpath.basename(t['card'])}")
                moved = True
            if moved:
                new_card = f"{dest_dir}/{posixpath.basename(t['card'])}"

        p = self.topics_file()
        lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
        start = None
        for i, ln in enumerate(lines):
            if ln.rstrip("\r\n") == f"## {title}":
                start = i
                break
        if start is not None:
            end = len(lines)
            for j in range(start + 1, len(lines)):
                if lines[j].startswith("## "):
                    end = j
                    break
            changed = False
            if new_card != t["card"]:
                old_line = f"- 卡: {t['card']}"
                for i in range(start + 1, end):
                    if lines[i].rstrip("\r\n") == old_line:
                        lines[i] = f"- 卡: {new_card}\n"
                        changed = True
                        break
            if not any(ln.startswith("- 状态: ") for ln in lines[start:end]):
                k = start
                while k + 1 < end and lines[k + 1].startswith("- "):
                    k += 1
                lines.insert(k + 1, "- 状态: archived\n")
                changed = True
            if changed:
                p.write_text("".join(lines), encoding="utf-8")
                self._index_note(TOPICS_FILE, "主题记忆注册表",
                                 p.read_text(encoding="utf-8"))
                self.snapshots.commit(f"topic: archive {title}")
        return {"title": title, "card": new_card, "archived": True}

    def memory_context(self, card_lines: int = 12,
                       identity: Identity | None = None) -> str:
        """Cold-start context: user profile first, then registry + active
        abstracts; identity 专属必读追加在最后（agent 层 + 本机层）。"""
        topics = self.load_topics()
        tf = self.topics_file()
        profile = self.root / PROFILE_FILE
        active = [t for t in topics if not t.get("archived")]
        identity_parts = (self._identity_context(identity)
                          if identity and identity.agent else [])
        if (not topics and not tf.is_file() and not profile.is_file()
                and not identity_parts):
            return "（尚无主题记忆——用 topic_register 注册第一个主题）"
        parts = []
        if profile.is_file():
            parts.append(profile.read_text(encoding="utf-8"))
        if tf.is_file():
            parts.append(tf.read_text(encoding="utf-8"))
        cards = []
        for t in active:
            p = self.root / t["card"] if t["card"] else None
            if t["card"] and p and p.is_file():
                head = "\n".join(p.read_text(encoding="utf-8").splitlines()[:card_lines])
                cards.append(f"### {t['title']}（{t['card']}）\n{head}")
        if cards:
            parts.append("\n# 主题摘要（abstract）\n" + "\n\n".join(cards))
        parts.extend(identity_parts)
        return "\n\n".join(parts)

    def _identity_context(self, identity: Identity) -> list[str]:
        """专属必读注入：agent 层（agents/<agent>/shared/必读.md，同 agent
        跨设备共享）+ identity 层（agents/<agent>/<device>/必读.md，本机专属）。
        必读按约定是指针型短文——全量注入，单文件 80 行兜底。"""
        parts = []
        found = False
        for label, prefix in (
                (f"{identity.agent}（同 agent 跨设备共享）", identity.shared_prefix),
                (f"{identity.agent}@{identity.device}（本机专属）", identity.device_prefix)):
            rel = f"{prefix}必读.md"
            p = self.root / rel
            if not p.is_file():
                continue
            found = True
            head = "\n".join(p.read_text(encoding="utf-8").splitlines()[:80])
            section = f"# 专属必读·{label}｜{rel}\n{head}"
            if "占位模板" in head:
                # WebUI 建 identity 时预创建的占位模板：注入时持续点名，
                # 促使 agent 尽快用 memory_edit 填写（确保"去读且去写"）
                section += ("\n⚠ 本必读仍是占位模板——请用 memory_edit 就地"
                            "填写专属纪律，填写后移除模板标记行。")
            parts.append(section)
        if not found:
            parts.append(
                "# 专属必读（尚未创建）\n"
                f"- agent 层（同 agent 跨设备共享）："
                f"memory_write(title=\"{identity.shared_prefix}必读\", …)\n"
                f"- identity 层（本机专属）："
                f"memory_write(title=\"{identity.device_prefix}必读\", …)\n"
                "- 必读只放指针与纪律，事实一律进 topics/（与 user 层共享）")
        return parts

    # -------------------------------------------------------------- profile

    def _find_profile_section(self, text: str, section: str) -> tuple[int, int] | None:
        """Span (start, end) of one ## section's body, or None if absent."""
        for m in re.finditer(r"^##\s+(.+?)\s*$", text, re.MULTILINE):
            if m.group(1).strip() == section:
                start = m.end()
                mm = re.search(r"^##\s+", text[start:], re.MULTILINE)
                end = start + mm.start() if mm else len(text)
                return start, end
        return None

    def get_preference(self, section: str = "") -> str:
        """Whole PROFILE.md or one ## section of it (memory-layer function)."""
        p = self.root / PROFILE_FILE
        if not p.is_file():
            return "（尚无用户画像/偏好记录——用 update_user_preference 建立）"
        text = p.read_text(encoding="utf-8")
        if not section.strip():
            return text
        wanted = section.strip()
        span = self._find_profile_section(text, wanted)
        if span is None:
            raise StoreError(
                f"PROFILE.md 中没有小节 '{wanted}'，可用 update_user_preference 创建")
        return text[span[0]:span[1]].strip("\n") or "（该小节为空）"

    def update_preference(self, section: str, content: str) -> dict:
        """Create-or-replace one ## section of PROFILE.md. The agent-maintained
        user profile & preferences: not a topic, never registered, first thing
        every session sees via memory_context."""
        wanted = (section or "").strip()
        if not wanted:
            raise StoreError("小节名不能为空。")
        body = (content or "").strip("\n")
        p = self.root / PROFILE_FILE
        if not p.is_file():
            text = f"# 用户画像与偏好\n\n## {wanted}\n\n{body}\n"
            p.write_text(text, encoding="utf-8")
            self._index_note(PROFILE_FILE, "用户画像与偏好", text)
            self.snapshots.commit(f"profile: init '{wanted}'")
            return {"path": PROFILE_FILE, "section": wanted, "created": True}
        text = p.read_text(encoding="utf-8")
        if self._find_profile_section(text, wanted) is not None:
            r = self.edit_section(PROFILE_FILE, wanted, body)  # snapshots "edit:"
            return {"path": PROFILE_FILE, "section": wanted,
                    "heading": r["heading"]}
        new_text = text.rstrip("\n") + f"\n\n## {wanted}\n\n{body}\n"
        p.write_text(new_text, encoding="utf-8")
        title = self._title_of(PROFILE_FILE, new_text)
        self._index_note(PROFILE_FILE, title, new_text)
        self.snapshots.commit(f"profile: add '{wanted}'")
        return {"path": PROFILE_FILE, "section": wanted, "created": True}

    # ------------------------------------------------- topic coverage (执法)

    def _path_covered(self, rel: str, topics: list[dict]) -> bool:
        """Registry coverage test shared by write-time gating and D4 stray
        detection — one semantics, two consumers. System files and free
        zones are always covered; anything else must be a registered
        topic's card/related file or live under such a file's directory
        (每主题一目录，目录即归属)."""
        if rel in (TOPICS_FILE, PROFILE_FILE) or rel.startswith(FREE_ZONES):
            return True
        covered_files, covered_dirs = set(), set()
        for t in topics:
            if t["card"]:
                covered_files.add(t["card"])
                d = posixpath.dirname(t["card"])
                if d:
                    covered_dirs.add(d)
            for r in t["related"]:
                covered_files.add(r)
                d = posixpath.dirname(r)
                if d:
                    covered_dirs.add(d)
        if rel in covered_files:
            return True
        return any(rel.startswith(d + "/") for d in covered_dirs)

    # ------------------------------------------------- identity (agents/ 执法)

    def _require_agents_write(self, rel: str, identity: Identity | None) -> None:
        """agents/ 专属区的写守卫。identity=None（人类/WebUI/服务端内部）
        不受限；identity 为空 agent（MCP 无 token，ANONYMOUS）与其他 agent /
        其他设备的专属区一律拒绝。user 层路径不经过本守卫。"""
        if not rel.startswith(AGENTS_PREFIX):
            return
        if identity is None:  # 人类入口（WebUI/内部）：全库管理员
            return
        if not identity.agent:
            raise StoreError(
                f"写入被拦截: {rel} 属于 identity 专属区（agents/），当前连接"
                "未携带 identity token。\n请在 MCP 配置里加请求头 "
                "Authorization: Bearer <device>_<agent>（stdio 用环境变量 "
                "YACMEMO_TOKEN）；user 层（topics/、journal/）不受影响。")
        if not writable(rel, identity):
            raise StoreError(
                f"写入被拦截: {rel} 不在你的 identity 专属范围内。\n"
                f"你的身份: {identity.token}——可写 {identity.shared_prefix}"
                "（同 agent 跨设备共享子树）与 "
                f"{identity.device_prefix}（本机专属）子树；"
                "agents/<agent>/ 第一层平铺文件只读兼容（历史遗留），"
                "写入请进上述两类子树；其他 agent / 其他设备的专属区互相不可见。")

    def _require_agents_visible(self, rel: str, identity: Identity | None) -> None:
        """agents/ 专属区的读守卫：其他 identity 的专属区不可见。
        identity=None（人类）恒可见；ANONYMOUS 对 agents/ 全部不可见。"""
        if visible(rel, identity):
            return
        if identity is None or not identity.agent:  # None 不会走到这（visible 恒 True）
            raise StoreError(
                f"读取被拦截: {rel} 属于 identity 专属区（agents/），当前连接"
                "未携带 identity token。配置方式见拦截消息与 docs/05。")
        raise StoreError(
            f"读取被拦截: {rel} 属于其他 identity 的专属区，互相不可见。")

    def _require_covered(self, rel: str, attempted_title: str = "") -> None:
        """Write-time enforcement of the topic registry: tool-created notes
        must belong to a registered topic. Non-bypassable — force only
        covers title conflicts. D4 audit stays as the backstop for files
        that enter the store without the tools (Obsidian hand-edits,
        unregister leftovers)."""
        if rel in (TOPICS_FILE, PROFILE_FILE):
            raise StoreError(
                f"系统文件不允许通过写入创建/覆盖: {rel}\n"
                "主题注册请用 topic_register，画像/偏好请用 update_user_preference。")
        if rel.startswith(self.config.journal_prefix):
            return
        topics = self.load_topics()
        if self._path_covered(rel, topics):
            return
        self.db.add_guard_event("uncovered", attempted_title, rel, forced=False)
        raise StoreError(self._uncovered_error(rel, topics))

    def _uncovered_error(self, rel: str, topics: list[dict]) -> str:
        """拦截消息 = 行动指引 + 近失诊断 + 完整度明确的活跃主题列表。

        2026-09-19 TeleAgent 实测的教训：写入漏了 topics/ 前缀被拦后，
        旧消息的活跃主题列表静默截断到 8 个，恰好切掉刚注册的主题，
        agent 得出"注册表未同步"的错误假设，白烧一个推理块才自纠。
        诊断行让错误从死胡同变成一步修复；列表带总数且命中主题必显示。"""
        active = [t for t in topics if not t.get("archived")]
        near, matched = self._near_miss_topics(rel, active)
        lines = [
            f"写入被拦截: {rel} 不属于任何注册主题（主题注册制硬约束，force 不豁免）。",
            *near,
            "- 新主题：先征得用户同意后 topic_register 注册"
            "（会在 topics/<主题>/abstract.md 建卡），\n"
            "  之后把笔记写入 topics/<主题>/ 目录下；\n"
            "- 已有主题：写入该主题目录下的模块笔记，如 topics/<主题>/笔记名.md；\n"
            "  abstract 是摘要卡（保持一句话现状），详细内容请写成模块笔记；\n"
            "- journal/、archive/、curator/、agents/ 免注册区不受限"
            "（agents/ 另有 identity 专属守卫）。",
        ]
        titles = [t["title"] for t in active]
        if not titles:
            lines.append("当前活跃主题: （暂无）")
        else:
            shown = list(dict.fromkeys(titles[:8] + matched))
            listed = "、".join(f"《{t}》" for t in shown)
            if len(titles) > len(shown):
                lines.append(f"当前活跃主题（共 {len(titles)} 个，"
                             f"显示与本次写入最相关者，其余略）: {listed} …")
            else:
                lines.append(f"当前活跃主题（共 {len(titles)} 个）: {listed}")
        return "\n".join(lines)

    def _near_miss_topics(self, rel: str,
                          active: list[dict]) -> tuple[list[str], list[str]]:
        """未覆盖路径的近失诊断：写入意图最可能是某个已注册主题，只是路径
        缺 topics/ 前缀或目录名拼错。返回 (诊断行, 需在活跃列表点名的标题)。"""
        first = rel.split("/", 1)[0]
        name = posixpath.splitext(posixpath.basename(rel))[0]
        by_title = {t["title"]: t for t in active}
        t = None
        if by_title:
            best = max(by_title, key=lambda k: fuzz.ratio(first, k))
            if fuzz.ratio(first, best) >= 60:
                t = by_title[best]
        lines, matched = [], []
        if t is not None and t["card"]:
            d = posixpath.dirname(t["card"])
            if d:
                if t["title"] == first:
                    lead = f"⚠ 疑似路径前缀/目录名不对：主题「{t['title']}」已注册，目录 {d}/。"
                else:
                    lead = (f"⚠ 疑似路径/目录名不对：你想写的可能是主题"
                            f"「{t['title']}」（名称最接近），其目录 {d}/。")
                lines.append(
                    lead + f"\n"
                    f"  改用 title=\"{d}/{name}\" 即可写入；abstract 是摘要卡，"
                    f"详细内容建议写成 {d}/<笔记名>。")
                matched.append(t["title"])
        return lines, matched

    def _stray_files(self, topics: list[dict]) -> list[str]:
        """Markdown files outside any registered topic (and outside free zones)."""
        strays = []
        for p in sorted(self.root.rglob("*.md")):
            rel = p.relative_to(self.root).as_posix()
            if "/.index/" in f"/{rel}" or ".git" in p.parts:
                continue
            if self._path_covered(rel, topics):
                continue
            strays.append(rel)
        return strays

    # ------------------------------------------------------------------ audit

    def audit(self) -> dict:
        resynced, missing = self._resync_stale_notes()
        added = self._sync_new_files()
        titles = self.db.all_titles()
        d1 = d1_scan(titles, self.config.guard.title_similarity_threshold)
        pruned_stale = self.db.prune_stale_collisions()
        collisions = self.db.list_collisions(status="open")

        contents = {}
        for row in titles:
            p = self.root / row["path"]
            if p.is_file():
                contents[row["path"]] = p.read_text(encoding="utf-8")
        # 主题名也算合法链接目标（卡的索引标题会随 H1 漂移，
        # [[主题名]] 指向其主题卡——与 read() 的解析链同口径）
        dangling = d3_scan(contents,
                           {r["title"] for r in titles} | set(self.topic_name_map()))

        # 缺向量笔记自愈：embedding 端点故障期间写入的笔记 vector_ok=0，
        # hash 未变，外部变更自愈不会重试——审计补位重试 embedding，
        # 端点仍不可用时保持点名（下次审计再试）
        missing_vectors = []
        if self.emb and self.vectors:
            missing_vectors = self.db.notes_missing_vectors()
            if missing_vectors:
                for row in missing_vectors:
                    content = contents.get(row["path"])
                    if content is not None:
                        self._index_note(row["path"], row["title"], content)
                missing_vectors = [r["path"]
                                   for r in self.db.notes_missing_vectors()]

        # 空白处置行自清（body 解析失败等事故产物，处置表没有删除接口）
        pruned_blank = self.db.prune_blank_audit_actions()

        topics = self.load_topics()
        stray = self._stray_files(topics)

        # 机器产物不参与 D1（归一化剥日期后标题互相近似，必然假阳性：
        # 快照标题同构、提案-0916 与 提案-0917 都归一为"提案"）。
        # agents/ 专属区同样不参与：标题是文件名语义（必读/环境……），
        # 跨 identity 同构，D1 只会产出噪音
        d1 = [c for c in d1
              if not (c["a_path"].startswith(self._machine_zones)
                      or c["b_path"].startswith(self._machine_zones))
              and not (c["a_path"].startswith(AGENTS_PREFIX)
                       or c["b_path"].startswith(AGENTS_PREFIX))]

        # 机器产物区同样不参与 D3：审计快照会引用上一轮悬空链接的原文，
        # 源笔记删除后快照自己被点名——审计追自己的尾巴（2026-09-18 实测）
        dangling = [d for d in dangling
                    if not d["path"].startswith(self._machine_zones)]

        # 人类已处置过的问题不再重放（D2 以 collisions.status 天然只列 open）
        disposed = {a["id"] for a in self.db.list_audit_actions()}
        d1 = [c for c in d1 if _d1_id(c) not in disposed]
        dangling = [c for c in dangling if _d3_id(c) not in disposed]
        stray = [p for p in stray if _d4_id(p) not in disposed]
        # D5：注册表指向不存在的 abstract（restructure/手工编辑 TOPICS.md 的遗留，
        # 2026-09-16 实例：notecalc-iced 的卡仍指向已移除的 projects/ 目录）
        dangling_cards = [_d5_id(t["title"], t["card"]) for t in topics
                          if t["card"] and not (self.root / t["card"]).is_file()
                          and _d5_id(t["title"], t["card"]) not in disposed]

        # 提案结案调和：文件已标已结案 = 人/agent 确认全部条目收口——
        # 为缺执行事件的条目补记 executed（覆盖事件机制上线前的执行、
        # 旧口径「已采纳」、以及无会话汇报通道的 agent 的手工打标）。
        # 复审失效标记的场景在 curator 追加复审节时即时撤标（见
        # curator.run_check），审计侧不重复撤标
        reconciled = self._reconcile_proposals()

        # 执行进度派生（判断与执行分离：人在 WebUI 判断，agent 经
        # memory_audit_update 汇报执行，审计只做最终验证——已执行且本轮
        # 不再报即复审通过，追加系统事件封口，下轮不再重放）
        exec_last = self.db.exec_last_status()
        current_ids = ({_d1_id(c) for c in d1}
                       | {f"D2:{c['id']}" for c in collisions}
                       | {_d3_id(c) for c in dangling}
                       | {_d4_id(p) for p in stray}
                       | set(dangling_cards))
        verified = []
        for iid, st in exec_last.items():
            # P 类（提案条目）没有确定性复审检查，复审封口只属于 D 类
            if iid.startswith("P:") or iid in current_ids or st["event"] == "verified":
                continue
            self.db.add_exec_event(iid, st["kind"], "verified",
                                   note="复审通过：本轮审计不再报告此问题")
            verified.append(iid)
        verified.sort()
        exec_status = {i: s for i, s in exec_last.items() if i in current_ids}

        # Out-of-band changes just healed (externally added/edited/deleted
        # files): snapshot them so the repo stays git-clean.
        healed = len(resynced) + len(missing) + len(added)
        if healed:
            self.snapshots.commit(
                f"external: self-healed {healed} note(s) via audit")

        # 审计快照落盘（journal/audit/ 免注册区，markdown 审计轨迹 + git 快照）
        audit_file = self._write_audit_snapshot(
            resynced, missing, added, d1, collisions, dangling, stray,
            dangling_cards, missing_vectors, pruned_blank, pruned_stale,
            verified, reconciled)

        res = {"title_duplicates": d1,
               "collisions": collisions,
               "dangling_links": dangling,
               "dangling_cards": dangling_cards,
               "resynced": resynced,
               "missing": missing,
               "added": added,
               "stray": stray,
               "missing_vectors": missing_vectors,
               "pruned_blank_actions": pruned_blank,
               "pruned_stale_collisions": pruned_stale,
               "exec_status": exec_status,
               "verified": verified,
               "reconciled_proposals": reconciled,
               "git": self.snapshots.status_line(),
               "guard_stats": self.db.guard_stats(),
               "audit_file": audit_file}
        # 最近一次审计挂在 Store 上：MCP memory_audit 与 WebUI 审计页
        # 共享同一缓存，agent 跑完审计页面立即可见（2026-09-18 之前两入口
        # 各自为政，WebUI 看不到 MCP 刚跑的结果）
        self.last_audit = {"audit": res, "ts": int(time.time())}
        return res

    # ---- audit snapshots & dispositions ----

    @property
    def _audit_dir(self) -> str:
        return f"{self.config.journal_prefix}audit/"

    @property
    def _machine_zones(self) -> tuple[str, ...]:
        """机器产物区（系统派生输出，不是记忆）：journal/audit/ 快照与 curator/
        提案报告。不参与 obs 索引（见 _index_note），不参与 D1/D2 候选——
        归一化剥日期后快照/报告标题互相近似，处置行则是伪 observation。"""
        return (self._audit_dir, "curator/")

    def _write_audit_snapshot(self, resynced, missing, added, d1, collisions,
                              dangling, stray, dangling_cards=(),
                              missing_vectors=None, pruned_blank=0,
                              pruned_stale=0, verified=(), reconciled=0) -> str:
        """每日一份审计快照（journal/audit/<YYYYMMDD>.md），同日重跑以"复审"
        小节追加进当天文件——对齐 curator 的同日合并，标题天然唯一不撞 D1，
        且 journal/audit/ 不会随审计频率无界膨胀（过期文件由 curator 清理）。"""
        body = self._audit_body(resynced, missing, added, d1, collisions,
                                dangling, stray, dangling_cards,
                                missing_vectors, pruned_blank, pruned_stale,
                                verified, reconciled)
        day = datetime.now().strftime("%Y%m%d")
        rel = f"{self._audit_dir}{day}.md"
        abs_path = self.root / rel
        if abs_path.is_file():
            old = abs_path.read_text(encoding="utf-8")
            stripped = body.strip("\n")
            review = (f"---\n\n## 复审（{datetime.now().strftime('%Y-%m-%d %H:%M')}）"
                      f"\n\n{stripped}\n")
            marker = "## 处置记录"
            if marker in old:  # 复审插在处置记录之前，处置行保持聚在文件末尾
                head, _, tail = old.partition(marker)
                content = f"{head.rstrip(chr(10))}\n\n{review}\n{marker}{tail}"
            else:
                content = f"{old.rstrip(chr(10))}\n\n{review}"
        else:
            head = (f"# 审计快照 {day}\n\n"
                    f"> 确定性审计 · {datetime.now(UTC).isoformat(timespec='seconds')} "
                    f"· git 快照: {self.snapshots.status_line()}\n")
            content = f"{head}{body}\n## 处置记录\n\n（暂无记录）\n"
        self.save(rel, content)
        return rel

    def _audit_body(self, resynced, missing, added, d1, collisions,
                    dangling, stray, dangling_cards,
                    missing_vectors=None, pruned_blank=0, pruned_stale=0,
                    verified=(), reconciled=0) -> str:
        guard = self.db.guard_stats()

        def _sec(title, items):
            # 必须返回行列表：调用处是 lines += _sec(...)，返回字符串会被
            # 逐字符拆进列表（2026-09-17 生产实爆：D5 段一字一行）
            return ["", f"## {title}", ""] + [f"- {i}" for i in items] + [""]

        lines = ["## 概览", "",
                 f"- 新增文件（已入索引）：{len(added)}",
                 f"- 外部修改（已重建索引）：{len(resynced)}",
                 f"- 外部删除（已清理索引）：{len(missing)}",
                 f"- 标题重复（D1）：{len(d1)}",
                 f"- 语义撞车（D2）：{len(collisions)}",
                 f"- 悬空链接（D3）：{len(dangling)}",
                 f"- 游离文件（D4）：{len(stray)}",
                 f"- 悬空主题卡（D5）：{len(dangling_cards)}",
                 f"- 缺向量笔记（已重试自愈）：{len(missing_vectors or [])}",
                 f"- 守卫：拒绝 {guard['refused']} / force {guard['forced']}"
                 f" / 未覆盖拦截 {guard['uncovered']}"]
        if verified:
            lines.append(f"- 复审通过（agent 已执行、本轮不再报告）：{len(verified)}")
        if reconciled:
            lines.append(f"- 提案结案补记：{reconciled} 条（文件已标结案，补记执行事件）")
        if pruned_blank:
            lines.append(f"- 已清理空白处置行：{pruned_blank}")
        if pruned_stale:
            lines.append(f"- 已自动清除过期冲突对：{pruned_stale}"
                         "（笔记已删除或重算后不再命中）")
        lines.append("")
        if added:
            lines += _sec("新增文件", added)
        if resynced:
            lines += _sec("外部修改", resynced)
        if missing:
            lines += _sec("外部删除", missing)
        if d1:
            lines += _sec("标题重复（D1）", [
                f"`{_d1_id(c)}` — `{c['a_path']}` ↔ `{c['b_path']}`"
                f"（score {c['score']}）" for c in d1])
        if collisions:
            lines += _sec("语义撞车（D2）", [
                f"`D2:{c['id']}` — `{c['a_path']}` ↔ `{c['b_path']}`"
                f"（score {c['score']}）" for c in collisions])
        if dangling:
            lines += _sec("悬空链接（D3）", [
                f"`{_d3_id(c)}` — `{c['path']}`: [[{c['link']}]]" for c in dangling])
        if stray:
            lines += _sec("游离文件（D4）", [f"`{_d4_id(p)}` — `{p}`" for p in stray])
        if dangling_cards:
            lines += _sec("悬空主题卡（D5）",
                          [f"`{cid}` — 注册表指向的 abstract 不存在" for cid in dangling_cards])
        if missing_vectors:
            lines += _sec("缺向量笔记（已重试自愈）",
                          [f"`{p}`" for p in missing_vectors])
        if verified:
            lines += _sec("复审通过（agent 已执行，本轮审计确认消除）",
                          [f"`{cid}`" for cid in verified])
        return "\n".join(lines) + "\n"

    def record_audit_action(self, audit_file: str, issue_id: str, action: str,
                            label: str, note: str = "") -> dict:
        """记录人类对某审计问题的处置（写入快照文件的处置记录 + 持久化）。

        - D2 撞车：同步 index 状态（open → resolved / dismissed），重跑不重放；
        - D1/D3/D4：写 audit_actions，重跑审计时不再列为待处理；
        - 处置行追加进当次快照文件（markdown 轨迹，git 自动快照）。
        """
        kind = issue_id.split(":", 1)[0]

        rel = audit_file.replace("\\", "/").strip("/")
        content = ""
        abs_path = self.root / rel
        if abs_path.is_file():
            content = abs_path.read_text(encoding="utf-8")
        ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
        verb = "已处理" if action == "resolved" else "已忽略"
        line = f"- [{ts}] {verb} {label}（`{issue_id}`）"
        if note:
            line += f" 备注：{note}"
        marker = "## 处置记录"
        if marker in content:
            head, _, tail = content.partition(marker)
            if "（暂无记录）" in tail:
                tail = tail.replace("（暂无记录）", "", 1)
            content = f"{head}{marker}{tail.rstrip()}\n{line}\n"
        else:
            content = content.rstrip() + f"\n\n{marker}\n{line}\n"
        # 快照先行：轨迹写盘成功后才同步 D2 状态与处置表，避免快照写失败
        # 时留下无轨迹的孤儿处置行（处置表没有删除接口，2026-09-18 实测）
        self.save(rel, content)
        if kind == "D2":
            cid = issue_id.split(":", 1)[1]
            rows = self.db.list_collisions(status=None)
            row = next((c for c in rows if c["id"] == cid), None)
            if row is not None:
                self.db.resolve_collision(
                    cid, "resolved" if action == "resolved" else "dismissed")
        self.db.record_audit_action(issue_id, kind, "", "", action, note)
        return rel

    def record_proposal_action(self, file: str, index: int, action: str,
                               type_: str = "", reason: str = "",
                               note: str = "") -> dict:
        """裁决 curator 提案条目（WebUI 人类裁决）：持久化 + 提案笔记留痕。

        - audit_actions 表记 P 类条目（id = P:<file>:<index>），前端据此
          展示裁决状态、重进页面不丢失；
        - 提案笔记追加「裁决记录」一节（git 自动快照）；执行仍由 agent
          按留痕进行——curator 铁律的延伸：系统与 WebUI 都只记录裁决，
          不直接改动任何笔记内容。
        """
        rel = file.replace("\\", "/").strip("/")
        abs_path = self.root / rel
        if not abs_path.is_file():
            raise StoreError(f"提案文件不存在: {file}")
        if index < 1:
            raise StoreError("提案条目序号非法。")
        issue_id = f"P:{file}:{index}"
        verb = "已采纳" if action == "adopted" else "已忽略"
        ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
        line = f"- [{ts}] {verb} 第{index}条 [{type_}] {reason}"
        if note:
            line += f" 备注：{note}"
        content = abs_path.read_text(encoding="utf-8")
        marker = "## 裁决记录"
        if marker in content:
            head, _, tail = content.partition(marker)
            content = f"{head}{marker}{tail.rstrip()}\n{line}\n"
        else:
            content = content.rstrip() + f"\n\n{marker}\n\n{line}\n"
        self.db.record_audit_action(issue_id, "P", "", "", action, note or reason)
        self.save(rel, content)
        if action == "dismissed":
            self._mark_proposal_settled(rel)
        return {"path": rel, "issue_id": issue_id, "action": action}

    _EXEC_EVENTS = ("executing", "progress", "executed", "blocked")
    _EXEC_KINDS = ("D1", "D2", "D3", "D4", "D5", "P")

    def audit_exec_report(self, issue_id: str, event: str,
                          note: str = "", identity: str = "") -> dict:
        """agent 汇报审计问题的执行进度（追加事件时间线，只增不改）。

        issue_id 与审计报告、WebUI 处置表共用同一命名（D3:<path>|<link> /
        P:<file>:<index> 等）；时间线在 audit_exec_events 表，复审通过由
        audit() 在问题消除时自动追加系统事件，agent 不代劳。
        """
        issue_id = (issue_id or "").strip()
        kind = issue_id.split(":", 1)[0].upper()
        if kind not in self._EXEC_KINDS or ":" not in issue_id:
            raise StoreError(
                f"issue_id 非法: {issue_id!r}——应为审计报告里的 id，"
                "形如 D3:topics/x/abstract.md|[[link]] 或 P:journal/curator/x.md:1")
        if event not in self._EXEC_EVENTS:
            raise StoreError(
                f"event 非法: {event!r}（可用：{'/'.join(self._EXEC_EVENTS)}）")
        e = self.db.add_exec_event(issue_id, kind, event, (note or "").strip(),
                                   identity or "")
        if kind == "P":
            # P 类事件可能补全结案条件——汇报后顺手检查是否全部条目已结案
            self._mark_proposal_settled(issue_id[2:].rsplit(":", 1)[0])
        return {"event": e, "timeline": self.db.list_exec_events(issue_id)}

    # ---- proposal settlement（提案全部条目执行/忽略后对 agent 隐去）----

    def _reconcile_proposals(self) -> int:
        """文件已标已结案 = 人/agent 断言全部条目收口：为缺执行事件且未被
        忽略的条目补记 executed。覆盖事件机制上线前的执行、旧口径「已采纳」、
        以及无会话汇报通道（旧客户端会话拿不到 memory_audit_update）的
        agent 的手工打标——0.3.5 承诺的"两条路都算数"。幂等。
        复审失效标记在 curator 追加复审节时即时撤标，此处不再撤。
        """
        imported = 0
        d = self.root / "curator"
        if not d.is_dir():
            return 0
        for p in sorted(d.glob("提案-*.md")):
            rel = p.relative_to(self.root).as_posix()
            try:
                content = p.read_text(encoding="utf-8")
            except OSError:
                continue
            if PROPOSAL_SETTLED_MARKER not in content:
                continue
            indices = self._proposal_findings_indices(rel)
            if not indices:
                continue
            prefix = f"P:{rel}:"
            has_event = set(self.db.exec_last_status().keys())
            dismissed = {a["id"] for a in self.db.list_audit_actions()
                         if a["id"].startswith(prefix) and a["action"] == "dismissed"}
            for i in indices:
                iid = f"{prefix}{i}"
                if iid in has_event or iid in dismissed:
                    continue
                self.db.add_exec_event(
                    iid, "P", "executed", identity="reconcile",
                    note="结案补记：提案文件已标记已结案（条目在事件机制上线前完成或经其他渠道处置）")
                imported += 1
        return imported

    def _proposal_findings_indices(self, rel: str) -> list[int]:
        """提案文件里的条目序号（1..N，跨原提案与同日复审节**连续编号**——
        P:<file>:<index> 的唯一性依赖它；复审节从头重新打印的序号不采用）。
        文件缺失或无提案节返回空表。"""
        p = self.root / rel
        if not p.is_file():
            return []
        indices, seq, in_section = [], 0, False
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.startswith("## "):
                head = line[3:].strip()
                in_section = head.startswith("提案") or head.startswith("复审")
                continue
            if in_section:
                m = re.match(r"^(\d+)\.\s+\*\*\[", line)
                if m:
                    seq += 1
                    indices.append(seq)
        return indices

    def _mark_proposal_settled(self, rel: str) -> bool:
        """全部条目已执行或忽略 → 文件头部打已结案标记（幂等，单向）。

        条目结案 = 对应 P 条目被人类忽略，或执行事件最新状态为
        executed/verified（复审封口由审计负责，P 类无自动复审）。
        历史「已采纳」行不算结案——采纳只是旧口径的派发意图，必须
        由执行事件或显式忽略收口。"""
        indices = self._proposal_findings_indices(rel)
        if not indices:
            return False
        abs_path = self.root / rel
        if not abs_path.is_file():
            return False
        content = abs_path.read_text(encoding="utf-8")
        if PROPOSAL_SETTLED_MARKER in content:
            return False
        prefix = f"P:{rel}:"
        done = {a["id"] for a in self.db.list_audit_actions()
                if a["id"].startswith(prefix) and a["action"] == "dismissed"}
        for iid, st in self.db.exec_last_status().items():
            if iid.startswith(prefix) and st["event"] in ("executed", "verified"):
                done.add(iid)
        if not all(f"{prefix}{i}" in done for i in indices):
            return False
        marker = (f"{PROPOSAL_SETTLED_MARKER}（{datetime.now(UTC).strftime('%Y-%m-%d')}）"
                  f"—— 全部 {len(indices)} 条提案已执行或忽略，agent 无需重复处理")
        lines = content.splitlines()
        if lines and lines[0].lstrip().startswith("#"):
            body = [lines[0], "", marker, *lines[1:]]
        else:
            body = [marker, *lines]
        content = "\n".join(body) + "\n"
        content = content.replace("**状态：待裁决**", "**状态：已结案**", 1)
        self.save(rel, content)
        logger.info("proposal settled: %s (%d findings)", rel, len(indices))
        return True

    def _sync_new_files(self) -> list[str]:
        """Index .md files that exist on disk but were never ingested
        (created out-of-band before the server saw them)."""
        added = []
        for p in sorted(self.root.rglob("*.md")):
            rel = p.relative_to(self.root).as_posix()
            if rel.startswith(".index") or "/.index/" in f"/{rel}":
                continue
            if self.db.get_note(rel) is not None:
                continue
            try:
                content = p.read_text(encoding="utf-8")
                title = self._title_from_content(rel, content)
                self._index_note(rel, title, content)
                added.append(rel)
            except Exception as e:
                logger.warning("audit: indexing new file %s failed: %s", rel, e)
        return added

    def _resync_stale_notes(self) -> tuple[list[str], list[str]]:
        """Self-healing: reconcile the index with out-of-band file changes.

        - externally edited (disk hash != notes.content_hash): rebuild that
          note's index entry; embeddings come from vec_cache for unchanged
          observation lines; collisions involving it are recomputed.
        - externally deleted: drop its index rows (the user's deletion is the
          source of truth; the store itself never deletes files).
        """
        resynced, missing = [], []
        for row in self.db.list_notes():
            p = self.root / row["path"]
            if not p.is_file():
                self.db.remove_note(row["path"])
                self.db.remove_collisions_involving(row["path"])
                if self.vectors:
                    self.vectors.delete_by_path(row["path"])
                missing.append(row["path"])
                continue
            content = p.read_text(encoding="utf-8")
            if content_hash(content) != row["content_hash"]:
                title = self._title_from_content(row["path"], content)
                self._index_note(row["path"], title, content)
                resynced.append(row["path"])
        return resynced, missing

    # ------------------------------------------------------------------ reindex

    def reindex(self) -> dict:
        self.db.clear_all()
        if self.vectors:
            self.vectors.wipe()
        failed = []
        count = 0
        for p in sorted(self.root.rglob("*.md")):
            rel = p.relative_to(self.root).as_posix()
            if rel.startswith(".index") or "/.index/" in f"/{rel}":
                continue
            try:
                content = p.read_text(encoding="utf-8")
                title = self._title_from_content(rel, content)
                self._index_note(rel, title, content)
                count += 1
            except Exception as e:
                failed.append({"path": rel, "error": str(e)})
        return {"indexed": count, "failed": failed}

    # ------------------------------------------------------------------ internals

    def _title_of(self, rel: str, content: str | None = None) -> str:
        row = self.db.get_note(rel)
        if row:
            return row["title"]
        c = content if content is not None else self._safe_read(rel)
        return self._title_from_content(rel, c or "")

    def _safe_read(self, rel: str) -> str | None:
        p = self.root / rel
        if p.is_file():
            return p.read_text(encoding="utf-8")
        return None

    @staticmethod
    def _title_from_content(rel: str, content: str) -> str:
        for line in content.splitlines():
            if line.startswith("# "):
                return line[2:].strip()
        return Path(rel).stem

    def _first_observation(self, rel: str) -> str | None:
        p = self.root / rel
        if not p.is_file():
            return None
        obs = parse_observations(p.read_text(encoding="utf-8"))
        return obs[0]["text"] if obs else None

    def _embed_cached(self, text: str) -> list[float]:
        if not self.emb:
            raise RuntimeError("embedding client not configured")
        chash = content_hash(text)
        cached = self.db.get_cached_vector(chash)
        if cached:
            return cached
        vec = self.emb.embed_one(text)
        self.db.put_cached_vector(chash, vec)
        return vec

    def _index_note(self, rel: str, title: str, content: str):
        """Synchronously sync every index for one note. File must be written already."""
        chash = content_hash(content)
        self.db.upsert_note(rel, title, chash)
        self.db.fts_replace(rel, title, content)

        self._last_cleared_collisions = 0
        if not (self.emb and self.vectors):
            return
        pre_open = len(self.db.collisions_for(rel))
        try:
            # Stale vectors/collisions from a previous version of this note
            # must go before re-adding (obs ids are content-addressed, so
            # changed observation lines would otherwise leave orphans).
            self.db.remove_collisions_involving(rel)
            self.vectors.delete_by_path(rel)

            note_vec = self._embed_cached(f"{title}\n{content}")
            self.vectors.upsert_note_vector(rel, f"{title}", note_vec)
            # note 级向量落库即算"不缺向量"（缺向量点名指 note 向量；
            # 两个早退分支在后面，标记必须在此之前打上）
            self.db.set_vector_ok(rel, True)

            def _settle():
                # 重索引后不再命中的旧冲突对即"自动清除"——合并型编辑的
                # 可见信号（2026-09-19 atlas 评审指出清除转换无留痕）
                self._last_cleared_collisions = max(
                    0, pre_open - len(self.db.collisions_for(rel)))

            if rel.startswith(self._machine_zones):
                # 机器产物不是记忆：处置行 "- [时间] 已处理 ..." 会被解析为
                # 伪 observation 且跨快照高度相似，入 obs 空间必然产生 D2 假阳性
                _settle()
                return
            obs_list = parse_observations(content)
            if not obs_list:
                _settle()
                return
            obs_vecs = []
            for obs in obs_list:
                vec = self._embed_cached(obs["text"])
                obs_vecs.append(vec)
                self.vectors.upsert_obs_vector(rel, obs["text"], vec)
            self._d2_check(rel, obs_list, obs_vecs)
            self.db.set_vector_ok(rel, True)
            _settle()
        except Exception as e:
            # 故障期写入的笔记标记缺向量：hash 未变，外部变更自愈不会重试，
            # 由审计补位重试（见 audit 的 missing_vectors 自愈）
            self._last_cleared_collisions = pre_open
            self.db.set_vector_ok(rel, False)
            logger.warning("Vector indexing failed for %s: %s", rel, e)

    def _d2_check(self, rel: str, obs_list: list[dict], obs_vecs: list[list[float]]):
        """Incremental D2: compare new observations against existing ones."""
        import numpy as np

        threshold = self.config.guard.collision_cosine_threshold
        topk = self.config.guard.obs_topk
        for obs, vec in zip(obs_list, obs_vecs, strict=True):
            try:
                hits = self.vectors.search_obs_vectors(vec, topk)
            except Exception as e:
                logger.warning("D2 vector search failed: %s", e)
                return
            a = np.asarray(vec, dtype=np.float32)
            a_norm = float(np.linalg.norm(a)) or 1.0
            for hit in hits:
                other_path = hit.get("source_path", "")
                if other_path == rel:
                    continue
                other_vec = np.asarray(hit.get("vector") or [], dtype=np.float32)
                if other_vec.size != a.size:
                    continue
                denom = a_norm * (float(np.linalg.norm(other_vec)) or 1.0)
                score = float(np.dot(a, other_vec) / denom)
                if score >= threshold:
                    self.db.add_collision(
                        kind="obs", a_path=other_path, b_path=rel,
                        a_text=hit.get("text", ""), b_text=obs["text"],
                        score=round(score, 3),
                    )
