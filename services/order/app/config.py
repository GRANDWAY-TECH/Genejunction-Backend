from decimal import Decimal

from gj_common.settings import ServiceSettings


class Settings(ServiceSettings):
    service_name: str = "order"
    catalog_service_url: str = "http://catalog:8000"
    payment_service_url: str = "http://payment:8000"
    bridge_service_url: str = "http://legacy-bridge:8000"
    pharmacy_delivery_fee: Decimal = Decimal("5.00")
    free_delivery_threshold: Decimal = Decimal("50.00")
    lab_home_collection_fee: Decimal = Decimal("20.00")
    payment_window_minutes: int = 30


settings = Settings()
