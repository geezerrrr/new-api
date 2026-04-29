"""Configuration loading and validation.

All runtime configuration comes from environment variables (loaded from a
`.env` file at startup). Validation rejects placeholder secrets, missing
key files, and weak (<32 char) signing keys at startup so a misconfigured
gateway never serves traffic.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, HttpUrl, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- server ----
    # `host`/`port` are NOT exposed: uvicorn binds via Dockerfile CMD args.
    # We deliberately don't read them here so operators don't get a silent
    # decoy ("I set HOST=... but nothing changed").
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    gateway_public_url: HttpUrl

    # ---- epay (gateway <-> new-api) ----
    # Keys must be at least 32 chars (~128 bits). 16 bytes is the documented
    # legacy minimum but is too low for an internet-exposed signing key.
    epay_pid: str
    epay_key: str = Field(min_length=32)
    new_api_notify_url: HttpUrl
    new_api_notify_timeout: float = 5.0

    # ---- attach HMAC ----
    attach_hmac_key: str = Field(min_length=32)

    # ---- alipay ----
    alipay_enabled: bool = False
    alipay_app_id: str = ""
    alipay_app_private_key_path: str = ""
    alipay_public_key_path: str = ""
    # "RSA" (SHA1) was deprecated by Alipay in 2018; keep RSA2 unless you
    # have a legacy app whose Alipay-side config still requires SHA1 signing.
    alipay_sign_type: Literal["RSA", "RSA2"] = "RSA2"
    alipay_sandbox: bool = False

    # ---- wechat pay ----
    wechat_enabled: bool = False
    wechat_app_id: str = ""
    wechat_mch_id: str = ""
    wechat_mch_cert_serial_no: str = ""
    wechat_mch_private_key_path: str = ""
    wechat_apiv3_key: str = ""
    wechat_cert_dir: str = ""
    # Timeouts (seconds) for synchronous calls to api.mch.weixin.qq.com.
    # The wechatpayv3 SDK passes these to `requests`; `None` means BLOCK
    # FOREVER. Without explicit timeouts, a single unreachable WeChat
    # endpoint will pin every thread in the asyncio default executor and
    # the gateway becomes unresponsive (incl. during startup cert fetch).
    wechat_connect_timeout: float = 5.0
    wechat_read_timeout: float = 30.0

    # ---- security ----
    rate_limit_per_minute: int = 60
    max_request_bytes: int = 8192

    # ---------- validators ----------

    @field_validator("epay_key", "attach_hmac_key")
    @classmethod
    def _no_placeholder(cls, v: str) -> str:
        if "CHANGE_ME" in v:
            raise ValueError("placeholder secret detected; replace with a real random key")
        return v

    @field_validator(
        "alipay_app_private_key_path",
        "alipay_public_key_path",
        "wechat_mch_private_key_path",
    )
    @classmethod
    def _key_file_exists(cls, v: str) -> str:
        if v and not Path(v).is_file():
            raise ValueError(f"key file not found: {v}")
        return v

    # ---------- derived ----------

    @property
    def alipay_notify_url(self) -> str:
        return str(self.gateway_public_url).rstrip("/") + "/alipay/notify"

    @property
    def alipay_return_url(self) -> str:
        return str(self.gateway_public_url).rstrip("/") + "/alipay/return"

    @property
    def wechat_notify_url(self) -> str:
        return str(self.gateway_public_url).rstrip("/") + "/wechat/notify"

    def assert_alipay_ready(self) -> None:
        if not self.alipay_enabled:
            raise RuntimeError("alipay disabled")
        missing = [
            n
            for n, v in [
                ("ALIPAY_APP_ID", self.alipay_app_id),
                ("ALIPAY_APP_PRIVATE_KEY_PATH", self.alipay_app_private_key_path),
                ("ALIPAY_PUBLIC_KEY_PATH", self.alipay_public_key_path),
            ]
            if not v
        ]
        if missing:
            raise RuntimeError(f"alipay enabled but missing: {', '.join(missing)}")

    def assert_wechat_ready(self) -> None:
        if not self.wechat_enabled:
            raise RuntimeError("wechat disabled")
        missing = [
            n
            for n, v in [
                ("WECHAT_APP_ID", self.wechat_app_id),
                ("WECHAT_MCH_ID", self.wechat_mch_id),
                ("WECHAT_MCH_CERT_SERIAL_NO", self.wechat_mch_cert_serial_no),
                ("WECHAT_MCH_PRIVATE_KEY_PATH", self.wechat_mch_private_key_path),
                ("WECHAT_APIV3_KEY", self.wechat_apiv3_key),
            ]
            if not v
        ]
        if missing:
            raise RuntimeError(f"wechat enabled but missing: {', '.join(missing)}")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


def reload_settings_for_tests() -> None:
    get_settings.cache_clear()
