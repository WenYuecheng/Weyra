"""Run the public service API offline without credentials or network access."""
import asyncio
import tempfile

from weyra.contracts import ModelInfo, ModelResponse, Usage
from weyra.model_runtime import ModelRuntime, ProviderRegistry, ScriptedProvider
from weyra.service import WeyraAppService


def offline_runtime():
    """Create one deterministic response for the SDK walkthrough."""
    registry = ProviderRegistry()
    registry.register('demo', ScriptedProvider([ModelResponse('Weyra SDK ready.', usage=Usage(8, 4))]))
    return ModelRuntime(registry, {'main': ModelInfo('demo', 'scripted', 131072, 4096)})


async def main():
    """Subscribe before submission and wait for the terminal event."""
    with tempfile.TemporaryDirectory(prefix='weyra-sdk-') as project:
        service = WeyraAppService(project, runtime_factory=offline_runtime,
                                 enable_memory=False, enable_evolution=False)
        await service.start()
        _, subscription = await service.subscribe('sdk-example')
        try:
            receipt = await service.submit('Hello', client_request_id='hello-1')
            assert receipt.accepted
            async with asyncio.timeout(10):
                while True:
                    event = await subscription.get()
                    if event is None:
                        raise RuntimeError('Subscription ended before task completion')
                    if event.kind == 'text_delta':
                        print(event.data['delta'], end='', flush=True)
                    elif event.kind == 'service_error':
                        raise RuntimeError(event.data['message'])
                    elif event.kind == 'turn_end':
                        assert event.data['state'] == 'completed'
                        print()
                        break
        finally:
            service.unsubscribe('sdk-example')
            await service.close()


if __name__ == '__main__':
    asyncio.run(main())
