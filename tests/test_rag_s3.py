"""The images of the archives in an S3-compatible service (MinIO): the signature is the one of the AWS documentation, and a stand-in server that checks every signature stands for MinIO."""
import re
import threading
import xml.sax.saxutils as xml
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, unquote, urlsplit

import pytest

from tadamoo.rag import RagError, RagStore, project_key
from tadamoo.rag.s3store import S3Error, S3ImageStore, authorization
from test_rag_figures import make_pdf

ACCESS, SECRET = "tadamoo-key", "tadamoo-secret-0123456789"
PROJ = project_key("project-one")


class Fake(ThreadingHTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.buckets, self.objects, self.calls = set(), {}, []
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def endpoint(self):
        return f"127.0.0.1:{self.server_address[1]}"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _checked(self, body):
        parts = urlsplit(self.path)
        sent = self.headers.get("Authorization", "")
        names = re.search(r"SignedHeaders=([^,]+)", sent).group(1).split(";") if "SignedHeaders=" in sent else []
        headers = {n: self.headers.get(n, "") for n in names}
        want = authorization(self.command, unquote(parts.path), dict(parse_qsl(parts.query)), headers, self.headers.get("x-amz-content-sha256", ""), self.headers.get("x-amz-date", ""),
                             ACCESS, SECRET)
        import hashlib
        if want != sent or hashlib.sha256(body).hexdigest() != (self.headers.get("x-amz-content-sha256") if body else hashlib.sha256(b"").hexdigest()):
            self._send(403)
            return None
        bucket, _, key = unquote(parts.path).lstrip("/").partition("/")
        return bucket, key, dict(parse_qsl(parts.query))

    def _send(self, status, body=b""):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _handle(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        got = self._checked(body)
        if got is None:
            return
        bucket, key, query = got
        s = self.server
        s.calls.append((self.command, key))
        if not key:
            if self.command == "PUT":
                s.buckets.add(bucket)
                return self._send(200)
            if bucket not in s.buckets:
                return self._send(404)
            if self.command == "HEAD":
                return self._send(200)
            keys = sorted(k for (b, k) in s.objects if b == bucket)
            start = int(query.get("continuation-token") or 0)
            page = keys[start:start + int(query.get("max-keys", 1000))]
            more = start + len(page) < len(keys)
            items = "".join(f"<Contents><Key>{xml.escape(k)}</Key></Contents>" for k in page)
            token = f"<NextContinuationToken>{start + len(page)}</NextContinuationToken>" if more else ""
            return self._send(200, (f'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><IsTruncated>{str(more).lower()}</IsTruncated>{token}{items}</ListBucketResult>').encode())
        if self.command == "PUT":
            s.objects[(bucket, key)] = body
            return self._send(200)
        if self.command == "DELETE":
            s.objects.pop((bucket, key), None)
            return self._send(204)
        if (bucket, key) not in s.objects:
            return self._send(404)
        self._send(200, s.objects[(bucket, key)] if self.command == "GET" else b"")

    do_GET = do_PUT = do_HEAD = do_DELETE = _handle


@pytest.fixture
def fake():
    server = Fake()
    yield server
    server.shutdown()


def test_the_signature_is_the_one_in_the_aws_documentation():
    h = {"host": "examplebucket.s3.amazonaws.com", "range": "bytes=0-9", "x-amz-content-sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
         "x-amz-date": "20130524T000000Z"}
    got = authorization("GET", "/test.txt", {}, h, h["x-amz-content-sha256"], "20130524T000000Z", "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
    assert got.endswith("Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41") and "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date" in got


def test_the_bucket_is_made_once_an_image_is_stored_once_read_back_listed_and_swept(fake):
    s3 = S3ImageStore(fake.endpoint, "tadamoo-images", ACCESS, SECRET)
    key = "ab" + "c" * 22
    assert s3.put(key, b"image-bytes") is True and s3.put(key, b"image-bytes") is False
    assert "tadamoo-images" in fake.buckets and list(fake.objects) == [("tadamoo-images", f"ab/cc/{key}.webp")]
    assert s3.has(key) and s3.get(key) == b"image-bytes" and s3.get("0" * 24) is None and not s3.has("0" * 24)
    for i in range(5):
        s3.put(f"{i:02x}" + "d" * 22, b"x" * i)
    assert len(list(s3.keys())) == 6
    assert s3.sweep({key}) == 5 and list(s3.keys()) == [key]
    assert s3.get("../escape") is None                                                         # not a key: there is nothing to read
    with pytest.raises(ValueError):
        s3.put("../escape", b"x")


def test_a_wrong_secret_is_refused_and_an_absent_service_is_an_error_not_a_hang(fake):
    with pytest.raises(S3Error) as e:
        S3ImageStore(fake.endpoint, "tadamoo-images", ACCESS, "wrong").put("ab" + "c" * 22, b"x")
    assert e.value.status == 403
    assert S3ImageStore("127.0.0.1:1", "tadamoo-images", ACCESS, SECRET, timeout=2).get("ab" + "c" * 22) is None
    with pytest.raises(S3Error):
        S3ImageStore("127.0.0.1:1", "tadamoo-images", ACCESS, SECRET, timeout=2).put("ab" + "c" * 22, b"x")
    for bad in ("A", "has space", "x" * 70):
        with pytest.raises(ValueError):
            S3ImageStore("127.0.0.1:9000", bad, ACCESS, SECRET)


def test_an_archive_can_keep_its_figures_in_the_service_with_the_secret_read_from_the_vault(tmp_path, fake):
    asked = []
    store = RagStore(tmp_path / "rag", password=lambda entry: (asked.append(entry), SECRET)[1])
    with pytest.raises(RagError):
        store.settings.write({"images_store": "files"})
    with pytest.raises(RagError):
        store.settings.write({"images_s3_endpoint": "http://host:9000"})
    store.settings.write({"images_store": "s3", "images_s3_endpoint": fake.endpoint, "images_s3_access_key": ACCESS, "images_s3_vault": "images-s3"})
    aid = store.create("Patents", project=PROJ)["id"]
    doc = store.add_file(aid, "patent.pdf", make_pdf(tmp_path))["document"]
    assert doc["figures"] == 1 and asked and set(asked) == {"images-s3"}
    rec = store._backend(aid).read_figures(aid, doc["id"])[0]
    assert {k for (_, k) in fake.objects} == {f"{rec['sha'][:2]}/{rec['sha'][2:4]}/{rec['sha']}.webp", f"{rec['sha'][:2]}/{rec['sha'][2:4]}/{rec['sha']}_t.webp"}
    assert not (tmp_path / "rag" / "images").exists() and not store._backend(aid).list_blobs(aid, "image")
    got = store.figure(aid, doc["id"], 1)
    assert got["image"] == fake.objects[("tadamoo-images", f"{rec['sha'][:2]}/{rec['sha'][2:4]}/{rec['sha']}.webp")] and got["thumb"]
    for d in store.get(aid)["document_list"]:
        store.remove_document(aid, d["id"])
    assert store.sweep_images() == 2 and not fake.objects


def test_without_an_endpoint_or_a_vault_entry_the_service_is_not_used_and_says_what_is_missing(tmp_path):
    store = RagStore(tmp_path / "rag")
    store.settings.write({"images_store": "s3"})
    with pytest.raises(RagError, match="not set"):
        store.image_store()
