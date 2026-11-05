"""The files of the team server in an S3 object store (Garage).

The key of a file is blobs/<sha256>. The server keeps the access key of the
object store; a client gets only presigned URLs. A presigned PUT signs the
header x-amz-checksum-sha256: the object store rejects other bytes, thus a
client cannot put other content under a key.

A multipart upload (a slow or unstable connection) goes to a staging key. The
object store gives no SHA-256 of the full file, thus the server reads the
object, compares the SHA-256, and copies it to its key (spike of the team
server: Garage copies the references, not the data).

All calls are blocking (boto3): the API calls them in a thread.
"""

from __future__ import annotations

import base64
import hashlib
import math
from datetime import datetime
from typing import Any

from .config import Settings

PART_SIZE = 16 << 20     # bytes of a part of a multipart upload
MAX_PARTS = 10000        # S3


class BlobError(Exception):
    """A file that the server does not accept (size, missing parts)."""


class WrongContent(BlobError):
    """The uploaded bytes are not the file of the SHA-256. The server removed them."""


def checksum(sha256: str) -> str:
    """The value of x-amz-checksum-sha256 for a hex SHA-256."""
    return base64.b64encode(bytes.fromhex(sha256)).decode()


def blob_key(sha256: str) -> str:
    return f"blobs/{sha256}"


def staging_key(upload_id: str) -> str:
    return f"staging/{upload_id}"


class BlobStore:
    def __init__(self, settings: Settings):
        import boto3
        from botocore.config import Config

        config = Config(s3={"addressing_style": "path"}, signature_version="s3v4",
                        request_checksum_calculation="when_required", response_checksum_validation="when_required")
        options = dict(region_name=settings.s3_region, aws_access_key_id=settings.s3_key_id,
                       aws_secret_access_key=settings.s3_secret, config=config)
        # The server uses the object store directly; the presigned URLs have the
        # address of the clients (nginx gives the Host header to Garage unchanged).
        self.s3 = boto3.client("s3", endpoint_url=settings.s3_endpoint, **options)
        self.public = boto3.client("s3", endpoint_url=settings.s3_public_url, **options)
        self.bucket = settings.s3_bucket
        self.expiry = settings.url_expiry

    def size(self, key: str) -> int | None:
        """The size of an object, or None when the object store does not have it."""
        from botocore.exceptions import ClientError

        try:
            return self.s3.head_object(Bucket=self.bucket, Key=key)["ContentLength"]
        except ClientError as e:
            if e.response["ResponseMetadata"]["HTTPStatusCode"] == 404:
                return None
            raise

    def put_url(self, sha256: str, size: int) -> dict[str, Any]:
        """A presigned PUT of the file: the URL, and the headers that the client must
        send. The URL signs the SHA-256 and the size: the object store rejects other
        bytes and a different size (the size limit of the server then applies)."""
        url = self.public.generate_presigned_url(
            "put_object", Params={"Bucket": self.bucket, "Key": blob_key(sha256), "ChecksumSHA256": checksum(sha256),
                                  "ContentLength": size},
            ExpiresIn=self.expiry)
        return {"url": url, "headers": {"x-amz-checksum-sha256": checksum(sha256)}, "expires_in": self.expiry}

    def get_url(self, sha256: str) -> str:
        return self.public.generate_presigned_url("get_object", Params={"Bucket": self.bucket, "Key": blob_key(sha256)},
                                                  ExpiresIn=self.expiry)

    def delete(self, key: str) -> None:
        self.s3.delete_object(Bucket=self.bucket, Key=key)

    # Multipart upload

    @staticmethod
    def part_count(size: int, part_size: int) -> int:
        return max(1, math.ceil(size / part_size))

    @staticmethod
    def part_length(size: int, part_size: int, number: int) -> int:
        """The size of part number (from 1): part_size, the last part the rest."""
        return min(part_size, size - (number - 1) * part_size)

    @staticmethod
    def part_size_for(size: int) -> int:
        """PART_SIZE, or larger when the file needs more than MAX_PARTS parts."""
        return max(PART_SIZE, math.ceil(size / MAX_PARTS))

    def start(self, upload_id: str) -> str:
        """Start a multipart upload to the staging key. Give the upload ID of the object store."""
        return self.s3.create_multipart_upload(Bucket=self.bucket, Key=staging_key(upload_id),
                                               ChecksumAlgorithm="SHA256")["UploadId"]

    def part_url(self, upload_id: str, s3_upload: str, number: int, part_sha256: str, length: int) -> dict[str, Any]:
        """A presigned PUT of one part, with the signed SHA-256 and size of the part."""
        url = self.public.generate_presigned_url(
            "upload_part", Params={"Bucket": self.bucket, "Key": staging_key(upload_id), "UploadId": s3_upload,
                                   "PartNumber": number, "ChecksumSHA256": checksum(part_sha256),
                                   "ContentLength": length},
            ExpiresIn=self.expiry)
        return {"number": number, "url": url, "headers": {"x-amz-checksum-sha256": checksum(part_sha256)}}

    def parts(self, upload_id: str, s3_upload: str) -> list[dict[str, Any]]:
        """The parts that the object store has: number, size, ETag, checksum. None
        when the multipart upload is not there any more (completed or removed)."""
        from botocore.exceptions import ClientError

        found, marker = [], 0
        while True:
            try:
                page = self.s3.list_parts(Bucket=self.bucket, Key=staging_key(upload_id), UploadId=s3_upload,
                                          PartNumberMarker=marker)
            except ClientError as e:
                if e.response["Error"]["Code"] == "NoSuchUpload":
                    return None
                raise
            found += [{"number": p["PartNumber"], "size": p["Size"], "etag": p["ETag"],
                       "checksum": p.get("ChecksumSHA256", "")} for p in page.get("Parts", [])]
            if not page.get("IsTruncated"):
                return found
            marker = page["NextPartNumberMarker"]

    def complete(self, upload_id: str, s3_upload: str, sha256: str, size: int, part_size: int) -> None:
        """Complete the upload, compare the SHA-256 of the full file, and copy it to
        its key. A BlobError when a part is missing (the upload stays), a
        WrongContent when the content is not the file of sha256 (the staging file
        is removed). A second call after a failure of the network continues: the
        staging file of a completed upload stays until its copy."""
        key = staging_key(upload_id)
        if self.size(key) is None:
            parts = self.parts(upload_id, s3_upload) or []
            count = self.part_count(size, part_size)
            if [p["number"] for p in parts] != list(range(1, count + 1)) or sum(p["size"] for p in parts) != size:
                raise BlobError(f"the upload has {len(parts)} of {count} parts, {sum(p['size'] for p in parts)} of "
                                f"{size} bytes")
            self.s3.complete_multipart_upload(
                Bucket=self.bucket, Key=key, UploadId=s3_upload,
                MultipartUpload={"Parts": [{"PartNumber": p["number"], "ETag": p["etag"],
                                            "ChecksumSHA256": p["checksum"]} for p in parts]})
        digest = hashlib.sha256()
        body = self.s3.get_object(Bucket=self.bucket, Key=key)["Body"]
        for block in iter(lambda: body.read(1 << 20), b""):
            digest.update(block)
        if digest.hexdigest() != sha256:
            self.delete(key)
            raise WrongContent(f"the uploaded file has the SHA-256 {digest.hexdigest()}, not {sha256}")
        if self.size(blob_key(sha256)) is None:
            self.s3.copy_object(Bucket=self.bucket, Key=blob_key(sha256), CopySource={"Bucket": self.bucket, "Key": key})
        self.delete(key)

    def abort(self, upload_id: str, s3_upload: str) -> None:
        """Remove a multipart upload, and its staging file when it was completed."""
        from botocore.exceptions import ClientError

        try:
            self.s3.abort_multipart_upload(Bucket=self.bucket, Key=staging_key(upload_id), UploadId=s3_upload)
        except ClientError as e:
            if e.response["ResponseMetadata"]["HTTPStatusCode"] != 404:
                raise
        self.delete(staging_key(upload_id))

    def orphans(self, known: set[str], older_than: datetime) -> list[tuple[str, str | None]]:
        """The multipart uploads and the staging files that the database does not
        know, from before older_than: (upload ID, upload ID of the object store, or
        None for a file). The time limit protects an upload that the API starts now
        (its row comes after the call to the object store)."""
        found: list[tuple[str, str | None]] = []
        options: dict[str, str] = {}
        while True:
            page = self.s3.list_multipart_uploads(Bucket=self.bucket, Prefix="staging/", **options)
            found += [(u["Key"].removeprefix("staging/"), u["UploadId"]) for u in page.get("Uploads", [])
                      if u["Key"].removeprefix("staging/") not in known and u["Initiated"] < older_than]
            if not page.get("IsTruncated"):
                break
            options = {"KeyMarker": page["NextKeyMarker"], "UploadIdMarker": page["NextUploadIdMarker"]}
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix="staging/"):
            found += [(o["Key"].removeprefix("staging/"), None) for o in page.get("Contents", [])
                      if o["Key"].removeprefix("staging/") not in known and o["LastModified"] < older_than]
        return found
