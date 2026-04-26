"""Async fan-out: query × engine matrix with bounded concurrency."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from tqdm.asyncio import tqdm_asyncio

from src.engines.base import Engine
from src.models import EngineResponse, Query

MAX_CONCURRENCY = 10


async def _run_one(
    engine: Engine,
    query: Query,
    sem: asyncio.Semaphore,
) -> EngineResponse:
    async with sem:
        return await engine.query(query.query, query.type)


async def run_audit(
    engines: list[Engine],
    queries: list[Query],
    output_dir: Path,
) -> list[EngineResponse]:
    sem = asyncio.Semaphore(MAX_CONCURRENCY)

    tasks = [
        _run_one(engine, query, sem)
        for query in queries
        for engine in engines
    ]

    responses: list[EngineResponse] = await tqdm_asyncio.gather(
        *tasks, desc="Querying engines"
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    raw_path = output_dir / "raw_responses.json"
    with raw_path.open("w") as f:
        json.dump(
            [r.model_dump() for r in responses],
            f,
            indent=2,
            default=str,
        )

    errors = [r for r in responses if r.error]
    if errors:
        err_path = output_dir / "errors.log"
        with err_path.open("w") as f:
            for r in errors:
                f.write(
                    f"[{r.engine_label}] {r.query!r}\n  -> {r.error}\n\n"
                )

    return responses
