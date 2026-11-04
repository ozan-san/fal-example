from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    fal_mode: Literal["mock", "live"] = "mock"
    fal_key: SecretStr | None = None

    # Model and output are fixed server-side so a caller cannot raise the cost per request.
    fal_endpoint: str = "fal-ai/flux/schnell"
    fal_image_size: Literal["square", "square_hd"] = "square"
    fal_num_inference_steps: int = Field(default=4, ge=1, le=4)
    fal_cost_per_megapixel_usd: float = 0.003

    fal_max_requests: int = Field(default=100, ge=0)
    fal_max_concurrency: int = Field(default=2, ge=1)
    fal_budget_file: Path = Path("/data/budget.json")

    fal_request_timeout_seconds: float = Field(default=120.0, gt=0)
    # Server-side limit on time before processing starts. Setting it also stops
    # fal-client from retrying a timed-out submit, which could double-bill.
    fal_start_timeout_seconds: int = Field(default=60, gt=0)
    fal_poll_interval_seconds: float = Field(default=0.2, gt=0)

    otel_exporter_otlp_endpoint: str = "http://localhost:4317"
    otel_export_interval_millis: int = 10_000

    @model_validator(mode="after")
    def _live_mode_needs_key(self) -> "Settings":
        if self.fal_mode == "live" and not (self.fal_key and self.fal_key.get_secret_value()):
            raise ValueError("FAL_KEY is required when FAL_MODE=live")
        return self
