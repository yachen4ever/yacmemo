> English | [简体中文](../01-architecture.md)

# yacmemo Lean Memory Layer Design (v2 Final Form)

> Design finalized 2026-09-13; updated 2026-09-14 (HTTP multi-machine deployment model, P0–P2 implemented)
> The v1 three-tier architecture design document lives in `legacy/docs/`, kept as a decision record. The remaining documents in this directory are the current v2 technical docs: 02 tool specs, 03 storage & retrieval, 04 consistency, 05 deployment, 06 evaluation, 07 WebUI console; 00 is the user manual (entry point).

---

## 1. The decision process: how we got here

### 1.1 Starting point: two pain points with OpenViking

- **Extraction blocking**: OV used a VLM to run extraction over the entire session history, synchronously blocking 90 seconds per item; writes got stuck.
- **Resource contention**: the VLM and the main model (Qwen3.8 Flash Next) fought for VRAM and bandwidth on the same M2 Ultra.

### 1.2 First-version yacmemo: right calls, overbuilt implementation

The first version's three-tier architecture (Agent writes .md → a separate small model does split extraction → a separate small model does consistency checking) contained two correct core judgments:

1. **The agent writes conclusions, not raw text** — distillation happens inside the conversation; a separate LLM should not redo extraction.
2. **Consistency needs dedicated treatment** — an agent that loses memory across sessions will not go back and fix old facts.

But the implementation had three mistakes:

- It handed the "maintainer" role to a small model with 1.3B active parameters, and let it **silently auto-invalidate** memories — wrongful kills do far more harm than missed detections;
- Layer 2's split-file mechanism spawned an entire file-protection subsystem (hash verification / conflict backups / integrity scans); the complexity went into protecting LLM-generated files;
- The docs said "real-time verification per node after extraction", but the actual implementation was "every write triggers a full scan", O(entire memory) per write.

### 1.3 Evaluating and abandoning Basic Memory

BM (basicmachines-co/basic-memory, AGPL-3.0, ~3.9k stars) shares the same philosophy as this project: markdown as source of truth, SQLite as derived index, zero LLM on the indexing path. Its key inspiration: **notes have stable addresses, and updating a fact = edit_note rewriting in place**, eliminating "multiple versions of the same fact coexisting" at the data-model level.

We then gave up on building from scratch and adopted BM + a bolt-on — and eventually gave up on that too. Reasons:

- Chinese embeddings (FastEmbed's default model) and Chinese FTS quality were questionable; once the bolt-on carried the retrieval load, BM's residual value (write-path tools + graph) shrank — **the bolt-on approach hollowed itself out**;
- Pinning versions = the sword of Damocles of frozen known bugs, docs/community drift, and breaking changes in 0.x upgrades;
- BM's `write_note` cannot be intercepted, so **consistency could only ever reach "detect + annotate", never "API-level enforcement"**;
- the merge tax of a fork-and-extend approach exceeded owning our own ~1k lines of code.

### 1.4 Conclusion: build a lightweight memory layer ourselves

Building our own is not a return to v1: it keeps only the parts proven valuable, and gains two capabilities neither BM nor BM-plus-bolt-on could deliver:

1. **Unified retrieval with Chinese as a first-class citizen**: a single hybrid search of FTS5 trigram + Qwen3-Embedding, no longer two retrieval tools of inconsistent quality;
2. **API-level consistency enforcement**: `memory_write` refuses near-duplicate titles and `memory_edit` enforces unique anchors — conventions are probabilistic; an API refusal is certain.

---

## 2. Core principles

### Principle 1: markdown files are the source of truth

SQLite (FTS/metadata/collision records) and LanceDB (vectors) are all derived indexes; delete them and they can be fully rebuilt from the files. Humans can read and edit directly, it works with git, it works with Obsidian.

### Principle 2: zero generative LLM on the memory read/write main path

The only model invocation in the store's read/write pipeline (retrieval/writes/guards/audit) is embedding (Qwen3-Embedding-0.6B, ~50ms per call, <1GB resident). All generative work — extraction, splitting, adjudication, summarization — either does not happen (structure comes from conventions) or is done by the main model inside the conversation (adjudication happens at read time). The one generative component is the **curator deep review** — but it lives outside the read/write main path: it runs offline (weekly timer), is read-only, proposes only, and never executes; the ruling belongs to the human, and anything that lands goes through the guarded store write path. This directly eliminates OV pain point 2.

### Principle 3: structure comes from conventions; consistency is enforced by the API

We do not extract structure; conventions produce structure (one note per topic; the observation/`[[链接]]` syntax is encouraged but not enforced). The consistency defenses come in three tiers of increasing strength:

```
第 1 层  API 强制（确定）    write 拒绝近重名；edit 强制锚点唯一
第 2 层  确定性检测（确定）  D1 标题重复 / D2 observation 撞车 / D3 悬空链接
第 3 层  主模型裁决（读取时）检索结果内联 ⚠ 标注，agent 顺手 edit_note 合并
```

### Principle 4: invalidation semantics over detection semantics

The system **never deletes and never hides** any memory. Both conflicting facts stay visible and both are returned, with only annotations added. Wrongful kills are architecturally impossible; git carries the true history.

### Principle 5: synchronous writes, no background compensation

The only resident processes are the yacmemo-server on the machine holding the data (HTTP, multi-user) and the weekly curator timer (read-only review + proposals written to disk, Type=oneshot). Indexing and git snapshots complete synchronously at write time (millisecond-scale); no background scans, no webhooks, no APScheduler, no queues. This directly eliminates v1's regression of "every write triggers a full scan".

(Evolution record: the initial version was stdio, started and stopped per session with no resident service; after P3 moved to HTTP multi-user, "stateless sessions, zero client-side processes" is unchanged, and the resident server became part of the deployment form.)

---

## 3. Architecture overview

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

The data model collapsed from v1's three tables (nodes/edges/events) into two levels, **notes + observations**: the note is the primary entity, and observations (when the agent uses the syntax) are fact lines inside a note, used for finer-grained retrieval and collision detection. No entity table, no edge table, no event table.

The two users' memories are fully independent: each Store binds its own root at construction time (fixed boundary; no cross-directory bleed from lazy resolution); HTTP transport uses stateless sessions, so any MCP client needs no session affinity. The 13 tools are registered once in `yacmemo/tools.py`; the stdio (`yacmemo-mcp`) and HTTP (`yacmemo-server`) entry points share the same tool surface.

---

## 4. Storage design

### 4.1 Directory conventions

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

The directory layout serves human browsing and topic-membership determination (the directory is the membership, see §13); retrieval does not depend on directories. The OV-era category directories (infra/knowledge/projects/work/preferences/people) were all merged into topic directories during the 2026-09-16 restructure and then removed.

### 4.2 Note format

- One topic per note; filename = title (Chinese allowed); first line `# 标题`;
- Free-form body, **no hard frontmatter requirement**;
- Fact lines are encouraged to use the observation syntax: `- [类别] 事实内容 #标签`;
- Cross-references are encouraged via `[[wiki-link]]`;
- Notes that do not use the syntax degrade with functionality intact: retrieval goes through note-level vectors + FTS; D2 detection gets coarser-grained but remains usable.

### 4.3 Index structure (all rebuildable)

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

`guard_events` is an addition beyond the 4 tables in the original design: force crossing a guard must be countable (the direct source of the P4 violation-rate metric), and refused events are used to tune the title-similarity threshold.

Two LanceDB vector tables (reusing the existing `vector.py`; three tables become two; the dimension stays 1024):

| Table | Content | Written when |
|---|---|---|
| `note_vectors` | Whole-note vector (title + body) | Synchronously on `memory_write` / `memory_edit` |
| `obs_vectors` | Per-observation vectors | Same as above (typically 0–10 lines per note, ~50ms/line) |

Write order: **write the file first, then update the index**. Worst case on a process crash is a lagging index (repaired by the next write or `reindex`); files are never corrupted. Rebuild procedure: delete `.index/` and call the `reindex` tool.

### 4.4 git snapshots (the history layer of the source of truth)

Every successful store change automatically produces a git commit (write/edit/edit_section/move/save/delete/topic_register/topic_unregister each have their own message format); invariant: **the memory repo is always git-clean**.

- The first write automatically runs `git init` (writes `.gitignore` ignoring `.index/`, and sets a repo-local identity); an existing identity is never overwritten;
- Identity is configurable: `[[users].git_user_name/git_user_email]` > `[memory].git_user_name/git_user_email]` > default `<id>` / `<id>@yacmemo.com`;
- External direct file edits (Obsidian/vim) are folded in by `memory_audit` self-healing under a unified `external:` snapshot;
- Degradation semantics: if git is unavailable or a call fails, only the snapshot is skipped (warning log; the `== git ==` line in audit output shows the latest failure reason) — **memory writes are never blocked**;
- Deployment caveat: systemd services have no HOME by default → git cannot read the global gitconfig's safe.directory exemption → dubious ownership degrades silently (stepped on in practice on 2026-09-16); the unit needs `Environment=HOME=/root`, and the code additionally has a pwd backfill as a safety net;
- No remote: the memory repo is purely local; remote backup (private remote / periodic bundle) is listed as future functionality.

### 4.5 Identity tiers and exclusive memory (2026-09-24)

MCP requests may carry an identity token (`Authorization: Bearer <device>_<agent>`, e.g. `r9000x_teleagent`; stdio uses the `YACMEMO_TOKEN` env var) that states "which agent on which machine". The token is a deterministic concatenation with no registration or storage; one user can mount multiple identities:

- **User tier (shared)**: `topics/`, `journal/`, `TOPICS.md`, `PROFILE.md` — fully shared by all identities;
- **Agent tier**: the `agents/<agent>/shared/` subtree — shared across that agent's devices (role discipline, must-reads);
- **Identity tier**: the `agents/<agent>/<device>/` subtree — visible only to that identity (per-machine environment, device differences).

Exclusive isolation is enforced server-side (`visible`/`writable` in `identity.py` are the single source of truth, shared by store/search/tools): reads, retrieval (scoped search), listing and writes are all filtered, and `memory_context` automatically injects the agent-tier and device-tier `必读.md` at cold start. Conventions: the first level under `agents/<agent>/` holds only two kinds of subdirectories — `shared/` and `<device>/` (`shared` is a reserved name and cannot be used as a device name), which keeps visibility a pure string predicate; legacy flat files are read-only, and all writes go into the two subtrees; must-reads hold pointers and discipline only, facts go into `topics/` to be shared with every agent. Legacy setups without a token keep working on the user tier; the `agents/` zone is invisible and unwritable for them. The WebUI is the human-administrator view (sees everything); its "Identities" page lists identities and mints tokens, and the WebUI's own access password is `[webui].password`.

---

## 5. Retrieval design

### 5.1 Hybrid retrieval

`memory_search` by default recalls from two channels and fuses with RRF:

```
score(d) = Σ_channels 1 / (rrf_k + rank_channel(d))    # rrf_k = 60
通道 A：FTS5 trigram 全文检索（BM25 排序）
通道 B：note_vectors 余弦近邻（Qwen3-Embedding）
```

RRF uses only ranks, never scores, avoiding score-scale alignment problems between the two channels. The `kind` parameter can force a single channel (`fts` / `vector`), for comparative evaluation in the P4 phase.

### 5.2 Chinese-specific details (the first validation point)

- The trigram tokenizer requires SQLite ≥ 3.34 and matches on substrings of ≥3 characters; **2-character short queries fall through on the FTS channel** and are caught by the vector channel (short queries are exactly the vector channel's strength);
- If P0 testing shows trigram recall is insufficient for real Chinese queries, the upgrade path: segment with jieba and write into an FTS shadow column (`fts_seg`), searching both columns simultaneously;
- Case-insensitivity follows the trigram default configuration: no effect on Chinese, a benefit for English titles.

### 5.3 Inline collision annotation

In `memory_search` results, whenever a hit note is involved in a status=open record in the `collisions` table, the following is appended inline to the result row:

```
1. yacmemo部署配置 (score 0.91)
   "服务端口为 9721，LLM 指向 m2ultra:11234"
   ⚠ 与 [[yacmemo部署记录0910]] 疑似重复 — 建议读两篇后用 memory_edit 合并
```

**The moment of reading is the moment of repair**: once the agent merges, the conflict pair naturally disappears (both sides' content hashes change, and stale records are cleared).

### 5.4 1-hop associations

Beyond the body, `memory_read` also returns "related notes": `[[链接]]` targets in the body (title + first observation) plus the top-2 nearest neighbors of the note's vector. Single-hop traversal is ~50 lines of code and covers 90% of graph needs; if multi-hop traversal ever becomes a real requirement, evaluate plugging in an off-the-shelf product then (that would be the right time for graph-database-style tools to enter).

---

## 6. MCP tool surface (17 tools)

| Tool | Signature | Key behavior |
|---|---|---|
| `memory_search` | `query, limit=10, kind="hybrid"\|"fts"\|"vector"` | Two-channel RRF fusion + inline collision annotation; queries under 3 characters fall back to LIKE; a degraded-mode notice is attached when the vector channel fails |
| `memory_read` | `path_or_title` | Body + 1-hop related notes |
| `memory_write` | `title, content, force=false, force_confirm=false` | **Topic hard block** (paths not covered by any registered topic are refused; force does not exempt, see 6.1) + **near-duplicate title block** (with two-level force confirmation); indexing runs synchronously on write |
| `memory_edit` | `path, old_string, new_string` | **Anchor uniqueness enforcement**: not found / multiple matches → refuse and list candidate locations |
| `memory_edit_section` | `path, heading, new_content` | Replace a whole section by its `##` heading |
| `memory_move` | `path, new_path` | Move + store-wide index updated to follow the path ([[链接]] resolves by title; moving does not change titles, so links need no rewriting); **the target path is subject to the same topic hard block** (moves into registry-free zones are allowed) |
| `memory_delete` | `path` | **Call only when the user explicitly asks**; deletes the file + all its index rows; the git snapshot preserves history; when the deleted note is the most recent audit snapshot, the last_audit cache is cleared in tandem |
| `memory_audit` | — | Self-healing (hash-level recomputation and cleanup for external modifications/deletions; retries embedding for notes missing vectors) + D1/D3/D4/D5 scans + collisions report + guard statistics + self-cleanup of blank disposition lines + git snapshot status line |
| `memory_list` | `path="", sort="name"\|"mtime"` | Directory tree / recent changes |
| `memory_context` | — | **Call first at session start**: PROFILE front-loaded + registry + abstract summary headers of active topics (cold-start recap) |
| `topic_list` | `tag` | List active/archived topics (grouped, with tags; filterable by tag) |
| `topic_register` | `title, description, related` | Registers a new topic (**only when the user explicitly asks**), creating topics/<主题>/abstract.md |
| `topic_tag` | `title, add, remove` | Add/remove topic tags (lightweight reversible; response carries the full tag inventory) |
| `topic_unregister` | `title` | Unregisters a topic (**only when the user explicitly asks**; only removed from the registry, notes untouched, adjudicated once stray) |
| `archive_topic` | `title` | Archives a topic (**only when the user explicitly asks**): the entire topic directory moves into archive/ (card paths rewritten in sync); retrieval keeps working, context no longer injects it |
| `get_user_preference` | `section=""` | Reads the full profile & preferences or a specified section (PROFILE.md feature layer) |
| `update_user_preference` | `section, content` | Creates/replaces one section of the profile & preferences (maintained by the agent) |

Full specifications: [02-mcp-tools.md](02-mcp-tools.md).

### 6.1 Write-path guards (the consistency core of this design)

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

- Refusals are **deterministic behavior**, not dependent on model self-restraint; `force=true` is the model's only channel for explicitly crossing the **title guard**;
- **The topic hard block cannot be crossed**: `force` only applies to near-duplicate title conflicts — "a write must belong to a topic" is a structural constraint of the registry; allowing force to bypass it would punch through the registry itself (`_path_covered` and D4 stray detection share the same coverage test, so both consumers always use identical criteria);
- **store.save (WebUI editor / curator reports / audit snapshots) blocks only new files**: overwriting existing files is unrestricted, preventing save from becoming a bypass; `memory_move`'s target path is under the same constraint (archive_topic's move into archive/ goes through the registry-free-zone exemption);
- **Two-level force confirmation**: once forced events within 24 hours reach `force_confirm_threshold` (default 3), bare `force=true` is refused and `force_confirm=true` must be passed as well (explicit human-confirmation semantics); the refusal lists candidate existing notes, and the whole process is countable;
- **The number of force invocations is the countable metric of the convention-violation rate** (the core P4 measurement), summarized in audit reports; uncovered blocks likewise enter guard statistics;
- The journal/ directory is not subject to blocking;
- The threshold `title_similarity_threshold` (default 0.85) is configurable; refusal events are logged in full for threshold tuning.

### 6.2 Index and snapshot synchrony

All write tools (write/edit/edit_section/move/delete) complete synchronously before returning success: file write → hash → embedding (only changed parts) → FTS/vector/collision-table updates → git snapshot. Total overhead per call < 300ms (typical note). No lazy indexing, no background compensation.

When the embedding endpoint fails, writes **do not fail**: the file and FTS land normally; missing vectors are flagged via `notes.vector_ok=0` (added 2026-09-18); `memory_audit` names them and retries embedding to self-heal — once the endpoint recovers, the next audit converges automatically.

---

## 7. Consistency mechanisms in detail

### 7.1 D1: title/topic duplication (write-time blocking + audit backstop)

- At write time: blocked by the 6.1 guards;
- Audit backstop: full pairwise comparison of normalized titles over the stock of existing notes (written before the guards went live, or forced through);
- Shared tags are compared too (≥2 common tags with title similarity ≥ 0.7 are also listed as candidates).

### 7.2 D2: observation semantic collisions (incremental detection at write time)

```
memory_write / memory_edit 完成 embedding 后：
    for obs_vector in 新写入的 obs_vectors:
        top-k = obs_vectors.search(obs_vector, k=5)     # 排除同 path
        for hit in top-k where cosine >= 0.86:
            insert collisions(kind="obs", a, b, score, status="open")
```

- The threshold is configurable (`collision_cosine_threshold`, default 0.86);
- **Deliberately does not judge whether the two contradict, nor adjudicate which one is valid** — it only flags "suspected to be talking about the same thing";
- **Machine-generated zones stay out of the obs space**: journal/audit/ audit snapshots and curator/ proposal reports are system-derived outputs (disposition lines like `- [时间] ...` are pseudo-observations, isomorphic once dates are stripped from titles); they do not participate in obs indexing or D1/D2 candidacy — the system's own outputs must not create consistency noise (see 04-consistency.md for details);
- **Stale cleanup and self-healing**: `memory_audit` compares on-disk file hashes against `notes.content_hash` — externally modified notes are re-indexed automatically (embedding goes through vec_cache; zero calls for unchanged rows) and their collisions are recomputed accordingly; externally deleted notes are purged from all indexes and listed in the `missing` report. Audit is self-healing, with no background process;
- Limitation (accepted): logically contradictory pairs that are lexically far apart will not be caught — the residual risk is covered by tier 3 (the main model judges when retrieval surfaces a suspicious pair); no infrastructure is built for this.

### 7.3 D3: dangling references

A list of `[[链接]]` pointing to nonexistent notes, output by audit. `memory_move`'s link rewriting prevents most of them; the rest get fixed by the agent in passing.

### 7.4 Violation-rate metrics (P4 measurement)

| Metric | Source | Meaning |
|---|---|---|
| force usage rate | write-tool logs | how often the model crosses the guards = how often conventions fail |
| D1/D2 hit and false-positive counts | the collisions table | detector quality, used to tune thresholds |
| Retrieval hit rate | a personal eval set of 20 real queries | share of target notes within top-3, compared per channel |
| Merge action count | changes in collisions status between audits | whether the repair loop is actually turning |

---

## 8. Agent usage conventions (paste into any agent's system prompt)

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

The conventions still go into the prompt (items 1, 2 and 4 reduce wasted round-trips), but the system no longer **relies** on the model honoring them — guards and detectors provide the backstop. This is the essential difference between this design and v1.

---

## 9. Deployment and multi-user

**One service, all machines, all agents**. yacmemo-server runs on debsvc, where the data lives, exposing MCP over streamable HTTP; any MCP client on any machine only needs to add a URL (`http://debsvc.local:9721/{user}/mcp`) — zero client installation, zero client processes. User isolation = URL path = disk directory, with boundaries fixed at construction time. Deployment details (systemd, per-client configuration examples, backup) are in `05-deployment.md`.

- git: one repo per memory_root, `.index/` goes into `.gitignore`; every change auto-snapshots (see 4.4); git carries the factual history and the repo is always git-clean;
- Optional: Syncthing syncs the memory directory to a Mac for browsing in Obsidian;
- Dependencies: `mcp` (FastMCP, pinned to `<2`; the 2.x MCPServer migration is listed as an evaluation item), `lancedb`, `pyarrow`, `numpy`, `rapidfuzz`, `httpx`, optional `jieba`. The server is a single process; the client has zero dependencies.

Example configuration:

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

## 10. Where the existing code assets go

| Existing file | Where it goes |
|---|---|
| `embedding.py` | Reused as-is |
| `vector.py` | Reused with rework: node/event/edge three tables → note/obs two tables |
| `fs_utils.py` | Reuses `content_hash`/`file_hash`; CRUD rewritten as `store.py` (with guards) |
| `consistency.py` | The first half (embed → vector search → distance filter) is rewritten as D2 in `detectors.py`; LLM adjudication and auto-invalidation are retired |
| `mcp_server.py` | Skeleton template (FastMCP structure, startup parameters) |
| `db.py` | Retired; `index_db.py` rewritten (4 tables) |
| `extractor.py`, `enhancer.py`, `webui/`, most of `config.py` | Retired and removed from the runtime path (code may be kept on file) |
| `docs/01–05` | Kept as decision records |

Estimated 600–800 lines of net-new code (including tests); roughly 400 lines reused.

---

## 11. Phased rollout

> **Implementation status (2026-09-14)**: P0–P2 implemented and committed (51 tests passing; three-channel baseline fts 6/10 → hybrid 9/10; HTTP multi-user loopback test passed). P3 (deployment + the conventions block going into agent system prompts) and P4 (two weeks of real-world testing) remain to be executed.

| Phase | Content | Effort | Acceptance criteria |
|---|---|---|---|
| P0 | Repo transition (old modules moved out of the runtime path), skeleton, **real-world trigram Chinese testing** | 0.5 day | Record recall baselines for the fts/vector/hybrid three channels using 10 real Chinese queries |
| P1 | `search/read/write/edit` + synchronous indexing | 1–2 days | Writes < 300ms; hybrid ≥ best single channel on the eval set |
| P2 | Guard completion (two-level force confirmation), `edit_section/move`, D1–D3, `audit` self-healing, collision annotation | 1 day | All 5 planted duplicate/contradiction sample groups blocked or flagged, false positives ≤ 2 |
| P3 | Deploy yacmemo-server + the conventions block into each agent's system prompt | 0.5 day | One full day of isolated multi-machine, multi-client operation with no data bleed |
| P4 | Two weeks of real-world testing | — | All metrics from 7.4 produced; thresholds tuned from the data |

Each phase can be rolled back independently: the system is already usable after P1 (without guards); the guards are purely additive.

---

## 12. Risks and open questions

| # | Risk | Mitigation |
|---|---|---|
| 1 | trigram misses 2-character Chinese short queries | The vector channel catches them; decide whether to enable the jieba shadow column after P0 testing |
| 2 | The model ignores conventions, high-frequency force | force is an explicit, conspicuous action and its count is measurable; beyond the threshold it tightens: force requires human confirmation (MCP returns a pending-confirmation marker) |
| 3 | Low adoption of the observation syntax | D2 automatically degrades to note-level vector comparison (coarser but usable); P4 measures the adoption rate |
| 4 | Title normalization blocks wrongly (two genuinely distinct topics with similar names) | The force channel + refusal-event logs drive threshold tuning |
| 5 | `[[链接]]` rewriting supports exact titles only | No alias support is a known simplification; unrewritten references are reported by audit |
| 6 | D2 misses logically contradictory pairs that are lexically distant | Accepted residual risk; the main model adjudicates at read time; no full-scale LLM scanning (the v1 lesson) |
| 7 | Consistency between the dual indexes (FTS/LanceDB) and the files | File-first-then-index write order + fully rebuildable indexes; worst case degrades without corruption |

---

## 13. Topic registry and quality curation (added 2026-09-15)

> Background: after the historical memory migration we found things "sprawling and without hierarchy" — all notes were peers in the system, cold starts began in amnesia, and clusters of same-topic snapshots were only discovered by human accident. The solution is to turn "hierarchy" from **retrieval-ranking luck** into **explicitly declared structure**.

### Topic registry (directory-based rework, 2026-09-16)

- **Topics are declared explicitly by the user** ("add X to long-term memory"), and the agent registers them by calling `topic_register` — the tool call is the voucher of user authorization; in normal operation the agent can only propose, never register on its own; registration is also a **precondition for writing** (the 6.1 topic hard block: paths not covered by any registered topic are always refused; force does not exempt);
- **One directory per topic**: `abstract.md` (the current-state handbook, agent-maintained and updated in place) + module md files for detailed memories within the topic (the agent may add them as needed) — the directory is the membership, replacing hand-maintained path lists in the registry;
- TOPICS.md is both registry and directory; **topic lifecycle**: register → active (context injects summaries) → **archive** (`archive_topic`: the registry gains a `状态: archived` entry and the card path is rewritten; the entire topic directory moves into archive/; retrieval still works, context no longer injects it, and it no longer counts as stray) → unregister (topic_unregister: only removed from the registry; notes become strays and go through D4 adjudication) — no silent data loss at any point;
- **Profile & preferences are a memory-layer feature, not a topic**: `PROFILE.md` is a single file divided into sections, maintained by the agent via `get_user_preference` / `update_user_preference` (metadata and domain knowledge are layered: the former is "how to collaborate with the user", the latter is "what to know"); `memory_context` injects PROFILE up front;
- **Stray-file detection (D4) as double insurance**: the write path is already hard-blocked (the tool surface cannot create strays), so D4 becomes the backstop — it covers strays from outside the tool surface, such as files hand-created in Obsidian or leftovers after unregistration (registry-free zones: journal/, archive/, curator/; TOPICS.md/PROFILE.md are exempt);
- Design stance: **hierarchy is declared, not computed** — no importance scoring / decay functions / automatic summarization.

### curator quality curation

- `yacmemo-curator` CLI + systemd timer (default: Saturdays 04:00); the LLM uses the main-model endpoint (the `[curator]` config section);
- Flow: read registry + topic cards + audit results → LLM review → a **proposal report note** (`curator/提案-<日期>.md`, status "待裁决" (pending adjudication)); same-day reruns create no new file — results are appended to that day's report as a "复审（HH:MM）" (re-review, HH:MM) section (titles stay unique per day, so D1 is not triggered);
- Review dimensions: duplicate / outdated / stray / stale-card / merge / forget;
- **Iron rule: propose only, never execute** — the final form of the v1 lesson about "auto-invalidation without asking": the maintainer LLM is back, but stripped of all write power (the sole exception: incidentally cleaning up expired journal/audit/ audit snapshots per `audit_retention_days` (default 7 days) — dispositions live in the audit_actions table and the full history in git; snapshot files are merely a view of the recent working set);
- Approved proposals are executed by the agent or a human, and execution leaves a record in the report note.

### Tool surface (17 tools in total)

The topic-lifecycle quartet `topic_list` / `topic_register` / `topic_unregister` / `archive_topic` plus the cold-start `memory_context`, and the profile & preferences pair `get_user_preference` / `update_user_preference`; specifications in [02-mcp-tools.md](02-mcp-tools.md). Unregistering only removes from the registry and never touches notes (afterwards the notes become stray files, named by D4 for adjudication); archiving keeps retrieval working but withdraws context injection — guaranteeing no silent data loss across the whole topic lifecycle.

## 14. Rejected alternatives (decision record)

| Alternative | Rejection reason |
|---|---|
| Continue using OpenViking | Extraction blocks 90s per item; the VLM and the main model fight for resources |
| First-version yacmemo (three-tier) | Small-model auto-invalidation silently killed good memories; inverted complexity of the split-file protection subsystem; every write triggered a full scan |
| Mem0 / Letta / Graphiti / Cognee / MemOS | Extraction paths require a generative LLM / graph database / resident service stack, violating core principles 2 and 5; see the 2026-09 research notes for details |
| Adopting Basic Memory directly | Version-pinning risk; weak Chinese embeddings/FTS; write_note cannot be intercepted, so consistency never reaches the API-enforcement layer |
| BM + yacmemo bolt-on | Once the bolt-on carried retrieval, BM's residual value shrank; two retrieval tools of inconsistent quality; the write path still cannot be guarded |

## 15. Non-goals and design topics (added 2026-09-19)

The independent review of this project by agent-memory-atlas (analyzing commit 2440aa5 with a 7-item mechanism rubric) offered an outside perspective to check against: some "missing pieces" are deliberate trade-offs (non-goals), others are genuine gaps (design topics). Each is spelled out below so they are not repeatedly raised as oversights later.

### Non-goals (deliberately not doing)

- **Explicit trust-state gating (trust state)**: no discrete states like "credible / unverified" on notes/observation lines, and no trust-based filtering of retrieval results. Contradictions are presented with both sides shown via ⚠, and judgment is left to the main model at read time — "never hide any memory" (§2) is at odds with trust gating;
- **Bi-temporal validity (bi-temporal)**: no tracking of "the span during which a fact was true vs. when the system recorded it". In a single-user setting, "updating the abstract in place when the current state changes, plus the immediacy of observation lines" already covers the need; a metadata tax on every fact for this would be disproportionate;
- **Retrieval scope (scope) guaranteed by construction; no in-store scope key**: each user gets an independent store (§9); isolation is done at the mount layer and the read path needs no filter. The review rubric's "Scope enforced" verdict of "—" is a definitional mismatch (it only recognizes one implementation form: an in-store scope key used as a read-path filter).

### Threat model note

Both git snapshots and usage.db live locally on the deployment machine: **anyone holding the memory directory or server filesystem permissions can rewrite git history**. In a single-person homelab (memory owner = server admin) this is an acceptable trade-off — the system defends against "the agent writing/deleting wrongly", not against "a malicious server admin". Mutually distrusting multi-user setups should use different memory roots (per-user independent store + independent git repo) rather than expecting git to act as tamper-proof auditing.

### Design topics (to be explored in 0.2.x, discussion welcome)

- **Rejected-value tombstones keyed by "value"** (a genuine gap identified by the review rubric): when the user explicitly says "X is wrong / stop recording X", the rejected value is registered as a system record (key = hash of the normalized value, with the original text, time, and source); from then on, observation lines parsed by `memory_write` / `memory_edit` that hit a tombstone value are blocked (force does not exempt); lifting a tombstone requires explicit user action (via the WebUI disposition surface). Open questions: keying granularity (exact value / line level / note level); the relationship with the existing title-key guard (near-duplicate titles are already guarded; the value key covers "rewriting the same wrong value under a different title"); tombstones only block "value recurrence", not unrelated new notes.
| Forking BM to extend it | The merge tax of a tens-of-thousands-line 0.x codebase > owning our own 1k lines |
