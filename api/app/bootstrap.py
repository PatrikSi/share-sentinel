import logging

import boto3
from botocore.exceptions import ClientError

from app.config import get_settings
from app.seed import main as seed_main

logger = logging.getLogger("share_sentinel.bootstrap")


def ensure_bucket_exists(s3_client, bucket_name: str) -> bool:
    try:
        s3_client.head_bucket(Bucket=bucket_name)
        return False
    except ClientError as exc:
        error = exc.response.get("Error", {}) if isinstance(exc.response, dict) else {}
        code = str(error.get("Code", ""))
        if code not in {"404", "NoSuchBucket", "NotFound"}:
            raise

    try:
        s3_client.create_bucket(Bucket=bucket_name)
        return True
    except ClientError as exc:
        error = exc.response.get("Error", {}) if isinstance(exc.response, dict) else {}
        code = str(error.get("Code", ""))
        if code in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
            return False
        raise


def main() -> None:
    settings = get_settings()
    s3_client = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
    )

    created_bucket = ensure_bucket_exists(s3_client, settings.s3_bucket)
    logger.info("artifact bucket ready bucket=%s created=%s", settings.s3_bucket, created_bucket)

    seed_main()


if __name__ == "__main__":
    main()
