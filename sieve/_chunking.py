"""Internal interface shared by the built-in chunking strategies."""

from __future__ import annotations

from abc import ABC, abstractmethod


class ChunkingStrategy(ABC):
    """Split text into chunks for the public chunking registry."""

    @abstractmethod
    def chunk(self, text: str) -> list[str]:
        """Return the chunks produced from *text*."""

