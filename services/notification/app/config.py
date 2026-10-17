from gj_common.settings import ServiceSettings


class Settings(ServiceSettings):
    service_name: str = "notification"
    identity_service_url: str = "http://identity:8000"
    billing_service_url: str = "http://billing:8000"
    # Patients in white-label apps get SMS alerts paid from the clinic group's credits.
    white_label_sms_alerts: bool = True


settings = Settings()
