"""Ensure the rich UI preserves the existing CLI's command and policy contracts."""
import asyncio
import pytest
from unittest.mock import AsyncMock
from weyra.service import WeyraAppService
from tests.test_service import make_test_runtime


def make_service(path, **kwargs):
    return WeyraAppService(path, runtime_factory=make_test_runtime,
                           enable_memory=False, enable_evolution=False, **kwargs)


def test_readonly_memory_and_original_commands(tmp_path):
    async def check():
        service = make_service(tmp_path, permission_mode='plan')
        await service.start()
        try:
            denied = await service.execute_command('/memory save preference content', client_request_id='write')
            assert not denied.ok
            tree = await service.execute_command('/tree', client_request_id='tree')
            assert tree.ok and isinstance(tree.data, list)
            fork = await service.execute_command('/fork snapshot', client_request_id='fork')
            assert fork.ok and fork.data == 'snapshot'
            invalid = await service.execute_command('/memory "', client_request_id='bad')
            assert not invalid.ok and invalid.code == 'invalid_input'
        finally:
            await service.close()
    asyncio.run(check())


def test_rejected_rollback_is_not_reported_as_success(tmp_path):
    async def check():
        service = make_service(tmp_path)
        await service.start()
        try:
            service.agent.rollback_skill = AsyncMock(return_value={
                'status': 'rejected', 'reason': 'Permission denied'})
            result = await service.execute_command('/skill-rollback demo 1.0.0', client_request_id='rollback')
            assert not result.ok
            assert result.code == 'permission_denied'
        finally:
            await service.close()
    asyncio.run(check())


def test_duplicate_submission_checks_text_and_schedules_only_one_task(tmp_path):
    async def check():
        service = make_service(tmp_path)
        await service.start()
        try:
            await service.submit('first', client_request_id='one')
            with pytest.raises(ValueError, match='request_conflict'):
                await service.submit('different', client_request_id='one')
            receipt = await service.submit('second', client_request_id='two')
            assert receipt.mode == 'steering'
        finally:
            await service.close()
    asyncio.run(check())


def test_missing_session_does_not_replace_current_session(tmp_path):
    async def check():
        service = make_service(tmp_path)
        await service.start()
        original = service.session_id
        try:
            with pytest.raises(ValueError):
                await service.open_session('nonexistent')
            assert service.session_id == original
            assert service.agent is not None
        finally:
            await service.close()
    asyncio.run(check())
