"""Legacy module alias."""
import sys
from weyra.core import context as _implementation
sys.modules[__name__] = _implementation
