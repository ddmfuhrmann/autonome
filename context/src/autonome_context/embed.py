from typing import Literal, Protocol

import httpx

Purpose = Literal["document", "query"]


class Embedder(Protocol):
    model: str
    dims: int

    async def embed(self, texts: list[str], purpose: Purpose) -> list[list[float]]: ...


# Some models are trained with task prefixes; using the wrong one (or none) hurts recall.
_PREFIXES: dict[str, dict[Purpose, str]] = {
    "nomic-embed-text": {"document": "search_document: ", "query": "search_query: "},
}


class OllamaEmbedder:
    def __init__(self, base_url: str, model: str, dims: int):
        self.model = model
        self.dims = dims
        self._client = httpx.AsyncClient(base_url=base_url, timeout=60)

    @classmethod
    async def create(cls, base_url: str, model: str) -> "OllamaEmbedder":
        probe = cls(base_url, model, dims=0)
        [vector] = await probe.embed(["probe"], "query")
        probe.dims = len(vector)
        return probe

    async def embed(self, texts: list[str], purpose: Purpose) -> list[list[float]]:
        prefix = _PREFIXES.get(self.model.split(":")[0], {}).get(purpose, "")
        resp = await self._client.post(
            "/api/embed", json={"model": self.model, "input": [prefix + t for t in texts]}
        )
        resp.raise_for_status()
        return resp.json()["embeddings"]
