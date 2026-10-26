import time
from urllib.parse import urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class SourceError(RuntimeError):
    """A deliberately credential-free error safe for the manifest and terminal."""


def origin(url: str) -> tuple:
    u = urlsplit(url)
    return u.scheme, u.hostname, u.port


class Http:
    def __init__(self, base: str, headers=None, cookies=None, delay=0.15):
        self.base = base.rstrip("/")
        self.headers = headers or {}
        self.delay = delay
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "SJTU-SSS/0.1 (personal study archive)"
        retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504],
                      allowed_methods=["GET", "HEAD"], respect_retry_after_header=True)
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        for name, value in (cookies or {}).items():
            self.session.cookies.set(name, value, domain=urlsplit(base).hostname, path="/", secure=True)

    def request(self, method, path, *, params=None, headers=None, **kwargs):
        url = urljoin(self.base + "/", path)
        request_headers = {**self.headers, **(headers or {})} if origin(url) == origin(self.base) else {}
        # Handle every redirect ourselves: custom Auth and jwt-token headers must not cross origins.
        for _ in range(12):
            if urlsplit(url).scheme != "https":
                raise SourceError("Refused non-HTTPS request")
            time.sleep(self.delay)
            try:
                response = self.session.request(method, url, params=params, headers=request_headers,
                                                timeout=(15, 90), allow_redirects=False, **kwargs)
            except requests.RequestException:
                raise SourceError("Network request failed (connection/timeout)") from None
            if response.is_redirect:
                target = urljoin(response.url, response.headers["Location"])
                if origin(target) != origin(url):
                    request_headers = {}
                if response.status_code == 303 or (response.status_code in (301, 302) and method == "POST"):
                    method = "GET"
                    kwargs.pop("data", None)
                    kwargs.pop("json", None)
                response.close()
                url, params = target, None
                continue
            if not response.ok:
                status = response.status_code
                response.close()
                raise SourceError(f"HTTP {status}" + (" (credentials missing, expired, or access denied)" if status in (401, 403) else ""))
            return response
        raise SourceError("Too many redirects")

    def json(self, method, path, **kwargs):
        with self.request(method, path, **kwargs) as response:
            try:
                return response.json()
            except ValueError:
                raise SourceError("Expected JSON; received a login page or incompatible response") from None

    def pages(self, path, params=None):
        url = urljoin(self.base + "/", path)
        seen = set()
        while url:
            if origin(url) != origin(self.base) or url in seen:
                raise SourceError("Invalid or cyclic pagination link")
            seen.add(url)
            with self.request("GET", url, params=params) as response:
                try:
                    items = response.json()
                except ValueError:
                    raise SourceError("Invalid paginated JSON") from None
                if not isinstance(items, list):
                    raise SourceError("Expected a paginated list")
                yield from items
                url = response.links.get("next", {}).get("url")
            params = None
