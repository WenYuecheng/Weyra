import asyncio
from weyra.memory import MemoryStore
from weyra.skills import SkillStore


def test_external_memory_write_refreshes_existing_instance(tmp_path):
    a, b = MemoryStore(tmp_path), MemoryStore(tmp_path)
    a.update('style', '中文写作避免空话')
    assert b.search('中文')[0]['content'] == '中文写作避免空话'
    a.update('style', '中文写作保持简洁')
    assert b.search('中文')[0]['version'] == 2


def test_recall_entries_pin_version_and_wrapper_budget(tmp_path):
    store = MemoryStore(tmp_path)
    store.update('a', '中' * 4000)
    entries = asyncio.run(store.recall_entries('中'))
    assert entries[0]['content_hash']
    assert len(entries[0]['wrapped'].encode()) <= 4096
    assert entries[0]['wrapped'].endswith('</memory>')


def test_project_skill_overrides_user(tmp_path):
    user = tmp_path / 'user'
    project = tmp_path / 'project'
    for directory, body in ((user, 'user'), (project / '.weyra' / 'skills', 'project')):
        path = directory / 'writer' / 'SKILL.md'
        path.parent.mkdir(parents=True)
        path.write_text('---\nname: writer\n---\n' + body)
    assert SkillStore(project, skill_dirs=[user]).load('writer').body == 'project'
