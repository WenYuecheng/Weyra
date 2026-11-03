import sqlite3

import pytest

from weyra.contracts import Message
from weyra.session import SessionStore, SessionError


def test_version_and_branch_projection(tmp_path):
    store = SessionStore(project=tmp_path)
    assert store._db.execute('PRAGMA user_version').fetchone()[0] == 2
    root = store.add_message(Message('user', 'root'))['id']
    store.append('input_queued', {'id': 'q', 'kind': 'steering', 'text': 'next'})
    assert store.domain_state()['queues']['steering'][0]['id'] == 'q'
    store.branch(root)
    assert store.domain_state()['queues']['steering'] == []
    assert [m.content for m in store.project()] == ['root']
    store.close()


def test_checkpoint_suffix_equals_full_replay(tmp_path):
    store = SessionStore(project=tmp_path)
    store.append('goal', {'objective': 'ship', 'state': 'active'})
    store.save_projection_checkpoint()
    store.append('input_queued', {'id': 'q', 'kind': 'follow_up', 'text': 'next'})
    expected = store.domain_state()
    store._projection_cache = None
    assert store.domain_state() == expected
    sid = store.session_id
    store.close()
    reopened = SessionStore(project=tmp_path, session_id=sid)
    assert reopened.domain_state() == expected
    reopened.close()


def test_future_schema_refused(tmp_path):
    store = SessionStore(project=tmp_path)
    store.close()
    with sqlite3.connect(tmp_path / '.weyra/state.db') as db:
        db.execute('PRAGMA user_version=99')
    with pytest.raises(SessionError, match='version'):
        SessionStore(project=tmp_path)


def test_event_versions_and_optional_extensions(tmp_path):
    store = SessionStore(project=tmp_path)
    store.append('extension', {'x': 1})
    with pytest.raises(SessionError):
        store.append('future', {}, ignorable=False)
    with pytest.raises(SessionError):
        store.append('goal', {}, schema_version=99)
    assert store.latest('extension') == {'x': 1}
    store.close()


def test_corrupt_checkpoint_is_discarded(tmp_path):
    store = SessionStore(project=tmp_path)
    store.add_message(Message('user', 'kept'))
    store.save_projection_checkpoint()
    store._db.execute("UPDATE projection_checkpoints SET state='{}'")
    store._projection_cache = None
    assert store.project()[0].content == 'kept'
    store.close()


def test_unknown_required_event_rejected_on_reopen(tmp_path):
    store = SessionStore(project=tmp_path)
    event = store.append('extension', {})
    sid = store.session_id
    store.close()
    with sqlite3.connect(tmp_path / '.weyra/state.db') as db:
        db.execute('UPDATE event_metadata SET ignorable=0 WHERE event_id=?', (event['id'],))
    with pytest.raises(SessionError, match='required'):
        SessionStore(project=tmp_path, session_id=sid)


def test_transaction_rollback_invalidates_projection(tmp_path):
    store = SessionStore(project=tmp_path)
    expected = store.domain_state()
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.append('goal', {'objective': 'discard'})
            assert store.latest('goal')['objective'] == 'discard'
            raise RuntimeError('rollback')
    assert store.domain_state() == expected
    store.close()


def test_optional_future_child_event_does_not_break_interruption_recovery(tmp_path):
    store = SessionStore(project=tmp_path)
    store.append('child_task', {'future_payload': True}, schema_version=2, ignorable=True)
    store.recover_interrupted()
    assert store.domain_state()['children'] == {}
    store.close()


def test_malformed_metadata_schema_does_not_upgrade_legacy_database(tmp_path):
    store = SessionStore(project=tmp_path)
    store.close()
    with sqlite3.connect(tmp_path / '.weyra/state.db') as db:
        db.execute('DROP TABLE event_metadata')
        db.execute('CREATE TABLE event_metadata (broken TEXT)')
        db.execute('PRAGMA user_version=0')
    with pytest.raises(SessionError):
        SessionStore(project=tmp_path)
    with sqlite3.connect(tmp_path / '.weyra/state.db') as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 0


def test_p1_configuration_reaches_runtime(tmp_path):
    from weyra.config import load_config
    (tmp_path / 'weyra.toml').write_text('[routes.main]\nprovider="deepseek"\nmodel="deepseek-chat"\ncontext_window=200000\nmax_output_tokens=1000\n[tools]\napply_patch=true\n[goal_policy]\nmax_continuations=5\n[retry_policy]\nargument_retries=0\n')
    config = load_config(tmp_path, {'provider': 'deepseek', 'model': 'deepseek-chat'})
    runtime = config.build_runtime()
    assert runtime.apply_patch_enabled is True
    assert runtime.goal_policy == {'max_continuations': 5, 'max_stagnant_rounds': 2}
    assert runtime.argument_retries == 0


@pytest.mark.parametrize('text', ['[tools]\napply_patch=1', '[goal_policy]\nmax_continuations=0', '[retry_policy]\nargument_retries=-1', '[tools]\nunknown=true'])
def test_invalid_p1_configuration_rejected(tmp_path, text):
    from weyra.config import load_config
    (tmp_path / 'weyra.toml').write_text(text)
    with pytest.raises(ValueError):
        load_config(tmp_path, {'provider': 'deepseek', 'model': 'deepseek-chat'})
