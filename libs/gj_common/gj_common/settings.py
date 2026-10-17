from pydantic_settings import BaseSettings, SettingsConfigDict


class ServiceSettings(BaseSettings):
    """Settings every service shares. Services subclass this and add their own fields."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    service_name: str = "service"
    env: str = "dev"
    log_level: str = "INFO"
    cors_origins: list[str] = ["*"]

    database_url: str | None = None
    db_pool_size: int = 10
    db_max_overflow: int = 20

    redis_url: str = "redis://localhost:6379/0"

    kafka_bootstrap_servers: str = "localhost:9092"
    kafka_enabled: bool = True
    run_outbox_relay: bool = True
    run_consumers: bool = True
    run_jobs: bool = True

    # Token verification. Locally the identity service issues RS256 tokens; in production
    # point jwks_url / jwt_issuer at the Amazon Cognito user pool.
    jwks_url: str = "http://identity:8000/.well-known/jwks.json"
    jwt_issuer: str = "https://auth.genejunction.com"
    jwt_audience: str | None = "genejunction"

    # Shared key for service-to-service calls on /internal routes. The gateway never
    # forwards /internal, and in production this sits behind the service mesh / mTLS.
    internal_api_key: str = "dev-internal-key"

    tenant_service_url: str = "http://tenant:8000"
    default_tenant_slug: str = "genejunction"

    s3_endpoint_url: str | None = None
    s3_public_endpoint_url: str | None = None
    s3_bucket: str = "genejunction-docs"
    s3_region: str = "ap-southeast-1"
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None

    @property
    def is_dev(self) -> bool:
        return self.env in ("dev", "test")
