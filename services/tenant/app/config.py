from gj_common.settings import ServiceSettings


class Settings(ServiceSettings):
    service_name: str = "tenant"
    identity_service_url: str = "http://identity:8000"
    partner_service_url: str = "http://partner:8000"
    platform_domains: list[str] = ["genejunction.com", "www.genejunction.com", "localhost", "127.0.0.1"]


settings = Settings()
