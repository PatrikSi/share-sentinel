from botocore.exceptions import ClientError

from app.bootstrap import ensure_bucket_exists


class _FakeS3:
    def __init__(self, *, head_error: ClientError | None = None, create_error: ClientError | None = None) -> None:
        self.head_error = head_error
        self.create_error = create_error
        self.calls: list[tuple[str, str]] = []

    def head_bucket(self, *, Bucket: str) -> None:
        self.calls.append(("head_bucket", Bucket))
        if self.head_error is not None:
            raise self.head_error

    def create_bucket(self, *, Bucket: str) -> None:
        self.calls.append(("create_bucket", Bucket))
        if self.create_error is not None:
            raise self.create_error


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "bucket-op")


def test_ensure_bucket_exists_skips_create_when_bucket_present() -> None:
    fake_s3 = _FakeS3()

    created = ensure_bucket_exists(fake_s3, "share-sentinel-artifacts")

    assert created is False
    assert fake_s3.calls == [("head_bucket", "share-sentinel-artifacts")]


def test_ensure_bucket_exists_creates_missing_bucket() -> None:
    fake_s3 = _FakeS3(head_error=_client_error("404"))

    created = ensure_bucket_exists(fake_s3, "share-sentinel-artifacts")

    assert created is True
    assert fake_s3.calls == [
        ("head_bucket", "share-sentinel-artifacts"),
        ("create_bucket", "share-sentinel-artifacts"),
    ]


def test_ensure_bucket_exists_handles_racing_bucket_creation() -> None:
    fake_s3 = _FakeS3(head_error=_client_error("NoSuchBucket"), create_error=_client_error("BucketAlreadyOwnedByYou"))

    created = ensure_bucket_exists(fake_s3, "share-sentinel-artifacts")

    assert created is False
    assert fake_s3.calls == [
        ("head_bucket", "share-sentinel-artifacts"),
        ("create_bucket", "share-sentinel-artifacts"),
    ]
