"""
config.py — centralised settings loaded from environment / .env file.

All other modules import from here instead of reading os.environ directly,
so there is exactly one place to change a value.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ── LLM ──────────────────────────────────────────────────────────────────
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    gemini_chat_model: str = Field(default="gemini-1.5-flash", alias="GEMINI_CHAT_MODEL")

    # ── Embeddings ────────────────────────────────────────────────────────────
    local_embed_model: str = Field(
        default="cnmoro/snowflake-arctic-embed-m-v2.0-cpu", alias="LOCAL_EMBED_MODEL"
    )
    local_embed_dim: int = Field(default=256, alias="LOCAL_EMBED_DIM")

    # ── Qdrant ────────────────────────────────────────────────────────────────
    # ":memory:" → in-process Qdrant (no server needed, data lost on restart)
    # "http://localhost:6333" → connect to a running Qdrant container
    qdrant_url: str = Field(default=":memory:", alias="QDRANT_URL")
    qdrant_collection: str = Field(default="kb_text_chunks", alias="QDRANT_COLLECTION")

    # ── Chunking ──────────────────────────────────────────────────────────────
    chunk_size_tokens: int = Field(default=512, alias="CHUNK_SIZE_TOKENS")
    chunk_overlap_tokens: int = Field(default=50, alias="CHUNK_OVERLAP_TOKENS")

    # ── Retrieval ─────────────────────────────────────────────────────────────
    retrieval_top_k: int = Field(default=15, alias="RETRIEVAL_TOP_K")


# Module-level singleton — import this object everywhere.
settings = Settings()
