> English | [简体中文](../07-webui.md)

# WebUI Console

> Ships with the server; open `http://<host>:9721/ui/` in a browser and it is ready to use. Shares process and port with MCP; no separate deployment.
> The frontend is a Vue 3 + Naive UI project (`frontend/`) that needs building: `scripts/build_webui.sh` → output lands in `yacmemo/webui/dist/` (served directly by `app.py`). **Without a build the service still starts**, `/ui/` returns a 503 with build instructions, and MCP/API are unaffected.

## 1. Overview

- **UI language**: a Chinese / English switcher in the header (persisted in localStorage); the naive-ui component language follows. Business messages returned by the backend remain in Chinese (the server-side message catalog is not internationalized yet);

- The WebUI and the MCP endpoints share the same Starlette application and the same user contexts (Store/Searcher/IndexDB instances) — **what you see and change in the browser is exactly the data the agents are using**; there is no second data path;
- Route order: `/api/*` and `/ui/*` are registered before the per-user MCP mounts; the config layer also reserves `api`/`ui`/`health` as user-id reserved words, ruling out shadowing;
- Error convention: business-level refusals (e.g. guard interception) return HTTP 200 + `{"ok": false, "error": "..."}`; unknown users return 404;
- No separate authentication; follows the "personal use on the internal network" trust boundary (see the security boundary in [05-deployment.md](05-deployment.md)).

## 2. Page Guide

A selector at the top switches the memory user (the `[[users]]` in config.toml); the left menu has seven pages: **Dashboard (default landing) / Topics / Search / Audit / Profile / Identity / Settings**.

### 2.1 Topic Browsing

The left side is the **topic tree** — every markdown file in storage is visible, with nothing shown twice:

- **Active topics**: each topic is one directory (`abstract.md` plus agent-added module notes), expanded level by level;
- **Archived**: expands all files under the `archive/<topic>/` directories;
- **Registry-free zones**: the two system directories journal / curator;
- **Stray files**: flagged with the same criteria as the backend's D4 stray detection (outside registry-free zones + system files + registry coverage) — under the topic hard interception the tool surface cannot produce new strays, so anything appearing here can only be files hand-created in Obsidian or left behind after unregistration; have an agent file them into place or delete them;
- **System files**: TOPICS.md (the registry) and PROFILE.md (profile & preferences).

Selecting an entry shows it on the right; this merges all capabilities of the old "Notes" page:

- **View**: the body renders as markdown by default; `编辑` (Edit) switches to a source textarea, `预览` (Preview) switches back to the rendered view;
- **Edit**: `保存` (Save) goes through `store.save` — whole-file overwrite, the title follows the first-line `# 标题` (# Title) heading, and the index is rebuilt in sync (FTS/vectors/collisions recomputed);
- **Delete**: requires a confirm; deletes the file + cleans all indexes. Still recoverable from git. abstract.md cannot be deleted from the UI;
- **Topic tags**: the tree toolbar filters topics by tag, topic nodes carry tag suffixes (〔work·dev〕 style); the "Tag management" modal supports renaming/deleting tags (batch-rewriting the registry) and per-topic tagging (with autocomplete of existing tags);
- **No UI entry for creating notes**: creation goes through MCP `memory_write` (stricter guards, prevents duplicate titles), or create by hand in Obsidian and let audit self-healing bring it into the index.

### 2.2 Search

A page for manually verifying retrieval quality. Switch between the `hybrid` (default) / `fts` / `vector` channels; results include scores, channel sources, and ⚠ collision flags; a banner appears at the top of the page when the vector channel is down or a short query misses. Usage advice is the same as for MCP: keyword-style queries are more reliable via fts, natural sentences rely on vector.

### 2.3 Audit

The audit page has a **two-tab** structure; each of the two audit engines gets one complete workflow. The role split is the core of the design: **humans only judge (dismiss false positives / dispatch issues to agents), agents only execute (reporting progress via `memory_audit_update`), the system only verifies (re-checks are confirmed by the audit automatically)** — the WebUI is a judgment desk and an observation board, not an executor.

**Tab 1 "Deterministic Audit"** — fast, zero LLM, includes self-healing; equivalent to `memory_audit`:

- "Audit Now" at the top; on page load the most recent audit result is shown (the in-memory cache hangs off the Store and is shared with MCP memory_audit; after a service restart a re-audit is needed; when a snapshot is deleted — manually or by curator expiry cleanup — the cache is cleared in step, and the page degrades to a notice for the missing snapshot file instead of an error);
- **Workflow strip**: pending → executing → executed-awaiting-recheck → verified, with live counters per stage — at a glance, which issues are stuck with whom; the selector on the right filters by status (only non-empty options are listed);
- **The issue list speaks the same two-level language as the proposals page**: one row per issue — status tag (pending·awaiting your call / executing·agent at work / executed·awaiting recheck / blocked·needs you / regressed·re-handle) + issue type + one-line description, with "Copy Execution Instruction / Ignore" right on the row; sorted by attention priority, issues needing your attention expanded by default, executing ones collapsed; expanding shows explanatory detail, the issue id, and the execution timeline;
- **"Ignore" is a human judgment** (false positive / won't fix): the disposition line is appended to that day's snapshot's 处置记录 (Disposition Record) section and persisted to the `audit_actions` table (reruns do not replay; D2 syncs collision status); "Copy Execution Instruction" embeds the `memory_audit_update` reporting convention — paste it to any agent to start work;
- Each issue can expand its **execution timeline**: every agent report (started / progress / done / blocked) is listed with identity and time;
- **Verified on re-audit**: issues the agent executed and this round's audit no longer reports move here automatically (the system appends a `verified` closing event) — verification is deterministic and needs no human sign-off; a verified issue that reappears is flagged as regressed;
- Self-healing cards (new file / external modification / external deletion / **notes missing vectors**) are display-only, with no disposition buttons — notes missing vectors were written while the embedding endpoint was down, and the audit has already retried and filled them in automatically;
- When audit snapshot files are deleted (manually or by curator expiry cleanup), the most-recent-audit cache is cleared in step, and the page degrades to a notice rather than an error;
- **Judgment & execution log**: human dispositions (`audit_actions` table) and agent execution reports (`audit_exec_events` table) merged in reverse chronological order — both lines leave a trail;
- **Historical audit snapshots**: two columns — the left holds the snapshot list (`journal/audit/<date>.md`, one per day, same-day reruns appending under a 复审 (Re-review) subsection, newest first) and the right renders the selected snapshot's markdown side by side instead of stacking it below; expiry cleanup is handled by the curator timer according to `audit_retention_days` (default 7 days).

Disposition guidance per issue type:

| Output | Meaning | Disposition |
|---|---|---|
| New file / external modification / external deletion | self-healing results | no action needed |
| Duplicate title (D1) | two notes with near-duplicate titles after normalization | merge manually, then have the agent report `executed`; or "Ignore" |
| Semantic collision (D2) | similar observation pairs across notes (with both texts and the score) | **human adjudication**: dispatch the merge to an agent; "Ignore" if a false positive (syncs collision status) |
| Dangling link (D3) | the `[[target]]` resolves to neither a title nor a path | "Copy Execution Instruction" to dispatch an agent to fix it; "Ignore" if it is not a note reference |
| Dangling topic card (D5) | the abstract the registry points to does not exist | dispatch an agent to fix the registry or rebuild the card |
| Stray file (D4) | loose notes not filed under any registered topic | dispatch an agent to file it into place; or "Ignore" |

> Registry-free zones (journal/archive/curator) are never judged stray. Guard statistics (refused / forced counts) sit at the bottom of the cards.

**Judging semantic collisions**: 1) the two notes are two copies of the same topic → merge the content into the keeper, delete the other (the agent reports `executed` afterwards); 2) the two notes cover different topics and merely both happen to contain this fact → click "Ignore"; 3) one note is an older version of the other → merge the new content into the keeper. Collision detection only applies to observation lines in the `- [类别] 内容` (`- [category] content`) form; GFM task lists (`- [x]`) are checkboxes and do not participate (see [03-storage-and-search.md](03-storage-and-search.md)).

**Tab 2 "Quality Proposals"** — the curator deep-review workflow:

- "Deep Review Now" hands the registry, the topic cards, and the audit results to the LLM configured under `[curator]` (about 1–3 minutes; the weekly timer runs it automatically); the report lands at `curator/提案-<日期>.md` (proposal-<date>.md), and same-day reruns append under a 复审 (Re-review) subsection (the title stays unique per day, so D1 is not triggered);
- **Two-level structure**: the first level is the proposal list, each proposal carrying a **proposal-level status** (blocked·needs you / pending·awaiting your call / executing·agent at work / settled·all done, sorted to the top in that priority) with per-state finding counts; expanding reveals the **finding-level** details (severity / type / re-review badge / finding status / suggestion / execution timeline) — proposals needing your attention are expanded by default, settled ones collapse; the stats row runs the full state machine (pending → executing → executed / blocked / dismissed) and the selector on the right filters by status;
- **There is no "Adopt" button — dispatching is adopting**: for pending items, "Copy Execution Instruction" pastes a complete instruction to any agent (the instruction embeds the `memory_audit_update` reporting convention); "Ignore" records a false positive / won't-do (persisted to the `audit_actions` table as a P-class entry and appended to the proposal note's 裁决记录 (Adjudication Record) section, git auto-snapshot); agent execution progress is shown as an event timeline under each item;
- Legacy "adopted but not executed" items (old-model dispatch intents) are tagged explicitly and can still be dispatched or dismissed to settle them;
- **Automatic closure + re-review interplay**: once every finding of a proposal is executed or dismissed, a `> 状态：已结案` (settled) marker is stamped at the top of the file (and the in-body "状态：待裁决" becomes "已结案", git snapshot) — `memory_search` no longer returns settled proposals by default (explicit `memory_read` still works), so execution work is never dispatched twice; the card title gains "（已结案）". An agent may also stamp the marker by hand with `memory_edit` after confirming everything is done (agents on old sessions without the reporting tool close out this way — the audit backfills "executed" for findings lacking events). **When a same-day re-review appends new findings, the curator revokes the stale marker immediately** (new findings enter the list with a "复审" badge, and the marker is re-stamped once they are handled). The system and the WebUI only record decisions and never modify note content directly — an extension of the iron rule "curator only proposes": changes always go through the guarded, git-snapshotted store tool semantics.

"Full Index Rebuild" is a dangerous maintenance operation (it also zeroes the run metrics); it lives in the **Settings → Health Overview → Maintenance** card, not on the audit page.

### 2.4 Profile & Preferences

Visual editing of PROFILE.md: the left side lists all sections (identity / communication style / materials & document preferences …), click to view, `编辑` (Edit) then saves the whole section; `+ 新建小节` (+ New Section) creates one by entering a section name. Equivalent to MCP's `get_user_preference` / `update_user_preference`, and likewise goes through the write path (automatic git snapshot).

### 2.5 Identity

Managing the `agents/<agent>/` exclusive memory zone: minting deterministic identity tokens per agent+device, and managing the agent-layer shared subtree (shared/必读.md) and per-device subtrees; see [09-agent-onboarding.md](09-agent-onboarding.md). Creating an identity pre-creates the shared/必读.md placeholder template.

### 2.6 Dashboard (default landing)

- **System status card**: service status, embedding configuration and model, curator status and model, today's call count;
- **User cards**: per-user note count / active topics / audit backlog / proposal count, with "Audit" and "Notes" shortcuts (switching the user and jumping to the page); "Last audit" honestly shows "not audited since restart" after a service restart;
- **Recent activity**: the last 8 MCP calls across users (tool / summary / status / time).

### 2.7 Settings

Four tabs: **Users** (structured add/edit/delete), **Service Config** (embedding/curator forms), **config.toml (advanced)**, **Usage Log**.

- **Users**: the user list (id / memory root / git identity / mount status) plus add / edit / delete — applied programmatically to the `[[users]]` blocks of config.toml (full validation before an annotated backup; comments and ordering preserved). Adding auto-creates the memory directory; a new user or a root change needs a restart before MCP mounts (the page says so); deletion requires typing the user id to confirm, and by default only removes the config entry while keeping the memory directory and git history, with an optional "also delete the memory directory" checkbox (irreversible);
- **Service Config**: [embedding] and [curator] as forms (endpoint / model / key / dimensions / timeout / retention days) with a **"Test connection" button that actually calls the endpoint** (embedding reports dimensions and latency, curator reports the model reply) — mistakes surface immediately; saving only touches the form fields (text surgery keeps comments), and the raw config.toml editor remains as the "advanced" mode;
- Usage log / maintenance (full index rebuild) unchanged;

- **Usage log**: a trace of every MCP tool call — top cards (calls/errors/clients over the last 14 days) + a table (time/user/tool/summary/client UA/IP/duration), filterable by tool; guard refusals count as normal business results and are not logged as errors;
- **Health overview**: embedding configuration status (unconfigured = FTS-only mode), plus per-user note counts / open collision counts / guard statistics / topic counts;
- **config.toml online editing**: before saving it automatically validates TOML syntax + structure (invalid configs are rejected outright), backs up the original file as `config.toml.bak-<timestamp>`, and keeps the 600 permission; optional "save and restart service" (systemd restart, about 3 seconds offline). Deleting a user is a dangerous operation — no button is provided; handle it manually over SSH.

## 3. API Reference

All responses are JSON; business failures return `{"ok": false, "error": "..."}` (HTTP 200), unknown users 404.

| Method | Path | Params | Description |
|---|---|---|---|
| GET | `/api/overview` | — | user list (notes/collisions/guard stats) + embedding status + today's call count |
| GET | `/api/usage` | `limit` `user` `tool` | call log (default 100 entries, newest first) |
| GET | `/api/usage/clients` | — | client summary (UA + IP + call count + last active) |
| GET | `/api/usage/days` | — | per-day call/error counts for the last 14 days |
| GET | `/api/{user}/notes` | `path` `sort` | note list (with mtime/size) |
| POST | `/api/{user}/notes` | `{title, content, force, force_confirm}` | create (goes through the write-path guards) |
| GET | `/api/{user}/note` | `path` | read one note (by path or title) |
| PUT | `/api/{user}/note` | `{path, content}` | whole-file save (title follows the first-line heading) |
| DELETE | `/api/{user}/note` | `path` | delete (file + all indexes) |
| GET | `/api/{user}/search` | `q` `limit` `kind` | search (includes ⚠ warnings) |
| POST | `/api/{user}/audit` | — | run the audit (includes self-healing; mutates the index); returns `audit_file` (this run's snapshot path) |
| GET | `/api/{user}/audit/last` | — | most recent audit result (in-memory cache, lost on restart) |
| GET | `/api/{user}/audit/runs` | — | historical audit snapshot list (journal/audit/*.md, newest first) |
| GET | `/api/{user}/audit/actions` | — | full disposition/adjudication history (audit_actions table) |
| GET | `/api/{user}/audit/exec` | — | full agent execution timeline (audit_exec_events table, newest first) |
| POST | `/api/{user}/audit/action` | `{file, id, action, label, note?}` | record a human disposition (appends to the snapshot's disposition record; D2 syncs collision status) |
| POST | `/api/{user}/proposal/action` | `{file, index, action, type?, reason?, note?}` | adjudicate a proposal item (persisted in the table + traced in the proposal note's 裁决记录 (Adjudication Record) section) |
| POST | `/api/{user}/collision` | `{id, status}` | collision adjudication: `resolved` / `dismissed` |
| POST | `/api/{user}/curator` | — | trigger a deep review (synchronous wait, about 1–2 minutes); returns the report markdown |
| GET | `/api/{user}/proposals` | — | list curator proposal reports |
| GET | `/api/config` | — | read config.toml verbatim (contains secrets; for internal-network administration only) |
| POST | `/api/config` | `{content, restart?}` | validate (TOML + load_config) → back up → save; with `restart=true` the service restarts after a delay |

`{user}` is a user id from config.toml. Scripting examples:

```bash
curl -s http://debsvc.local:9721/api/yachen/search?q=端口 | python -m json.tool
curl -s -X POST http://debsvc.local:9721/api/yachen/audit
```

## 4. Usage Log (usage.db)

- Location: `[server].data_dir/usage.db` (default `data/usage.db`, relative to the service startup directory);
- Table `call_log`: `id / ts / user_id / client / ip / tool / summary / duration_ms / ok / error`;
- **Rolling retention of the most recent 20,000 entries**; no maintenance needed;
- Writer: `tools.py` records after every MCP tool call (stdio calls record the client as `stdio`; HTTP calls take the request UA and the remote IP);
- `ok` semantics: unexpected exceptions = 0; business results such as guard refusals = 1 (guard behavior is separately queryable in the `guard_events` table).

## 5. Build and Implementation Notes

- **Frontend project**: Vue 3 + Naive UI + Vite (`frontend/`), sources `src/App.vue` + `src/components/` (the five page components) + `src/composables/api.js` (a unified fetch wrapper);
- **Build**: `scripts/build_webui.sh` (npm ci + vite build) → output lands in `yacmemo/webui/dist/`, matching `app.py`'s `STATIC_DIR`; the vite outDir uses `new URL('../yacmemo/webui/dist', import.meta.url)`, resolved relative to `frontend/vite.config.js` (one level up is the repo root) — do not change it to `../../yacmemo/webui/dist` (that would point outside the repository);
- **Chunking**: manualChunks puts `vue` and `naive-ui` into their own chunks — iterating on business code does not invalidate the big-dependency caches;
- **Dev mode**: `cd frontend && npm run dev` (Vite dev server on port 5173, `/api` proxied to local 9721);
- **Unbuilt behavior**: when `dist/` does not exist the service starts normally and `/ui/` returns 503 + build instructions (PlainTextResponse); MCP/API remain fully functional;
- **No npm on the server**: build on a dev machine, then scp: `scp -r yacmemo/webui/dist <server>:/srv/yacmemo/yacmemo/webui/` (dist is not in git);
- Backend handlers are async; Store's blocking operations run via `run_in_threadpool` and never block the MCP event loop; cross-thread safety is guaranteed by the IndexDB/VectorStore/Store instance locks;
- Static assets are mounted only at `/ui/assets` (Vite output); `/ui/` is served `index.html` directly by the handler.

## 6. Common Operations

- **See what the agents did in the last two weeks**: Settings page → in the usage log area filter by tool `memory_write`; the summary column is the list of written titles;
- **Adjudicate a collision**: Audit page → read both texts → click "Handled" if already merged; click "Ignore" if it is confirmed a false positive; before deciding, click the path to jump to the Topics page and verify;
- **Troubleshoot "can't find it"**: on the Search page try `fts` / `vector` separately → check Settings → Health Overview for whether embedding is configured → check the Audit page for whether the file made it into the index;
- **Manually edited files**: click run on the Audit page and the index aligns;
- **Upgrade frontend dependencies / change components**: run `scripts/build_webui.sh` on a dev machine → scp dist → a browser refresh applies it (no service restart needed).
