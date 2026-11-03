"""Pure session-domain recovery. Reducers never own runtime resources or effects."""
from copy import deepcopy

VERSION = 1


def initial_state():
    return {'latest': {}, 'queues': {'steering': [], 'follow_up': []}, 'children': {},
            'messages': []}


def apply_event(state, record):
    """Compatibility aggregate; domain units own all transition definitions.

    Keep the historical in-place interface while sharing the reducers used by
    SessionStore's narrow reads and checkpoint recovery.
    """
    for key in state:
        state[key] = UNITS[key].apply(state[key], record)
    return state


def replay(records, state=None):
    state = initial_state() if state is None else deepcopy(state)
    for record in records:
        apply_event(state, record)
    return state


class ProjectionUnit:
    """Versioned deterministic transition for one domain; no runtime effects."""
    state_version = 1

    def __init__(self, key, kinds, initial):
        self.key, self.kinds, self.initial = key, frozenset(kinds), initial

    def init(self):
        return deepcopy(self.initial)

    def validate(self, state):
        if self.key == 'messages':
            return isinstance(state, list) and all(isinstance(m,dict) and m.get('role') in {'user','assistant','system','tool'} and isinstance(m.get('content',''),str) for m in state)
        if self.key == 'model':
            return state is None or (isinstance(state,dict) and all(isinstance(state.get(k),str) for k in ('provider_id','model_id')))
        if self.key == 'goal':
            return state is None or state == {} or (isinstance(state,dict) and isinstance(state.get('objective'),str) and type(state.get('revision',1)) is int and state.get('revision',1) >= 1)
        if self.key == 'queues':
            return (isinstance(state,dict) and set(state) == {'steering','follow_up'}
                    and all(isinstance(q,list) and all(isinstance(item,dict) and all(isinstance(item.get(k),str) for k in ('id','text')) for item in q) for q in state.values()))
        return isinstance(state,dict) and all(isinstance(v,dict) for v in state.values())

    def apply(self, state, record):
        kind, data = record['kind'], record['data']
        if record.get('_projectable') is False or (self.key != 'latest' and kind not in self.kinds):
            return state
        if self.key == 'messages':
            return state + [deepcopy(data)] if kind == 'message' else deepcopy(data['messages'])
        if self.key == 'model':
            return deepcopy(data)
        if self.key == 'goal':
            if kind == 'goal_change':
                if data['operation'] == 'create':
                    if data['revision'] != 1 or (state and state.get('state') != 'completed'):
                        raise ValueError('Invalid goal creation sequence')
                elif not state or data['revision'] != state.get('revision', 1) + 1 or (state.get('goal_id') and state['goal_id'] != data['goal_id']):
                    raise ValueError('Discontinuous goal revision')
            return deepcopy(data.get('snapshot', {}) if kind == 'goal_change' else data)
        result = deepcopy(state)
        if self.key == 'latest':
            result[kind] = deepcopy(data)
            if kind == 'goal_change':
                result['goal'] = deepcopy(data['snapshot'])
        elif self.key == 'queues':
            if kind == 'input_queued':
                result.setdefault(data['kind'], []).append(deepcopy(data))
            else:
                result = {key: [item for item in queue if item['id'] != data['id']] for key, queue in result.items()}
        elif self.key == 'children':
            task = result.setdefault(data['task_id'], {})
            task.update(deepcopy(data))
            if kind == 'child_result':
                task['result'] = deepcopy(data)
            elif kind == 'child_result_ack':
                task['acknowledged'] = True
        return result

    def checkpoint(self, state):
        if not self.validate(state):
            raise ValueError('Invalid projection state: ' + self.key)
        return deepcopy(state)


UNITS = {
    'messages': ProjectionUnit('messages', {'message', 'seed', 'compaction'}, []),
    'model': ProjectionUnit('model', {'model_change'}, None),
    'queues': ProjectionUnit('queues', {'input_queued', 'input_consumed'}, {'steering': [], 'follow_up': []}),
    'goal': ProjectionUnit('goal', {'goal', 'goal_change'}, None),
    'children': ProjectionUnit('children', {'child_task', 'child_result', 'child_result_ack'}, {}),
    'latest': ProjectionUnit('latest', set(), {}),
}
