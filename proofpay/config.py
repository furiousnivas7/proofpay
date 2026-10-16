"""Settings loaded from environment variables and the .env file."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from proofpay.paypal_client import SANDBOX_BASE_URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    paypal_client_id: str = ""
    paypal_client_secret: str = ""
    paypal_base_url: str = SANDBOX_BASE_URL
    paypal_webhook_id: str = ""

    app_base_url: str = "http://localhost:8000"
    database_url: str = "sqlite:///./proofpay.db"

    demo_client_password: str = "client-demo"
    demo_freelancer_password: str = "freelancer-demo"

    currency: str = "USD"
    # PayPal honours an authorization in full for 3 days, then on a best-effort basis up to 29 days.
    honor_period_days: int = 3
    authorization_valid_days: int = 29
    expiry_warning_days: int = 3


@lru_cache
def get_settings() -> Settings:
    return Settings()
