from gj_common.settings import ServiceSettings


class Settings(ServiceSettings):
    service_name: str = "catalog"
    mongo_url: str = "mongodb://localhost:27017"
    mongo_db: str = "gj_catalog"
    low_stock_threshold: int = 10
    reservation_ttl_minutes: int = 30
    partner_service_url: str = "http://partner:8000"


settings = Settings()
