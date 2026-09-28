"""
Copyright (c) 2023-2024 Bipinkrish
This file is part of the Zlibrary-API by Bipinkrish
Zlibrary-API / Zlibrary.py

Enhanced fork (granary17/Zlibrary-API), changes vs upstream:
  - every request returns a real value or raises; no more print()+None
  - configurable domain/mirror, optional proxies (e.g. Tor socks5)
  - requests.Session reuse, timeouts, retry with backoff on 429/5xx/network
  - HTTP status checked before .json(); non-JSON (challenge) pages raise
  - correct type annotations, py3.7+ via future annotations
  - 100% upstream method names/signatures kept (drop-in); getBookFormat()
    added as alias of the misspelled getBookForamt()

Upstream: https://github.com/bipinkrish/Zlibrary-API/
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple, Union

import requests

logger = logging.getLogger("zlibrary")

DEFAULT_DOMAIN = "1lib.sk"
DEFAULT_TIMEOUT = 30
DEFAULT_RETRIES = 2
RETRY_STATUSES = (429, 500, 502, 503, 504)

_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "accept-language": "en-US,en;q=0.9",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36",
}


def _normalize_domain(domain: str) -> str:
    d = domain.strip()
    for prefix in ("https://", "http://"):
        if d.startswith(prefix):
            d = d[len(prefix):]
    return d.rstrip("/")


class ZlibraryError(Exception):
    """Base class for all zlibrary client errors."""


class LoginFailed(ZlibraryError):
    """The server rejected the credentials / token."""


class NotLoggedInError(ZlibraryError):
    """An authenticated endpoint was called before login()."""


class HTTPError(ZlibraryError):
    """Non-200 HTTP status, or a non-JSON body (challenge page)."""

    def __init__(self, url: str, status: int, hint: str = ""):
        self.url = url
        self.status = status
        msg = "%s returned HTTP %d" % (url, status)
        if hint:
            msg += " (%s)" % hint
        super().__init__(msg)


class Zlibrary:
    def __init__(
        self,
        email: Optional[str] = None,
        password: Optional[str] = None,
        remix_userid: Union[int, str, None] = None,
        remix_userkey: Optional[str] = None,
        domain: str = DEFAULT_DOMAIN,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_RETRIES,
        proxies: Optional[Dict[str, str]] = None,
    ):
        self._domain = _normalize_domain(domain)
        self._timeout = timeout
        self._max_retries = max_retries
        self._session = requests.Session()
        self._session.headers.update(_HEADERS)
        if proxies:
            self._session.proxies.update(proxies)

        self._cookies: Dict[str, str] = {"siteLanguageV2": "en"}
        self._loggedin = False
        self._email: Optional[str] = None
        self._name: Optional[str] = None
        self._kindle_email: Optional[str] = None
        self._remix_userid: Optional[str] = None
        self._remix_userkey: Optional[str] = None

        if email is not None and password is not None:
            self.login(email, password)
        elif remix_userid is not None and remix_userkey is not None:
            self.loginWithToken(remix_userid, remix_userkey)

    # --------------------------- properties ---------------------------

    @property
    def domain(self) -> str:
        return self._domain

    @property
    def email(self) -> Optional[str]:
        return self._email

    @property
    def name(self) -> Optional[str]:
        return self._name

    def isLoggedIn(self) -> bool:
        return self._loggedin

    def switchDomain(self, domain: str) -> str:
        """Point the client at another mirror (e.g. from getDomains())."""
        self._domain = _normalize_domain(domain)
        logger.debug("switched domain to %s", self._domain)
        return self._domain

    # --------------------------- transport ---------------------------

    def _request(
        self,
        method: str,
        path: str,
        data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        override: bool = False,
        cookies: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        if not self._loggedin and not override and cookies is None:
            raise NotLoggedInError(
                "not logged in: call login()/loginWithToken() first (%s)" % path
            )
        url = "https://" + self._domain + path
        attempt = 0
        while True:
            try:
                resp = self._session.request(
                    method,
                    url,
                    data=data,
                    params=params,
                    cookies=self._cookies if cookies is None else cookies,
                    timeout=self._timeout,
                )
            except requests.RequestException as exc:
                attempt += 1
                if attempt > self._max_retries:
                    raise ZlibraryError(
                        "%s %s failed after %d attempts: %s"
                        % (method, url, attempt, exc)
                    ) from exc
                wait = min(2 ** attempt, 8)
                logger.debug("network error, retry %d in %ss: %s", attempt, wait, exc)
                time.sleep(wait)
                continue
            if resp.status_code in RETRY_STATUSES and attempt < self._max_retries:
                attempt += 1
                wait = min(2 ** attempt, 8)
                logger.debug("HTTP %d, retry %d in %ss", resp.status_code, attempt, wait)
                time.sleep(wait)
                continue
            break
        if resp.status_code != 200:
            raise HTTPError(
                url,
                resp.status_code,
                hint="rate-limited or challenge page" if resp.status_code in (403, 429) else "",
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise HTTPError(
                url, resp.status_code, hint="body is not JSON (challenge page?)"
            ) from exc

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None,
             cookies: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        return self._request("GET", path, params=params, cookies=cookies)

    def _post(self, path: str, data: Optional[Dict[str, Any]] = None,
              override: bool = False) -> Dict[str, Any]:
        return self._request("POST", path, data=data, override=override)

    # --------------------------- auth ---------------------------

    def _absorb(self, response: Dict[str, Any]) -> Dict[str, Any]:
        if response.get("success") is False:
            raise LoginFailed(
                str(
                    response.get("validationError")
                    or response.get("error")
                    or response
                )
            )
        user = response.get("user")
        if not user:
            raise ZlibraryError("unexpected login response: %r" % (response,))
        self._email = user.get("email")
        self._name = user.get("name")
        self._kindle_email = user.get("kindle_email")
        self._remix_userid = str(user.get("id"))
        self._remix_userkey = user.get("remix_userkey")
        self._cookies["remix_userid"] = self._remix_userid
        self._cookies["remix_userkey"] = self._remix_userkey
        self._loggedin = True
        return response

    def login(self, email: str, password: str) -> Dict[str, Any]:
        return self._absorb(
            self._post("/eapi/user/login", data={"email": email, "password": password},
                       override=True)
        )

    def loginWithToken(
        self, remix_userid: Union[int, str], remix_userkey: str
    ) -> Dict[str, Any]:
        return self._absorb(
            self._get(
                "/eapi/user/profile",
                cookies={
                    "siteLanguageV2": "en",
                    "remix_userid": str(remix_userid),
                    "remix_userkey": remix_userkey,
                },
            )
        )

    # --------------------------- account ---------------------------

    def getProfile(self) -> Dict[str, Any]:
        return self._get("/eapi/user/profile")

    def getDonations(self) -> Dict[str, Any]:
        return self._get("/eapi/user/donations")

    def updateInfo(
        self,
        email: Optional[str] = None,
        password: Optional[str] = None,
        name: Optional[str] = None,
        kindle_email: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self._post(
            "/eapi/user/update",
            data={
                k: v
                for k, v in {
                    "email": email,
                    "password": password,
                    "name": name,
                    "kindle_email": kindle_email,
                }.items()
                if v is not None
            },
        )

    def hideBanner(self) -> Dict[str, Any]:
        return self._get("/eapi/user/hide-banner")

    def getDownloadsLeft(self) -> int:
        user_profile: Dict[str, Any] = self.getProfile()["user"]
        return user_profile.get("downloads_limit", 10) - user_profile.get(
            "downloads_today", 0
        )

    def makeTokenSigin(self, name: str, id_token: str) -> Dict[str, Any]:
        return self._post(
            "/eapi/user/token-sign-in",
            data={"name": name, "id_token": id_token},
            override=True,
        )

    def sendCode(self, email: str, password: str, name: str) -> Dict[str, Any]:
        response = self._post(
            "/papi/user/verification/send-code",
            data={
                "email": email,
                "password": password,
                "name": name,
                "rx": 215,
                "action": "registration",
                "site_mode": "books",
                "isSinglelogin": 1,
            },
            override=True,
        )
        if response.get("success"):
            response["msg"] = (
                "Verification code is sent to mail, use verifyCode to complete registration"
            )
        return response

    def verifyCode(
        self, email: str, password: str, name: str, code: str
    ) -> Dict[str, Any]:
        return self._post(
            "/rpc.php",
            data={
                "email": email,
                "password": password,
                "name": name,
                "verifyCode": code,
                "rx": 215,
                "action": "registration",
                "redirectUrl": "",
                "isModa": True,
                "gg_json_mode": 1,
            },
            override=True,
        )

    def recoverPassword(self, email: str) -> Dict[str, Any]:
        return self._post(
            "/eapi/user/password-recovery", data={"email": email}, override=True
        )

    def makeRegistration(self, email: str, password: str, name: str) -> Dict[str, Any]:
        return self._post(
            "/eapi/user/registration",
            data={"email": email, "password": password, "name": name},
            override=True,
        )

    def resendConfirmation(self) -> Dict[str, Any]:
        return self._post("/eapi/user/email/confirmation/resend")

    # --------------------------- books ---------------------------

    def search(
        self,
        message: Optional[str] = None,
        yearFrom: Optional[int] = None,
        yearTo: Optional[int] = None,
        languages: Optional[str] = None,
        extensions: Optional[List[str]] = None,
        order: Optional[str] = None,
        page: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        return self._post(
            "/eapi/book/search",
            data={
                k: v
                for k, v in {
                    "message": message,
                    "yearFrom": yearFrom,
                    "yearTo": yearTo,
                    "languages": languages,
                    "extensions[]": extensions,
                    "order": order,
                    "page": page,
                    "limit": limit,
                }.items()
                if v is not None
            },
        )

    def getBookInfo(
        self, bookid: Union[int, str], hashid: str, switch_language: Optional[str] = None
    ) -> Dict[str, Any]:
        if switch_language is not None:
            return self._get(
                "/eapi/book/%s/%s" % (bookid, hashid),
                params={"switch-language": switch_language},
            )
        return self._get("/eapi/book/%s/%s" % (bookid, hashid))

    def getBookForamt(self, bookid: Union[int, str], hashid: str) -> Dict[str, Any]:
        return self._get("/eapi/book/%s/%s/formats" % (bookid, hashid))

    def getBookFormat(self, bookid: Union[int, str], hashid: str) -> Dict[str, Any]:
        """Alias for getBookForamt (upstream typo kept for compatibility)."""
        return self.getBookForamt(bookid, hashid)

    def getSimilar(self, bookid: Union[int, str], hashid: str) -> Dict[str, Any]:
        return self._get("/eapi/book/%s/%s/similar" % (bookid, hashid))

    def getMostPopular(self, switch_language: Optional[str] = None) -> Dict[str, Any]:
        if switch_language is not None:
            return self._get(
                "/eapi/book/most-popular", params={"switch-language": switch_language}
            )
        return self._get("/eapi/book/most-popular")

    def getRecently(self) -> Dict[str, Any]:
        return self._get("/eapi/book/recently")

    def getUserRecommended(self) -> Dict[str, Any]:
        return self._get("/eapi/user/book/recommended")

    def getUserDownloaded(
        self, order: Optional[str] = None, page: Optional[int] = None, limit: Optional[int] = None
    ) -> Dict[str, Any]:
        """order takes one of the values ["year",...]"""
        params = {
            k: v
            for k, v in {"order": order, "page": page, "limit": limit}.items()
            if v is not None
        }
        return self._get("/eapi/user/book/downloaded", params=params)

    def getUserSaved(
        self, order: Optional[str] = None, page: Optional[int] = None, limit: Optional[int] = None
    ) -> Dict[str, Any]:
        """order takes one of the values ["year",...]"""
        params = {
            k: v
            for k, v in {"order": order, "page": page, "limit": limit}.items()
            if v is not None
        }
        return self._get("/eapi/user/book/saved", params=params)

    def saveBook(self, bookid: Union[int, str]) -> Dict[str, Any]:
        return self._get("/eapi/user/book/%s/save" % bookid)

    def unsaveUserBook(self, bookid: Union[int, str]) -> Dict[str, Any]:
        return self._get("/eapi/user/book/%s/unsave" % bookid)

    def deleteUserBook(self, bookid: Union[int, str]) -> Dict[str, Any]:
        return self._get("/eapi/user/book/%s/delete" % bookid)

    def sendTo(self, bookid: Union[int, str], hashid: str, totype: str) -> Dict[str, Any]:
        return self._get("/eapi/book/%s/%s/send-to-%s" % (bookid, hashid, totype))

    # --------------------------- info ---------------------------

    def getExtensions(self) -> Dict[str, Any]:
        return self._get("/eapi/info/extensions")

    def getDomains(self) -> Dict[str, Any]:
        return self._get("/eapi/info/domains")

    def getLanguages(self) -> Dict[str, Any]:
        return self._get("/eapi/info/languages")

    def getPlans(self, switch_language: Optional[str] = None) -> Dict[str, Any]:
        if switch_language is not None:
            return self._get("/eapi/info/plans", params={"switch-language": switch_language})
        return self._get("/eapi/info/plans")

    def getInfo(self, switch_language: Optional[str] = None) -> Dict[str, Any]:
        if switch_language is not None:
            return self._get("/eapi/info", params={"switch-language": switch_language})
        return self._get("/eapi/info")

    # --------------------------- files ---------------------------

    def _download_bytes(self, url: str) -> bytes:
        attempt = 0
        while True:
            try:
                resp = self._session.get(url, timeout=self._timeout)
            except requests.RequestException as exc:
                attempt += 1
                if attempt > self._max_retries:
                    raise ZlibraryError(
                        "download of %s failed after %d attempts: %s"
                        % (url, attempt, exc)
                    ) from exc
                time.sleep(min(2 ** attempt, 8))
                continue
            break
        if resp.status_code != 200:
            raise HTTPError(url, resp.status_code, hint="file download")
        return resp.content

    def getImage(self, book: Dict[str, Any]) -> bytes:
        return self._download_bytes(book["cover"])

    def downloadBook(self, book: Dict[str, Any]) -> Tuple[str, bytes]:
        response = self._get("/eapi/book/%s/%s/file" % (book["id"], book["hash"]))
        file = response.get("file")
        if not file or not file.get("downloadLink"):
            raise ZlibraryError("no downloadLink in response: %r" % (response,))
        filename = file.get("description") or str(book.get("id", "book"))
        if file.get("author"):
            filename += " (" + file["author"] + ")"
        filename += "." + file.get("extension", "pdf")
        content = self._download_bytes(file["downloadLink"])
        return filename, content
