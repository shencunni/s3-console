"""Local S3 console API.

The browser talks to this process instead of receiving S3 credentials. Saved
profiles contain connection metadata only; secret keys and session tokens live
in memory for the active connection and are never written to disk.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from uuid import uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError


ROOT = Path(__file__).resolve().parent
PROFILE_FILE = ROOT / ".s3_console" / "profiles.json"
WEB_DIST = ROOT / "web" / "dist"
MAX_BODY_BYTES = 1_000_000
DOWNLOAD_DIR = PROFILE_FILE.parent / "downloads"
DOWNLOAD_TTL_SECONDS = 60 * 60
MAX_DOWNLOAD_OBJECTS = 50_000
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024 * 1024
LOGGER = logging.getLogger("s3_console")


def configure_logging() -> None:
    if LOGGER.handlers:
        return
    PROFILE_FILE.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler = RotatingFileHandler(
        PROFILE_FILE.parent / "server.log",
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    LOGGER.addHandler(file_handler)
    LOGGER.addHandler(console_handler)
    LOGGER.setLevel(logging.INFO)


def safe_log(value: object, limit: int = 240) -> str:
    """Keep user-provided values single-line and bounded in local logs."""
    text = str(value).replace("\r", "\\r").replace("\n", "\\n")
    return text[:limit]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def text_value(payload: dict[str, Any], key: str, *, required: bool = True) -> str:
    value = str(payload.get(key, "")).strip()
    if required and not value:
        raise ValueError(f"缺少配置项：{key}")
    return value


def endpoint_url(endpoint: str, disable_ssl: bool) -> str:
    candidate = endpoint if "://" in endpoint else f"https://{endpoint}"
    parts = urlsplit(candidate)
    scheme = "http" if disable_ssl else "https"
    return urlunsplit((scheme, parts.netloc, parts.path, parts.query, parts.fragment))


class ProfileStore:
    """Persist non-secret S3 connection profiles in a local JSON file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            if not self.path.is_file():
                return []
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return []
            return payload if isinstance(payload, list) else []

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        profile = {
            "id": str(payload.get("id") or uuid4().hex[:12]),
            "name": text_value(payload, "name"),
            "endpoint": text_value(payload, "endpoint"),
            "region": text_value(payload, "region"),
            "bucket": text_value(payload, "bucket"),
            "access_key_id": text_value(payload, "access_key_id"),
            "disable_ssl": bool(payload.get("disable_ssl", False)),
            "updated_at": utc_now(),
        }
        with self._lock:
            records = self.list()
            replaced = False
            for index, existing in enumerate(records):
                if existing.get("id") == profile["id"]:
                    profile["created_at"] = existing.get("created_at", profile["updated_at"])
                    records[index] = profile
                    replaced = True
                    break
            if not replaced:
                profile["created_at"] = profile["updated_at"]
                records.insert(0, profile)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
            temporary.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        return profile

    def delete(self, profile_id: str) -> bool:
        with self._lock:
            records = self.list()
            remaining = [record for record in records if record.get("id") != profile_id]
            if len(remaining) == len(records):
                return False
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
            temporary.write_text(json.dumps(remaining, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
            return True


@dataclass
class Connection:
    client: Any
    bucket: str
    label: str
    endpoint: str
    region: str
    connected_at: str
    id: str = ""


class ConnectionRegistry:
    def __init__(self) -> None:
        self._items: dict[str, Connection] = {}
        self._lock = threading.RLock()

    def add(self, connection: Connection) -> str:
        connection_id = uuid4().hex
        connection.id = connection_id
        with self._lock:
            self._items[connection_id] = connection
        return connection_id

    def get(self, connection_id: str) -> Connection | None:
        with self._lock:
            return self._items.get(connection_id)

    def remove(self, connection_id: str) -> bool:
        with self._lock:
            return self._items.pop(connection_id, None) is not None


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class DownloadCancelled(Exception):
    """Raised internally when a download task is cancelled."""


def safe_download_filename(value: str, fallback: str) -> str:
    name = Path(value).name.strip() or fallback
    safe = "".join(character if character.isprintable() and character not in "/\\" else "_" for character in name)
    return safe[:180] or fallback


def zip_entry_name(key: str, prefix: str) -> str | None:
    relative = key[len(prefix) :] if prefix and key.startswith(prefix) else key
    parts = [part for part in PurePosixPath(relative.replace("\\", "/")).parts if part not in {"", ".", ".."}]
    return "/".join(parts) or None


def zip_compression_for(name: str) -> int:
    stored_extensions = {".7z", ".avi", ".gif", ".gz", ".jpeg", ".jpg", ".m4a", ".mkv", ".mov", ".mp3", ".mp4", ".pdf", ".png", ".webm", ".webp", ".zip"}
    return zipfile.ZIP_STORED if Path(name).suffix.lower() in stored_extensions else zipfile.ZIP_DEFLATED


@dataclass
class DownloadJob:
    id: str
    connection: Connection
    kind: str
    target: str
    download_name: str
    created_at: str
    status: str = "queued"
    total_objects: int = 0
    completed_objects: int = 0
    total_bytes: int = 0
    completed_bytes: int = 0
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    content_type: str = "application/octet-stream"
    temporary_path: Path | None = None
    final_path: Path | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    lock: threading.RLock = field(default_factory=threading.RLock)


class DownloadRegistry:
    """Manage in-memory download jobs and their local temporary artifacts."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.directory, 0o700)
        except OSError:
            pass
        self.jobs: dict[str, DownloadJob] = {}
        self.lock = threading.RLock()
        self.cleanup_expired()

    def create(self, connection: Connection, kind: str, target: str) -> DownloadJob:
        if kind not in {"file", "prefix"}:
            raise ValueError("下载类型必须是 file 或 prefix")
        if not target.strip():
            raise ValueError("下载目标不能为空")
        self.cleanup_expired()
        job_id = uuid4().hex
        if kind == "file":
            download_name = safe_download_filename(target, "download.bin")
            content_type = mimetypes.guess_type(download_name)[0] or "application/octet-stream"
        else:
            download_name = safe_download_filename(target.rstrip("/"), "s3-prefix") + ".zip"
            content_type = "application/zip"
        job = DownloadJob(
            id=job_id,
            connection=connection,
            kind=kind,
            target=target,
            download_name=download_name,
            created_at=utc_now(),
            content_type=content_type,
        )
        with self.lock:
            self.jobs[job.id] = job
        LOGGER.info("download queued id=%s kind=%s target=%s", job.id[:12], kind, safe_log(target))
        thread = threading.Thread(target=self._run, args=(job,), name=f"s3-download-{job.id[:8]}", daemon=True)
        thread.start()
        return job

    def get(self, job_id: str) -> DownloadJob | None:
        with self.lock:
            return self.jobs.get(job_id)

    def list_public(self) -> list[dict[str, Any]]:
        self.cleanup_expired()
        with self.lock:
            jobs = list(self.jobs.values())
        jobs.sort(key=lambda item: item.created_at, reverse=True)
        return [self.public(job) for job in jobs]

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None:
            return False
        with job.lock:
            if job.status in {"completed", "failed", "cancelled"}:
                return False
            job.cancel_event.set()
            if job.status == "queued":
                job.status = "cancelled"
                job.finished_at = utc_now()
        LOGGER.info("download cancel requested id=%s", job.id[:12])
        return True

    def public(self, job: DownloadJob) -> dict[str, Any]:
        with job.lock:
            progress = 0
            if job.total_bytes:
                progress = min(100, round(job.completed_bytes / job.total_bytes * 100))
            elif job.total_objects:
                progress = min(100, round(job.completed_objects / job.total_objects * 100))
            return {
                "id": job.id,
                "kind": job.kind,
                "target": job.target,
                "download_name": job.download_name,
                "connection_id": job.connection.id,
                "connection_label": job.connection.label,
                "bucket": job.connection.bucket,
                "status": job.status,
                "total_objects": job.total_objects,
                "completed_objects": job.completed_objects,
                "total_bytes": job.total_bytes,
                "completed_bytes": job.completed_bytes,
                "progress": progress,
                "error": job.error,
                "created_at": job.created_at,
                "started_at": job.started_at,
                "finished_at": job.finished_at,
                "ready": job.status == "completed" and bool(job.final_path and job.final_path.is_file()),
            }

    def cleanup_expired(self) -> None:
        cutoff = time.time() - DOWNLOAD_TTL_SECONDS
        with self.lock:
            expired = [
                job_id
                for job_id, job in self.jobs.items()
                if job.finished_at and self._timestamp(job.finished_at) < cutoff
            ]
            for job_id in expired:
                job = self.jobs.pop(job_id)
                self._unlink_artifacts(job)
        try:
            for artifact in self.directory.iterdir():
                if artifact.is_file() and artifact.stat().st_mtime < cutoff:
                    artifact.unlink()
        except OSError:
            pass

    @staticmethod
    def _timestamp(value: str) -> float:
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            return time.time()

    def _run(self, job: DownloadJob) -> None:
        temporary = self.directory / f".{job.id}.part"
        final = self.directory / f"{job.id}-{safe_download_filename(job.download_name, 'download.bin')}"
        with job.lock:
            if job.status == "cancelled":
                return
            job.status = "scanning" if job.kind == "prefix" else "running"
            job.started_at = utc_now()
            job.temporary_path = temporary
            job.final_path = final
        try:
            if job.kind == "file":
                self._download_file(job, temporary)
            else:
                self._download_prefix(job, temporary)
            if job.cancel_event.is_set():
                raise DownloadCancelled()
            temporary.replace(final)
            with job.lock:
                job.status = "completed"
                job.finished_at = utc_now()
                if job.total_bytes and job.completed_bytes < job.total_bytes:
                    job.completed_bytes = job.total_bytes
            LOGGER.info("download completed id=%s objects=%s bytes=%s", job.id[:12], job.completed_objects, job.completed_bytes)
        except DownloadCancelled:
            with job.lock:
                job.status = "cancelled"
                job.finished_at = utc_now()
                job.error = None
            LOGGER.info("download cancelled id=%s", job.id[:12])
        except ClientError as error:
            details = error.response.get("Error", {})
            message = f"{details.get('Code', 'S3_ERROR')}: {details.get('Message', 'S3 请求失败')}"
            self._fail(job, message)
        except (BotoCoreError, OSError, ValueError, zipfile.BadZipFile) as error:
            self._fail(job, f"{type(error).__name__}: {error}")
        except Exception as error:  # pragma: no cover - defensive worker boundary
            self._fail(job, f"{type(error).__name__}: {error}")
            LOGGER.exception("download worker failed id=%s", job.id[:12])
        finally:
            if job.status != "completed":
                self._unlink_artifacts(job)

    def _download_file(self, job: DownloadJob, temporary: Path) -> None:
        response = job.connection.client.get_object(Bucket=job.connection.bucket, Key=job.target)
        content_length = int(response.get("ContentLength") or 0)
        with job.lock:
            job.total_objects = 1
            job.total_bytes = content_length
        body = response["Body"]
        try:
            with temporary.open("wb") as output:
                self._copy_body(job, body, output)
        finally:
            body.close()
        with job.lock:
            job.completed_objects = 1

    def _download_prefix(self, job: DownloadJob, temporary: Path) -> None:
        objects = []
        total_bytes = 0
        paginator = job.connection.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=job.connection.bucket, Prefix=job.target):
            for item in page.get("Contents", []):
                key = str(item.get("Key", ""))
                if not key or key.endswith("/"):
                    continue
                objects.append(item)
                total_bytes += int(item.get("Size", 0) or 0)
                if len(objects) > MAX_DOWNLOAD_OBJECTS:
                    raise ValueError(f"前缀对象数量超过上限 {MAX_DOWNLOAD_OBJECTS}")
                if total_bytes > MAX_DOWNLOAD_BYTES:
                    raise ValueError(f"前缀总大小超过上限 {format_bytes_for_log(MAX_DOWNLOAD_BYTES)}")
        if not objects:
            raise ValueError("该前缀下没有可下载的对象")
        with job.lock:
            job.total_objects = len(objects)
            job.total_bytes = total_bytes
            job.status = "running"
        with zipfile.ZipFile(temporary, "w", allowZip64=True) as archive:
            for index, item in enumerate(objects, start=1):
                if job.cancel_event.is_set():
                    raise DownloadCancelled()
                key = str(item["Key"])
                entry_name = zip_entry_name(key, job.target)
                if not entry_name:
                    continue
                info = zipfile.ZipInfo(entry_name)
                info.compress_type = zip_compression_for(entry_name)
                response = job.connection.client.get_object(Bucket=job.connection.bucket, Key=key)
                body = response["Body"]
                try:
                    with archive.open(info, "w", force_zip64=True) as entry:
                        self._copy_body(job, body, entry)
                finally:
                    body.close()
                with job.lock:
                    job.completed_objects = index

    def _copy_body(self, job: DownloadJob, body: Any, output: Any) -> None:
        while True:
            if job.cancel_event.is_set():
                raise DownloadCancelled()
            chunk = body.read(8 * 1024 * 1024)
            if not chunk:
                break
            output.write(chunk)
            with job.lock:
                job.completed_bytes += len(chunk)

    def _fail(self, job: DownloadJob, message: str) -> None:
        with job.lock:
            job.status = "failed"
            job.finished_at = utc_now()
            job.error = message
        LOGGER.warning("download failed id=%s error=%s", job.id[:12], safe_log(message))

    @staticmethod
    def _unlink_artifacts(job: DownloadJob) -> None:
        for path in (job.temporary_path, job.final_path):
            if path:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    pass


def format_bytes_for_log(value: int) -> str:
    if value >= 1024 * 1024 * 1024:
        return f"{value / 1024 / 1024 / 1024:.1f} GB"
    return f"{value / 1024 / 1024:.1f} MB"


def build_client(payload: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    endpoint = text_value(payload, "endpoint")
    region = text_value(payload, "region")
    bucket = text_value(payload, "bucket")
    access_key_id = text_value(payload, "access_key_id")
    secret_access_key = text_value(payload, "secret_access_key")
    disable_ssl = bool(payload.get("disable_ssl", False))
    token = text_value(payload, "session_token", required=False)

    client_args: dict[str, Any] = {
        "endpoint_url": endpoint_url(endpoint, disable_ssl),
        "region_name": region,
        "aws_access_key_id": access_key_id,
        "aws_secret_access_key": secret_access_key,
        "use_ssl": not disable_ssl,
        "config": Config(connect_timeout=5, read_timeout=15, retries={"total_max_attempts": 1}),
    }
    if token:
        client_args["aws_session_token"] = token

    client = boto3.client("s3", **client_args)
    return client, {
        "bucket": bucket,
        "endpoint": endpoint_url(endpoint, disable_ssl),
        "region": region,
        "disable_ssl": disable_ssl,
    }


def list_bucket_objects(
    connection: Connection,
    prefix: str,
    recursive: bool,
    continuation_token: str | None,
    _limit: int,
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "Bucket": connection.bucket,
        "Prefix": prefix,
    }
    # Keep the request shape aligned with the existing check_s3.py probe. Some
    # S3-compatible gateways reject an otherwise valid custom MaxKeys value.
    if not recursive:
        request["Delimiter"] = "/"
    if continuation_token:
        request["ContinuationToken"] = continuation_token
    response = connection.client.list_objects_v2(**request)
    return {
        "prefix": prefix,
        "recursive": recursive,
        "prefixes": [item["Prefix"] for item in response.get("CommonPrefixes", [])],
        "objects": [
            {
                "key": item["Key"],
                "size": item.get("Size", 0),
                "last_modified": item["LastModified"].isoformat()
                if item.get("LastModified")
                else None,
                "etag": str(item.get("ETag", "")).strip('"'),
                "storage_class": item.get("StorageClass"),
            }
            for item in response.get("Contents", [])
        ],
        "key_count": response.get("KeyCount", 0),
        "is_truncated": bool(response.get("IsTruncated", False)),
        "next_token": response.get("NextContinuationToken"),
    }


def normalize_directory_prefix(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("目录前缀必须是字符串")
    prefix = value.strip()
    if not prefix or prefix == "/":
        raise ValueError("为了安全，网页不支持清空整个 Bucket；请输入目录前缀")
    return prefix if prefix.endswith("/") else f"{prefix}/"


def prefix_delete_preview(connection: Connection, prefix: str) -> dict[str, Any]:
    object_count = 0
    total_bytes = 0
    sample_keys: list[str] = []
    paginator = connection.client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=connection.bucket, Prefix=prefix):
        for item in page.get("Contents", []):
            key = str(item.get("Key", ""))
            if not key:
                continue
            object_count += 1
            total_bytes += int(item.get("Size", 0) or 0)
            if len(sample_keys) < 8:
                sample_keys.append(key)
    return {
        "prefix": prefix,
        "object_count": object_count,
        "total_bytes": total_bytes,
        "sample_keys": sample_keys,
    }


def delete_prefix_objects(connection: Connection, prefix: str) -> dict[str, Any]:
    requested_count = 0
    deleted_count = 0
    errors: list[dict[str, Any]] = []
    batch: list[dict[str, str]] = []
    paginator = connection.client.get_paginator("list_objects_v2")

    def flush() -> None:
        nonlocal deleted_count, batch
        if not batch:
            return
        response = connection.client.delete_objects(
            Bucket=connection.bucket,
            Delete={"Objects": batch, "Quiet": False},
        )
        batch_errors = [item for item in response.get("Errors", []) or [] if isinstance(item, dict)]
        errors.extend(batch_errors)
        deleted = response.get("Deleted")
        deleted_count += len(deleted) if isinstance(deleted, list) else len(batch) - len(batch_errors)
        batch = []

    for page in paginator.paginate(Bucket=connection.bucket, Prefix=prefix):
        for item in page.get("Contents", []):
            key = str(item.get("Key", ""))
            if not key:
                continue
            requested_count += 1
            batch.append({"Key": key})
            if len(batch) == 1000:
                flush()
    flush()
    return {
        "prefix": prefix,
        "requested_count": requested_count,
        "deleted_count": deleted_count,
        "error_count": len(errors),
        "errors": errors,
    }


class S3ConsoleHandler(BaseHTTPRequestHandler):
    profiles = ProfileStore(PROFILE_FILE)
    connections = ConnectionRegistry()
    downloads = DownloadRegistry(DOWNLOAD_DIR)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors_headers()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        LOGGER.info("GET %s", safe_log(path))
        try:
            if path == "/api/health":
                self._send_json(HTTPStatus.OK, {"ok": True, "service": "s3-console"})
                return
            if path == "/api/profiles":
                self._send_json(HTTPStatus.OK, {"profiles": self.profiles.list()})
                return
            if path == "/api/downloads":
                self._send_json(HTTPStatus.OK, {"downloads": self.downloads.list_public()})
                return
            if path.startswith("/api/downloads/"):
                parts = path.removeprefix("/api/downloads/").split("/")
                job = self.downloads.get(unquote(parts[0])) if parts and parts[0] else None
                if job is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "下载任务不存在"})
                    return
                if len(parts) == 2 and parts[1] == "file":
                    self._serve_download(job)
                else:
                    self._send_json(HTTPStatus.OK, self.downloads.public(job))
                return
            if path.startswith("/api/"):
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "接口不存在"})
                return
            self._serve_static(path)
        except Exception as error:  # pragma: no cover - defensive HTTP boundary
            self._handle_exception(error)

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        LOGGER.info("POST %s", safe_log(path))
        try:
            payload = self._read_json()
            if path == "/api/prefix-delete-preview":
                connection = self.connections.get(text_value(payload, "connection_id"))
                if connection is None:
                    self._send_json(HTTPStatus.CONFLICT, {"error": "连接已失效，请重新连接"})
                    return
                prefix = normalize_directory_prefix(payload.get("prefix"))
                summary = prefix_delete_preview(connection, prefix)
                LOGGER.info(
                    "prefix delete preview id=%s bucket=%s prefix=%s objects=%s",
                    safe_log(connection.id, 12),
                    safe_log(connection.bucket, 160),
                    safe_log(prefix),
                    summary["object_count"],
                )
                self._send_json(HTTPStatus.OK, summary)
                return
            if path == "/api/profiles":
                self._send_json(HTTPStatus.OK, {"profile": self.profiles.save(payload)})
                LOGGER.info("profile saved id=%s", safe_log(payload.get("id") or "new", 32))
                return
            if path == "/api/downloads":
                connection = self.connections.get(text_value(payload, "connection_id"))
                if connection is None:
                    self._send_json(HTTPStatus.CONFLICT, {"error": "连接已失效，请重新连接"})
                    return
                job = self.downloads.create(
                    connection,
                    text_value(payload, "kind"),
                    text_value(payload, "target"),
                )
                self._send_json(HTTPStatus.ACCEPTED, {"download": self.downloads.public(job)})
                return
            if path == "/api/connect":
                LOGGER.info(
                    "connect start name=%s endpoint=%s region=%s bucket=%s token=%s",
                    safe_log(payload.get("name", ""), 80),
                    safe_log(payload.get("endpoint", "")),
                    safe_log(payload.get("region", ""), 80),
                    safe_log(payload.get("bucket", ""), 160),
                    bool(payload.get("session_token")),
                )
                client, summary = build_client(payload)
                client.head_bucket(Bucket=summary["bucket"])
                connection = Connection(
                    client=client,
                    bucket=summary["bucket"],
                    label=str(payload.get("name") or summary["bucket"]),
                    endpoint=summary["endpoint"],
                    region=summary["region"],
                    connected_at=utc_now(),
                )
                connection_id = self.connections.add(connection)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "connection": {
                            "id": connection_id,
                            "label": connection.label,
                            "bucket": connection.bucket,
                            "endpoint": connection.endpoint,
                            "region": connection.region,
                            "connected_at": connection.connected_at,
                        }
                    },
                )
                LOGGER.info(
                    "connect success id=%s endpoint=%s region=%s bucket=%s",
                    connection_id[:12],
                    safe_log(connection.endpoint),
                    safe_log(connection.region, 80),
                    safe_log(connection.bucket, 160),
                )
                return
            if path == "/api/disconnect":
                connection_id = text_value(payload, "connection_id")
                removed = self.connections.remove(connection_id)
                LOGGER.info("disconnect id=%s removed=%s", safe_log(connection_id, 12), removed)
                self._send_json(HTTPStatus.OK, {"disconnected": removed})
                return
            if path == "/api/list":
                connection_id = text_value(payload, "connection_id")
                connection = self.connections.get(connection_id)
                if connection is None:
                    LOGGER.warning("list rejected id=%s reason=connection_missing", safe_log(connection_id, 12))
                    self._send_json(HTTPStatus.CONFLICT, {"error": "连接已失效，请重新连接"})
                    return
                prefix = str(payload.get("prefix", ""))
                recursive = bool(payload.get("recursive", False))
                raw_continuation_token = payload.get("continuation_token")
                continuation_token = (
                    raw_continuation_token.strip()
                    if isinstance(raw_continuation_token, str) and raw_continuation_token.strip()
                    else None
                )
                try:
                    limit = int(payload.get("limit", 200))
                except (TypeError, ValueError):
                    limit = 200
                LOGGER.info(
                    "list start id=%s bucket=%s prefix=%s recursive=%s continuation=%s limit=%s",
                    safe_log(connection_id, 12),
                    safe_log(connection.bucket, 160),
                    safe_log(prefix),
                    recursive,
                    bool(continuation_token),
                    limit,
                )
                result = list_bucket_objects(connection, prefix, recursive, continuation_token, limit)
                LOGGER.info(
                    "list success id=%s prefix=%s prefixes=%s objects=%s truncated=%s",
                    safe_log(connection_id, 12),
                    safe_log(prefix),
                    len(result["prefixes"]),
                    len(result["objects"]),
                    result["is_truncated"],
                )
                self._send_json(
                    HTTPStatus.OK,
                    result,
                )
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "接口不存在"})
        except Exception as error:  # pragma: no cover - defensive HTTP boundary
            self._handle_exception(error)

    def do_DELETE(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        LOGGER.info("DELETE %s", safe_log(path))
        try:
            if path == "/api/objects":
                payload = self._read_json()
                connection = self.connections.get(text_value(payload, "connection_id"))
                if connection is None:
                    self._send_json(HTTPStatus.CONFLICT, {"error": "连接已失效，请重新连接"})
                    return
                raw_key = payload.get("key")
                if not isinstance(raw_key, str) or not raw_key:
                    raise ValueError("缺少配置项：key")
                key = raw_key
                LOGGER.info(
                    "delete object start id=%s bucket=%s key=%s",
                    safe_log(connection.id, 12),
                    safe_log(connection.bucket, 160),
                    safe_log(key),
                )
                response = connection.client.delete_object(Bucket=connection.bucket, Key=key)
                LOGGER.info(
                    "delete object success id=%s bucket=%s key=%s",
                    safe_log(connection.id, 12),
                    safe_log(connection.bucket, 160),
                    safe_log(key),
                )
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "deleted": True,
                        "bucket": connection.bucket,
                        "key": key,
                        "delete_marker": response.get("DeleteMarker"),
                        "version_id": response.get("VersionId"),
                    },
                )
                return
            if path == "/api/prefixes":
                payload = self._read_json()
                connection = self.connections.get(text_value(payload, "connection_id"))
                if connection is None:
                    self._send_json(HTTPStatus.CONFLICT, {"error": "连接已失效，请重新连接"})
                    return
                prefix = normalize_directory_prefix(payload.get("prefix"))
                LOGGER.info(
                    "delete prefix start id=%s bucket=%s prefix=%s",
                    safe_log(connection.id, 12),
                    safe_log(connection.bucket, 160),
                    safe_log(prefix),
                )
                result = delete_prefix_objects(connection, prefix)
                LOGGER.info(
                    "delete prefix finished id=%s bucket=%s prefix=%s requested=%s deleted=%s errors=%s",
                    safe_log(connection.id, 12),
                    safe_log(connection.bucket, 160),
                    safe_log(prefix),
                    result["requested_count"],
                    result["deleted_count"],
                    result["error_count"],
                )
                self._send_json(HTTPStatus.MULTI_STATUS if result["errors"] else HTTPStatus.OK, result)
                return
            if path.startswith("/api/downloads/"):
                job_id = unquote(path.removeprefix("/api/downloads/"))
                if not job_id or self.downloads.get(job_id) is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "下载任务不存在"})
                    return
                self._send_json(HTTPStatus.OK, {"cancelled": self.downloads.cancel(job_id)})
                return
            if not path.startswith("/api/profiles/"):
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "接口不存在"})
                return
            profile_id = unquote(path.removeprefix("/api/profiles/"))
            deleted = self.profiles.delete(profile_id)
            self._send_json(HTTPStatus.OK, {"deleted": deleted})
        except Exception as error:  # pragma: no cover - defensive HTTP boundary
            self._handle_exception(error)

    def _serve_download(self, job: DownloadJob) -> None:
        if job.status != "completed" or not job.final_path or not job.final_path.is_file():
            status = HTTPStatus.CONFLICT if job.status not in {"failed", "cancelled"} else HTTPStatus.GONE
            self._send_json(status, {"error": job.error or "下载文件尚未准备好", "status": job.status})
            return
        try:
            size = job.final_path.stat().st_size
            stream = job.final_path.open("rb")
        except OSError as error:
            self._send_json(HTTPStatus.GONE, {"error": f"下载文件不可用：{error}"})
            return
        try:
            self.send_response(HTTPStatus.OK)
            self._cors_headers()
            self.send_header("Content-Type", job.content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(job.download_name)}")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            while chunk := stream.read(8 * 1024 * 1024):
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            LOGGER.info("download client disconnected id=%s", job.id[:12])
        finally:
            stream.close()

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("请求体长度无效") from error
        if length > MAX_BODY_BYTES:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("请求体不是有效 JSON") from error
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return payload

    def _serve_static(self, path: str) -> None:
        if not WEB_DIST.is_dir():
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "前端尚未构建，请先运行 npm run build"})
            return
        relative = path.removeprefix("/") or "index.html"
        candidate = (WEB_DIST / relative).resolve()
        if WEB_DIST.resolve() not in candidate.parents and candidate != WEB_DIST.resolve():
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "资源不存在"})
            return
        if not candidate.is_file():
            candidate = WEB_DIST / "index.html"
        body = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8" if content_type.startswith("text/") else content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: HTTPStatus, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _cors_headers(self) -> None:
        origin = self.headers.get("Origin")
        allowed_origins = {"http://localhost:5173", "http://127.0.0.1:5173"}
        self.send_header("Access-Control-Allow-Origin", origin if origin in allowed_origins else "http://localhost:5173")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _handle_exception(self, error: Exception) -> None:
        if isinstance(error, ClientError):
            details = error.response.get("Error", {})
            status_code = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 502)
            request_id = error.response.get("ResponseMetadata", {}).get("RequestId", "")
            LOGGER.warning(
                "s3 error code=%s status=%s request_id=%s message=%s",
                safe_log(details.get("Code", "S3_ERROR"), 80),
                status_code,
                safe_log(request_id, 100),
                safe_log(details.get("Message", "S3 请求失败")),
            )
            try:
                status = HTTPStatus(int(status_code))
            except ValueError:
                status = HTTPStatus.BAD_GATEWAY
            self._send_json(
                status,
                {
                    "error": {
                        "code": str(details.get("Code", "S3_ERROR")),
                        "message": str(details.get("Message", "S3 请求失败")),
                    }
                },
            )
            return
        if isinstance(error, BotoCoreError):
            LOGGER.warning("s3 client error type=%s", type(error).__name__)
            self._send_json(
                HTTPStatus.BAD_GATEWAY,
                {"error": {"code": type(error).__name__, "message": "S3 服务连接失败"}},
            )
            return
        if isinstance(error, ValueError):
            LOGGER.warning("invalid request: %s", safe_log(error))
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": {"code": "INVALID_REQUEST", "message": str(error)}})
            return
        LOGGER.exception("unhandled server error")
        self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": {"code": "SERVER_ERROR", "message": str(error)}})

    def log_message(self, format: str, *args: object) -> None:
        return


def run_server(host: str = "127.0.0.1", port: int = 8765) -> None:
    configure_logging()
    server = ReusableThreadingHTTPServer((host, port), S3ConsoleHandler)
    print(f"S3 console API: http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nS3 console stopped")
    finally:
        server.server_close()


if __name__ == "__main__":
    run_server()
