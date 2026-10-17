from gj_common.settings import ServiceSettings


class Settings(ServiceSettings):
    service_name: str = "lab"
    order_service_url: str = "http://order:8000"
    billing_service_url: str = "http://billing:8000"
    default_slots: list[str] = ["08:00-10:00", "10:00-12:00", "14:00-16:00", "16:00-18:00"]
    default_slot_capacity: int = 10


settings = Settings()
