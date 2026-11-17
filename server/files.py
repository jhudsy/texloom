"""Where the relay keeps the content of stored files: a folder, or an S3-compatible bucket.

The database (relay.Store) records which files a project has stored, their sizes and kinds; this
holds their bytes, encrypted by the clients. A file's key is "<project>/<id>". Ids name the content,
so a key only ever holds that content (encrypted differently from one upload to the next, which
readers don't mind): writing it again is harmless, and nothing is changed in place.

Any S3-compatible store works: Garage, RustFS, Cloudflare R2, Backblaze B2, AWS S3. Only four
requests are used (PUT, GET, DELETE of one object, and HEAD of the bucket), signed with AWS
Signature Version 4, with path-style addresses (https://endpoint/bucket/key).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import re
import secrets
import time
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import quote, urlsplit

import aiohttp

log = logging.getLogger("texloom.files")

KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}/[0-9a-f]{32}$")
PIECE = 256 * 1024  # a download goes out this much at a time
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
TRIES = 4  # an S3 request is tried this often before an error is passed on
TIMEOUT = aiohttp.ClientTimeout(total=None, connect=10, sock_read=60)


class FilesError(Exception):
    """The store couldn't do what was asked (unreachable, refused, misconfigured)."""


def _check(key: str) -> str:
    if not KEY_RE.match(key):
        raise ValueError(f"not a stored file's key: {key!r}")
    return key


class Files:
    """The interface: put, get, delete one file by key."""

    name = ""

    async def put(self, key: str, data: bytes | memoryview) -> None:
        raise NotImplementedError

    def get(self, key: str) -> AsyncIterator[bytes]:
        """The file a piece at a time. FileNotFoundError if there is none."""
        raise NotImplementedError

    async def delete(self, key: str) -> None:
        """Remove the file; nothing happens if it isn't there."""
        raise NotImplementedError

    async def check(self) -> None:
        """Fail early, with a clear message, if the store can't be used."""

    async def close(self) -> None:
        pass


class FolderFiles(Files):
    """Files in a folder on this server: <root>/<project>/<id>. Reads and writes run on worker
    threads, so a large file doesn't hold up live editing."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.name = str(self.root)
        self.tmp = self.root / ".incoming"

    def _path(self, key: str) -> Path:
        return self.root / _check(key)

    def _write(self, key: str, data: bytes | memoryview) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.tmp.mkdir(parents=True, exist_ok=True)
        part = self.tmp / f"{secrets.token_hex(8)}.part"
        try:
            with open(part, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(part, path)  # whole or not at all
        finally:
            part.unlink(missing_ok=True)

    async def put(self, key: str, data: bytes | memoryview) -> None:
        await asyncio.to_thread(self._write, key, data)

    async def get(self, key: str) -> AsyncIterator[bytes]:
        f = await asyncio.to_thread(open, self._path(key), "rb")
        try:
            while piece := await asyncio.to_thread(f.read, PIECE):
                yield piece
        finally:
            f.close()

    def _remove(self, key: str) -> None:
        path = self._path(key)
        path.unlink(missing_ok=True)
        try:
            path.parent.rmdir()  # the project's folder, once it's empty
        except OSError:
            pass

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._remove, key)

    async def check(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.tmp.mkdir(exist_ok=True)
        for stale in self.tmp.iterdir():  # writes a stop cut short
            stale.unlink(missing_ok=True)


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


class S3Files(Files):
    """Files in an S3-compatible bucket, under an optional prefix."""

    def __init__(self, endpoint: str, bucket: str, prefix: str, region: str, key_id: str, secret: str):
        u = urlsplit(endpoint)
        if u.scheme not in ("http", "https") or not u.netloc:
            raise FilesError(f"the S3 endpoint must be an http(s):// address, not {endpoint!r}")
        if not bucket or not key_id or not secret:
            raise FilesError("an S3 store needs a bucket, an access key id and a secret key")
        self.base = f"{u.scheme}://{u.netloc}"
        self.host = u.netloc
        self.bucket = bucket
        self.prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""
        self.region = region
        self.key_id = key_id
        self.secret = secret
        self.name = f"s3://{bucket}/{self.prefix} at {self.base}"
        self._session: aiohttp.ClientSession | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _sess(self) -> aiohttp.ClientSession:
        loop = asyncio.get_running_loop()
        if self._session is None or self._session.closed or self._loop is not loop:
            self._session = aiohttp.ClientSession(timeout=TIMEOUT)
            self._loop = loop
        return self._session

    def _path(self, key: str | None) -> str:
        return f"/{self.bucket}" + (f"/{self.prefix}{_check(key)}" if key is not None else "")

    def _headers(self, method: str, path: str, payload_hash: str, extra: dict[str, str] | None = None) -> dict[str, str]:
        """Signature Version 4 headers for a request with no query string."""
        now = time.gmtime()
        amz_date = time.strftime("%Y%m%dT%H%M%SZ", now)
        day = amz_date[:8]
        headers = {"host": self.host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date, **(extra or {})}
        names = sorted(headers)
        canonical = "\n".join([
            method, quote(path, safe="/-_.~"), "",
            "".join(f"{n}:{headers[n].strip()}\n" for n in names), ";".join(names), payload_hash])
        scope = f"{day}/{self.region}/s3/aws4_request"
        to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
        k = _hmac(_hmac(_hmac(_hmac(f"AWS4{self.secret}".encode(), day), self.region), "s3"), "aws4_request")
        signature = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
        headers["authorization"] = (f"AWS4-HMAC-SHA256 Credential={self.key_id}/{scope}, "
                                    f"SignedHeaders={';'.join(names)}, Signature={signature}")
        return headers

    async def _request(self, method: str, key: str | None, data: bytes | memoryview | None = None,
                       ok: tuple[int, ...] = (200,)) -> int:
        """A request whose answer is just a status (PUT, DELETE, HEAD), tried again on failure."""
        path = self._path(key)
        payload_hash = await asyncio.to_thread(lambda: hashlib.sha256(data).hexdigest()) if data is not None else EMPTY_SHA256
        last = ""
        for attempt in range(TRIES):
            if attempt:
                await asyncio.sleep(0.5 * 2 ** (attempt - 1))
            try:
                async with self._sess().request(method, self.base + path, data=data,
                                                headers=self._headers(method, path, payload_hash)) as r:
                    if r.status in ok:
                        return r.status
                    body = (await r.text())[:300]
                    last = f"{method} {path}: HTTP {r.status} {body}"
                    if r.status < 500 and r.status != 429:
                        break  # refused: trying again won't help
            except (aiohttp.ClientError, TimeoutError) as e:
                last = f"{method} {path}: {e!r}"
        raise FilesError(last)

    async def put(self, key: str, data: bytes | memoryview) -> None:
        await self._request("PUT", key, data)

    async def get(self, key: str) -> AsyncIterator[bytes]:
        path = self._path(key)
        last = ""
        for attempt in range(TRIES):  # tried again only until the first byte has gone out
            if attempt:
                await asyncio.sleep(0.5 * 2 ** (attempt - 1))
            try:
                r = await self._sess().get(self.base + path, headers=self._headers("GET", path, EMPTY_SHA256))
            except (aiohttp.ClientError, TimeoutError) as e:
                last = f"GET {path}: {e!r}"
                continue
            try:
                if r.status == 404:
                    raise FileNotFoundError(key)
                if r.status != 200:
                    last = f"GET {path}: HTTP {r.status} {(await r.text())[:300]}"
                    if r.status < 500 and r.status != 429:
                        break
                    continue
                async for piece in r.content.iter_chunked(PIECE):
                    yield piece
                return
            finally:
                r.release()
        raise FilesError(last)

    async def delete(self, key: str) -> None:
        await self._request("DELETE", key, ok=(200, 204, 404))

    async def check(self) -> None:
        try:
            await self._request("HEAD", None)
        except FilesError as e:
            raise FilesError(f"can't use the bucket {self.bucket} at {self.base} (region {self.region}): {e}") from None

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None


def open_files(where: str, default: Path, s3: dict[str, str] | None = None) -> Files:
    """The store named by `where` (the files setting): a folder, or s3://bucket[/prefix] with the
    endpoint, region and keys in `s3`. Empty: the folder `default`."""
    if not where:
        return FolderFiles(default)
    if where.startswith("s3://"):
        bucket, _, prefix = where[5:].partition("/")
        s3 = s3 or {}
        return S3Files(endpoint=s3.get("endpoint", ""), bucket=bucket, prefix=prefix, region=s3.get("region") or "auto",
                       key_id=s3.get("key_id", ""), secret=s3.get("secret", ""))
    return FolderFiles(Path(where))
