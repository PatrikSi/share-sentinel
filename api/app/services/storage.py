import boto3

from app.config import get_settings

_settings = get_settings()

_s3 = boto3.client(
    "s3",
    endpoint_url=_settings.s3_endpoint,
    aws_access_key_id=_settings.s3_access_key,
    aws_secret_access_key=_settings.s3_secret_key,
)


def upload_fileobj(fileobj, key: str, content_type: str | None = None) -> None:
    extra = {"ContentType": content_type} if content_type else {}
    _s3.upload_fileobj(fileobj, _settings.s3_bucket, key, ExtraArgs=extra)


def get_object_stream(key: str):
    response = _s3.get_object(Bucket=_settings.s3_bucket, Key=key)
    return response["Body"]
