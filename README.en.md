> English | [简体中文](README.md)

# yacmemo

A personal memory layer with API-level consistency guards — markdown-first, fully local, agent-agnostic.

## What It Is

yacmemo lets **all AI agents on all your computers** share one and the same long-term memory, and keeps that memory self-consistent:

- **markdown is the single source of truth** — notes are just plain files on your server: human-readable, git-friendly, Obsidian-friendly. SQLite + LanceDB are merely derived indexes; delete them and they can be rebuilt anytime.
- **One service, all devices** — the only service process runs on the machine where the data lives (streamable HTTP). Claude Code, Codex, Cursor, in-house runtimes... any MCP client only needs to add one URL: zero installation, zero processes on the client.
- **Zero generative LLM on the memory read/write main path** — the only resident model call is a 0.6B embedding (~50ms). Structure is produced by conventions, consistency is enforced by deterministic API guards, and fuzzy judgment is left to your main model at read time; the one generative component is the curator deep review, which runs offline, is read-only, proposes only and never executes — anything that lands goes through human dispatch plus the guarded write path.
- **Topic registry** — long-term memory topics are declared explicitly by you ("add X to long-term memory"); the registry + topic cards keep primary and secondary content clearly separated; **writes are hard-limited to registered topics** (paths under unregistered topics are always rejected, and force does not exempt — register first, write later); agents review the memory system at the start of a session, so cold starts no longer mean amnesia;
- **Curator quality curation** — a configurable LLM periodically reviews memory quality and produces **proposal reports**: proposals only, never executed automatically; **judgment / execution / verification are separated** — in the WebUI a human only judges (dismiss false positives / copy an execution instruction to dispatch to any agent), agents execute and report progress via `memory_audit_update`, and re-verification is the audit's job (executed and no longer reported next round = verified); once every finding is settled the proposal is **closed** automatically and disappears from agent search results;
- **WebUI console** — open `/ui/` in a browser (Vue 3 + Naive UI, built with `scripts/build_webui.sh`): browse/edit notes inside the topic tree (**every markdown in storage is visible**: registered topics, archived, registry-free zones, stray files, system files), online search, **dual-mode audit** (deterministic rules + curator LLM deep-review proposals; workflow strip + status tags + execution timelines + status filters), **audit history snapshots and the judgment/execution log** (journal/audit/ + audit_actions/audit_exec_events, reviewable and traceable), profile & preferences editing, usage log and health overview, **online config.toml editing**;
- **Consistency is enforced, not hoped for** — `memory_write` applies topic-coverage interception first and near-duplicate title rejection second, `memory_edit` enforces unique anchors, `memory_move`/`save` targets are bound by the same constraints, contradictions are presented side by side with ⚠ markers in search results, and the system never silently deletes or hides any memory.
- **Memory is version-controlled** — every write/edit/move/delete automatically produces a git commit (`write: x.md`), and the memory repo is always git-clean; mistaken deletions are recoverable, history is traceable, and agents need no filesystem permissions.

## 🤖 Agent Onboarding Guide (AI-Specific)

> **Are you an AI agent? Read [docs/en/09-agent-onboarding.md](docs/en/09-agent-onboarding.md) first and run the onboarding self-check**: confirm whether you already have the yacmemo MCP configured — if not onboarded, guide the user through configuration; if already onboarded, weave memory logic into your workflow and show your onboarding status to the user. For conventions on maintaining desktop agents' local files (USER.md/MEMORY.md), see [docs/en/08-agent-config.md](docs/en/08-agent-config.md).

## Architecture

```
Your computers (any MCP agents)
   │  Each client only adds one URL, nothing to install:
   │  http://debsvc.local:9721/yachen/mcp
   ▼
yacmemo-server (single process, streamable HTTP, stateless sessions)
   ├── /yachen/mcp → Store(root=.../yachen/memory)
   └── /user2/mcp   → Store(root=.../user2/memory)
         store.py      CRUD + write-path guards + index sync + topic registry + git snapshots
         search.py     FTS5 trigram + vectors, RRF fusion
         detectors.py  deterministic D1/D3 detection
         index_db.py   SQLite: metadata/FTS/collisions/guard events
         vector.py     LanceDB: note + observation vectors
         embedding.py  embedding calls (0.6B, ~50ms)
    ├── /ui/ → WebUI console (Vue 3 build: topics/search/audit/profile/settings)
   ▼
yacmemo-curator (systemd timer, weekly) → quality proposal reports, propose only, never execute
   ▼
memory_root (git repo, auto commit on every change, always git-clean)
  ├── TOPICS.md                  topic registry (active/archived)
  ├── PROFILE.md                 profile & preferences (functional layer, injected ahead of context)
  ├── topics/<topic>/            abstract.md + module md files added by agents
  ├── archive/ journal/ curator/  registry-free zones (archived topics / running logs / proposals)
  ├── agents/<agent>/shared/      identity-exclusive memory: agent tier shared across devices +
  │                               <device>/ per-machine tier; token = <device>_<agent>;
  │                               identities are mutually invisible
  └── .index/                    derived indexes (rebuildable, not in git)
```

## Quick Start

### Server (the machine where the data lives)

```bash
git clone <your-repo> yacmemo && cd yacmemo
uv sync
cp config.example.toml config.toml   # fill in the embedding endpoint and each user's root
uv run yacmemo-server --config config.toml
curl http://127.0.0.1:9721/health    # → {"status":"ok","users":["user2","yachen"]}
```

Opening `http://debsvc.local:9721/ui/` in a browser gives you the built-in management console (topics / search / audit / profile / settings, with the version number and commit shown at the bottom of the sidebar); see [docs/en/07-webui.md](docs/en/07-webui.md) for details. The frontend must be built first: one command on the same machine, `scripts/deploy_webui.sh` (build + scp to debsvc, requires Node 18+), or step by step with `scripts/build_webui.sh` followed by a manual scp (see the deployment doc).

### Clients (every computer, every agent)

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

Agents on the same machine can also use stdio: `uv run yacmemo-mcp --root /path/to/memory`.

**Using it for the first time?** Read the [user guide](docs/en/00-user-guide.md) first — onboarding, daily usage, and the FAQ are all in there.

## MCP Tools (19)

| Tool | Purpose |
|---|---|
| `memory_search` | Hybrid retrieval (FTS trigram + vector, RRF fusion); suspected duplicates/contradictions carry inline ⚠ markers; hints are appended when the vector channel fails or a short query misses (<3-character queries fall back to LIKE) |
| `memory_read` | Full note text (verbatim body between the [body start/end] markers) + related notes (tool-appended info: wiki-links + semantic neighbors) |
| `memory_write` | Create new notes (**with the `topics/` prefix, write `topics/<topic>/<note-name>`**); **paths under unregistered topics are rejected outright**, and the interception message carries near-miss diagnostics (a missing prefix or a misspelled directory yields a retryable title) + near-duplicate title rejection (`force` requires two-level confirmation) |
| `memory_edit` | In-place update; the text anchor must be unique; on a miss, self-correcting diagnostics are attached (pointing out anchors mixed with appended info, whitespace-difference variants of the original text, and the closest line); when resolving a semantic collision, returns the count of conflict pairs auto-cleared |
| `memory_edit_section` | Replace an entire section in one go |
| `memory_move` | Move a file, index follows; the destination path is bound by the same topic registry constraints |
| `memory_delete` | Delete a note (**only when the user explicitly asks**; recoverable from git history) |
| `memory_audit` | Self-healing consistency audit (self-heals external changes/deletions, D1–D5 consistency issues, names notes missing vectors + self-heal retry, guard statistics, expired conflict-pair cleanup counts, audit snapshot path); output includes "execution progress" and "verified on re-audit" sections |
| `memory_audit_update` | Report execution progress while fixing an audit issue (executing/progress/executed/blocked); identity is recorded into the timeline automatically; re-verification is confirmed by the audit |
| `memory_list` | Directory tree / recent changes |
| `memory_context` | **Call first at session start**: returns the integration contract version header + topic registry + each topic card's abstract header (cold-start review) |
| `topic_list` | List long-term memory topics (active/archived, with tags; `tag` param filters by tag) |
| `topic_tag` | Add/remove tags on a topic (lightweight reversible metadata; the response carries the full tag inventory to guide reuse) |
| `topic_register` | Register a new topic (**call only when the user explicitly asks**, e.g. "add X to long-term memory"); creates topics/<topic>/abstract.md; on success returns a copyable write template |
| `topic_unregister` | Unregister a topic (**only on explicit user instruction**; only removes it from the registry, notes untouched, adjudicated as strays afterwards) |
| `archive_topic` | Archive a topic (**only on explicit user instruction**): the whole topic directory moves into archive/ — still searchable, no longer injected into context |
| `get_user_preference` | Read profile & preferences (PROFILE.md functional layer; full text or a specified section) |
| `update_user_preference` | Create/replace one section of the profile & preferences (maintained by the agent) |
| `integration_check` | **Agent integration contract version check**: the agent reports the contract version recorded locally; when behind, the server returns incremental changes plus the write-convention quick reference — the agent then updates its local prompt autonomously (docs/09 §4) |

Full specifications: [docs/en/02-mcp-tools.md](docs/en/02-mcp-tools.md); usage conventions (paste into the agent system prompt): Section 8 of [docs/en/01-architecture.md](docs/en/01-architecture.md).

## Documentation

| Document | Contents |
|---|---|
| [00-user-guide.md](docs/en/00-user-guide.md) | **User guide (start here)** |
| [01-architecture.md](docs/en/01-architecture.md) | Design, decision records, principles |
| [02-mcp-tools.md](docs/en/02-mcp-tools.md) | Tool specifications |
| [03-storage-and-search.md](docs/en/03-storage-and-search.md) | File formats, indexes, hybrid retrieval, self-healing |
| [04-consistency.md](docs/en/04-consistency.md) | Three layers of defense, the force ladder, metrics |
| [05-deployment.md](docs/en/05-deployment.md) | systemd, client configuration, backup, security |
| [06-evaluation.md](docs/en/06-evaluation.md) | Retrieval baseline and re-test methodology |
| [07-webui.md](docs/en/07-webui.md) | WebUI console: pages and API reference |
| [08-agent-config.md](docs/en/08-agent-config.md) | **Per-agent memory onboarding and local USER.md/MEMORY.md configuration & maintenance** |
| [09-agent-onboarding.md](docs/en/09-agent-onboarding.md) | **Agent onboarding self-check and memory-logic weaving guide (AI: read this one)** |

v1 (the three-layer extraction architecture) is frozen in [`legacy/`](legacy/), kept only as a decision record.

## Tech Stack

Python 3.11+ · mcp SDK (FastMCP) · SQLite (FTS5 trigram, WAL) · LanceDB · Qwen3-Embedding-0.6B (any OpenAI-compatible endpoint) · rapidfuzz; WebUI frontend Vue 3 + Naive UI + Vite. The server is a single process; no queues, no graph databases; zero generative LLM on the read/write main path (the curator deep review is the sole generative component: offline, read-only, proposes only, never executes).

## License

MIT
