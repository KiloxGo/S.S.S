from sss.study import prompt_catalog
from sss.sync import Sync


def course_plan():
    return {"schema_version": 1, "code": "EE3325", "name": "高电压技术", "term": {"id": 30, "name": "2026-2027 Fall"}, "canvas_ids": [93540], "teachers": ["吴建东"]}


def test_course_export_is_self_contained_and_protects_personal_work_on_refresh(tmp_path):
    plan = course_plan()
    first = Sync({}, plan, tmp_path / "data")
    first.run([])
    for name in ("AGENTS.md", "CLAUDE.md", "SCHEMA.md", "PROMPTS.md"):
        assert (first.root / name).is_file()
        assert name in (first.root / "README.md").read_text(encoding="utf-8")
    guide = (first.root / "AGENTS.md").read_text(encoding="utf-8")
    assert "吴建东" in guide and "2026-2027 Fall" in guide
    assert "{{" not in guide
    assert "HH:MM:SS" in guide and "年份未知" in guide
    assert "render-" not in (first.root / "PROMPTS.md").read_text(encoding="utf-8")
    notes = first.root / "notes" / "我的笔记.md"
    notes.parent.mkdir()
    notes.write_text("我的长期笔记", encoding="utf-8")
    (first.root / "source.md").write_text("我的长期笔记", encoding="utf-8")
    (first.root / "AGENTS.md").write_text("我的手写课程约定", encoding="utf-8")
    first.state.db.close()
    second = Sync({}, plan, tmp_path / "data")
    second.run([])
    assert notes.read_text(encoding="utf-8") == "我的长期笔记"
    assert (second.root / "AGENTS.md").read_text(encoding="utf-8") == "我的手写课程约定"
    assert (second.root / "source.md").is_file()
    second.state.db.close()


def test_prompt_context_and_evidence_rules_are_available_without_repository(tmp_path):
    root = tmp_path / "课程资料"
    prompts = prompt_catalog(course_plan(), root)
    assert len(prompts) == 6
    for prompt in prompts:
        assert str(root.resolve()) in prompt["text"]
        assert "EE3325" in prompt["text"] and "吴建东" in prompt["text"]
        assert "SCHEMA.md" in prompt["text"] and "HH:MM:SS" in prompt["text"]
        assert "往届" in prompt["text"] and "待核实" in prompt["text"]
    assert not root.exists()  # Copying a prompt does not sync or create a course.
