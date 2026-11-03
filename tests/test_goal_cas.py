import pytest
from weyra.session import SessionStore


def test_goal_resume_preserves_and_stale_ref_rejected(tmp_path):
    from weyra.goals import GoalService, GoalError
    store = SessionStore(project=tmp_path)
    service = GoalService(store)
    goal = service.create('ship', [{'text':'ship','done':False}])
    goal.continuations = 2
    goal = service.update(service.ref(), goal)
    stale = service.ref()
    service.mutate(stale, 'pause')
    with pytest.raises(GoalError, match='stale_goal_revision'):
        service.mutate(stale, 'resume')
    goal = service.mutate(service.ref(), 'resume')
    assert goal.continuations == 2
    goal = service.mutate(service.ref(), 'restart')
    assert goal.continuations == 0
    restored = GoalService(store).current()
    assert not restored.armed and restored.goal_id == goal.goal_id
    store.close()


def test_goal_limit_restart_and_completed_transition(tmp_path):
    from weyra.goals import GoalService, GoalError
    store = SessionStore(project=tmp_path)
    service = GoalService(store, {'max_continuations':1})
    goal = service.create('x')
    goal.continuations = 1
    service.update(service.ref(), goal)
    with pytest.raises(GoalError,match='goal_limit_reached'):
        service.mutate(service.ref(),'resume')
    service.mutate(service.ref(),'restart')
    service.mutate(service.ref(),'complete')
    with pytest.raises(GoalError,match='invalid_goal_transition'):
        service.mutate(service.ref(),'resume')
    store.close()
