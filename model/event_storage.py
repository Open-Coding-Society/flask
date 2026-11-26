"""Where event files are kept: on the server's disk, or in S3 when it is set up.

EventFileStore keeps files under instance/data/events, on the instance volume. It is not instance/uploads on
purpose: GET /uploads/<path> serves that folder to anyone, and an event's files follow the event's own rules
(a draft's files only reach its managers, see Event.visible_to).

S3EventFileStore is used instead when AWS_BUCKET_NAME, AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY are all
set. They are Spring's names for the same bucket, so one .env serves both. AWS_REGION (default us-east-1) and
AWS_S3_ENDPOINT (a local fake S3) are optional. boto3 is imported only then, so development needs nothing set.

STORE is chosen once, at startup. Code reads event_storage.STORE when it runs, so a test can replace it.
"""
import os

from flask import current_app, redirect, send_file

# The content type of every extension the file kinds in dataconfigs/event_rules.json may take
EXT_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}
SECONDS = 300  # how long a browser may keep a file, and how long a presigned S3 link lasts


class EventStorageError(Exception):
    """The file store did not answer. The API turns it into a 503 with Retry-After, never a 500."""


def _headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"  # the browser must not guess another type
    response.cache_control.public = None
    response.cache_control.private = True  # a draft's file must not sit in a shared cache
    return response


class EventFileStore:
    """Event files on the server's disk."""

    def __init__(self, folder=None):
        self.folder = folder

    def _base(self):
        return self.folder or os.path.join(current_app.config["DATA_FOLDER"], "events")

    def _path(self, key):
        base = os.path.abspath(self._base())
        path = os.path.abspath(os.path.join(base, key))
        if not path.startswith(base + os.sep):  # keys are made by Event.add_file; this only guards against a bad one
            raise EventStorageError(f"the key {key!r} is outside the events folder")
        return path

    def save(self, key, data, content_type):
        path = self._path(key)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(data)
        except OSError as error:
            raise EventStorageError(f"could not save {key}: {error}") from error

    def answer(self, key, name, content_type, inline):
        """The response that sends the file, or None when the file is not on the disk."""
        path = self._path(key)
        if not os.path.isfile(path):
            return None
        return _headers(send_file(path, mimetype=content_type, as_attachment=not inline, download_name=name, max_age=SECONDS))

    def remove(self, key):
        """Delete the file. A file that is already gone is not an error."""
        try:
            os.remove(self._path(key))
        except FileNotFoundError:
            pass
        except OSError as error:
            raise EventStorageError(f"could not remove {key}: {error}") from error


class S3EventFileStore(EventFileStore):
    """Event files in an S3 bucket, under s3_prefix in dataconfigs/event_rules.json."""

    def __init__(self, bucket, region="us-east-1", endpoint=None, client=None):
        super().__init__()
        from botocore.exceptions import BotoCoreError, ClientError
        self.bucket = bucket
        self.failures = (BotoCoreError, ClientError)
        if client is None:
            import boto3
            client = boto3.client("s3", region_name=region, endpoint_url=endpoint)
        self.client = client

    def _key(self, key):
        from model import event_rules
        return event_rules.s3_prefix() + key

    def save(self, key, data, content_type):
        try:
            self.client.put_object(Bucket=self.bucket, Key=self._key(key), Body=data, ContentType=content_type)
        except self.failures as error:
            raise EventStorageError(f"S3 could not save {key}: {error}") from error

    def answer(self, key, name, content_type, inline):
        """A redirect to a presigned link that lasts five minutes and gives the file its type and name."""
        disposition = f'{"inline" if inline else "attachment"}; filename="{name}"'
        try:
            url = self.client.generate_presigned_url("get_object", ExpiresIn=SECONDS, Params={
                "Bucket": self.bucket, "Key": self._key(key),
                "ResponseContentType": content_type, "ResponseContentDisposition": disposition})
        except self.failures as error:
            raise EventStorageError(f"S3 could not sign {key}: {error}") from error
        return _headers(redirect(url, 302))

    def remove(self, key):
        try:
            self.client.delete_object(Bucket=self.bucket, Key=self._key(key))
        except self.failures as error:
            raise EventStorageError(f"S3 could not remove {key}: {error}") from error


def _make_store():
    """S3 when its bucket and keys are all set, else the disk."""
    names = ("AWS_BUCKET_NAME", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")
    if all(os.environ.get(name) for name in names):
        return S3EventFileStore(os.environ["AWS_BUCKET_NAME"], region=os.environ.get("AWS_REGION") or "us-east-1",
                                endpoint=os.environ.get("AWS_S3_ENDPOINT") or None)
    return EventFileStore()


STORE = _make_store()
