"""Application configuration and credential management."""

from __future__ import annotations

import os
from pathlib import Path
from dotenv import dotenv_values, load_dotenv, set_key

BASE_DIR = Path(__file__).resolve().parent.parent
SKILLS_DIR = BASE_DIR / "skills"
REFERENCES_DIR = SKILLS_DIR / "tableau-performance-auditor" / "references"
SAMPLES_DIR = BASE_DIR / "app" / "samples"
ENV_FILE = BASE_DIR / ".env"

GOOGLE_AI_STUDIO_KEY_URL = "https://aistudio.google.com/app/api-keys"
MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB cap for .twb / .twbx uploads


class RuntimeSettings:
    """Thread/async-safe container for runtime settings & Gemini API credentials."""

    def __init__(self) -> None:
        if ENV_FILE.exists():
            load_dotenv(ENV_FILE, override=False)
        self._runtime_api_key: str | None = os.environ.get("GEMINI_API_KEY") or None
        self.app_host: str = os.environ.get("APP_HOST", "127.0.0.1")
        self.app_port: int = int(os.environ.get("APP_PORT", "8000"))

    @property
    def api_key(self) -> str | None:
        env_key = os.environ.get("GEMINI_API_KEY")
        return self._runtime_api_key or env_key or None

    @property
    def has_api_key(self) -> bool:
        key = self.api_key
        return bool(key and key.strip())

    def set_api_key(self, api_key: str, persist_to_env: bool = False) -> None:
        cleaned = api_key.strip()
        if not cleaned:
            raise ValueError("API key cannot be empty.")
        self._runtime_api_key = cleaned
        os.environ["GEMINI_API_KEY"] = cleaned
        if persist_to_env:
            if not ENV_FILE.exists():
                ENV_FILE.write_text("", encoding="utf-8")
            set_key(str(ENV_FILE), "GEMINI_API_KEY", cleaned)

    def clear_api_key(self, remove_from_env: bool = False) -> None:
        self._runtime_api_key = None
        os.environ.pop("GEMINI_API_KEY", None)
        if remove_from_env and ENV_FILE.exists():
            vals = dotenv_values(ENV_FILE)
            if "GEMINI_API_KEY" in vals:
                set_key(str(ENV_FILE), "GEMINI_API_KEY", "")

    def status_dict(self) -> dict:
        key = self.api_key
        masked = None
        if key:
            masked = f"{key[:4]}...{key[-4:]}" if len(key) > 10 else "configured"
        return {
            "has_api_key": self.has_api_key,
            "masked_api_key": masked,
            "api_key_setup_url": GOOGLE_AI_STUDIO_KEY_URL,
            "sdk_backend": "google-antigravity (LocalAgentConfig - default model)",
            "skills_dir": str(SKILLS_DIR),
        }


settings = RuntimeSettings()
