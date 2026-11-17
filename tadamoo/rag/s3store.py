"""The images of the archives' documents in an S3-compatible service (MinIO, or any other that speaks S3), with the same calls as the folder store (`imagestore.ImageStore`).

Objects are named like the files of the folder store (`ab/cd/<hash>.webp`), so the same picture is one object whatever uses it. Requests are signed with AWS Signature Version 4,
path style (`/bucket/key`), over `http.client`: nothing to install. The secret key is not kept here: the caller reads it from the vault and passes it in."""
from __future__ import annotations

import hashlib
import hmac
import http.client
import re
import time
import xml.etree.ElementTree as ET
from typing import Dict, Iterator, Optional, Set, Tuple
from urllib.parse import quote

from tadamoo.rag.imagestore import EXT, KEY

EMPTY_SHA = hashlib.sha256(b"").hexdigest()
UNRESERVED = "-_.~"
LIST_PAGE = 1000
BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")


class S3Error(RuntimeError):
    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


def _hmac(key: bytes, text: str) -> bytes:
    return hmac.new(key, text.encode("utf-8"), hashlib.sha256).digest()


def authorization(method: str, path: str, query: Dict[str, str], headers: Dict[str, str], payload_sha: str, amz_date: str, access_key: str, secret_key: str,
                  region: str = "us-east-1") -> str:
    """The `Authorization` header of a request signed with AWS Signature Version 4 for S3. `headers` holds every header that is signed (host, x-amz-date, x-amz-content-sha256, ...)."""
    day = amz_date[:8]
    names = sorted(h.lower() for h in headers)
    canon_headers = "".join(f"{n}:{' '.join(str(headers[next(h for h in headers if h.lower() == n)]).split())}\n" for n in names)
    canon_query = "&".join(f"{quote(k, safe=UNRESERVED)}={quote(v, safe=UNRESERVED)}" for k, v in sorted(query.items()))
    canonical = "\n".join([method, quote(path, safe="/" + UNRESERVED), canon_query, canon_headers, ";".join(names), payload_sha])
    scope = f"{day}/{region}/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode("utf-8")).hexdigest()])
    key = _hmac(_hmac(_hmac(_hmac(("AWS4" + secret_key).encode("utf-8"), day), region), "s3"), "aws4_request")
    signature = hmac.new(key, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={';'.join(names)}, Signature={signature}"


class S3ImageStore:
    def __init__(self, endpoint: str, bucket: str, access_key: str, secret_key: str, secure: bool = False, region: str = "us-east-1", timeout: float = 30.0) -> None:
        if not BUCKET.match(bucket or ""):
            raise ValueError("bucket: 3 to 63 lower-case letters, digits, dots or hyphens")
        if not endpoint or "/" in endpoint or " " in endpoint:
            raise ValueError("endpoint: host and port, such as 127.0.0.1:9000")
        self.endpoint, self.bucket, self.secure, self.region, self.timeout = endpoint, bucket, secure, region, timeout
        self._access, self._secret = access_key, secret_key
        self._ready = False

    # -- one signed request ------------------------------------------------------------------------

    def _call(self, method: str, key: str = "", query: Optional[Dict[str, str]] = None, body: bytes = b"") -> Tuple[int, bytes]:
        path = f"/{self.bucket}" + (f"/{key}" if key else "")
        amz = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        sha = hashlib.sha256(body).hexdigest() if body else EMPTY_SHA
        headers = {"host": self.endpoint, "x-amz-content-sha256": sha, "x-amz-date": amz}
        headers_out = {**headers, "Authorization": authorization(method, path, query or {}, headers, sha, amz, self._access, self._secret, self.region)}
        conn = (http.client.HTTPSConnection if self.secure else http.client.HTTPConnection)(self.endpoint, timeout=self.timeout)
        try:
            target = quote(path, safe="/" + UNRESERVED) + ("?" + "&".join(f"{quote(k, safe=UNRESERVED)}={quote(v, safe=UNRESERVED)}" for k, v in sorted((query or {}).items())) if query else "")
            conn.request(method, target, body=body or None, headers=headers_out)
            resp = conn.getresponse()
            return resp.status, resp.read()
        except OSError as e:
            raise S3Error(f"the image service at {self.endpoint} did not answer: {e}") from None
        finally:
            conn.close()

    def ensure_bucket(self) -> None:
        """Makes the bucket if it is not there (once for this object)."""
        if self._ready:
            return
        status, _ = self._call("HEAD")
        if status == 404:
            status, body = self._call("PUT")
            if status not in (200, 409):
                raise S3Error(f"the bucket could not be made (HTTP {status})", status)
        elif status in (401, 403):
            raise S3Error("the image service refused the access key or secret", status)
        elif status != 200:
            raise S3Error(f"the image service answered HTTP {status} for the bucket", status)
        self._ready = True

    @staticmethod
    def name(key: str) -> str:
        if not isinstance(key, str) or not KEY.match(key):
            raise ValueError("an image key is the hash of its bytes (and `_t` for its thumbnail)")
        return f"{key[:2]}/{key[2:4]}/{key}{EXT}"

    # -- the same calls as the folder store --------------------------------------------------------

    def has(self, key: str) -> bool:
        self.ensure_bucket()
        return self._call("HEAD", self.name(key))[0] == 200

    def put(self, key: str, data: bytes) -> bool:
        """Keeps `data` under `key`; False when it is already there."""
        if self.has(key):
            return False
        status, _ = self._call("PUT", self.name(key), body=data)
        if status not in (200, 201):
            raise S3Error(f"the image could not be stored (HTTP {status})", status)
        return True

    def get(self, key: str) -> Optional[bytes]:
        try:
            self.ensure_bucket()
            status, body = self._call("GET", self.name(key))
        except (S3Error, ValueError):
            return None
        return body if status == 200 else None

    def keys(self) -> Iterator[str]:
        self.ensure_bucket()
        token = ""
        while True:
            query = {"list-type": "2", "max-keys": str(LIST_PAGE), **({"continuation-token": token} if token else {})}
            status, body = self._call("GET", query=query)
            if status != 200:
                raise S3Error(f"the images could not be listed (HTTP {status})", status)
            root = ET.fromstring(body)
            ns = root.tag[: root.tag.index("}") + 1] if root.tag.startswith("{") else ""
            for item in root.iter(ns + "Key"):
                stem = (item.text or "").rsplit("/", 1)[-1]
                if stem.endswith(EXT) and KEY.match(stem[: -len(EXT)]):
                    yield stem[: -len(EXT)]
            if (root.findtext(ns + "IsTruncated") or "").lower() != "true":
                return
            token = root.findtext(ns + "NextContinuationToken") or ""
            if not token:
                return

    def sweep(self, keep: Set[str]) -> int:
        """Removes every object whose key is not in `keep`; how many went."""
        gone = 0
        for key in list(self.keys()):
            if key not in keep and self._call("DELETE", self.name(key))[0] in (200, 204):
                gone += 1
        return gone
