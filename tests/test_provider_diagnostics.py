import asyncio
import json
import pytest
from weyra.llm.providers import _error, ModelError
from weyra.contracts import ModelInfo, PreparedRequest, Message
from weyra.core.model_runtime import ModelRuntime, ProviderRegistry
from weyra.core.state import Budget, RunContext
from weyra.session import SessionStore


def test_sanitized_diagnostic_persists_without_secret_message(tmp_path):
    class UpstreamError(Exception):
        status_code = 400
        body = {"error": {"code": "invalid_request_error", "message": "Bearer secret-key"}}

    class Provider:
        async def stream(self, request, context):
            raise UpstreamError("secret-key")
            yield

        async def close(self):
            pass

    async def run():
        registry = ProviderRegistry()
        registry.register("test", Provider())
        model = ModelInfo("test", "test", 131072, 4096)
        runtime = ModelRuntime(registry, {"main": model})
        s = SessionStore(project=tmp_path)
        ctx = RunContext(tmp_path, s, Budget())
        try:
            with pytest.raises(ModelError):
                await runtime.complete(
                    PreparedRequest(model, "test", (Message("user", "hi"),)), ctx
                )
            row = s.latest("attempt_end")
            assert row["exception_type"] == "UpstreamError"
            assert row["error_code"] == "invalid_request_error"
            assert row["request_id"]
            assert "secret-key" not in json.dumps(s.records)
        finally:
            await ctx.close()
            await runtime.close()
            s.close()

    asyncio.run(run())


def test_unknown_vendor_codes_cannot_leak_credentials():
    e = type("SDKError", (Exception,), {"body": {"error": {"code": "sk-secret-value"}}})("secret")
    result = _error(e)
    assert result.diagnostic["error_code"] == "other"
    assert "secret" not in str(result.diagnostic)
