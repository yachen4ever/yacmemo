"""Store tests: write-path guards, edit anchors, move, read-related, journal exemption."""

from __future__ import annotations

import pytest

from yacmemo.fs_utils import content_hash
from yacmemo.store import AnchorError, Store, StoreError, TitleConflict

NOTE_A = """# yacmemo部署配置

yacmemo 服务部署在 debsvc 上。

- [配置] 服务端口为 9721
- [配置] LLM 指向 m2ultra:11234
"""

NOTE_B = """# 备份策略

数据目录用 restic 每日备份。

- [运维] 备份目标是 NAS 的 backup 共享
"""


def test_write_creates_file_and_indexes(store: Store):
    r = store.write("notes/yacmemo部署配置", NOTE_A)
    assert r["path"] == "notes/yacmemo部署配置.md"
    assert (store.root / "notes/yacmemo部署配置.md").is_file()
    assert store.db.get_note_by_title("yacmemo部署配置") is not None
    # FTS finds it
    assert store.db.fts_search("服务端口")[0]["path"] == "notes/yacmemo部署配置.md"
    # vector + obs indexed
    assert store.vectors.search_note_vectors(
        store._embed_cached("yacmemo部署配置\n" + NOTE_A), 5)


def test_title_guard_refuses_near_duplicate(store: Store):
    store.write("notes/yacmemo部署配置", NOTE_A)
    with pytest.raises(TitleConflict) as e:
        store.write("notes/yacmemo部署配置-2", "# yacmemo部署配置-2\n端口改成 8080")
    assert e.value.matches and e.value.matches[0]["title"] == "yacmemo部署配置"
    assert store.db.guard_stats()["refused"] == 1


def test_title_guard_date_suffix_ignored(store: Store):
    """Same topic with a date suffix is still a conflict (dates stripped)."""
    store.write("notes/yacmemo部署配置", NOTE_A)
    with pytest.raises(TitleConflict):
        store.write("notes/yacmemo部署配置 2026-09-13", "内容")


def test_title_guard_force_bypass_records_event(store: Store):
    store.write("notes/yacmemo部署配置", NOTE_A)
    r = store.write("notes/yacmemo部署配置-2", "# yacmemo部署配置-2\n内容", force=True)
    assert r["forced"] is True
    assert store.db.guard_stats()["forced"] == 1


def test_journal_dir_exempt_from_guard(store: Store):
    store.write("notes/yacmemo部署配置", NOTE_A)
    r = store.write("journal/yacmemo部署配置-0913", "# yacmemo部署配置-0913\n今天调了端口")
    assert r["forced"] is False
    assert r["path"].startswith("journal/")


def test_unrelated_titles_no_conflict(store: Store):
    store.write("notes/yacmemo部署配置", NOTE_A)
    r = store.write("notes/备份策略", NOTE_B)
    assert r["forced"] is False


def test_edit_anchor_must_exist(store: Store):
    store.write("notes/yacmemo部署配置", NOTE_A)
    with pytest.raises(AnchorError):
        store.edit("yacmemo部署配置", "不存在的锚点", "x")


def test_edit_anchor_must_be_unique(store: Store):
    content = "# 配置\n\n- [配置] 端口为 9721\n\n- [配置] 端口为 9721\n"
    store.write("notes/配置", content)
    with pytest.raises(AnchorError) as e:
        store.edit("配置", "端口为 9721", "x")
    assert "2 处" in str(e.value)


def test_edit_updates_index(store: Store):
    store.write("notes/yacmemo部署配置", NOTE_A)
    store.edit("yacmemo部署配置", "服务端口为 9721", "服务端口改为 8080")
    content = (store.root / "notes/yacmemo部署配置.md").read_text(encoding="utf-8")
    assert "服务端口改为 8080" in content
    # FTS reflects the new content
    hits = store.db.fts_search("8080")
    assert [h["path"] for h in hits] == ["notes/yacmemo部署配置.md"]
    hits = store.db.fts_search("9721")
    assert hits == []


def test_move_updates_all_indexes(store: Store):
    store.write("notes/projects/yacmemo部署配置", NOTE_A)
    r = store.move("notes/projects/yacmemo部署配置", "notes/infra/yacmemo部署配置")
    assert r["new_path"] == "notes/infra/yacmemo部署配置.md"
    assert not (store.root / "notes/projects/yacmemo部署配置.md").exists()
    assert (store.root / "notes/infra/yacmemo部署配置.md").is_file()
    # notes + fts follow the new path
    assert store.db.get_note("notes/infra/yacmemo部署配置.md") is not None
    hits = store.db.fts_search("服务端口")
    assert [h["path"] for h in hits] == ["notes/infra/yacmemo部署配置.md"]
    # title lookup still resolves to the new path
    assert store.resolve("yacmemo部署配置") == "notes/infra/yacmemo部署配置.md"


def test_move_refuses_existing_target(store: Store):
    store.write("notes/a笔记", "# a笔记\n内容A")
    store.write("notes/b笔记", "# b笔记\n内容B")
    with pytest.raises(Exception, match="已存在"):
        store.move("notes/a笔记", "notes/b笔记")


def test_read_returns_related_by_link(store: Store):
    store.write("notes/debsvc服务器", "# debsvc服务器\n\n- [主机] IP 192.168.5.7\n")
    note = "# yacmemo部署配置\n\n部署在 [[debsvc服务器]] 上。\n还引用了 [[不存在的笔记]]。\n"
    store.write("notes/yacmemo部署配置", note)

    r = store.read("yacmemo部署配置")
    by_title = {x["title"]: x for x in r["related"]}
    assert by_title["debsvc服务器"]["via"] == "link"
    assert "IP 192.168.5.7" in by_title["debsvc服务器"]["note"]
    assert by_title["不存在的笔记"]["missing"] is True


def test_read_related_resolves_path_form_links(store: Store):
    store.topic_register("m2ultra本地大模型", description="M2 推理端现状")
    store.write(
        "notes/yacmemo部署",
        "# yacmemo部署\n\n- [[topics/m2ultra本地大模型/abstract.md]] — 推理端\n"
        "- [[topics/ghost/abstract.md]] — 不存在\n")

    r = store.read("yacmemo部署")
    rel = {x["title"]: x for x in r["related"]}
    # 路径形式解析到目标笔记（卡标题从 H1 提取会与主题名漂移，
    # 路径是 agent 唯一能从 memory_list 拿到的确定形式）
    assert rel["topics/m2ultra本地大模型/abstract.md"]["via"] == "link"
    assert rel["topics/m2ultra本地大模型/abstract.md"]["path"] == \
        "topics/m2ultra本地大模型/abstract.md"
    # 真不存在的路径仍报 missing
    assert rel["topics/ghost/abstract.md"]["missing"] is True


def test_list_notes_sort_modes(store: Store):
    store.write("notes/a笔记", "# a笔记\nA")
    store.write("notes/b笔记", "# b笔记\nB")
    assert store.list_notes() == [
        "TOPICS.md", "notes/a.md", "notes/a笔记.md", "notes/b.md", "notes/b笔记.md"]
    mtimes = store.list_notes(sort="mtime")
    assert set(mtimes) == {
        "TOPICS.md", "notes/a.md", "notes/a笔记.md", "notes/b.md", "notes/b笔记.md"}


def test_write_without_embedding_still_indexes_fts(cfg, db):
    """FTS-only degradation: no embedding client configured."""
    s = Store(cfg, db, emb=None, vectors=None)
    (s.root / "notes").mkdir()
    (s.root / "TOPICS.md").write_text(
        "# 主题记忆注册表\n\n## 笔记主题\n- 卡: notes/a.md\n- 现状: x\n",
        encoding="utf-8")
    s.write("notes/端口配置", "# 端口配置\n\n服务端口为 9721\n")
    assert s.db.fts_search("端口配置")[0]["path"] == "notes/端口配置.md"


def test_reindex_rebuilds_from_files(store: Store):
    store.write("notes/yacmemo部署配置", NOTE_A)
    store.write("notes/备份策略", NOTE_B)
    # simulate index loss
    store.db.clear_all()
    store.vectors.wipe()
    assert store.db.list_notes() == []

    r = store.reindex()
    assert r["indexed"] == 5 and r["failed"] == []  # 种子 3 + 写入 2
    assert store.db.get_note_by_title("yacmemo部署配置") is not None
    assert store.db.fts_search("restic")[0]["path"] == "notes/备份策略.md"


def test_edit_miss_diagnoses_read_decoration(store: Store):
    """agent 把 memory_read 的附加信息当文件内容抄进锚点时，拒绝消息直接点破
    （2026-09-16 TeleAgent 连续撞墙的根因形态一）。"""
    store.write("notes/ESXi宿主机与核显直通", "# ESXi宿主机与核显直通\n\n## 宿主机事实\n\n- 内容\n")
    with pytest.raises(AnchorError) as e:
        store.edit("ESXi宿主机与核显直通", "## 相关笔记\n- [[hardware]] (vector)", "x")
    msg = str(e.value)
    assert "不是文件内容" in msg and "相关笔记" in msg


def test_edit_miss_whitespace_suggests_verbatim_anchor(store: Store):
    """凭记忆重打导致空行数不对时（形态二），把逐字原文行还给 agent。"""
    content = ("# ESXi宿主机与核显直通\n\n## 宿主机事实\n\n"
               "- **ESXi 8.0.3 build-25205845（8.0 U3）**，全 VM 为 vmx-21\n\n"
               "## 相关文件\n\n- vmx 备份见 datastore1\n")
    store.write("notes/ESXi宿主机与核显直通", content)
    bad = ("- **ESXi 8.0.3 build-25205845（8.0 U3）**，全 VM 为 vmx-21\n\n\n"
           "## 相关文件")  # 行序列相同，仅空行数不同
    with pytest.raises(AnchorError) as e:
        store.edit("ESXi宿主机与核显直通", bad, "x")
    msg = str(e.value)
    assert "仅空白不一致" in msg
    suggested = "- **ESXi 8.0.3 build-25205845（8.0 U3）**，全 VM 为 vmx-21"
    assert suggested in msg
    # 建议的锚点直接可用（一轮恢复，不用反复试错）
    r = store.edit("ESXi宿主机与核显直通", suggested, "- **已替换**\n\n## 相关文件")
    assert r["path"] == "notes/ESXi宿主机与核显直通.md"


def test_edit_miss_fuzzy_shows_closest_line(store: Store):
    """实质差异（如记错数字）时给出最接近的原文行，避免盲目重试。"""
    content = "# ESXi宿主机与核显直通\n\n- [配置] 管理网络 vmk0 192.168.5.10\n"
    store.write("notes/ESXi宿主机与核显直通", content)
    with pytest.raises(AnchorError) as e:
        store.edit("ESXi宿主机与核显直通", "- [配置] 管理网络 vmk0 192.168.5.11", "x")
    msg = str(e.value)
    assert "实质差异" in msg and "192.168.5.10" in msg


def test_title_guard_blocks_compact_date_suffix(store: Store):
    """紧凑日期尾巴（无分隔符）也必须拦截——2026-09-16 生产实测漏拦。"""
    store.write("notes/女儿音乐启蒙", "# 女儿音乐启蒙\n内容\n")
    with pytest.raises(TitleConflict):
        store.write("notes/女儿音乐启蒙0916", "# 女儿音乐启蒙0916\n内容\n")
    with pytest.raises(TitleConflict):
        store.write("notes/女儿音乐启蒙20260916", "# 女儿音乐启蒙20260916\n内容\n")
    # 型号/年份类数字结尾不是日期，不误拦
    r = store.write("notes/女儿音乐启蒙模型1972", "# 女儿音乐启蒙模型1972\n内容\n")
    assert r["forced"] is False


# ---------------- 主题注册制硬拦截（2026-09-17）----------------

def test_write_outside_registered_topics_refused(store: Store):
    """未覆盖路径拒写：错误带行动指引，并记 uncovered 守卫事件。"""
    with pytest.raises(StoreError) as e:
        store.write("test/散记", "# 散记\n内容\n")
    msg = str(e.value)
    assert "不属于任何注册主题" in msg
    assert "topic_register" in msg and "免注册区" in msg
    assert store.db.guard_stats()["uncovered"] == 1


def test_write_force_does_not_bypass_topic_gate(store: Store):
    """force 只管近似标题冲突，不豁免主题注册制。"""
    with pytest.raises(StoreError, match="不属于任何注册主题"):
        store.write("test/散记", "# 散记\n内容\n", force=True, force_confirm=True)


# ---- 拦截消息近失诊断（2026-09-19 TeleAgent 实测教训）----

def test_write_missing_topics_prefix_gets_near_miss_hint(store: Store):
    """注册后写入漏 topics/ 前缀：错误须直接给出可重试的 title，并点名该主题。"""
    store.topic_register("女儿AI陪伴老师", description="测试")
    with pytest.raises(StoreError) as e:
        store.write("女儿AI陪伴老师/abstract", "# x\n")
    msg = str(e.value)
    assert "疑似路径前缀" in msg
    assert 'title="topics/女儿AI陪伴老师/abstract"' in msg
    assert "《女儿AI陪伴老师》" in msg  # 命中主题必须在活跃列表出现
    assert store.db.guard_stats()["uncovered"] == 1


def test_active_topic_list_shows_count_and_near_match_beyond_eight(store: Store):
    """列表带总数；第 9 个主题（注册表末尾）被近失命中时必须点名——
    旧版静默截断到 8 个曾把刚注册的主题切掉，诱导 agent 误判注册表未同步。"""
    for i in range(2, 10):
        store.topic_register(f"主题{i:02d}", description="t")
    with pytest.raises(StoreError) as e:
        store.write("主题09/abstract", "# x\n")
    msg = str(e.value)
    assert "共 9 个" in msg
    assert "《主题09》" in msg


def test_near_miss_fuzzy_dir_name(store: Store):
    """目录名拼错：按名称最接近给出修正路径。"""
    store.topic_register("女儿AI陪伴老师", description="t")
    with pytest.raises(StoreError) as e:
        store.write("女儿AI陪伴老湿/笔记", "# x\n")
    assert "名称最接近" in str(e.value)
    assert 'title="topics/女儿AI陪伴老师/笔记"' in str(e.value)


def test_uncovered_write_without_near_match_keeps_guidance(store: Store):
    """无近失命中时不给诊断行，行动指引保持完整。"""
    with pytest.raises(StoreError) as e:
        store.write("zzz/无关主题", "# x\n")
    msg = str(e.value)
    assert "疑似路径" not in msg
    assert "topic_register" in msg and "免注册区" in msg
    assert "共 1 个" in msg  # 种子主题：笔记主题


# ---- 变更留痕：before_hash 与冲突对自动清除计数（2026-09-19，atlas 评审回应）----

def test_edit_reports_cleared_collisions_and_before_hash(store: Store):
    """合并型编辑：旧冲突对不再命中即"自动清除"，计数与 before_hash 一并返回。"""
    store.write("notes/A配置", "# A配置\n\n- [配置] 服务端口为 9721\n")
    store.write("notes/B配置", "# B配置\n\n- [配置] 服务端口为 9721\n")
    assert len(store.db.list_collisions(status="open")) == 1
    old = (store.root / "notes/A配置.md").read_text(encoding="utf-8")

    r = store.edit("notes/A配置", "服务端口为 9721", "每日备份到 NAS")

    assert r["cleared_collisions"] == 1
    assert r["before_hash"] == content_hash(old)
    assert store.db.list_collisions(status="open") == []


def test_edit_without_collision_change_reports_zero(store: Store):
    """普通编辑（不消解撞车）不虚报清除计数。"""
    store.write("notes/普通笔记", "# 普通笔记\n\n- [配置] 服务端口为 9721\n")
    r = store.edit("notes/普通笔记", "9721", "9722")
    assert r["cleared_collisions"] == 0


def test_save_and_delete_return_before_hash(store: Store):
    store.write("notes/已有笔记", "# 已有笔记\n内容A\n")
    r = store.save("notes/已有笔记.md", "# 已有笔记\n内容B\n")
    assert r["before_hash"] == content_hash("# 已有笔记\n内容A\n")
    r2 = store.save("notes/全新笔记.md", "# 全新笔记\n")
    assert r2["before_hash"] == ""  # 新建没有变更前状态
    r3 = store.delete_note("已有笔记")
    assert r3["before_hash"] == content_hash("# 已有笔记\n内容B\n")


def test_audit_reports_pruned_stale_collisions(store: Store):
    """审计的过期冲突对清理是"索引损坏"级兜底：notes 行消失而 collision
    行残留（正常路径都会联动清理），审计清除并计数。"""
    store.write("notes/A配置", "# A配置\n\n- [配置] 服务端口为 9721\n")
    store.write("notes/B配置", "# B配置\n\n- [配置] 服务端口为 9721\n")
    assert len(store.db.list_collisions(status="open")) == 1
    store.db.remove_note("notes/A配置.md")
    (store.root / "notes/A配置.md").unlink()

    r = store.audit()

    assert r["pruned_stale_collisions"] == 1


def test_write_system_files_refused(store: Store):
    """系统文件走专用工具，不允许 memory_write 直写。"""
    with pytest.raises(StoreError, match="系统文件"):
        store.write("TOPICS", "# 主题记忆注册表\n")  # title_to_path 自动补 .md
    with pytest.raises(StoreError, match="系统文件"):
        store.write("PROFILE", "# 用户画像\n")


def test_write_inside_topic_dir_allowed(store: Store):
    """注册主题目录即归属：卡所在目录下的模块笔记放行。"""
    r = store.write("notes/模块笔记", "# 模块笔记\n内容\n")
    assert r["path"] == "notes/模块笔记.md"


def test_write_journal_free_zone_allowed(store: Store):
    r = store.write("journal/2026-09-17-流水", "# 流水\n内容\n")
    assert r["path"].startswith("journal/")


def test_topic_register_then_module_write_allowed(store: Store):
    """先注册后写入：topic_register 是新主题的唯一授权门。"""
    store.topic_register("新主题", description="测试")
    r = store.write("topics/新主题/模块", "# 模块\n内容\n")
    assert r["path"] == "topics/新主题/模块.md"


def test_save_creation_outside_topics_refused_overwrite_allowed(store: Store):
    """save 只拦新建：编辑器覆盖已有文件与免注册区新建不受影响。"""
    with pytest.raises(StoreError, match="不属于任何注册主题"):
        store.save("test/散记.md", "# 散记\n内容\n")
    store.write("notes/已有笔记", "# 已有笔记\n内容\n")
    r = store.save("notes/已有笔记.md", "# 已有笔记\n改\n")
    assert r["path"] == "notes/已有笔记.md"
    r = store.save("curator/报告.md", "# 报告\n内容\n")
    assert r["path"] == "curator/报告.md"


def test_move_to_uncovered_target_refused(store: Store):
    """move 目标同样受注册制约束；archive/ 免注册区放行。"""
    store.write("notes/搬测试", "# 搬测试\n内容\n")
    with pytest.raises(StoreError, match="不属于任何注册主题"):
        store.move("notes/搬测试", "test/散记.md")
    r = store.move("notes/搬测试", "archive/搬测试.md")
    assert r["new_path"] == "archive/搬测试.md"


def test_audit_records_last_result_for_webui(store: Store):
    """store.audit() 把结果挂到 last_audit——MCP 跑完审计 WebUI 立即可见。"""
    store.write("notes/审计缓存测试", "# 审计缓存测试\n内容\n")
    r = store.audit()
    assert store.last_audit is not None
    assert store.last_audit["audit"]["audit_file"] == r["audit_file"]
    assert store.last_audit["ts"] > 0


def test_audit_exec_report_validates(store: Store):
    with pytest.raises(Exception, match="issue_id 非法"):
        store.audit_exec_report("随便写", "executing")
    with pytest.raises(Exception, match="event 非法"):
        store.audit_exec_report("D3:notes/a笔记.md|ghost", "done")
    r = store.audit_exec_report("D3:notes/a笔记.md|ghost", "executing",
                                note="开始", identity="r9000x_teleagent")
    assert r["timeline"][0]["identity"] == "r9000x_teleagent"
    assert r["timeline"][0]["kind"] == "D3"


def test_audit_auto_verifies_executed_issue(store: Store):
    """判断与执行分离的闭环：agent 汇报执行 → 问题消除 → 审计自动追加
    verified 封口事件（复审通过），且只确认一次不重放。"""
    store.write("notes/a笔记", "# a笔记\n引用 [[ghost]]。\n")
    r1 = store.audit()
    assert len(r1["dangling_links"]) == 1
    d3 = "D3:notes/a笔记.md|ghost"
    store.audit_exec_report(d3, "executing")
    store.audit_exec_report(d3, "executed", note="已补目标笔记")
    # 执行中的问题在审计输出里带最新动态
    r_mid = store.audit()
    assert r_mid["exec_status"][d3]["event"] == "executed"

    store.write("notes/ghost", "# ghost\n目标出现了。\n")
    r2 = store.audit()
    assert r2["dangling_links"] == []
    assert r2["verified"] == [d3]
    assert store.db.exec_last_status()[d3]["event"] == "verified"

    # 复审通过只确认一次：再跑审计不重放
    r3 = store.audit()
    assert r3["verified"] == []


def _proposal_markdown() -> str:
    return (
        "# 记忆质量提案（测试，2026-09-25）\n\n"
        "**状态：待裁决** —— 本报告由 curator 生成，仅含提案。\n\n"
        "## 提案（2 条）\n"
        "1. **[medium] outdated** — 条目一\n"
        "   - 涉及: notes/a.md\n"
        "   - 建议: 处理条目一\n"
        "2. **[low] other** — 条目二\n"
        "   - 涉及: notes/b.md\n"
    )


def test_proposal_settled_marker_and_search_hiding(store: Store, searcher):
    """全部条目执行/忽略 → 提案自动打已结案标记并从检索结果隐去；
    显式 memory_read 仍可读（明确查阅不受限）。"""
    from yacmemo.store import PROPOSAL_SETTLED_MARKER

    rel = "curator/提案-20260925测试.md"
    store.save(rel, _proposal_markdown())
    assert any(h["path"] == rel for h in searcher.search("提案"))

    store.record_proposal_action(rel, 1, "dismissed", type_="outdated", reason="条目一")
    store.audit_exec_report(f"P:{rel}:2", "executed", note="done")
    content = (store.root / rel).read_text(encoding="utf-8")
    assert PROPOSAL_SETTLED_MARKER in content
    assert "**状态：已结案**" in content

    # 检索隐去（FTS 仍命中，被 search 主动过滤）；显式读取不受限
    assert not any(h["path"] == rel for h in searcher.search("提案"))
    assert "已结案" in store.read(rel)["content"]


def test_proposal_not_settled_until_all_findings_done(store: Store):
    from yacmemo.store import PROPOSAL_SETTLED_MARKER

    rel = "curator/提案-20260925b.md"
    store.save(rel, _proposal_markdown())
    store.record_proposal_action(rel, 1, "dismissed", type_="outdated", reason="条目一")
    assert PROPOSAL_SETTLED_MARKER not in (store.root / rel).read_text(encoding="utf-8")
    store.audit_exec_report(f"P:{rel}:2", "executing")  # 执行中不算结案
    assert PROPOSAL_SETTLED_MARKER not in (store.root / rel).read_text(encoding="utf-8")


def test_audit_reconciles_hand_stamped_settled_proposal(store: Store):
    """0.3.4 之前完成的工作：agent 手工打了结案标、无执行事件——审计补记
    executed（含旧口径「已采纳」条目）；dismissed 不翻转；幂等；P 类不产生
    复审通过事件。"""
    rel = "curator/提案-20260925c.md"
    content = _proposal_markdown().replace(
        "**状态：待裁决**",
        "> 状态：已结案 —— 全部 2 条已裁决执行完毕（agent 2026-09-25 补标）")
    store.save(rel, content)
    store.record_proposal_action(rel, 1, "adopted", type_="outdated", reason="条目一")
    store.record_proposal_action(rel, 2, "dismissed", type_="other", reason="条目二")

    r = store.audit()
    assert r["reconciled_proposals"] == 1
    last = store.db.exec_last_status()
    assert last[f"P:{rel}:1"]["event"] == "executed"
    assert last[f"P:{rel}:1"]["identity"] == "reconcile"
    assert f"P:{rel}:2" not in last

    r2 = store.audit()
    assert r2["reconciled_proposals"] == 0
    assert store.db.exec_last_status()[f"P:{rel}:1"]["event"] == "executed"


def test_reconciles_untracked_findings_under_marker(store: Store, searcher):
    """手工打标 = 人/agent 断言全部收口：无记录条目也补记 executed
    （0.3.5 承诺的"两条路都算数"，旧客户端会话拿不到汇报工具）。"""
    rel = "curator/提案-20260925d.md"
    content = _proposal_markdown().replace(
        "**状态：待裁决**", "> 状态：已结案（agent 补标）")
    store.save(rel, content)
    r = store.audit()
    assert r["reconciled_proposals"] == 2
    last = store.db.exec_last_status()
    assert last[f"P:{rel}:1"]["event"] == "executed"
    assert last[f"P:{rel}:2"]["event"] == "executed"
    assert not any(h["path"] == rel for h in searcher.search("提案"))


def test_rereview_findings_reconciled_under_hand_stamped_marker(store: Store, searcher):
    """复审追加的条目由 agent 处置后手工打标（旧客户端会话无汇报工具）——
    审计补记全部缺事件条目，标记保持、检索保持隐身。
    （复审"失效标"场景在 curator 追加复审节时即时撤标，不依赖审计侧。）"""
    rel = "curator/提案-20260925e.md"
    store.save(rel, _proposal_markdown())
    p = store.root / rel
    # curator 同日复审追加复审节（旧版本无撤标钩子时的文件形态）
    p.write_text(p.read_text(encoding="utf-8")
                 + "\n---\n\n## 复审（2026-09-25 10:00）\n\n"
                   "本次复审发现 2 条新问题，待裁决：\n"
                   "1. **[low] other** — 新条目一\n   - 涉及: notes/a.md\n"
                   "2. **[low] other** — 新条目二\n   - 涉及: notes/b.md\n",
                 encoding="utf-8")
    # agent 处置完毕后手工打标（覆盖全部 4 条）
    p.write_text("> 状态：已结案 —— 复审条目已处置\n" + p.read_text(encoding="utf-8"),
                 encoding="utf-8")
    r = store.audit()
    assert r["reconciled_proposals"] == 4
    last = store.db.exec_last_status()
    for i in range(1, 5):
        assert last[f"P:{rel}:{i}"]["event"] == "executed"
    assert "已结案" in p.read_text(encoding="utf-8")
    assert not any(h["path"] == rel for h in searcher.search("提案"))


def test_rereview_numbering_is_sequential_across_sections(store: Store):
    """复审节打印序号从 1 重来，但全局序号必须跨节连续（P id 唯一性）。"""
    rel = "curator/提案-20260925f.md"
    store.save(rel, _proposal_markdown())
    p = store.root / rel
    p.write_text(p.read_text(encoding="utf-8")
                 + "\n---\n\n## 复审（2026-09-25 11:00）\n\n"
                   "本次复审发现 3 条新问题，待裁决：\n"
                   "1. **[low] other** — 新一\n2. **[low] other** — 新二\n"
                   "3. **[medium] stale-card** — 新三\n", encoding="utf-8")
    assert store._proposal_findings_indices(rel) == [1, 2, 3, 4, 5]


def test_topic_name_links_resolve(store: Store, searcher):
    """[[主题名]] 指向主题卡：卡的索引标题随 H1 漂移后，按注册表主题名
    解析（read 相关笔记 + D3 不误报）。"""
    store.topic_register("网络主题", description="测试")
    # H1 漂移：卡的索引标题不再是主题名
    store.save("topics/网络主题/abstract.md", "# network\n\n网络主题的现状卡。\n")
    store.write("notes/引用方", "# 引用方\n\n详见 [[网络主题]]。\n")

    # D3 不误报
    r = store.audit()
    assert all("网络主题" not in d["link"] for d in r["dangling_links"])
    # 相关笔记按主题名解析到卡
    rel = {x["title"]: x for x in store.read("notes/引用方")["related"]}
    assert rel["网络主题"]["path"] == "topics/网络主题/abstract.md"


def test_settlement_cycle_with_rereview_and_old_session_agent(store: Store, searcher):
    """全周期不变量（0.3.5/0.3.7 承诺，TeleAgent 实况回归）：
    结案 → 同日复审追加（curator 即时撤标）→ 新条目可见待裁决 →
    旧会话 agent（拿不到汇报工具）手工打标收尾 → 再收敛。每一步
    对 WebUI 状态机与检索的可见性都必须正确。"""
    rel = "curator/提案-20260925g.md"
    store.save(rel, _proposal_markdown())
    # 轮 1：全部采纳+执行 → 自动打标、检索隐身
    for i, t in [(1, "outdated"), (2, "other")]:
        store.record_proposal_action(rel, i, "adopted", type_=t, reason=f"条目{i}")
    for i in (1, 2):
        store.audit_exec_report(f"P:{rel}:{i}", "executed")
    store.audit()
    assert "已结案" in (store.root / rel).read_text(encoding="utf-8")
    assert not any(h["path"] == rel for h in searcher.search("提案"))

    # 轮 2：同日复审追加新条目（curator 钩子即时撤标 + 状态复位）
    p = store.root / rel
    lines = [ln for ln in p.read_text(encoding="utf-8").splitlines()
             if "已结案" not in ln]
    content = ("\n".join(lines) + "\n").replace(
        "**状态：已结案**", "**状态：待裁决**", 1)
    content += ("\n---\n\n## 复审（2026-09-25 10:00）\n\n"
                "本次复审发现 1 条新问题，待裁决：\n"
                "1. **[low] other** — 复审新条目\n   - 涉及: notes/a.md\n")
    p.write_text(content, encoding="utf-8")
    store.audit()
    # 新条目可见待裁决（全局序号 3）
    assert store.db.exec_last_status().get(f"P:{rel}:3") is None
    assert any(h["path"] == rel for h in searcher.search("提案"))

    # 轮 3：旧会话 agent（无 memory_audit_update）手工打标收尾
    lines = p.read_text(encoding="utf-8").splitlines()
    content = "\n".join(
        [lines[0], "", "> 状态：已结案 —— 复审条目已由用户裁决处置"]
        + lines[1:]) + "\n"
    p.write_text(content, encoding="utf-8")
    r2 = store.audit()
    assert r2["reconciled_proposals"] == 1   # 全局序号 3 补记
    assert "已结案" in p.read_text(encoding="utf-8")
    assert not any(h["path"] == rel for h in searcher.search("提案"))
    # 幂等
    assert store.audit()["reconciled_proposals"] == 0


def test_write_echoes_new_collisions(store):
    """写时撞车必须即时回显（agent 不该等审计才知道制造了 D2）。"""
    store.write("notes/撞车甲", "# 撞车甲\n\n- [配置] yacmemo 服务端口是 9721\n")
    r = store.write("notes/撞车乙", "# 撞车乙\n\n- [配置] yacmemo 服务端口是 9721\n")
    assert r["new_collisions"], "写响应必须带回新撞车"
    assert any(c["with_path"] == "notes/撞车甲.md" for c in r["new_collisions"])
    assert r["new_collisions"][0]["text"] == "yacmemo 服务端口是 9721"


def test_note_archive_and_unarchive_roundtrip(store: Store):
    """单篇归档：主题内模块笔记 → archive/<主题名>/，摘要行注入；
    abstract 拒绝；取消归档移回；整主题归档目的地兼容（同目录）。"""
    store.topic_register("家庭主题", description="x")
    store.write("topics/家庭主题/外网笔记", "# 外网笔记\n\n- [配置] VPS 2 台\n")
    rel = "topics/家庭主题/外网笔记.md"

    r = store.note_archive(rel, reason="历史参考")
    assert r["to"] == "archive/家庭主题/外网笔记.md"
    assert not (store.root / rel).is_file()
    body = (store.root / r["to"]).read_text(encoding="utf-8")
    assert "已归档（" in body and "历史参考" in body

    # abstract 拒绝单独归档
    with pytest.raises(Exception, match="主题卡"):
        store.note_archive("topics/家庭主题/abstract.md")

    # 先取消归档再验证往返
    r2 = store.note_unarchive(r["to"])
    assert r2["to"] == rel
    assert (store.root / rel).is_file()
