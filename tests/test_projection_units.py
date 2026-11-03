from weyra.session import SessionStore
from weyra.contracts import Message


def test_narrow_goal_read_does_not_load_messages(tmp_path, monkeypatch):
    store = SessionStore(project=tmp_path)
    store.add_message(Message('user', 'large history'))
    store.append('goal', {'objective': 'ship'})
    assert store.goal_state()['objective'] == 'ship'
    assert 'messages' not in store._projection_cache
    expected = store.domain_state()
    store.save_projection_checkpoint()
    store._projection_cache = None
    assert store.domain_state() == expected
    store.close()


def test_compatibility_replay_uses_child_delivery_acknowledgement():
    from weyra.projections import UNITS, replay
    records = [
        {'kind': 'child_task', 'data': {'task_id': 'child', 'state': 'running'}},
        {'kind': 'child_result', 'data': {'task_id': 'child', 'text': 'done'}},
        {'kind': 'child_result_ack', 'data': {'task_id': 'child'}},
    ]
    children = UNITS['children'].init()
    for record in records:
        children = UNITS['children'].apply(children, record)
    assert replay(records)['children'] == children
    assert children['child']['acknowledged'] is True
