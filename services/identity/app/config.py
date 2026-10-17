from gj_common.settings import ServiceSettings


class Settings(ServiceSettings):
    service_name: str = "identity"
    access_token_ttl: int = 900
    refresh_token_ttl: int = 30 * 86400
    otp_ttl: int = 300
    otp_max_attempts: int = 5
    otp_requests_per_hour: int = 5
    otp_resend_cooldown: int = 30
    # Dev only: echo the OTP in the API response so the flow can be tested without SMS.
    otp_debug_return: bool = False
    bootstrap_admin_email: str | None = None
    bootstrap_admin_password: str | None = None


settings = Settings()
