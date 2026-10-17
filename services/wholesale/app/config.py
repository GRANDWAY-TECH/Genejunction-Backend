from gj_common.settings import ServiceSettings


class Settings(ServiceSettings):
    service_name: str = "wholesale"
    catalog_service_url: str = "http://catalog:8000"
    payment_service_url: str = "http://payment:8000"


settings = Settings()
