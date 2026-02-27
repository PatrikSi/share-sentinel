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


def create_multipart_upload(key: str, content_type: str | None = None) -> str:
    kwargs = {"Bucket": _settings.s3_bucket, "Key": key}
    if content_type:
        kwargs["ContentType"] = content_type
    response = _s3.create_multipart_upload(**kwargs)
    return response["UploadId"]


def upload_part(key: str, upload_id: str, part_number: int, body: bytes) -> str:
    response = _s3.upload_part(
        Bucket=_settings.s3_bucket,
        Key=key,
        UploadId=upload_id,
        PartNumber=part_number,
        Body=body,
    )
    return response["ETag"]


def complete_multipart_upload(key: str, upload_id: str, parts: list[dict]) -> None:
    _s3.complete_multipart_upload(
        Bucket=_settings.s3_bucket,
        Key=key,
        UploadId=upload_id,
        MultipartUpload={"Parts": parts},
    )


def abort_multipart_upload(key: str, upload_id: str) -> None:
    _s3.abort_multipart_upload(Bucket=_settings.s3_bucket, Key=key, UploadId=upload_id)


def get_object_stream(key: str):
    response = _s3.get_object(Bucket=_settings.s3_bucket, Key=key)
    return response["Body"]
