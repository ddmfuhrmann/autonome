"""Process-wide services, created on first use."""

import asyncio

from psycopg_pool import AsyncConnectionPool

from . import config
from .chunking import STRATEGIES
from .embed import Embedder, OllamaEmbedder
from .store import Indexer

settings = config.load()
_pool: AsyncConnectionPool | None = None
_embedder: Embedder | None = None
_lock = asyncio.Lock()


async def get() -> tuple[AsyncConnectionPool, Indexer]:
    global _pool, _embedder
    async with _lock:
        if _pool is None:
            _pool = AsyncConnectionPool(settings.database_url, open=False)
            await _pool.open()
        if _embedder is None:
            _embedder = await OllamaEmbedder.create(settings.ollama_url, settings.embed_model)
    return _pool, Indexer(list(STRATEGIES.values()), _embedder)
