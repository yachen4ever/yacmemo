> English | [简体中文](../02-mcp-tools.md)

# MCP Tool Specifications (17 tools)

> Applicable transports: stdio (`yacmemo-mcp`) and HTTP (`yacmemo-server`); the tool surface is identical.
> All tools return human-readable text; errors are returned directly as Chinese messages (no protocol errors are thrown), readable and self-correctable by the agent.

## General rules

- **Path semantics**: all `path` parameters accept a path relative to memory_root or a note title (exact title match; see `memory_read`'s resolution order).
- **Synchronous indexing**: before returning success, all write tools complete file write → hash → embedding → FTS/vector/collision-table updates; a single call typically costs < 300ms. Success returned means the index is available.
- **git snapshots**: every write/delete/topic operation automatically produces a git commit after success (in `{tool}: {path}` format), so the memory repo is always git-clean; the first write automatically runs `git init` (including the `.index/` ignore and a repo-local identity); when git is unavailable it degrades to write-without-snapshot and never blocks memory functionality. External changes made by directly editing files (Obsidian/vim) are snapshotted together when `memory_audit` self-heals (with the `external:` prefix). The commit identity resolves as `[[users].git_user_name/email]` > `[memory].git_user_name/email]` > default `<id>` / `<id>@yacmemo.com`; an existing repo identity is never overwritten; the most recent snapshot failure is shown on the `== git ==` line of the `memory_audit` output (eliminating silent degradation).
- **Failure semantics**: files always come first. If embedding fails, content is still written and FTS is still updated; only the vector/D2 side is missing (backfilled by the next write or by `memory_audit` self-healing).
- **Guard rejections are normal returns** (not errors): the agent should read the rejection message and switch to the suggested tool.

## 0. Identity and exclusive memory

An MCP request may carry an **identity token** stating "which agent on which machine":

- HTTP: request header `Authorization: Bearer <device>_<agent>` (e.g. `r9000x_teleagent`; the `X-Yacmemo-Token` header also works);
- stdio: environment variable `YACMEMO_TOKEN=<device>_<agent>`.

The token is a deterministic concatenation (`<device>_<agent>`; lowercase letters/digits/dashes, no underscores) with no registration or storage; the WebUI "Identities" page validates names and generates per-client config snippets. The tier model:

| Tier | Paths | Visibility | Purpose |
|---|---|---|---|
| user tier | `topics/`, `journal/`, `TOPICS.md`, `PROFILE.md`, ... | shared by all identities | topic memory, profile, running logs |
| agent tier | `agents/<agent>/shared/` subtree | shared across that agent's devices | role discipline, must-reads |
| identity tier | `agents/<agent>/<device>/` subtree | this identity only | per-machine environment, device differences |

- **Exclusive isolation is server-enforced**: reads, retrieval, listing and writes are all filtered end to end — an identity sees only the user tier + its own agent tier + its own device subtree; other identities' exclusive zones are invisible and unwritable;
- **Scoped search**: `memory_search` returns only the user tier + this identity's exclusive zone;
- **memory_context auto-injection**: with a token, the agent-tier `agents/<agent>/shared/必读.md` and device-tier `agents/<agent>/<device>/必读.md` sections are appended (a write template is provided when they do not exist yet; a placeholder stub keeps prompting until filled);
- **Legacy setups without a token keep working** on the user tier, but the `agents/` zone is invisible and unwritable (writes are intercepted with configuration guidance); a misspelled token is rejected as an invalid token with the convention spelled out;
- Write convention: **must-reads hold pointers and discipline only — facts always go into `topics/`** to be shared with every agent; the first level under `agents/<agent>/` holds only two kinds of subdirectories — `shared/` and `<device>/` (legacy flat files are read-only; all writes go into the two subtrees). When referencing another tier's file, substitute the real device name (`agents/teleagent/r9000x/必读.md`); template placeholders always use angle brackets (`agents/<agent>/<device>/…`) and empty segments are forbidden — `agents/teleagent//必读.md` is treated as a literal path and retrieval will always fail.

## 1. memory_search

```
memory_search(query: str, limit: int = 10, kind: str = "hybrid") -> str
```

Dual-channel retrieval + Reciprocal Rank Fusion (k=60, using only ranks, never scores):

| Channel | Mechanism | Best at |
|---|---|---|
| fts | SQLite FTS5 trigram, BM25 ranking | Keyword-style queries (literal substring matching, tokens ≥ 3 chars) |
| vector | Qwen3-Embedding nearest notes (cosine/L2) | Natural sentences, paraphrases |

- `kind="fts"` / `"vector"` forces a single channel (for evaluation); the default is hybrid.
- **Query wording advice**: feed the FTS channel keywords ("port 9721", "restic backup"), and leave natural sentences to the vector channel. For mixed queries ("yacmemo port"), both channels work at once.
- **Short-query fallback**: queries shorter than 3 characters cannot be hit by trigram; the tool automatically falls back to a LIKE substring scan; when LIKE also misses, it outputs the hint "please switch to longer keywords".
- **Degradation notice**: when the vector channel fails, the tool outputs `⚠ vector channel unavailable (reason); this round's results are FTS only` — "no relevant notes found" is no longer treated as an authoritative conclusion (the silent-degradation problem observed in the real 2026-09-18 endpoint outage).
- **Settled proposals hidden** (added 2026-09-25): once every finding of a curator/ proposal report is executed or dismissed, a `> 状态：已结案` (settled) marker is stamped at the top of the file and search results no longer return it by default (with the notice "N settled proposal(s) hidden"); explicit `memory_read` still works — that is deliberate lookup. Never execute findings from a settled proposal.

Return format:

```
1. yacmemo部署配置 (score 0.0316, fts+vector)
   path: projects/yacmemo部署配置.md
   ⚠ Suspected duplicate with [[yacmemo部署记录0910]] (score 0.91) — suggested to read both notes and then merge with memory_edit. Other note's content: the service port is 8080
```

**Convention for handling ⚠ annotations**: first `memory_read` both notes → merge with `memory_edit` → then answer the user. Once merged, the collision pair disappears automatically (the content-hash change triggers recomputation).

## 2. memory_read

```
memory_read(path_or_title: str) -> str
```

Resolution order: ① exact existence as a relative path → ② exact match on note title → ③ append the `.md` suffix and retry as a path.

Return = `[正文开始 | path | 锚点提示]` + **verbatim body** + `[正文结束]` + related notes:

- The text inside the body block is the file's original text — `memory_edit`'s `old_string` must be **copied verbatim** from it; do not retype from memory;
- The "related notes" after `[正文结束]` are **tool-appended information, not file content** (no longer uses `##` heading syntax, to avoid being mistaken for note sections):
  - For `[[wiki-link]]` targets that exist: lists the title + the other note's first observation (`via: link`);
  - For missing targets: annotated "target does not exist" (the agent can create or clean it up in passing);
  - Semantic nearest neighbors top-2 (`via: vector`, requires an embedding endpoint).

## 3. memory_write

```
memory_write(title: str, content: str, force: bool = False,
             force_confirm: bool = False) -> str
```

Creates a new note. `title` may include a directory prefix; **within a topic directory, write as `topics/<topic>/<note-name>`** (a missing `topics/` prefix is hard-blocked by the topic registry, and the block message provides the corrected title). The directory is only for archiving; **the note's title is the topic name with the directory stripped**. Filenames are sanitized by replacing illegal characters (`\ / : * ? " < > |`).

**Topic hard block** (added 2026-09-17; force is not exempt): the write path must be covered by some registered topic — i.e. located under the directory containing a registered topic's card/related notes (one directory per topic; the directory is the membership) — otherwise refused:

```
Write blocked: 女儿AI陪伴老师/abstract.md does not belong to any registered topic (hard constraint of the topic registry; force is not exempt).
⚠ Suspected wrong path prefix/directory name: topic "女儿AI陪伴老师" is registered; its directory is topics/女儿AI陪伴老师/.
  Use title="topics/女儿AI陪伴老师/abstract" and the write goes through; abstract is the summary card,
  for detailed content it is recommended to write topics/女儿AI陪伴老师/<note-name>.
- New topic: after getting the user's consent, register with topic_register (which creates the card at topics/<topic>/abstract.md),
  then write notes under the topics/<topic>/ directory;
- Existing topic: write module notes under that topic's directory, e.g. topics/<topic>/<note-name>.md;
  abstract is the summary card (keep it a one-sentence status); write detailed content as module notes;
- journal/, archive/, curator/ and agents/ are registry-free zones and unrestricted (agents/ additionally has the identity-exclusive guard).
Currently active topics (9 in total): 《……》
```

**Near-miss diagnosis** (added 2026-09-19): before blocking, the first segment of the write path is compared exactly/fuzzily against registered topic names; on a hit, the error directly provides a retryable title — errors like a missing `topics/` prefix or a misspelled directory name self-correct in one round, no guessing. The active-topic list carries the total count, and **a hit topic is always shown regardless of its position** (in TeleAgent field testing the list was silently truncated to 8 entries, which cut off the just-registered topic; the agent misjudged "registry not synced" and burned a reasoning block for nothing).

System files (`TOPICS.md`/`PROFILE.md`) may not be created/overwritten through this tool — use `topic_register` / `update_user_preference` respectively. Block events are recorded in `guard_events` (kind=`uncovered`) and enter the guard statistics alongside refused/forced.

**Near-duplicate title guard**: after normalization (lowercase, strip punctuation/whitespace, strip suffixes like date strings/`-2`/`(新)`/`更新`/`v3`), a fuzzy comparison runs against all existing titles; similarity ≥ `title_similarity_threshold` (default 0.85) is refused:

```
A note with a near-duplicate title already exists; refusing to create:
  - [[yacmemo部署配置]] (projects/yacmemo部署配置.md, similarity 0.93)
To update content use memory_edit / memory_edit_section; if it really is a new topic use memory_write(force=true).
```

**Two-stage force confirmation** (when forced events within a 24-hour rolling window ≥ `force_confirm_threshold`, default 3):

- Bare `force=true` is refused, returning "manual confirmation required" plus a list of candidate notes;
- After confirming it really is a new topic, `force=true, force_confirm=true` is allowed through;
- The whole process is recorded in `guard_events` — the refused / forced counts are the violation-rate metrics.

**journal exemption**: writes under the `journal/` directory are not subject to the duplicate-name block (timeline entries are naturally named by date); registry-free zones are likewise not subject to the topic hard block.

## 4. memory_edit

```
memory_edit(path: str, old_string: str, new_string: str) -> str
```

Unique text anchor replacement (same semantics as Claude Code's Edit):

- `old_string` not found → refused, with **deterministic diagnostics** attached (the rejection message is an executable next-step instruction):
  - The anchor mixes in `memory_read` appended-information markers (`相关笔记` / `(vector)` / `[正文开始` etc.) → points out "these are not file content";
  - Whitespace-only differences (blank-line counts/trailing spaces) → hands back the verbatim original at that position (preferring a unique single-line anchor); copying it recovers in one round;
  - Substantive difference → shows the closest original line and its similarity, advising a `memory_read` first to verify;
- Multiple hits → refused with approximate line numbers listed, asked to extend the anchor's context;
- Exactly one hit → replace, write to disk, fully re-index the note (FTS/vector/collision recomputation).

Matching semantics are always **strict verbatim unique**; diagnostics only change the information content of the rejection message, never performing fuzzy replacement.

**This is the right way to update facts** — fact changes are always edited in place, never by creating new notes.

**Merge-clearance count** (added 2026-09-19): after an edit dissolves a semantic collision (the re-index recomputation no longer matches the old collision pair), the success return appends "(automatically cleared N stale collision pairs)" — seeing it means this edit merged away a D2 duplicate; the count also appears in the audit overview/snapshot and in the `memory_audit` output (see §7).

## 5. memory_edit_section

```
memory_edit_section(path: str, heading: str, new_content: str) -> str
```

Replaces an entire section matched by a `##` or deeper heading: the heading line is kept, and the replacement extends to the next heading of the same or higher level, or to the end of the file.

- Heading does not exist → refused, listing **all existing section names**;
- Multiple hits on the same heading name → refused with line numbers listed;
- Level-1 headings (`# Note title`) are unavailable — that is the note itself; use `memory_edit`.

Suits rewriting a whole passage in one go (e.g. swapping out the entire "## Deployment steps"); more efficient than multiple `memory_edit` calls.

## 6. memory_move

```
memory_move(path: str, new_path: str) -> str
```

Moves a file to a new relative path (`.md` appended automatically). notes/FTS/vector all update with the path (embedding goes through vec_cache, zero API calls). `[[link]]`s resolve by title, and moving does not change the title, so **links do not need rewriting**. Refused if the target already exists.

## 7. memory_audit

```
memory_audit() -> str
```

Full consistency audit that also **self-heals**:

1. **External modification self-healing**: notes whose on-disk hash ≠ `notes.content_hash` are automatically re-indexed (even external edits made in Obsidian/vim get aligned);
2. **External deletion cleanup**: notes whose files have vanished are cleaned out of all indexes and listed in the report;
3. D1 duplicate titles, full pairwise scan;
4. The list of D2 semantic collisions in open status (with both sides' text and scores);
5. D3 dangling `[[link]]`s;
6. D5 dangling topic cards (registry `卡:` pointing to a nonexistent abstract — leftovers from restructure / manual TOPICS.md edits);
7. D4 stray files (loose files outside the registry-free zones that belong to no registered topic — the agent uses this to prompt the user to file them back);
8. **Missing-vector note call-outs + self-healing retry**: notes written during an embedding endpoint outage (vector_ok=0) have embedding retried at audit time; success means self-healed, continued failure keeps the call-out;
9. Guard statistics (refused / forced / uncovered counts); all-empty disposition lines are cleaned up automatically;
10. **Auto-cleared stale collision pair statistics** (added 2026-09-19): D2 old pairs that no longer match after note deletion or recomputation — an overview line + the `== auto-cleared stale collision pairs ==` line + a record in the audit snapshot.

Fix suggestions are inlined throughout the output. Findings are shown as soon as discovered; **the system never auto-deletes or auto-invalidates anything**. External changes involved in self-healing are snapshotted into the repo uniformly as `external: self-healed N note(s)`, preserving the git-clean invariant (the end of the output carries a git snapshot status line and the current audit snapshot path `journal/audit/<date>.md` — one per day, with same-day re-audits appending; stale snapshots are cleaned up by curator according to `audit_retention_days`).

Output sections added 2026-09-25: `== execution progress ==` (issues currently being worked on by agents, as reported via `memory_audit_update`, with the latest updates), `== verified on re-audit ==` (D-class issues already executed and no longer reported this round — confirmed automatically, no human sign-off) and `== proposal settlement backfill ==` (proposals whose file carries the settled marker get `executed` events backfilled for findings lacking them — hand-stamped markers and structured reports both count).

## 7.5 memory_audit_update

```
memory_audit_update(issue_id: str, event: str, note: str = "") -> str
```

Reports **execution progress** for an audit issue (new in contract 0.3.3). This is the agent-side entry point of the judgment/execution split: for issues dispatched via the WebUI's "copy execution instruction" or discovered through `memory_audit`, report progress to the server while fixing them.

- `issue_id`: the id from the audit report / execution instruction (`D3:<path>|<link>`, `P:<file>:<index>`, etc.);
- `event`: `executing` started / `progress` update / `executed` done / `blocked` stuck, needs a human;
- `note`: one-line explanation (what was done / what is blocking);
- identity is recorded automatically (which agent on which device reported); the timeline is append-only and rendered item by item on the WebUI audit page;
- **re-verification is not the agent's job**: when done, re-run `memory_audit` — an issue no longer reported is verified automatically (the system appends the closing event); never claim "already verified" and never dismiss issues on a human's behalf.

## 8. memory_list

```
memory_list(path: str = "", sort: str = "name") -> str
```

Lists all `.md` files under memory_root (or a subdirectory); with `sort="mtime"` the most recently changed come first. `.index/` is never listed.

## Decision tree for agent tool selection

```
Record a new topic?          → memory_search for duplicates → memory_write (if refused, switch to edit)
Update an existing fact?     → memory_edit (unique anchor) / memory_edit_section (whole-section rewrite)
Find "where was X recorded?" → memory_search (keyword-style query)
Get the full picture of a topic? → memory_read (follow the related-note links)
Periodic checkup?            → memory_audit
Fixing an audit issue?       → memory_audit_update to report progress (executing → progress → executed)
Archive one note in a topic? → archive_note (the abstract cannot be archived alone)
Unarchive a single note?     → unarchive_note
Categorizing a topic?        → topic_tag (prefer reusing tags from the response's inventory)
```


## 9. topic_list

```
topic_list() -> str
```

Lists all currently registered long-term memory topics (title, one-sentence status, topic card path, tags). Registry-free zones (journal/, archive/, curator/) are listed separately with a note.

- The `tag` parameter filters by tag (e.g. topic_list(tag="work"));
- Tags live in the registry `- 标签:` line (0-N, for perspective grouping, not status).

## 10. topic_register

```
topic_register(title: str, description: str = "", related: str = "", tags: str = "") -> str
```

Registers a new long-term memory topic: appends to the `TOPICS.md` registry and creates `topics/<topic>/abstract.md` (or uses an existing note as the abstract).

- **Invocation gate**: call only when the user explicitly asks ("add X to long-term memory") — this is written into the convention block; the registration act itself is the proof of user authorization;
- **Registration is a prerequisite for writing**: under the topic hard block (see §3), paths not covered by a registered topic are always refused — `topic_register` is the sole authorization gate for new topics;
- Duplicate topic names are refused (advised to edit the existing abstract directly);
- After registration, the abstract and the registry are indexed immediately; within the topic directory the agent may freely add detailed md files by module (the directory is the membership);
- **The success return includes a copyable write template** (added 2026-09-19), for example:

```
Registered topic "女儿AI陪伴老师", abstract: topics/女儿AI陪伴老师/abstract.md.
Write conventions going forward:
- Detailed notes: memory_write(title="topics/女儿AI陪伴老师/<note-name>", ...) — must include the directory prefix (e.g. "topics/女儿AI陪伴老师/xxx"); a missing prefix is hard-blocked by the topic registry;
- abstract is the summary card, keep it a one-sentence status: when the status changes, update in place with memory_edit; do not stuff long text into abstract.
```

## 10.5 topic_tag

```
topic_tag(title: str, add: str = "", remove: str = "") -> str
```

Adds/removes tags on a topic (contract 0.3.8). Tags are lightweight reversible metadata: use when the user asks for a tag, or when executing a tag-related curator proposal.

- `add` / `remove` are comma-separated tag lists (Chinese commas tolerated), usable together;
- The response carries the **full tag inventory** — prefer reusing existing tags to avoid synonym sprawl;
- Unprompted batch-tagging is against convention; tags do not affect memory_search content retrieval.

## 11. topic_unregister

```
topic_unregister(title: str) -> str
```

Unregisters a long-term memory topic: removes that topic's block from `TOPICS.md` (all other content kept verbatim).

- **Invocation gate same as registration**: call only when the user explicitly asks ("X no longer needs long-term recording");
- **Only the registry is touched; note files are never touched** — after unregistration the related notes become stray files (called out as D4); the tool's return message guides the agent to confirm with the user and then either file them into `archive/` or delete them;
- Unknown topic names are refused, listing the existing topics.

## 12. memory_context

```
memory_context() -> str
```

**Call first at the start of every session**. Returns the core memory context = integration contract version header + the full `TOPICS.md` registry + each topic card's summary header (first 12 lines) + identity must-reads (with a token, `agents/<agent>/必读.md` and `agents/<agent>/<device>/必读.md` are injected automatically; a write template is provided when they do not exist yet — see §0). Solves cold-start amnesia: the agent does not have to "think of what to search for" — the topic system is directly present.

**Version header** (added 2026-09-19): of the form `[yacmemo integration contract v0.1.3 — when it differs from the version you have recorded locally, call integration_check(onboarded_version="<your version>") to self-update]`. The agent records the contract version it onboarded with in its local onboarding prompt, compares at every session start, and self-updates when behind (see §17).

## 13. memory_delete

```
memory_delete(path: str) -> str
```

Deletes a note (removing the file + all index rows + related collisions/vectors); the deletion automatically produces a `delete:` git snapshot; history is recoverable.

- **Invocation gate**: call only when the user explicitly asks ("delete X" / "X no longer needs recording") — the same level of manual-confirmation semantics as topic_register;
- Unknown paths/titles are refused;
- The system (store) itself never deletes proactively — this is the boundary drawn from v1's lesson of "auto-invalidation wrongly killing memories": deletion is always something instructed.

## 14. archive_topic

```
archive_topic(title: str) -> str
```

Archives a topic (lifecycle: register → active → archive → unregister): the **entire topic directory** moves into `archive/<topic>/` (the directory is the membership — every module note in the topic goes along with the abstract, leaving no strays); inside the registry block, `- 状态: archived` is set and the `- 卡:` path is rewritten in sync.

- **Invocation gate**: call only when the user explicitly asks ("archive X" / "this project is behind us");
- Difference from unregistration: **archiving does not lose retrievability** — the topic's notes remain searchable in the index; only memory_context stops injecting the abstract and topic_list groups it under archived; archive/ is a registry-free zone and does not count as strays;
- Reversibility: git history can be rolled back; manually deleting the registry status line restores active status.

## 14.5 archive_note / unarchive_note

```
archive_note(path: str, reason: str = "") -> str
unarchive_note(path: str) -> str
```

Archive/unarchive **a single note inside a topic** (contract 0.3.9; whole-topic archival uses archive_topic):

- archive_note moves the note into `archive/<topic>/`, optionally writing a status line with `reason` at the top of the note;
- The abstract (topic card) cannot be archived alone; an existing destination is refused;
- After archiving the note stays searchable and is visible under the "archived notes" group in the WebUI topic tree;
- unarchive_note resolves the active topic from the directory name and moves the note back to `topics/<topic>/`; free-form archive directories raise an error;
- Only on explicit user request; **never hand-move notes into archive/ root with memory_move** (it breaks topic ownership and the topic tree).

## 15. get_user_preference

```
get_user_preference(section: str = "") -> str
```

Reads the user profile & preferences (`PROFILE.md`, a memory-layer function file, not topic memory).

- Empty `section` returns the full text; a specified section returns that section's body;
- A missing file returns onboarding copy; a missing section reports an error and suggests creating it with update_user_preference;
- `memory_context` already injects PROFILE up front at session start, so most scenarios need no separate call.

## 16. update_user_preference

```
update_user_preference(section: str, content: str) -> str
```

Creates or replaces one section of the profile & preferences (maintained by the agent):

- Section exists → whole-section replacement (edit_section semantics, heading preserved);
- Section missing → new section appended at the end of the file; file missing → created together with the `# 用户画像与偏好` heading;
- Write distilled conclusions (the "- [category] content" syntax), never raw conversation excerpts;
- Every update automatically produces a git snapshot, traceable.

## 17. integration_check

```
integration_check(onboarded_version: str = "") -> str
```

**Agent integration contract version check** (added 2026-09-19, the "teach the AI to self-update" mechanism):

```
integration_check(onboarded_version="0.1.2")
→ yacmemo integration contract current version: 0.1.3
  Version you declared: 0.1.2 — there is an update; please self-update your local onboarding prompt accordingly and record the new version from this check.

  [0.1.3]
  - Block errors carry built-in near-miss diagnostics: ...

  ## Write-convention digest
  - Long-term memory writes only inside registered topic directories: topics/<topic>/<note-name> ...
```

How it works (the server does not push; the agent pulls on its own):

1. **Version declaration**: the agent records the contract version it onboarded with in its local onboarding prompt/USER.md (e.g. `yacmemo integration contract version: 0.1.3`);
2. **Discovery**: the `memory_context` header carries the current contract version along, so the comparison happens naturally at every session start;
3. **Update**: when the version is behind, call this tool; it returns **incremental changes** (per-version entries) + the **full write-convention digest** — the agent refreshes its local prompt and records the new version accordingly, with no human intervention;
4. Leaving `onboarded_version` empty returns the current version + all recorded changes; if the declared version is newer than the server's, it hints "you may be connected to an old instance";
5. **The contract version is independent of the package version**: it advances only when agent-perceivable behavior changes (tool semantics, return copy, write conventions); the data source is `yacmemo/agent_changes.py` — changing a convention requires appending an `AGENT_CHANGELOG` entry and bumping the version in sync (guarded by tests).

## Agent decision tree (updated)

```
Session start                          → memory_context (contract version header + profile/preferences preloaded + topic system)
Contract version behind                → integration_check(onboarded_version=...) → self-update the local onboarding prompt
User wants a new long-term memory topic → topic_register (only when the user explicitly asks) → write inside the topics/<topic>/ directory per the returned template
User stops long-term-recording a topic → topic_unregister (only when the user explicitly asks) → guide filing/cleanup
User says a project is behind them     → archive_topic (only when the user explicitly asks; retrieval retained, dropped from context)
User wants a memory deleted            → memory_delete (only when the user explicitly asks; recoverable via git)
User's profile/preferences changed     → update_user_preference (replace/append per section)
Record a new topic?                    → memory_search for duplicates → memory_write (if refused, switch to edit)
Update an existing fact?               → memory_edit / memory_edit_section
Find "where was X recorded?"           → memory_search (keyword-style query)
Get the full picture of a topic?       → memory_read / abstract
Periodic checkup?                      → memory_audit (+ the WebUI audit page)
Don't know which topics exist?         → topic_list (filterable by tag)
User wants topics grouped by perspective? → topic_tag (lightweight reversible; never batch-tag unprompted)
```
