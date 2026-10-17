"""S3 document storage (MinIO locally). Clients upload/download directly with presigned URLs."""

import boto3
from botocore.config import Config

from gj_common.settings import ServiceSettings


class ObjectStorage:
    def __init__(self, settings: ServiceSettings) -> None:
        self.bucket = settings.s3_bucket
        common = dict(
            region_name=settings.s3_region,
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )
        # Presigning is pure computation, so a client bound to the public endpoint is enough
        # for URLs handed to browsers/apps.
        self._public = boto3.client("s3", endpoint_url=settings.s3_public_endpoint_url or settings.s3_endpoint_url, **common)

    def presign_upload(self, key: str, content_type: str, expires: int = 900) -> str:
        return self._public.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket, "Key": key, "ContentType": content_type},
            ExpiresIn=expires,
        )

    def presign_download(self, key: str, expires: int = 900) -> str:
        return self._public.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=expires
        )
