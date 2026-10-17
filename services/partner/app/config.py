import base64
import hashlib

from gj_common.settings import ServiceSettings


class Settings(ServiceSettings):
    service_name: str = "partner"
    identity_service_url: str = "http://identity:8000"
    # Fernet key for bank account numbers. Use a KMS-managed secret in production.
    bank_encryption_key: str | None = None

    @property
    def fernet_key(self) -> bytes:
        if self.bank_encryption_key:
            return self.bank_encryption_key.encode()
        return base64.urlsafe_b64encode(hashlib.sha256(self.internal_api_key.encode()).digest())


settings = Settings()
