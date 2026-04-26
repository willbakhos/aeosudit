"""Abstract base class for answer-engine adapters."""
from __future__ import annotations

from abc import ABC, abstractmethod

from src.models import EngineResponse


class Engine(ABC):
    label: str

    @abstractmethod
    async def query(self, prompt: str, query_type: str) -> EngineResponse:
        """Run a single query against this engine and return a normalised response."""
        raise NotImplementedError
