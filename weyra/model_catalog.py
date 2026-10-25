"""Compatibility alias for the neutral model catalog."""
import sys
from .llm import catalog as _implementation
sys.modules[__name__] = _implementation
