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
    memory_collection: str = Field(default="kb_memory_records", alias="MEMORY_COLLECTION")

    # ── Chunking ──────────────────────────────────────────────────────────────
    chunk_size_tokens: int = Field(default=512, alias="CHUNK_SIZE_TOKENS")
    chunk_overlap_tokens: int = Field(default=50, alias="CHUNK_OVERLAP_TOKENS")

    # ── Retrieval ─────────────────────────────────────────────────────────────
    retrieval_top_k: int = Field(default=15, alias="RETRIEVAL_TOP_K")

    # Phase 1 — hybrid retrieval + reranking
    # Number of candidates fetched from hybrid search before reranking
    hybrid_candidates: int = Field(default=20, alias="HYBRID_CANDIDATES")
    # Cross-encoder model for reranking (sentence-transformers cross-encoder)
    reranker_model: str = Field(
        default="cross-encoder/ms-marco-MiniLM-L-6-v2", alias="RERANKER_MODEL"
    )
    # How many chunks the reranker passes to the LLM
    reranker_top_n: int = Field(default=6, alias="RERANKER_TOP_N")

    # ── Phase 1B — Multimodal ingestion ──────────────────────────────────────
    upload_dir: str = Field(default="./uploaded_files", alias="UPLOAD_DIR")
    metadata_db_path: str = Field(default="./raggy_metadata.sqlite3", alias="METADATA_DB_PATH")
    memory_db_path: str = Field(default="./raggy_memory.sqlite3", alias="MEMORY_DB_PATH")
    whisper_model: str = Field(default="base", alias="WHISPER_MODEL")
    table_chunk_rows: int = Field(default=50, alias="TABLE_CHUNK_ROWS")


# Module-level singleton — import this object everywhere.
settings = Settings()
