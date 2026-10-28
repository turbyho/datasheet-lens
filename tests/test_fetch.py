import email.message
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from datasheet_lens import fetch
from datasheet_lens.cli import main
from datasheet_lens.errors import LensError
from datasheet_lens.fetch import _filename, add_from_url
from datasheet_lens.store import Store


@pytest.fixture
def server(make_pdf):
    """A local HTTP server: /ds.pdf (a PDF), /get?id=7 (a PDF with
    Content-Disposition), /login (an HTML page), others 404. Gives the base URL
    and the User-Agents of the requests."""
    pdf = make_pdf("served.pdf", pages=1).read_bytes()
    agents = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            agents.append(self.headers.get("User-Agent", ""))
            if self.path in ("/ds.pdf", "/OPA2134%20rev%20B.pdf"):
                self._send(200, "application/pdf", pdf)
            elif self.path == "/viewer":
                self._send(200, "text/html", b"<html><body>Open the %PDF viewer</body></html>")
            elif self.path.startswith("/get"):
                self._send(200, "application/octet-stream", pdf,
                           {"Content-Disposition": 'attachment; filename="OPA2134 rev B.pdf"'})
            elif self.path == "/login":
                self._send(200, "text/html", b"<html><body>Access Denied</body></html>")
            else:
                self._send(404, "text/plain", b"not found")

        def _send(self, code, kind, body, headers=None):
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", agents, pdf
    httpd.shutdown()
    httpd.server_close()


def test_download(store_home, server, tmp_path):
    base, agents, pdf = server
    with Store() as store:
        # A document that is in the store with no URL gets the URL.
        (tmp_path / "local.pdf").write_bytes(pdf)
        local = store.add_document(tmp_path / "local.pdf")
        doc = add_from_url(store, f"{base}/ds.pdf")
        assert doc.id == local.id
        url = lambda: store.db.execute("SELECT source_url FROM documents WHERE id = ?",  # noqa: E731
                                       (doc.id,)).fetchone()["source_url"]
        assert url() == f"{base}/ds.pdf"
        add_from_url(store, f"{base}/get?id=7")  # the same content: the first URL stays
        assert url() == f"{base}/ds.pdf"
        # A space in a URL (copied from a CAD field).
        assert add_from_url(store, f"{base}/OPA2134 rev B.pdf").id == doc.id
        assert not any((store.home / "tmp").iterdir())  # no files left
    assert agents[0].startswith("Mozilla/5.0")


def test_download_moves_the_file(store_home, server):
    base = server[0]
    with Store() as store:
        doc = add_from_url(store, f"{base}/get?id=7")
        assert doc.filename == "OPA2134 rev B.pdf" and doc.path.is_file()
        assert not any((store.home / "tmp").iterdir())


def test_download_errors(store_home, server):
    base = server[0]
    with Store() as store:
        with pytest.raises(LensError, match="did not give a PDF .*text/html.*by hand"):
            add_from_url(store, f"{base}/login")
        with pytest.raises(LensError, match="HTTP 404"):
            add_from_url(store, f"{base}/missing.pdf")
        with pytest.raises(LensError, match="not an http or https URL"):
            add_from_url(store, "file:///etc/passwd")
        with pytest.raises(LensError, match="download failed"):
            add_from_url(store, "http://127.0.0.1:9/ds.pdf")
        # "%PDF" in an HTML page is not a PDF.
        with pytest.raises(LensError, match="did not give a PDF"):
            add_from_url(store, f"{base}/viewer")
        assert store.documents() == []


def test_download_limits(store_home, server, monkeypatch):
    base = server[0]
    with Store() as store:
        monkeypatch.setattr(fetch, "MAX_BYTES", 10)
        with pytest.raises(LensError, match="larger than"):
            add_from_url(store, f"{base}/ds.pdf")  # from Content-Length, before the download
        monkeypatch.setattr(fetch, "MAX_BYTES", 300 << 20)
        monkeypatch.setattr(fetch, "MIN_RATE_AFTER_S", -1)
        monkeypatch.setattr(fetch, "MIN_RATE", 1 << 40)
        with pytest.raises(LensError, match="server sends less than"):
            add_from_url(store, f"{base}/ds.pdf")


def headers(disposition):
    m = email.message.Message()
    m["Content-Disposition"] = disposition
    return m


def test_file_names():
    assert _filename("https://x.com/a/OPA2134.PDF") == "OPA2134.pdf"
    assert _filename("https://x.com/get?id=1", headers('attachment; filename="a;b.pdf"')) == "a;b.pdf"
    assert _filename("https://x.com/get", headers("attachment; filename*=UTF-8'en'r%C3%A9v.pdf")) == "rév.pdf"
    assert _filename("https://x.com/get", headers(
        "attachment; filename*=ISO-8859-1''r%E9v.pdf")) == "rév.pdf"
    assert _filename("https://x.com/get", headers(
        'attachment; filename="x.pdf"; filename*=UTF-8\'\'y.pdf')) == "y.pdf"
    assert _filename("https://x.com/") == "document.pdf"
    assert _filename("https://x.com/ds/..") == "document.pdf"
    assert len(_filename("https://x.com/get", headers(f'attachment; filename="{"a" * 300}.pdf"'))) == 124


def test_cli_and_mcp(store_home, server, capsys, monkeypatch):
    base = server[0]
    assert main(["add", f"{base}/ds.pdf", "--part", "OPA2134"]) == 0
    assert "ds.pdf (1 pages)" in capsys.readouterr().out
    assert main(["add", f"{base}/login"]) == 2
    assert "did not give a PDF" in capsys.readouterr().err
    assert main(["add", f"{base}/ds.pdf", "--url", "https://vendor/x.pdf"]) == 2
    assert "--url is only for a local file" in capsys.readouterr().err

    import asyncio

    from datasheet_lens import server as srv

    monkeypatch.setattr("datasheet_lens.setup.element_backend", lambda: "text")
    result = asyncio.run(srv.mcp.call_tool("add_document", {"path": f"{base}/get?id=7", "part": "OPA2134"}))
    blocks = result[0] if isinstance(result, tuple) else getattr(result, "content", result)
    assert "1 pages" in "\n".join(getattr(b, "text", "") for b in blocks)
    for job in list(srv._builds.values()):
        job["thread"].join(10)
    srv._builds.clear()
