"""Local settings and operating-system credential storage."""

from __future__ import annotations

import getpass
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import keyring
from platformdirs import user_data_path

from .errors import BBError

KEYRING_SERVICE = "ustc-bb-mcp"
BB_HOSTS = frozenset({"bb.ustc.edu.cn", "www.bb.ustc.edu.cn"})


def private_json(path: Path, data: dict) -> None:
    """Atomically replace a user-local file; restrictive Unix permissions."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".bb-", delete=False
        ) as stream:
            temporary = stream.name
            os.chmod(temporary, 0o600)
            json.dump(data, stream, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        if temporary and Path(temporary).exists():
            Path(temporary).unlink()


@dataclass
class Settings:
    base_url: str = "https://www.bb.ustc.edu.cn"
    data_dir: Path = field(default_factory=lambda: user_data_path("ustc-bb-mcp", appauthor=False))
    output_dir: Path = field(default_factory=lambda: Path.home() / "Downloads" / "USTC-BB")
    timeout: float = 30
    max_pages: int = 500
    cache_ttl: float = 60  # seconds to reuse course lists and content scans; 0 disables
    max_upload_bytes: int = 200 * 1024 * 1024
    username: str = ""
    test_mode: bool = False

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir).expanduser().resolve()
        self.output_dir = Path(self.output_dir).expanduser().resolve()
        try:
            part = urlsplit(self.base_url)
            port = part.port
        except ValueError as exc:
            raise BBError("invalid_config", "BB 地址格式不正确。") from exc
        loopback = part.hostname in {"127.0.0.1", "localhost", "::1"}
        valid = part.scheme == "https" and part.hostname in BB_HOSTS and port in {None, 443}
        if self.test_mode and loopback and part.scheme in {"http", "https"}:
            valid = True
        if not valid or part.username or part.password or part.query or part.fragment:
            raise BBError("invalid_config", "BB 地址必须是中科大 BB 的 HTTPS 地址。")
        if part.path not in {"", "/"}:
            raise BBError("invalid_config", "BB 地址不能包含页面路径。")
        self.base_url = self.base_url.rstrip("/")
        if self.timeout <= 0 or self.max_pages <= 0:
            raise BBError("invalid_config", "超时和扫描页数必须大于零。")
        if self.cache_ttl < 0 or self.max_upload_bytes <= 0:
            raise BBError("invalid_config", "缓存时间不能为负，上传大小上限必须大于零。")

    @property
    def state_path(self) -> Path:
        return self.data_dir / "session.json"

    @property
    def profile_dir(self) -> Path:
        return self.data_dir / "browser-profile"

    @property
    def config_path(self) -> Path:
        return self.data_dir / "config.json"

    @classmethod
    def load(cls) -> Settings:
        data_dir = Path(os.environ.get("BB_DATA_DIR", user_data_path("ustc-bb-mcp", appauthor=False)))
        config: dict = {}
        path = data_dir.expanduser() / "config.json"
        if path.exists():
            try:
                config = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(config, dict):
                    raise ValueError
            except (OSError, ValueError) as exc:
                raise BBError("invalid_config", "本地 config.json 无法读取，请重新运行 configure。") from exc
        try:
            cache_ttl = float(os.environ.get("BB_CACHE_TTL", config.get("cache_ttl", 60)))
        except (TypeError, ValueError) as exc:
            raise BBError("invalid_config", "BB_CACHE_TTL 必须是秒数。") from exc
        return cls(
            base_url=os.environ.get("BB_BASE_URL", config.get("base_url", "https://www.bb.ustc.edu.cn")),
            data_dir=data_dir,
            output_dir=Path(os.environ.get("BB_OUTPUT_DIR", config.get("output_dir", str(Path.home() / "Downloads" / "USTC-BB")))),
            cache_ttl=cache_ttl,
            username=os.environ.get("USTC_CAS_USR", config.get("username", "")).strip(),
        )


def _secure_keyring():
    """Do not silently accept a plaintext third-party keyring backend."""
    try:
        backend = keyring.get_keyring()
        backends = getattr(backend, "backends", [backend])
        allowed = ("Windows", "macOS", "OS_X", "SecretService", "libsecret", "kwallet")
        for candidate in backends:
            module = type(candidate).__module__
            if any(f"keyring.backends.{name}" in module for name in allowed) and candidate.priority > 0:
                return candidate
    except Exception:
        pass
    raise BBError(
        "credential_store_unavailable",
        "系统凭据库不可用。请使用 USTC_CAS_USR/USTC_CAS_PWD 环境变量，或运行 login --browser；不会改存明文密码。",
    )


def credentials(settings: Settings) -> tuple[str, str]:
    username = os.environ.get("USTC_CAS_USR", settings.username).strip()
    password = os.environ.get("USTC_CAS_PWD", "")
    if not password and username:
        # A broken credential store is a different problem from "not configured
        # yet"; say so instead of asking the user to run configure again.
        backend = _secure_keyring()
        try:
            password = backend.get_password(KEYRING_SERVICE, username) or ""
        except Exception as exc:
            raise BBError(
                "credential_store_unavailable",
                "无法从系统凭据库读取密码。请重新运行 configure，或使用 login --browser / USTC_CAS_PWD。",
            ) from exc
    return username, password


def configure(settings: Settings, username: str | None = None) -> dict:
    username = (username or input("统一身份认证账号: ")).strip()
    if not username:
        raise BBError("invalid_config", "账号不能为空。")
    backend = _secure_keyring()
    password = getpass.getpass("统一身份认证密码（隐藏输入）: ")
    if not password:
        raise BBError("invalid_config", "密码不能为空。")
    try:
        backend.set_password(KEYRING_SERVICE, username, password)
    except Exception as exc:
        raise BBError("credential_store_unavailable", "无法写入系统凭据库；未保存明文密码。") from exc
    private_json(settings.config_path, {"username": username})
    return {"configured": True, "username": username, "password_storage": "system_keyring"}
