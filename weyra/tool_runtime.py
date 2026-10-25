"""Legacy module alias."""
import sys
from weyra.core import tools as _implementation
from weyra.tools import install_builtin_tools
_implementation.install_builtin_tools = install_builtin_tools
sys.modules[__name__] = _implementation
