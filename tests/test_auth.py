import asyncio
import json
import os
from urllib.parse import parse_qs

import httpx
import pytest

from ustc_bb_mcp import auth, config
from ustc_bb_mcp.auth import PORTAL_PATH, Session
from ustc_bb_mcp.config import Settings, configure, credentials
from ustc_bb_mcp.errors import BBError


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", output_dir=tmp_path / "files")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("BB_DATA_DIR", "BB_OUTPUT_DIR", "BB_BASE_URL", "USTC_CAS_USR", "USTC_CAS_PWD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(auth, "_authenticated", lambda text, url="": "data-auth='true'" in text)
    monkeypatch.setattr(auth, "_login_page", lambda text, url="": "data-login='true'" in text)


def client_for(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_settings_environment_and_config(tmp_path, monkeypatch):
    data_dir = tmp_path / "settings"
    data_dir.mkdir()
    (data_dir / "config.json").write_text('{"username":"old"}', encoding="utf-8")
    monkeypatch.setenv("BB_DATA_DIR", str(data_dir))
    monkeypatch.setenv("BB_OUTPUT_DIR", str(tmp_path / "downloads"))
    monkeypatch.setenv("USTC_CAS_USR", " new ")
    actual = Settings.load()
    assert actual.username == "new"
    assert actual.data_dir == data_dir
    assert actual.output_dir == tmp_path / "downloads"
    assert actual.state_path == data_dir / "session.json"


@pytest.mark.parametrize("url", [
    "https://example.org", "http://www.bb.ustc.edu.cn", "https://www.bb.ustc.edu.cn.evil.test",
    "https://user:password@www.bb.ustc.edu.cn", "https://www.bb.ustc.edu.cn:444",
])
def test_settings_reject_foreign_or_insecure_origin(url, tmp_path):
    with pytest.raises(BBError):
        Settings(base_url=url, data_dir=tmp_path)


def test_test_mode_is_loopback_only(tmp_path):
    assert Settings(base_url="http://127.0.0.1:4567", test_mode=True, data_dir=tmp_path)
    with pytest.raises(BBError):
        Settings(base_url="http://example.org", test_mode=True, data_dir=tmp_path)


def test_configure_password_only_in_keyring(settings, monkeypatch):
    saved = {}
    class Backend:
        def set_password(self, service, username, password):
            saved.update(service=service, username=username, password=password)
    monkeypatch.setattr(config, "_secure_keyring", lambda: Backend())
    monkeypatch.setattr(config.getpass, "getpass", lambda prompt: "a-private-password")
    result = configure(settings, "student")
    assert result["configured"]
    assert saved["password"] == "a-private-password"
    assert json.loads(settings.config_path.read_text(encoding="utf-8")) == {"username": "student"}
    assert "a-private-password" not in settings.config_path.read_text()


def test_keyring_unavailable_does_not_write(settings, monkeypatch):
    def fail():
        raise BBError("credential_store_unavailable", "unavailable")
    monkeypatch.setattr(config, "_secure_keyring", fail)
    with pytest.raises(BBError):
        configure(settings, "student")
    assert not settings.config_path.exists()


def test_credentials_environment_overrides_keyring(settings, monkeypatch):
    monkeypatch.setenv("USTC_CAS_USR", "student")
    monkeypatch.setenv("USTC_CAS_PWD", "secret")
    assert credentials(settings) == ("student", "secret")


async def test_existing_session_positive_check_and_safe_cookie_state(settings):
    async def handler(request):
        return httpx.Response(200, text="<main data-auth='true'></main>", headers={"set-cookie": "JSESSIONID=bb-secret; Path=/; Secure; HttpOnly"})
    async with client_for(handler) as client:
        session = Session(settings, client)
        assert (await session.ensure_session())["authenticated"]
        state = await session.storage_state()
        assert state["cookies"][0]["httpOnly"] is True
        assert len(session.identity) == 64
        assert "bb-secret" not in session.identity
        assert settings.state_path.exists()
        if os.name != "nt":
            assert settings.state_path.stat().st_mode & 0o777 == 0o600


async def test_missing_credentials_is_actionable(settings, monkeypatch):
    monkeypatch.setattr(auth, "credentials", lambda settings: ("", ""))
    async with client_for(lambda r: httpx.Response(200, text="<form data-login='true'></form>")) as client:
        with pytest.raises(BBError, match="login --browser") as failure:
            await Session(settings, client).ensure_session()
    assert failure.value.code == "credentials_required"


async def test_only_allowed_bb_requests_receive_cookies(settings):
    calls = []
    async def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://evil.test/download?ticket=secret"})
    async with client_for(handler) as client:
        session = Session(settings, client)
        session._authenticated = True
        with pytest.raises(BBError) as error:
            await session.get("/file")
        assert error.value.code == "unsafe_url"
        assert "secret" not in str(error.value)
        assert len(calls) == 1
        with pytest.raises(BBError):
            await session.get("https://evil.test/file")
        assert len(calls) == 1


async def test_transient_read_retries_once_and_strips_network_error(settings):
    calls = 0
    async def handler(request):
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("password=top-secret&ticket=st-secret", request=request)
    async with client_for(handler) as client:
        session = Session(settings, client)
        session._authenticated = True
        with pytest.raises(BBError) as error:
            await session.get("/file?ticket=st-secret")
    assert calls == 2
    assert "secret" not in str(error.value)


async def test_503_retries_once(settings):
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(503 if calls == 1 else 200, text="content")
    async with client_for(handler) as client:
        session = Session(settings, client)
        session._authenticated = True
        assert (await session.get("/file")).text == "content"
    assert calls == 2


async def test_concurrency_limited_to_three(settings):
    active = maximum = 0
    async def handler(request):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(200, text="content")
    async with client_for(handler) as client:
        session = Session(settings, client)
        session._authenticated = True
        await asyncio.gather(*(session.get(f"/file/{i}") for i in range(10)))
    assert maximum == 3


async def test_expired_session_reauthenticates_once(settings, monkeypatch):
    calls = 0
    auth_calls = []
    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, text="<div data-login='true'></div>" if calls == 1 else "content")
    async with client_for(handler) as client:
        session = Session(settings, client)
        session._authenticated = True
        async def ensure(force=False):
            auth_calls.append(force)
            session._authenticated = True
        monkeypatch.setattr(session, "ensure_session", ensure)
        assert (await session.get("/file")).text == "content"
    assert auth_calls == [True]
    assert calls == 2


async def test_forbidden_content_does_not_reauthenticate_valid_session(settings, monkeypatch):
    def handler(request):
        if request.url.path == auth.urlsplit(PORTAL_PATH).path:
            return httpx.Response(200, text="<main data-auth='true'></main>")
        return httpx.Response(403, text="forbidden")
    async with client_for(handler) as client:
        session = Session(settings, client)
        session._authenticated = True
        async def forbidden_login(*args, **kwargs):
            pytest.fail("valid sessions must not retry credential authentication for a forbidden item")
        monkeypatch.setattr(session, "_cas_login", forbidden_login)
        with pytest.raises(BBError) as error:
            await session.get("/forbidden")
    assert error.value.code == "permission_denied"


async def test_service_outage_is_not_reported_as_missing_credentials(settings, monkeypatch):
    async with client_for(lambda r: httpx.Response(503)) as client:
        session = Session(settings, client)
        with pytest.raises(BBError) as error:
            await session.ensure_session()
    assert error.value.code == "http_error"


async def test_foreign_and_expired_cookies_are_not_persisted(settings):
    async with client_for(lambda r: httpx.Response(200)) as client:
        session = Session(settings, client)
        await session.update_cookies({"cookies": [
            {"name": "JSESSIONID", "value": "good", "domain": "www.bb.ustc.edu.cn", "path": "/", "secure": True, "httpOnly": False, "expires": -1},
            {"name": "CAS", "value": "private-cas", "domain": "id.ustc.edu.cn", "path": "/"},
            {"name": "broad", "value": "bad", "domain": ".ustc.edu.cn", "path": "/"},
            {"name": "old", "value": "bad", "domain": "www.bb.ustc.edu.cn", "path": "/", "expires": 1},
        ]})
        state = await session.storage_state()
    assert [item["name"] for item in state["cookies"]] == ["JSESSIONID"]
    assert state["cookies"][0]["httpOnly"] is False
    assert "private-cas" not in settings.state_path.read_text()


async def test_running_process_reloads_browser_login_from_disk(settings, monkeypatch):
    def handler(request):
        return httpx.Response(200, text="<main data-auth='true'></main>" if "fresh" in request.headers.get("cookie", "") else "<div data-login='true'></div>")
    async with client_for(handler) as client:
        session = Session(settings, client)
        config.private_json(settings.state_path, {"cookies": [{"name": "JSESSIONID", "value": "fresh", "domain": "www.bb.ustc.edu.cn", "path": "/", "secure": True}]})
        monkeypatch.setattr(auth, "credentials", lambda settings: ("", ""))
        assert (await session.ensure_session())["authenticated"]


async def test_valid_running_session_notices_new_browser_account_before_query(settings):
    seen = []
    def handler(request):
        seen.append(request.headers.get("cookie", ""))
        return httpx.Response(200, text="<main data-auth='true'></main>")
    async with client_for(handler) as client:
        session = Session(settings, client)
        client.cookies.set("JSESSIONID", "old-account", domain="www.bb.ustc.edu.cn", path="/")
        session._authenticated = True
        config.private_json(settings.state_path, {"cookies": [{"name": "JSESSIONID", "value": "new-account", "domain": "www.bb.ustc.edu.cn", "path": "/", "secure": True}]})
        await session.get("/course")
    assert seen and all("new-account" in cookie and "old-account" not in cookie for cookie in seen)


async def test_identity_ignores_tracking_cookie_changes(settings):
    async with client_for(lambda r: httpx.Response(200)) as client:
        session = Session(settings, client)
        client.cookies.set("JSESSIONID", "session-one", domain="www.bb.ustc.edu.cn", path="/")
        first = session.identity
        client.cookies.set("analytics", "changed", domain="www.bb.ustc.edu.cn", path="/")
        assert session.identity == first
        client.cookies.set("JSESSIONID", "session-two", domain="www.bb.ustc.edu.cn", path="/")
        assert session.identity != first


async def test_stream_keeps_slot_until_body_is_read(settings):
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"first"
            await asyncio.sleep(0.01)
            yield b"last"
    async with client_for(lambda r: httpx.Response(200, stream=Body())) as client:
        session = Session(settings, client)
        session._authenticated = True
        async with session.stream("/file") as response:
            assert not response.is_closed
            assert b"".join([chunk async for chunk in response.aiter_bytes()]) == b"firstlast"
        assert response.is_closed


async def test_stream_does_not_swallow_caller_error(settings):
    async with client_for(lambda r: httpx.Response(200, content=b"file")) as client:
        session = Session(settings, client)
        session._authenticated = True
        with pytest.raises(BBError, match="caller"):
            async with session.stream("/file"):
                raise BBError("session_expired", "caller")


async def test_stream_reauthenticates_200_login_page(settings, monkeypatch):
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, text="<form data-login='true'></form>", headers={"content-type": "text/html"})
        return httpx.Response(200, content=b"PDF", headers={"content-type": "application/pdf"})
    async with client_for(handler) as client:
        session = Session(settings, client)
        session._authenticated = True
        auth_calls = []
        async def ensure(force=False):
            auth_calls.append(force)
            session._authenticated = True
        monkeypatch.setattr(session, "ensure_session", ensure)
        async with session.stream("/file") as response:
            assert await response.aread() == b"PDF"
        assert calls == 2
        assert auth_calls == [True]


async def test_stream_rejects_html_403(settings):
    async with client_for(lambda r: httpx.Response(403, text="Forbidden", headers={"content-type": "text/html"})) as client:
        session = Session(settings, client)
        session._authenticated = True
        with pytest.raises(BBError) as error:
            async with session.stream("/file"):
                pytest.fail("403 body must not be exposed as a download")
    assert error.value.code == "permission_denied"


@pytest.mark.parametrize("url,top,expected", [
    ("https://id.ustc.edu.cn/cas/login", True, "continue"),
    ("https://foreign.test/", True, "abort"),
    ("https://captcha-cdn.test/widget.js", False, "continue"),
    ("http://captcha-cdn.test/widget.js", False, "abort"),
    ("http://www.bb.ustc.edu.cn/callback", True, "upgrade"),
])
async def test_browser_guard_allows_https_captcha_assets_but_bounds_navigation(url, top, expected):
    from types import SimpleNamespace
    class Route:
        request = SimpleNamespace(url=url, is_navigation_request=lambda: top, frame=SimpleNamespace(parent_frame=None))
        outcome = None
        async def abort(self):
            self.outcome = "abort"
        async def continue_(self, **kwargs):
            assert not kwargs
            self.outcome = "continue"
        async def fulfill(self, *, status, headers, body):
            assert status == 307
            assert headers["Location"] == url.replace("http://", "https://", 1)
            assert body == ""
            self.outcome = "upgrade"
    route = Route()
    await auth._browser_route(route)
    assert route.outcome == expected


async def test_browser_guard_upgrades_legacy_callback_with_real_chromium():
    from playwright.async_api import Error, async_playwright

    original = "http://www.bb.ustc.edu.cn/callback?ticket=fictional"
    upgraded = original.replace("http://", "https://", 1)
    requests, responses, routing_errors = [], [], []

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        # Redirected requests bypass route handlers. Offline mode guarantees
        # that even the HTTPS redirect cannot reach the real school website.
        context = await browser.new_context(offline=True)
        try:
            async def handle(route):
                try:
                    await auth._browser_route(route)
                except Error as error:
                    routing_errors.append(str(error))

            await context.route("**/*", handle)
            page = await context.new_page()
            page.on("request", lambda request: requests.append(request.url))
            page.on("response", lambda response: responses.append(response))
            with pytest.raises(Error):
                await page.goto(original, timeout=3000)

            assert routing_errors == []
            assert requests == [original, upgraded]
            assert len(responses) == 1
            assert responses[0].status == 307
            assert (await responses[0].all_headers())["location"] == upgraded
        finally:
            await context.close()
            await browser.close()


@pytest.mark.parametrize("failing_check", ["count", "visibility"])
async def test_login_entry_navigation_race_keeps_window_available(failing_check):
    from types import SimpleNamespace

    from playwright.async_api import Error

    class Link:
        @property
        def first(self):
            return self

        async def count(self):
            if failing_check == "count":
                raise Error("Document was replaced during navigation")
            return 1

        async def is_visible(self):
            raise Error("Document was replaced during navigation")

        async def click(self, **kwargs):
            pytest.fail("A replaced document must not be clicked")

    page = SimpleNamespace(url="https://www.bb.ustc.edu.cn/", get_by_role=lambda *a, **k: Link())
    await auth._advance_login_entry(page)


async def test_browser_login_survives_handoff_and_probe_failure_without_reopening(settings, monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    import playwright.async_api
    from playwright.async_api import Error

    events = []
    replacement = SimpleNamespace(url="https://www.bb.ustc.edu.cn/?step=0")

    class InitialPage:
        async def goto(self, *args, **kwargs):
            context.pages = [replacement]
            raise Error("Initial page was closed after opening the authentication page")

    class Context:
        pages = [InitialPage()]

        async def route(self, *args):
            pass

        async def storage_state(self):
            pytest.fail("Polling storage_state would create transient SSO tabs")

        async def cookies(self):
            events.append("cookies")
            replacement.url = f"https://www.bb.ustc.edu.cn/?step={len(events)}"
            return []

        async def close(self):
            events.append("close")

    context = Context()

    async def launch(*args, **kwargs):
        events.append("launch")
        return context

    @asynccontextmanager
    async def manager():
        yield SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))

    async def advance(page):
        events.append("advance")

    async def sleep(seconds):
        pass

    monkeypatch.setattr(playwright.async_api, "async_playwright", manager)
    monkeypatch.setattr(auth, "_advance_login_entry", advance)
    monkeypatch.setattr(auth.asyncio, "sleep", sleep)
    async with client_for(lambda r: httpx.Response(200)) as client:
        session = Session(settings, client)
        probes = iter([BBError("network_error", "Temporary outage"), False, True])

        async def probe():
            events.append("probe")
            value = next(probes)
            if isinstance(value, Exception):
                raise value
            return value

        async def save():
            assert "close" not in events
            events.append("save")

        monkeypatch.setattr(session, "_probe", probe)
        monkeypatch.setattr(session, "_save_state", save)
        assert (await session.login_browser())["authenticated"]

    assert events == ["launch", "advance", "cookies", "probe", "cookies", "probe", "cookies", "probe", "save", "close"]


async def test_browser_login_failure_reports_safe_stage(settings, monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    import playwright.async_api

    async def launch(*args, **kwargs):
        raise RuntimeError("net::ERR_CONNECTION_RESET https://school.test/?ticket=secret password=private")

    @asynccontextmanager
    async def manager():
        yield SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))

    monkeypatch.setattr(playwright.async_api, "async_playwright", manager)
    async with client_for(lambda r: httpx.Response(200)) as client:
        with pytest.raises(BBError) as failure:
            await Session(settings, client).login_browser()
    assert failure.value.as_dict() == {
        "code": "browser_login_failed",
        "message": "浏览器登录未完成，请根据失败阶段检查；未自动重新打开窗口。",
        "stage": "browser_launch",
        "exception_type": "RuntimeError",
        "network_error": "net::ERR_CONNECTION_RESET",
    }


async def test_two_stage_cas_and_legacy_http_service(settings, monkeypatch):
    import pyustc
    gateway_service = "http://www.bb.ustc.edu.cn/nginx_auth/?next=https%3A%2F%2Fwww.bb.ustc.edu.cn%2F"
    bb_service = "https://www.bb.ustc.edu.cn/webapps/login/?action=cas"
    phase = 0
    services = []
    seen_urls = []
    def handler(request):
        nonlocal phase
        seen_urls.append(str(request.url))
        query = parse_qs(request.url.query.decode())
        if "ticket" in query:
            phase += 1
            return httpx.Response(200, text="callback", headers={"set-cookie": f"JSESSIONID=phase{phase}; Path=/; Secure; HttpOnly"})
        if request.url.path == "/nginx_auth/":
            return httpx.Response(200, text=f'<a href="https://id.ustc.edu.cn/cas/login?{auth.urlencode({"service": gateway_service})}">登录</a>')
        if request.url.path == "/":
            return httpx.Response(302, headers={"Location": "/nginx_auth/"})
        if request.url.path == auth.urlsplit(PORTAL_PATH).path:
            if phase == 2:
                return httpx.Response(200, text="<main data-auth='true'></main>")
            return httpx.Response(302, headers={"Location": "https://id.ustc.edu.cn/cas/login?" + auth.urlencode({"service": bb_service if phase else gateway_service})})
        return httpx.Response(200, text="login")
    class CAS:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def get_ticket(self, service):
            services.append(service)
            return "ST-private-ticket"
    monkeypatch.setattr(pyustc.CASClient, "login_by_pwd", lambda usr, pwd: CAS())
    monkeypatch.setattr(auth, "credentials", lambda settings: ("student", "password"))
    async with client_for(handler) as client:
        result = await Session(settings, client).ensure_session()
    assert result["authenticated"]
    assert services == [gateway_service, bb_service]
    assert all(url.startswith("https://www.bb.ustc.edu.cn") for url in seen_urls)
    assert "ticket" not in settings.state_path.read_text()


async def test_cas_failure_does_not_expose_exception(settings, monkeypatch):
    import pyustc
    class BadCAS:
        async def __aenter__(self):
            raise RuntimeError("password=very-private ticket=ST-secret")
        async def __aexit__(self, *args):
            pass
    monkeypatch.setattr(pyustc.CASClient, "login_by_pwd", lambda usr, pwd: BadCAS())
    async with client_for(lambda r: httpx.Response(200)) as client:
        session = Session(settings, client)
        async def discover(start=None):
            return "https://www.bb.ustc.edu.cn/callback"
        monkeypatch.setattr(session, "discover_cas_service", discover)
        with pytest.raises(BBError) as error:
            await session._cas_login("student", "password")
    assert error.value.code == "interaction_required"
    assert "private" not in str(error.value)
