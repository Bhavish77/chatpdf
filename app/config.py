from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM
    GEMINI_API_KEY: str = ""
    GEN_MODEL: str = "gemini-3.5-flash"
    HELPER_MODEL: str = "gemini-3.5-flash-lite"
    EMBED_MODEL: str = "gemini-embedding-2"
    EMBED_DIM: int = 768
    EMBED_BATCH_SIZE: int = 32
    EMBED_MIN_INTERVAL_S: float = 0.7
    LLM_MAX_CONCURRENCY: int = 3

    # Database / worker
    DATABASE_URL: str = "postgresql://askdocs:askdocs@localhost:5432/askdocs"
    EMBEDDED_WORKER: bool = False
    WORKER_CONCURRENCY: int = 1

    # Retrieval / grading
    RETRIEVAL_K: int = 8
    GRADE_MIN_RELEVANT: int = 2
    ENABLE_GROUNDING_CHECK: bool = True

    # Auth
    COOKIE_SECURE: bool = True
    PUBLIC_ORIGIN: str = ""
    SESSION_TTL_DAYS: int = 7
    SIGNUPS_ENABLED: bool = True
    SIGNUP_INVITE_CODE: str = ""
    SIGNUP_RATE_PER_HOUR: int = 5
    LOGIN_MAX_FAILURES: int = 5

    # Limits and retention
    MAX_FILE_MB: int = 15
    MAX_PAGES: int = 200
    MAX_CHUNKS_PER_DOC: int = 1500
    MAX_DOCS_PER_USER: int = 10
    MAX_USER_STORAGE_MB: int = 50
    MAX_TOTAL_STORAGE_MB: int = 400
    USER_CHAT_PER_DAY: int = 100
    CHAT_RATE_PER_MIN: int = 15
    UPLOAD_RATE_PER_HOUR: int = 20
    DOC_TTL_DAYS: int = 7
    ACCOUNT_TTL_DAYS: int = 30
    TRUST_PROXY_HEADERS: bool = False
    LOG_LEVEL: str = "INFO"

    @property
    def session_cookie_name(self) -> str:
        return "__Host-askdocs_session" if self.COOKIE_SECURE else "askdocs_session"


@lru_cache
def get_settings() -> Settings:
    return Settings()
