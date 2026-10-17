from gj_common.settings import ServiceSettings


class Settings(ServiceSettings):
    service_name: str = "legacy-bridge"
    legacy_mode: str = "mock"  # mock | http
    legacy_base_url: str = "https://api.genejunction.com"
    legacy_api_key: str = ""
    legacy_webhook_secret: str = "dev-legacy-secret"
    identity_service_url: str = "http://identity:8000"
    order_service_url: str = "http://order:8000"
    hold_minutes: int = 35


settings = Settings()
