"""Transactional goal mutations; execution authority never enters the log."""
from copy import deepcopy
from dataclasses import dataclass, asdict
from .contracts import digest, new_id
from .core.state import Goal


@dataclass(frozen=True)
class GoalRef:
    goal_id: str
    revision: int

    def __post_init__(self):
        if not isinstance(self.goal_id, str) or not self.goal_id or type(self.revision) is not int or self.revision < 1:
            raise ValueError('Invalid goal reference')


class GoalError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class GoalService:
    def __init__(self, session, policy=None):
        self.session, self.policy = session, dict(policy or {})
        self._armed = False

    def current(self):
        data = self.session.goal_state()
        if not data:
            return None
        goal = Goal.restore(data)
        goal.goal_id = goal.goal_id or digest(self.session.session_id + ':legacy-goal')[:32]
        goal.armed = self._armed and goal.state == 'active'
        return goal

    def ref(self):
        goal = self.current()
        if goal is None:
            raise GoalError('goal_not_found')
        return GoalRef(goal.goal_id, goal.revision)

    def _commit(self, expected, operation, change, armed=None):
        with self.session.transaction():
            current = self.current()
            if current is None:
                raise GoalError('goal_not_found')
            if GoalRef(current.goal_id, current.revision) != expected:
                raise GoalError('stale_goal_revision')
            candidate = change(deepcopy(current))
            candidate.goal_id, candidate.revision = current.goal_id, current.revision + 1
            self.session.append('goal_change', {'goal_id': candidate.goal_id,
                'revision': candidate.revision, 'operation': operation, 'snapshot': asdict(candidate)}, True)
        self._armed = candidate.armed if armed is None else armed
        candidate.armed = self._armed and candidate.state == 'active'
        return candidate

    def create(self, objective, todos=None):
        if self.current() is not None and self.current().state != 'completed':
            raise GoalError('invalid_goal_transition')
        goal = Goal(objective, todos=todos or [{'text': objective, 'done':False}], goal_id=new_id())
        with self.session.transaction():
            if self.current() is not None and self.current().state != 'completed':
                raise GoalError('invalid_goal_transition')
            self.session.append('goal_change', {'goal_id':goal.goal_id,'revision':goal.revision,
                'operation':'create','snapshot':asdict(goal)}, True)
        self._armed = True
        return goal

    def update(self, expected, candidate, operation='update'):
        return self._commit(expected, operation, lambda previous: deepcopy(candidate))

    def mutate(self, expected, operation, **values):
        def change(goal):
            if operation == 'resume':
                if goal.state == 'completed':
                    raise GoalError('invalid_goal_transition')
                if goal.continuations >= self.policy.get('max_continuations',3):
                    raise GoalError('goal_limit_reached')
                goal.state, goal.armed = 'active', True
                goal.blocker_code = goal.blocker_reason = ''
            elif operation in {'restart','revise'}:
                if operation == 'revise':
                    goal.objective = values['objective']
                    goal.todos = values.get('todos') or [{'text':goal.objective,'done':False}]
                goal.continuations = goal.stagnant_rounds = 0
                goal.progress = goal.blocker_code = goal.blocker_reason = ''
                goal.state, goal.armed = 'active', True
            elif operation in {'pause','complete','block'}:
                goal.state = {'pause':'paused','complete':'completed','block':'blocked'}[operation]
                goal.armed = False
                if operation == 'block':
                    goal.blocker_code = values.get('code','execution-failed')
                    goal.blocker_reason = values.get('reason','')
            else:
                raise GoalError('invalid_goal_transition')
            return goal
        return self._commit(expected, operation, change)
