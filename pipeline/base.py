"""
pipeline/base.py — Common contract for all pipeline stages.

A stage receives a packet dict, may enrich it, filter it (return None to drop),
or raise an exception to signal a non-fatal error.
"""

from abc import ABC, abstractmethod


class PipelineStage(ABC):
    """
    Interface for a single processing stage in the pipeline.

    process(packet) → dict  : enriched packet, forwarded to the next stage
    process(packet) → None  : packet dropped (filtered out)
    """

    @abstractmethod
    async def process(self, packet: dict) -> dict | None:
        ...

    async def reset(self) -> None:
        """
        Reset internal state.

        Called by the orchestrator on session changes (e.g. playback start,
        recalibration). Override in stages that carry state between packets.
        """
        pass
