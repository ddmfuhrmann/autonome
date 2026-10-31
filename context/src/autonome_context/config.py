import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    ollama_url: str
    embed_model: str
    host: str
    port: int


def load() -> Settings:
    return Settings(
        database_url=os.getenv(
            "AUTONOME_DATABASE_URL", "postgresql://autonome:autonome@localhost:5433/autonome"
        ),
        ollama_url=os.getenv("OLLAMA_URL", "http://localhost:11434"),
        embed_model=os.getenv("AUTONOME_EMBED_MODEL", "nomic-embed-text"),
        host=os.getenv("AUTONOME_HOST", "127.0.0.1"),
        port=int(os.getenv("AUTONOME_PORT", "8765")),
    )
