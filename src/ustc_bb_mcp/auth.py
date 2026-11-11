"""Bounded Blackboard HTTP session and USTC CAS authentication."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from contextlib import asynccontextmanager
from http.cookiejar import Cookie
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

from .config import BB_HOSTS, Settings, credentials, private_json
from .errors import BBError

PORTAL_PATH = "/webapps/portal/execute/tabs/tabAction?tab_tab_group_id=_2_1"
CAS_HOSTS = frozenset({"passport.ustc.edu.cn", "id.ustc.edu.cn"})


def _login_page(source: str, url: str = "") -> bool:
    from .parsing import is_login_page

    return is_login_page(source, url)


def _authenticated(source: str, url: str = "") -> bool:
    from .parsing import is_authenticated_portal

    return is_authenticated_portal(source, url)


async def _browser_route(route) -> None:
    """Keep the login window on school sites without breaking CAPTCHA assets."""
    request = route.request
    part = urlsplit(request.url)
    allowed = BB_HOSTS | CAS_HOSTS
    if part.username or part.password:
        await route.abort()
        return
    if part.scheme == "http" and part.hostname in allowed:
        # Playwright cannot change a request's protocol via continue_. Use an
        # actual redirect so legacy CAS callbacks reach HTTPS without hanging.
        await route.fulfill(
            status=307,
            headers={"Location": urlunsplit(("https", part.netloc, part.path, part.query, part.fragment))},
            body="",
        )
        return
    if part.scheme != "https":
        await route.abort()
        return
    top_navigation = request.is_navigation_request() and request.frame.parent_frame is None
    if top_navigation and part.hostname not in allowed:
        await route.abort()
        return
    # HTTPS subresources/embedded challenges are selected by the official login
    # page. Normal browser origin rules keep BB/CAS cookies off unrelated hosts.
    await route.continue_()


async def _advance_login_entry(page) -> None:
    """Follow only the known read-only login links, never submit a CAS form."""
    from playwright.async_api import Error as PlaywrightError

    part = urlsplit(page.url)
    if part.hostname not in BB_HOSTS:
        return
    if part.path.startswith("/nginx_auth"):
        name = re.compile(r"^(登录|Login)$", re.IGNORECASE)
    elif part.path in {"", "/", "/webapps/login", "/webapps/login/"}:
        name = re.compile(r"统一身份认证|Single Sign.?On|SSO", re.IGNORECASE)
    else:
        return
    try:
        link = page.get_by_role("link", name=name)
        if await link.count() and await link.first.is_visible():
            await link.first.click(timeout=5000)
    except PlaywrightError:
        # Navigation can replace the document during count/visibility checks as
        # well as during click. Keep the authentication window available.
        pass


class Session:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(
            timeout=settings.timeout,
            follow_redirects=False,
            headers={"User-Agent": "USTC-BB-MCP/0.1", "Accept-Language": "zh-CN,en;q=0.8"},
        )
        self._owns_client = client is None
        self._semaphore = asyncio.Semaphore(3)
        self._auth_lock = asyncio.Lock()
        self._authenticated = False
        self._last_portal: httpx.Response | None = None
        self._state_version: tuple[int, int] | None = None
        self._load_state()

    def _bb_url(self, url: str) -> str:
        full = urljoin(self.settings.base_url + "/", url)
        try:
            part = urlsplit(full)
            port = part.port
        except ValueError as exc:
            raise BBError("unsafe_url", "BB 链接格式不正确，未发送会话。") from exc
        base = urlsplit(self.settings.base_url)
        same_host = part.hostname in BB_HOSTS and port in {None, 443}
        if self.settings.test_mode:
            same_host = part.netloc == base.netloc
        if part.username or part.password or not same_host:
            raise BBError("unsafe_url", "BB 链接超出允许的站点范围，未发送会话。")
        scheme = part.scheme
        if scheme == "http" and not self.settings.test_mode:
            scheme = "https"
        if scheme != "https" and not (self.settings.test_mode and scheme == "http"):
            raise BBError("unsafe_url", "拒绝访问不安全的 BB 链接。")
        return urlunsplit((scheme, part.netloc, part.path or "/", part.query, ""))

    def _valid_cookie(self, cookie: dict) -> bool:
        domain = str(cookie.get("domain", "")).lstrip(".").lower()
        if self.settings.test_mode:
            return domain == urlsplit(self.settings.base_url).hostname
        return domain in BB_HOSTS

    def _import_cookies(self, state: dict) -> None:
        if not isinstance(state, dict) or not isinstance(state.get("cookies", []), list):
            raise ValueError("invalid session state")
        for item in state.get("cookies", []):
            if not isinstance(item, dict) or not self._valid_cookie(item):
                continue
            if not isinstance(item.get("name"), str) or not isinstance(item.get("value"), str):
                continue
            expires = item.get("expires", -1)
            if expires and expires > 0 and expires <= time.time():
                continue
            domain = item["domain"]
            self.client.cookies.jar.set_cookie(Cookie(
                version=0, name=item["name"], value=item["value"], port=None, port_specified=False,
                domain=domain, domain_specified=domain.startswith("."), domain_initial_dot=domain.startswith("."),
                path=item.get("path", "/"), path_specified=True,
                secure=item.get("secure", not self.settings.test_mode),
                expires=int(expires) if expires and expires > 0 else None,
                discard=not expires or expires < 0, comment=None, comment_url=None,
                rest={"HttpOnly": item.get("httpOnly", True), "SameSite": item.get("sameSite", "Lax")},
                rfc2109=False,
            ))

    def _load_state(self) -> None:
        path = self.settings.state_path
        if not path.exists():
            return
        try:
            with path.open(encoding="utf-8") as stream:
                state = json.load(stream)
                stat = os.fstat(stream.fileno())
            self.client.cookies.clear()
            self._import_cookies(state)
            self._state_version = (stat.st_mtime_ns, stat.st_size)
        except (OSError, ValueError, TypeError):
            # A corrupt/expired local session can be replaced by a fresh login.
            self.client.cookies.clear()

    def _reload_changed_state(self) -> None:
        try:
            stat = self.settings.state_path.stat()
        except OSError:
            return
        if (stat.st_mtime_ns, stat.st_size) != self._state_version:
            self._load_state()
            self._authenticated = False

    async def _save_state(self) -> None:
        private_json(self.settings.state_path, await self.storage_state())
        stat = self.settings.state_path.stat()
        self._state_version = (stat.st_mtime_ns, stat.st_size)

    async def storage_state(self) -> dict:
        cookies = []
        for cookie in self.client.cookies.jar:
            same_site = str(cookie.get_nonstandard_attr("SameSite", "Lax")).capitalize()
            if same_site not in {"Lax", "Strict", "None"}:
                same_site = "Lax"
            item = {
                "name": cookie.name, "value": cookie.value, "domain": cookie.domain,
                "path": cookie.path or "/", "expires": cookie.expires or -1,
                "httpOnly": cookie.has_nonstandard_attr("HttpOnly") and cookie.get_nonstandard_attr("HttpOnly") is not False, "secure": cookie.secure,
                "sameSite": same_site,
            }
            if self._valid_cookie(item):
                cookies.append(item)
        return {"cookies": cookies, "origins": []}

    async def update_cookies(self, state: dict) -> None:
        # Replace only BB cookies; unrelated cookies are neither loaded nor persisted.
        self.client.cookies.clear()
        self._import_cookies(state)
        await self._save_state()

    @property
    def identity(self) -> str:
        # A preview is valid only in the same authenticated BB session.
        cookies = [
            cookie for cookie in self.client.cookies.jar
            if self._valid_cookie({"domain": cookie.domain})
        ]
        authentication = [
            cookie for cookie in cookies
            if "session" in cookie.name.lower() or cookie.name.lower() in {"bbngauth", "bbnga"}
        ]
        values = sorted(
            (cookie.domain, cookie.path, cookie.name, cookie.value)
            for cookie in authentication or cookies
        )
        return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()

    async def _send(self, method: str, url: str, *, retry: bool = True, **kwargs) -> httpx.Response:
        attempts = 2 if retry and method in {"GET", "HEAD"} else 1
        for attempt in range(attempts):
            try:
                async with self._semaphore:
                    response = await self.client.request(
                        method, url, follow_redirects=False, timeout=self.settings.timeout, **kwargs
                    )
                if response.status_code in {429, 500, 502, 503, 504} and attempt + 1 < attempts:
                    await asyncio.sleep(0.1)
                    continue
                return response
            except httpx.RequestError as exc:
                if attempt + 1 >= attempts:
                    raise BBError("network_error", "BB 请求失败或超时，请检查网络后重试。") from exc
                await asyncio.sleep(0.1)
        raise BBError("network_error", "BB 请求失败。")

    async def _follow(self, method: str, url: str, *, auth_redirect: bool = False, **kwargs) -> httpx.Response:
        current = self._bb_url(url)
        for _ in range(10):
            response = await self._send(method, current, **kwargs)
            if response.status_code not in {301, 302, 303, 307, 308}:
                return response
            location = urljoin(current, response.headers.get("Location", ""))
            part = urlsplit(location)
            if part.hostname in CAS_HOSTS:
                if auth_redirect:
                    return response
                raise BBError("session_expired", "BB 会话已失效，需要重新登录。")
            current = self._bb_url(location)
            if method not in {"GET", "HEAD"}:
                if response.status_code in {301, 302, 303}:
                    method, kwargs = "GET", {}
                else:
                    raise BBError("write_redirect", "BB 要求重定向写入请求；已停止，避免重复提交。")
        raise BBError("redirect_loop", "BB 重定向次数过多，请重新登录。")

    async def _probe(self) -> bool:
        response = await self._follow("GET", PORTAL_PATH, auth_redirect=True)
        self._last_portal = response
        if response.status_code == 429 or response.status_code >= 500:
            raise BBError("http_error", f"BB 认证检查暂不可用（HTTP {response.status_code}），请稍后重试。")
        return response.status_code == 200 and _authenticated(response.text, str(response.url))

    def _cas_service(self, candidate: str) -> str | None:
        part = urlsplit(candidate)
        if part.hostname not in CAS_HOSTS or part.username or part.password:
            return None
        if part.scheme not in {"http", "https"}:
            return None
        service = parse_qs(part.query).get("service", [None])[0]
        if not service:
            return None
        # CAS service is an exact identifier: retain legacy http for get_ticket.
        self._bb_url(service)
        return service

    async def discover_cas_service(self, start: str | None = None) -> str:
        pending = [start] if start else [self.settings.base_url + "/", self.settings.base_url + "/webapps/login/"]
        seen: set[str] = set()
        while pending and len(seen) < 10:
            current = self._bb_url(pending.pop(0))
            if current in seen:
                continue
            seen.add(current)
            response = await self._follow("GET", current, auth_redirect=True)
            if response.status_code >= 400:
                continue
            candidates = [urljoin(str(response.url), response.headers.get("Location", ""))]
            candidates.extend(
                urljoin(str(response.url), str(link.get("href", "")))
                for link in BeautifulSoup(response.text, "html.parser").select("a[href]")
            )
            for candidate in candidates:
                service = self._cas_service(candidate)
                if service:
                    return service
                part = urlsplit(candidate)
                if part.path.startswith(("/nginx_auth", "/webapps/login")):
                    try:
                        bounded = self._bb_url(candidate)
                    except BBError:
                        continue
                    if bounded not in seen:
                        pending.append(bounded)
        raise BBError("auth_entry_not_found", "未发现 BB 统一认证入口，请运行 login --browser 完成登录。")

    async def _cas_login(self, username: str, password: str) -> None:
        from pyustc import CASClient

        try:
            service = await self.discover_cas_service()
            async with asyncio.timeout(self.settings.timeout * 4):
                async with CASClient.login_by_pwd(username, password) as cas:
                    seen = set()
                    for _ in range(4):
                        if service in seen:
                            raise BBError("auth_loop", "统一认证返回相同入口，请运行 login --browser。")
                        seen.add(service)
                        ticket = await cas.get_ticket(service)
                        part = urlsplit(self._bb_url(service))
                        query = parse_qs(part.query, keep_blank_values=True)
                        query["ticket"] = [ticket]
                        callback = urlunsplit((part.scheme, part.netloc, part.path, urlencode(query, doseq=True), ""))
                        await self._follow("GET", callback, auth_redirect=True)
                        if await self._probe():
                            self._authenticated = True
                            await self._save_state()
                            return
                        last = self._last_portal
                        location = urljoin(str(last.url), last.headers.get("Location", "")) if last else ""
                        service = self._cas_service(location) or await self.discover_cas_service()
        except BBError:
            raise
        except Exception as exc:
            raise BBError(
                "interaction_required",
                "统一认证未完成。请检查账号密码；若需要验证码或二次认证，运行 ustc-bb-mcp login --browser。",
            ) from exc
        raise BBError("interaction_required", "CAS 已通过但 BB 仍未登录，请运行 login --browser。")

    async def ensure_session(self, force: bool = False) -> dict:
        async with self._auth_lock:
            self._reload_changed_state()
            if force:
                self._authenticated = False
            if await self._probe():
                self._authenticated = True
                await self._save_state()
                return {"authenticated": True, "configured_username": self.settings.username or None}
            # A separate `login --browser` command may have refreshed the on-disk
            # session while this long-running MCP process was still alive.
            old_identity = self.identity
            self._load_state()
            if self.identity != old_identity and await self._probe():
                self._authenticated = True
                await self._save_state()
                return {"authenticated": True, "configured_username": self.settings.username or None}
            self._authenticated = False
            username, password = credentials(self.settings)
            if not username or not password:
                raise BBError(
                    "credentials_required",
                    "尚未登录。请运行 ustc-bb-mcp configure 配置凭据，或 ustc-bb-mcp login --browser；也可设置 USTC_CAS_USR/USTC_CAS_PWD。",
                )
            await self._cas_login(username, password)
            return {"authenticated": True, "username": username}

    async def get(self, url: str, authenticate: bool = True) -> httpx.Response:
        self._bb_url(url)  # Validate before even probing or authenticating.
        self._reload_changed_state()
        if authenticate and not self._authenticated:
            await self.ensure_session()
        for attempt in range(2 if authenticate else 1):
            try:
                response = await self._follow("GET", url)
                expired = response.status_code == 401 or _login_page(response.text, str(response.url))
                if response.status_code == 403 and authenticate and attempt == 0 and not expired:
                    # A 403 may mean either a forbidden item or an expired gateway.
                    expired = not await self._probe()
            except BBError as exc:
                if exc.code != "session_expired":
                    raise
                expired, response = True, None
            if expired and authenticate and attempt == 0:
                self._authenticated = False
                await self.ensure_session(force=True)
                continue
            if expired:
                raise BBError("session_expired", "BB 会话失效，请重新登录。")
            if response is None:
                raise BBError("network_error", "BB 未返回可用响应。")
            if response.status_code == 403:
                raise BBError("permission_denied", "没有访问该 BB 内容的权限。")
            if response.status_code >= 400:
                raise BBError("http_error", f"BB 返回 HTTP {response.status_code}。")
            return response
        raise BBError("session_expired", "BB 会话失效，请重新登录。")

    async def _stream_follow(self, url: str) -> httpx.Response:
        current = self._bb_url(url)
        for _ in range(10):
            response = None
            for attempt in range(2):
                try:
                    request = self.client.build_request("GET", current, timeout=self.settings.timeout)
                    response = await self.client.send(request, stream=True, follow_redirects=False)
                    if response.status_code in {429, 500, 502, 503, 504} and attempt == 0:
                        await response.aclose()
                        await asyncio.sleep(0.1)
                        continue
                    break
                except httpx.RequestError as exc:
                    if attempt:
                        raise BBError("network_error", "课件下载连接失败或超时。") from exc
                    await asyncio.sleep(0.1)
            if response is None:
                raise BBError("network_error", "课件下载未返回响应。")
            if response.status_code not in {301, 302, 303, 307, 308}:
                return response
            location = urljoin(current, response.headers.get("Location", ""))
            await response.aclose()
            if urlsplit(location).hostname in CAS_HOSTS:
                raise BBError("session_expired", "BB 会话已失效，需要重新登录。")
            current = self._bb_url(location)
        raise BBError("redirect_loop", "课件下载重定向次数过多。")

    @asynccontextmanager
    async def stream(self, url: str):
        """Hold one concurrency slot until the caller finishes reading the file."""
        self._bb_url(url)
        self._reload_changed_state()
        if not self._authenticated:
            await self.ensure_session()
        for attempt in range(2):
            expired = False
            async with self._semaphore:
                response = None
                yielded = False
                try:
                    response = await self._stream_follow(url)
                    if response.status_code == 401:
                        expired = True
                    elif response.status_code == 403:
                        raise BBError("permission_denied", "没有下载该 BB 文件的权限。")
                    elif response.status_code >= 400:
                        raise BBError("http_error", f"课件下载返回 HTTP {response.status_code}。")
                    elif "text/html" in response.headers.get("content-type", "").lower():
                        # Blackboard can serve a 200 login page for an expired
                        # download URL. Only HTML is buffered; binary files stream.
                        await response.aread()
                        expired = _login_page(response.text, str(response.url))
                        if not expired:
                            yielded = True
                            yield response
                            return
                    else:
                        yielded = True
                        yield response
                        return
                except BBError as exc:
                    if yielded or exc.code != "session_expired":
                        raise
                    expired = True
                except httpx.RequestError as exc:
                    # A partially consumed body is never silently restarted here.
                    raise BBError("download_interrupted", "课件下载中断，请重新下载。") from exc
                finally:
                    if response is not None:
                        await response.aclose()
            if expired and attempt == 0:
                self._authenticated = False
                await self.ensure_session(force=True)
                continue
            raise BBError("session_expired", "BB 会话失效，请重新登录后下载。")

    async def login_browser(self) -> dict:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import async_playwright

        self.settings.profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        stage = "browser_launch"
        try:
            async with async_playwright() as playwright:
                context = await playwright.chromium.launch_persistent_context(
                    str(self.settings.profile_dir), headless=False, locale="zh-CN", service_workers="block"
                )
                try:
                    stage = "browser_setup"
                    await context.route("**/*", _browser_route)
                    state = await self.storage_state()
                    if state["cookies"]:
                        await context.add_cookies(state["cookies"])
                    page = context.pages[0] if context.pages else await context.new_page()
                    stage = "initial_navigation"
                    try:
                        await page.goto(self.settings.base_url + "/", wait_until="domcontentloaded", timeout=45000)
                    except PlaywrightError:
                        if not context.pages:
                            raise BBError("login_cancelled", "登录窗口已关闭，未更新会话。") from None
                        # A popup or navigation handoff may replace the initial
                        # page. Do not close the remaining authentication pages.
                    advanced: set[tuple[str | None, str]] = set()
                    last_probe_error = None
                    deadline = time.monotonic() + 300
                    while time.monotonic() < deadline:
                        if not context.pages:
                            raise BBError("login_cancelled", "登录窗口已关闭，未更新会话。")
                        stage = "login_entry"
                        for current_page in list(context.pages):
                            part = urlsplit(current_page.url)
                            entry = (part.hostname, part.path.rstrip("/"))
                            if (part.hostname in BB_HOSTS
                                and (entry[1] in {"", "/webapps/login"} or part.path.startswith("/nginx_auth"))
                                and entry not in advanced):
                                # Query changes must not click the same login
                                # entry again and create another authentication tab.
                                advanced.add(entry)
                                await _advance_login_entry(current_page)
                        await asyncio.sleep(2)
                        stage = "browser_state"
                        try:
                            # storage_state() may open temporary pages for origins
                            # left behind by SSO. Poll cookies without creating tabs.
                            state = {"cookies": await context.cookies(), "origins": []}
                        except PlaywrightError:
                            if not context.pages:
                                raise BBError("login_cancelled", "登录窗口已关闭，未更新会话。") from None
                            continue
                        self.client.cookies.clear()
                        self._import_cookies(state)
                        stage = "session_probe"
                        try:
                            authenticated = await self._probe()
                        except BBError as error:
                            if error.code not in {"network_error", "http_error", "session_expired"}:
                                raise
                            last_probe_error = error.code
                            continue
                        if authenticated:
                            self._authenticated = True
                            stage = "session_save"
                            await self._save_state()
                            return {"authenticated": True, "method": "browser"}
                    raise BBError("login_timeout", "浏览器登录等待超过 5 分钟，请重新运行 login --browser。",
                                  details={"last_probe_error": last_probe_error})
                finally:
                    await context.close()
        except BBError:
            raise
        except Exception as exc:
            details = {"stage": stage, "exception_type": type(exc).__name__}
            network_error = re.search(r"net::ERR_[A-Z0-9_]+", str(exc))
            if network_error:
                details["network_error"] = network_error[0]
            raise BBError("browser_login_failed", "浏览器登录未完成，请根据失败阶段检查；未自动重新打开窗口。",
                          details=details) from exc

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()
