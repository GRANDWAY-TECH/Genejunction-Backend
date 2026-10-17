from gj_common.settings import ServiceSettings


class Settings(ServiceSettings):
    service_name: str = "pharmacy"
    order_service_url: str = "http://order:8000"
    courier_provider: str = "mock"
    courier_webhook_secret: str = "dev-courier-secret"


settings = Settings()
