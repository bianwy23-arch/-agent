"""Single-event-loop coordination; no cross-process locking."""
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from agents.models.interface import Model


@dataclass
class TurnObservation:
    attempts: int = 0
    inputs: list = field(default_factory=list)
    responses: list = field(default_factory=list)
    model_waits: list = field(default_factory=list)


class LimitedModel(Model):
    """Hold one slot for each model call, including provider retries, never tools."""
    def __init__(self, model, slots, waits):
        self.model, self.slots, self.waits = model, slots, waits

    @asynccontextmanager
    async def slot(self):
        started = time.monotonic()
        try:
            await self.slots.acquire()
        finally:
            self.waits.append(time.monotonic() - started)
        try:
            yield
        finally:
            self.slots.release()

    async def get_response(self, *args, **kwargs):
        async with self.slot():
            return await self.model.get_response(*args, **kwargs)

    async def stream_response(self, *args, **kwargs):
        async with self.slot():
            async for event in self.model.stream_response(*args, **kwargs):
                yield event

    def get_retry_advice(self, request):
        return self.model.get_retry_advice(request)

    async def _cleanup_on_run_end(self, owner):
        await self.model._cleanup_on_run_end(owner)
