from decimal import Decimal

from gj_common.settings import ServiceSettings


class Settings(ServiceSettings):
    service_name: str = "billing"
    payment_service_url: str = "http://payment:8000"
    partner_service_url: str = "http://partner:8000"
    catalog_service_url: str = "http://catalog:8000"
    sst_rate: Decimal = Decimal("8.00")  # Malaysian Service Tax on digital/platform services
    credit_validity_days: int = 365
    grace_period_days: int = 7
    low_balance_pct: Decimal = Decimal("20")
    payout_interval_seconds: int = 7 * 86400
    min_payout_amount: Decimal = Decimal("10.00")


settings = Settings()
