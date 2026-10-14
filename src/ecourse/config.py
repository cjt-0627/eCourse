import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    secret_dir: Path
    base_url: str = "http://localhost:8765"
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1")
    environment: str = "local"

    def __post_init__(self):
        url = urlsplit(self.base_url)
        if self.environment not in {"local", "production"}:
            raise ValueError("ECOURSE_ENV 必須為 local 或 production。")
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.path not in {"", "/"} or url.query or url.fragment:
            raise ValueError("ECOURSE_BASE_URL 必須為不含路徑、帳密或查詢的 HTTP(S) origin。")
        try:
            url.port
        except ValueError:
            raise ValueError("ECOURSE_BASE_URL 連接埠無效。") from None
        if url.scheme != "https" and (self.environment != "local" or url.hostname not in {"localhost", "127.0.0.1", "::1"}):
            raise ValueError("HTTP 僅允許 localhost 模式；其他部署必須 HTTPS。")
        if not self.allowed_hosts or any(not host or "*" in host or "/" in host or ":" in host for host in self.allowed_hosts):
            raise ValueError("ECOURSE_ALLOWED_HOSTS 必須使用精確主機名稱，不可包含萬用字元或連接埠。")
        if url.hostname not in self.allowed_hosts:
            raise ValueError("ECOURSE_BASE_URL 主機必須在 ECOURSE_ALLOWED_HOSTS 中。")
        object.__setattr__(self, "base_url", self.base_url.rstrip("/"))

    @classmethod
    def from_env(cls) -> "Settings":
        base = Path.home() / ".local" / "share"
        data = Path(os.environ.get("ECOURSE_DATA_DIR", base / "ecourse")).expanduser()
        secrets = Path(os.environ.get("ECOURSE_SECRET_DIR", base / "ecourse-secrets")).expanduser()
        if not data.is_absolute() or not secrets.is_absolute():
            raise ValueError("資料與金鑰目錄必須使用絕對路徑。")
        data, secrets = data.resolve(), secrets.resolve()
        if data == secrets or data in secrets.parents or secrets in data.parents:
            raise ValueError("資料與金鑰必須使用互不包含的獨立目錄。")
        return cls(
            data, secrets,
            base_url=os.environ.get("ECOURSE_BASE_URL", "http://localhost:8765"),
            allowed_hosts=tuple(host.strip() for host in os.environ.get("ECOURSE_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")),
            environment=os.environ.get("ECOURSE_ENV", "local"),
        )
