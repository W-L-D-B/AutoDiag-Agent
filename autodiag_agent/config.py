from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
SECRET_PATTERN = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,})\b")


def redact(text: str) -> str:
    return SECRET_PATTERN.sub("[REDACTED]", text)


@dataclass(frozen=True)
class Config:
    api_key: str = field(default="", repr=False)
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    provider: str = "deepseek"
    max_tokens: int = 2048
    max_calls: int = 8
    timeout_s: float = 30
    session_budget_cny: float = 5

    def __post_init__(self):
        if self.provider not in {"deepseek", "openai-compatible"}:
            raise ValueError("Unsupported model provider")
        url = urlsplit(self.base_url)
        if url.username or url.password or url.query or url.fragment:
            raise ValueError(
                "Provider URL cannot contain credentials, query or fragment"
            )
        if not url.hostname or not (
            url.scheme == "https"
            or url.scheme == "http"
            and url.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError("Provider requires HTTPS or a local HTTP test endpoint")
        if not 32 <= self.max_tokens <= 4096 or not 1 <= self.max_calls <= 16:
            raise ValueError("Invalid model token/call limit")
        if not 1 <= self.timeout_s <= 120 or not 0 < self.session_budget_cny <= 100:
            raise ValueError("Invalid provider timeout/session budget")

    @classmethod
    def from_env(cls, project_root: Path = ROOT) -> Config:
        values = {}
        path = project_root / ".env"
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line and not line.lstrip().startswith("#") and "=" in line:
                    name, value = line.split("=", 1)
                    values[name.strip()] = value.strip().strip('"').strip("'")
        values.update(os.environ)
        return cls(
            api_key=values.get("AUTODIAG_MODEL_API_KEY", ""),
            base_url=values.get("AUTODIAG_MODEL_BASE_URL") or cls.base_url,
            model=values.get("AUTODIAG_MODEL_NAME") or cls.model,
            provider=values.get("AUTODIAG_MODEL_PROVIDER") or cls.provider,
            max_tokens=int(values.get("AUTODIAG_MODEL_MAX_TOKENS", "2048")),
            max_calls=int(values.get("AUTODIAG_MODEL_MAX_CALLS", "8")),
            timeout_s=float(values.get("AUTODIAG_MODEL_TIMEOUT_S", "30")),
            session_budget_cny=float(
                values.get("AUTODIAG_MODEL_SESSION_BUDGET_CNY", "5")
            ),
        )

    def public(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "configured": bool(self.api_key),
        }
