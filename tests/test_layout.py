import importlib
from pathlib import Path


def test_agents_public_layout():
    assert importlib.import_module('weyra.agent').Agent
    assert importlib.import_module('weyra.main').main
    assert not (Path(__file__).parents[1] / 'src').exists()
