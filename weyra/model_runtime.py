"""Compatibility alias for the model execution coordinator."""
import sys
from .core import model_runtime as _implementation
sys.modules[__name__] = _implementation
