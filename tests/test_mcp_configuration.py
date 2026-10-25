import asyncio
import tempfile
import unittest

from weyra.agent import Agent
from weyra.contracts import ModelInfo
from weyra.model_runtime import ModelRuntime, ProviderRegistry, ScriptedProvider


def runtime(config):
    registry = ProviderRegistry()
    registry.register("test", ScriptedProvider([]))
    model = ModelRuntime(registry, {"main": ModelInfo("test", "test", 10000, 100)})
    model.mcp_servers = config
    return model


class MCPConfigTests(unittest.TestCase):
    def test_project_configuration_cannot_self_trust_or_keep_changed_command_trust(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                server = {"local": {"command": "python3", "args": ["safe.py"], "env": {}}}
                agent = Agent(directory, runtime(server), enable_evolution=False, context_policy={"headroom_tokens":100,"max_tokens":100})
                self.assertIn("local", agent.mcp.clients)
                self.assertFalse(agent.mcp.clients["local"].trusted)
                with self.assertRaises(PermissionError):
                    agent.trust_mcp("local")
                agent.trust_mcp("local", authorized=True)
                self.assertTrue(agent.mcp.clients["local"].trusted)
                await agent.close()
                server["local"]["args"] = ["changed.py"]
                changed = Agent(directory, runtime(server), enable_evolution=False, context_policy={"headroom_tokens":100,"max_tokens":100})
                self.assertFalse(changed.mcp.clients["local"].trusted)
                await changed.close()

        asyncio.run(run())
