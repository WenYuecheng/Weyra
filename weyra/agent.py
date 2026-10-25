"""Compatibility alias preserving old monkeypatch and class identities."""
import sys
from weyra.coding import agent as _implementation
sys.modules[__name__] = _implementation
