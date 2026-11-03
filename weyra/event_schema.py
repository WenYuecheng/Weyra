"""Version declarations for owned durable events and optional extensions."""
OWNED = frozenset(('attempt_end', 'attempt_start', 'baseline', 'branch', 'budget_checkpoint', 'budget_settlement', 'child_result', 'child_result_ack', 'child_task', 'cleanup_errors', 'compaction', 'compression_failure', 'context_measurement', 'context_version', 'environment', 'evaluation_case', 'evaluation_case_start', 'evaluation_group', 'evaluation_report', 'evolution_result', 'goal', 'goal_change', 'input_consumed', 'input_queued', 'job_cancel', 'job_start', 'job_status', 'job_transfer', 'mcp_trust', 'memory_recall', 'message', 'model_change', 'model_request', 'model_route_change', 'model_usage', 'pending_window', 'project_rules', 'resource_context', 'seed', 'session', 'skill_evolution', 'skill_judge', 'skill_proposal', 'skill_validation', 'summary_segment', 'tool_intent', 'tool_result', 'tool_result_view', 'turn_end', 'turn_start'))

def validate_event(kind, data, version=1, ignorable=True):
    if not isinstance(kind, str) or not kind or not isinstance(data, dict):
        raise ValueError("Invalid event envelope")
    if type(version) is not int or version < 1 or type(ignorable) is not bool:
        raise ValueError("Invalid event version declaration")
    if kind not in OWNED or version != 1:
        if not ignorable:
            raise ValueError("Unsupported required event version")
        return False
    required = {'message': ('role',), 'seed': ('messages',), 'compaction': ('messages',),
                'input_queued': ('id', 'kind', 'text'), 'input_consumed': ('id',),
                'child_task': ('task_id',), 'child_result': ('task_id',)}
    if any(key not in data for key in required.get(kind, ())):
        raise ValueError("Invalid owned event payload: " + kind)
    if kind in {'seed', 'compaction'} and not isinstance(data['messages'], list):
        raise ValueError("Invalid message projection")
    if kind == 'message':
        if data['role'] not in {'user', 'assistant', 'system', 'tool'} or not isinstance(data.get('content', ''), str):
            raise ValueError("Invalid message payload")
    if kind == 'input_queued' and (data['kind'] not in {'steering', 'follow_up'} or any(not isinstance(data[k], str) for k in ('id', 'text'))):
        raise ValueError("Invalid queued input")
    if kind == 'goal' and data and not isinstance(data.get('objective'), str):
        raise ValueError("Invalid goal objective")
    if kind == 'goal_change':
        if (data.get('operation') not in {'create','revise','restart','resume','pause','complete','block','update'}
                or not isinstance(data.get('goal_id'), str) or not data['goal_id']
                or type(data.get('revision')) is not int or data['revision'] < 1
                or not isinstance(data.get('snapshot'), dict)
                or data['snapshot'].get('goal_id') != data['goal_id']
                or data['snapshot'].get('revision') != data['revision']):
            raise ValueError('Invalid goal change envelope')
    return True
