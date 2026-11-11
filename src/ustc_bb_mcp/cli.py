"""Local setup commands. Only `serve` uses stdout for the MCP protocol."""

import argparse
import asyncio
import importlib.metadata
import json
import logging
import os
import sys
from pathlib import Path

from . import __version__
from .auth import Session
from .config import Settings, _secure_keyring, configure
from .errors import BBError


def doctor(settings: Settings) -> dict:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        chromium = Path(playwright.chromium.executable_path)
    try:
        credential_store = type(_secure_keyring()).__name__
    except BBError:
        credential_store = None
    return {"version": __version__, "python": sys.version.split()[0],
            "mcp": importlib.metadata.version("mcp"), "pyustc": importlib.metadata.version("pyustc"),
            "chromium_installed": chromium.is_file(), "data_dir": str(settings.data_dir),
            "output_dir": str(settings.output_dir), "session_file_present": settings.state_path.is_file(),
            "account_configured": bool(settings.username),
            # None means configure cannot store a password; use login --browser or env vars.
            "credential_store": credential_store, "cache_ttl_seconds": settings.cache_ttl,
            "network_checked": False}


def _log_level() -> int:
    """BB_LOG_LEVEL=DEBUG prints request paths (never URLs with tickets) to stderr."""
    name = os.environ.get("BB_LOG_LEVEL", "WARNING").strip().upper()
    return logging.getLevelNamesMapping().get(name, logging.WARNING)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="ustc-bb-mcp", description="中科大 Blackboard 本地 MCP 服务")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="启动 stdio MCP 服务")
    config_parser = commands.add_parser("configure", help="将密码存入系统凭据库")
    config_parser.add_argument("--username")
    login_parser = commands.add_parser("login", help="登录 BB 并保存会话")
    login_parser.add_argument("--browser", action="store_true", help="在独立浏览器中人工认证")
    commands.add_parser("doctor", help="不联网检查安装和配置")
    args = parser.parse_args()
    logging.basicConfig(stream=sys.stderr, level=_log_level(), format="%(levelname)s %(name)s: %(message)s")
    # Never expose HTTP URLs, CAS tickets or request headers through dependency
    # logs, regardless of BB_LOG_LEVEL; only this package's redacted logs are shown.
    for name in ("httpx", "httpcore", "pyustc", "playwright"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        settings = Settings.load()
        if args.command == "serve":
            from .server import create_server
            create_server(settings).run(transport="stdio")
            return 0
        if args.command == "doctor":
            result = doctor(settings)
        elif args.command == "configure":
            result = configure(settings, args.username)
        else:
            async def login():
                session = Session(settings)
                try:
                    return await (session.login_browser() if args.browser else session.ensure_session())
                finally:
                    await session.close()
            result = asyncio.run(login())
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except BBError as error:
        print(json.dumps(error.as_dict(), ensure_ascii=False), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("操作已取消。", file=sys.stderr)
        return 130
    except Exception:
        print("操作未完成，请检查安装、配置和文件权限；未输出敏感诊断信息。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
