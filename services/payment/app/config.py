from gj_common.settings import ServiceSettings


class Settings(ServiceSettings):
    service_name: str = "payment"
    provider: str = "mock"  # mock | <real FPX/card gateway adapter>
    public_base_url: str = "http://localhost:8080"
    webhook_secret: str = "dev-webhook-secret"
    enable_mock_gateway: bool = False


settings = Settings()
