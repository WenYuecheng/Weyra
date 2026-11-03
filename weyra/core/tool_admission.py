"""Immutable registration snapshots and process-local step admission."""
import asyncio
import copy
from dataclasses import dataclass, field
from contextvars import ContextVar

ACTIVE_CALL = ContextVar('weyra_active_call', default=(None, None))


class FrozenDict(dict):
    def _deny(self, *a, **kw):
        raise TypeError('Tool schema is immutable')
    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _deny

    def __deepcopy__(self, memo):
        return {k: copy.deepcopy(v, memo) for k, v in self.items()}


class FrozenList(list):
    def _deny(self, *a, **kw):
        raise TypeError('Tool schema is immutable')
    __setitem__ = __delitem__ = append = extend = insert = pop = remove = clear = sort = reverse = __iadd__ = __imul__ = _deny

    def __deepcopy__(self, memo):
        return [copy.deepcopy(v, memo) for v in self]


def freeze(value):
    if isinstance(value, dict):
        return FrozenDict({k: freeze(v) for k, v in value.items()})
    if isinstance(value, (tuple, list)):
        return FrozenList(freeze(v) for v in value)
    return copy.deepcopy(value)


@dataclass(frozen=True)
class ToolSnapshot:
    name: str
    description: str
    schema: dict
    handler: object
    execution_mode: str = 'exclusive'
    permission: str = 'write'

    def declaration(self):
        return {'name': self.name, 'description': self.description, 'parameters': copy.deepcopy(self.schema)}


@dataclass(frozen=True)
class ToolActivationContext:
    model: object = None
    ctx: object = None
    scope: object = None


@dataclass(eq=False)
class ToolRegistration:
    runtime: object
    spec: ToolSnapshot
    generation: int
    when: object = None
    live: bool = True
    tasks: set = field(default_factory=set)

    def dispose(self):
        self.live = False
        if self.runtime._registrations.get(self.spec.name) is self:
            self.runtime._registrations.pop(self.spec.name, None)
            self.runtime.specs.pop(self.spec.name, None)

    async def aclose(self):
        self.dispose()
        pending = [task for task in self.tasks if task is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


class ResolvedToolSet:
    def __init__(self, runtime, registrations):
        self.runtime, self.registrations, self.closed = runtime, dict(registrations), False

    def validate(self, name):
        registration = self.registrations.get(name)
        return bool(not self.closed and registration and registration.live
                    and self.runtime._registrations.get(name) is registration
                    and self.runtime.specs.get(name) is registration.spec)

    def get(self, name):
        return self.registrations[name].spec if self.validate(name) else None

    def declarations(self):
        return tuple(self.registrations[name].spec.declaration() for name in sorted(self.registrations)
                     if not name.startswith('mcp__'))

    async def aclose(self):
        self.closed = True
