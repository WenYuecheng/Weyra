from weyra.presentation import project_raw_session_records


def test_resource_snapshots_are_not_presented_as_user_messages():
    records = [
        {'id': 'context', 'kind': 'message', 'data': {'role': 'user', 'source': 'project_rules', 'content': 'rules', 'provider_data': {'context_version': 1}}},
        {'id': 'user', 'kind': 'message', 'data': {'role': 'user', 'source': 'user', 'content': 'task'}},
    ]
    assert [c.content for c in project_raw_session_records(records)] == ['task']
