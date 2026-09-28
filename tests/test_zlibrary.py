"""Offline unit tests: the HTTP layer is fully mocked, nothing touches the network.

Run: pytest tests/  or  python tests/test_zlibrary.py
"""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from Zlibrary import (  # noqa: E402
    HTTPError,
    LoginFailed,
    NotLoggedInError,
    Zlibrary,
    ZlibraryError,
    _normalize_domain,
)


class FakeResponse:
    def __init__(self, status_code=200, json_body=None, content=b"", text=""):
        self.status_code = status_code
        self._json = json_body
        self.content = content
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("not json")
        return self._json


class FakeSession:
    """Stand-in for requests.Session recording every call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.responses.pop(0)

    headers = {}
    proxies = {}


def make_client(responses, **kwargs):
    client = Zlibrary(domain="z-library.sk", **kwargs)
    fake = FakeSession(responses)
    client._session = fake
    return client, fake


def test_normalize_domain():
    assert _normalize_domain(" https://z-library.sk/ ") == "z-library.sk"
    assert _normalize_domain("http://abc.def.org") == "abc.def.org"
    assert _normalize_domain("1lib.sk") == "1lib.sk"


def test_not_logged_in_raises():
    client, _ = make_client([])
    try:
        client.search(message="x")
        assert False, "should have raised"
    except NotLoggedInError:
        pass


def test_login_failure_raises_with_message():
    client, _ = make_client([
        FakeResponse(200, {"success": False, "validationError": "Wrong email or password"}),
    ])
    try:
        client.login("a@b.c", "pw")
        assert False, "should have raised"
    except LoginFailed as e:
        assert "Wrong email or password" in str(e)


def test_login_success_and_cookie_flow():
    client, fake = make_client([
        FakeResponse(200, {"success": True, "user": {"id": 42, "email": "a@b.c",
                                                     "remix_userkey": "KEY", "name": "n"}}),
        FakeResponse(200, {"results": []}),
    ])
    client.login("a@b.c", "pw")
    assert client.isLoggedIn() is True
    assert client.email == "a@b.c"
    client.search(message="x")
    method, url, kwargs = fake.calls[1]
    assert url == "https://z-library.sk/eapi/book/search"
    assert kwargs["cookies"]["remix_userid"] == "42"
    assert kwargs["cookies"]["remix_userkey"] == "KEY"


def test_retry_on_500_then_success():
    client, fake = make_client([
        FakeResponse(200, {"success": True, "user": {"id": 1, "remix_userkey": "k"}}),
        FakeResponse(500),
        FakeResponse(200, {"ok": True}),
    ])
    client.login("a@b.c", "pw")
    with patch("Zlibrary.time.sleep") as slept:
        out = client.getProfile()
    assert out == {"ok": True}
    assert slept.called


def test_http_error_on_403_after_retries():
    client, _ = make_client([
        FakeResponse(200, {"success": True, "user": {"id": 1, "remix_userkey": "k"}}),
        FakeResponse(403), FakeResponse(403), FakeResponse(403),
    ])
    client.login("a@b.c", "pw")
    try:
        client.getProfile()
        assert False, "should have raised"
    except HTTPError as e:
        assert e.status == 403
        assert "/eapi/user/profile" in e.url


def test_non_json_body_raises_http_error():
    client, _ = make_client([
        FakeResponse(200, {"success": True, "user": {"id": 1, "remix_userkey": "k"}}),
        FakeResponse(200, json_body=None, text="<html>challenge</html>"),
    ])
    client.login("a@b.c", "pw")
    try:
        client.getProfile()
        assert False, "should have raised"
    except HTTPError as e:
        assert "not JSON" in str(e)


def test_download_book_builds_filename():
    client, fake = make_client([
        FakeResponse(200, {"success": True, "user": {"id": 1, "remix_userkey": "k"}}),
        FakeResponse(200, {"file": {"description": "Some Book", "author": "An Author",
                                    "extension": "EPUB",
                                    "downloadLink": "https://dyn.example.com/file"}}),
        FakeResponse(200, content=b"BOOKBYTES"),
    ])
    client.login("a@b.c", "pw")
    filename, content = client.downloadBook({"id": 123, "hash": "abc"})
    assert filename == "Some Book (An Author).EPUB"
    assert content == b"BOOKBYTES"
    assert fake.calls[2][1] == "https://dyn.example.com/file"


def test_discover_domains_parses_api_response():
    client, fake = make_client([
        FakeResponse(200, {"domains": [
            {"domain": "z-library.sk", "contentAvailable": True, "isRedirector": False},
            {"domain": "1lib.sk", "contentAvailable": True, "isRedirector": False},
        ]}),
    ])
    domains = client.discoverDomains()  # unauthenticated endpoint
    assert [d["domain"] for d in domains] == ["z-library.sk", "1lib.sk"]
    assert fake.calls[0][1] == "https://z-library.sk/eapi/info/domains"


def test_get_book_format_alias():
    client, fake = make_client([
        FakeResponse(200, {"success": True, "user": {"id": 1, "remix_userkey": "k"}}),
        FakeResponse(200, {"formats": []}),
        FakeResponse(200, {"formats": []}),
    ])
    client.login("a@b.c", "pw")
    assert client.getBookForamt(1, "h") == client.getBookFormat(1, "h")
    assert fake.calls[1][1].endswith("/eapi/book/1/h/formats")
    assert fake.calls[2][1].endswith("/eapi/book/1/h/formats")


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(
        (k, v) for k, v in globals().items() if k.startswith("test_")
    ):
        try:
            fn()
            print("PASS", name)
        except AssertionError as e:
            fails += 1
            print("FAIL", name, e)
        except Exception as e:
            fails += 1
            print("ERROR", name, "->", type(e).__name__, e)
    sys.exit(1 if fails else 0)
