from typing import Literal, Protocol

import httpx

Purpose = Literal["document", "query"]


class Embedder(Protocol):
    model: str
    dims: int
    max_chars: int  # longest text it embeds whole; the indexer splits anything longer

    async def embed(self, texts: list[str], purpose: Purpose) -> list[list[float]]: ...


# Some models are trained with task prefixes; using the wrong one (or none) hurts recall.
_PREFIXES: dict[str, dict[Purpose, str]] = {
    "nomic-embed-text": {"document": "search_document: ", "query": "search_query: "},
}


# Context window the model was trained with (Ollama would otherwise ask for more and cap it).
_CONTEXT_TOKENS: dict[str, int] = {"nomic-embed-text": 2048}
_CHARS_PER_TOKEN = 2  # conservative: code, JSON and Portuguese run well under the usual 4


class OllamaEmbedder:
    def __init__(self, base_url: str, model: str, dims: int):
        self.model = model
        self.dims = dims
        self.num_ctx = _CONTEXT_TOKENS.get(model.split(":")[0], 2048)
        self.max_chars = self.num_ctx * _CHARS_PER_TOKEN - 100  # room for the task prefix
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
            "/api/embed",
            # truncate is the backstop: the indexer already splits texts longer than max_chars.
            json={"model": self.model, "input": [prefix + t for t in texts], "truncate": True,
                  "options": {"num_ctx": self.num_ctx}},
        )
        resp.raise_for_status()
        return resp.json()["embeddings"]
