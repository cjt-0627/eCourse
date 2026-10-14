import argparse
import asyncio
from dataclasses import replace
import getpass
import fcntl
import importlib.metadata
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request

from ecourse.backup import BackupError, create_backup, restore_backup
from ecourse.config import Settings
from ecourse.vault import Vault, VaultError, initialize_key, private_directory


def doctor(browser: bool = False) -> int:
    report = {
        "python": platform.python_version(),
        "system": platform.system(),
        "architecture": platform.machine(),
        "packages": {name: importlib.metadata.version(name) for name in ("cryptography", "playwright")},
        "browser": "未測試（加上 --browser 可測試 Chromium 啟動與 context 還原）",
        "school_login": "請在網站連接設定完成登入；本指令不驗證校方登入",
    }
    okay = sys.version_info[:2] == (3, 12)
    if browser:
        from playwright.sync_api import Error, sync_playwright

        try:
            with sync_playwright() as playwright:
                with playwright.chromium.launch(headless=True, chromium_sandbox=True) as chromium:
                    context = chromium.new_context()
                    # Reserved test domain; this probe makes no remote requests.
                    context.add_cookies([{"name": "probe", "value": "synthetic", "domain": "example.test", "path": "/", "secure": True}])
                    with tempfile.TemporaryDirectory(prefix="ecourse-browser-") as root:
                        data, secrets = Path(root) / "data", Path(root) / "secrets"
                        initialize_key(secrets, data)
                        Vault(secrets).save(data / "session.enc", context.storage_state(), platform="elearn", account_id="probe")
                        context.close()
                        restored = chromium.new_context(storage_state=Vault(secrets).load(data / "session.enc", platform="elearn", account_id="probe"))
                        if restored.cookies()[0]["value"] != "synthetic":
                            raise VaultError("瀏覽器測試狀態還原失敗。")
                        restored.close()
                    report["browser"] = "通過：Chromium 啟動、測試 Cookie 加密保存及 context 重建（非校方登入）"
                    report["chromium"] = chromium.version
        except (Error, VaultError, OSError):
            report["browser"] = "失敗：請確認 Chromium 已安裝及主機允許啟動瀏覽器；校方登入仍未驗證。"
            okay = False
    report["status"] = "ok" if okay else "failed"
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if okay else 1


def self_test() -> int:
    with tempfile.TemporaryDirectory(prefix="ecourse-vault-") as root:
        data, secrets = Path(root) / "data", Path(root) / "secrets"
        initialize_key(secrets, data)
        state = {"cookies": [{"name": "probe", "value": "synthetic-secret"}], "origins": []}
        saved = data / "session.enc"
        Vault(secrets).save(saved, state, platform="elearn", account_id="probe")
        if b"synthetic-secret" in saved.read_bytes():
            raise VaultError("測試失敗：檔案含明文。")
        if Vault(secrets).load(saved, platform="elearn", account_id="probe") != state:
            raise VaultError("測試失敗：登入狀態未正確還原。")
        try:
            Vault(secrets).load(saved, platform="eeclass", account_id="probe")
        except VaultError:
            pass
        else:
            raise VaultError("測試失敗：平台隔離無效。")
    print("通過：測試登入狀態加密、重新讀取與平台隔離。臨時資料已清除；未連線校方。")
    return 0


def initialize(settings: Settings) -> int:
    """Serialize first-run key/database creation without holding the runtime lock."""
    private_directory(settings.secret_dir)
    lock_path = settings.secret_dir / "init.lock"
    with lock_path.open("a+") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _initialize_unlocked(settings)


def _initialize_unlocked(settings: Settings) -> int:
    from ecourse.auth.service import initialize_internal_owner, initialize_owner
    from ecourse.db import Database
    from ecourse.db.models import Owner

    db = Database(settings)
    if db.ready():
        Vault(settings.secret_dir)
        db.initialize()
        with db.session() as session:
            if session.query(Owner).first():
                print("eCourse 已初始化。可使用 ecourse dev 啟動。")
                return 0
    key = settings.secret_dir / "master-v1.key"
    if settings.environment == "local":
        if key.exists():
            Vault(settings.secret_dir)
            private_directory(settings.data_dir)
        else:
            initialize_key(settings.secret_dir, settings.data_dir)
        db.initialize()
        initialize_internal_owner(settings)
        print("eCourse 已完成本機初始化。請在網站使用校務帳密登入。")
        return 0
    print("建立本機 eCourse 帳號（不是校務帳號）。密碼不會顯示或保存為明文。")
    login_name = input("登入名稱：").strip()
    if not login_name or len(login_name) > 120:
        raise ValueError("登入名稱不可空白，且不可超過 120 個字元。")
    password = prompt_password()
    if key.exists():
        Vault(settings.secret_dir)
        private_directory(settings.data_dir)
    else:
        initialize_key(settings.secret_dir, settings.data_dir)
    db.initialize()
    initialize_owner(settings, login_name, password)
    print("eCourse 帳號與資料庫已建立。校務帳密請在網站的連接設定輸入。")
    print(f"下一步：ecourse dev，然後開啟 {settings.base_url}")
    return 0


def prompt_password() -> str:
    if not sys.stdin.isatty():
        raise ValueError("請在互動式終端輸入密碼，不接受管線或命令列明文密碼。")
    password = getpass.getpass("密碼（至少 12 字元）：")
    if len(password) < 12:
        raise ValueError("密碼至少需要 12 個字元。")
    if password != getpass.getpass("再次輸入密碼："):
        raise ValueError("兩次輸入的密碼不一致。")
    return password


def require_initialized(settings: Settings) -> None:
    from ecourse.db import Database
    from ecourse.db.models import Owner

    db = Database(settings)
    if not db.ready():
        raise ValueError("請先執行 ecourse init 建立本機帳號與資料庫。")
    with db.session() as session:
        if not session.query(Owner).first():
            raise ValueError("請先執行 ecourse init 建立本機帳號。")
    Vault(settings.secret_dir)


def run_server(settings: Settings, host: str, port: int) -> int:
    import uvicorn
    from ecourse.web import create_app

    print(f"eCourse 網站：{settings.base_url}", flush=True)
    uvicorn.run(create_app(settings), host=host, port=port, access_log=False, proxy_headers=False)
    return 0


def open_browser(url: str, browser: str = "brave") -> None:
    if browser == "none":
        return
    try:
        if platform.system() == "Darwin":
            arguments = ["open", "-a", "Brave Browser", url] if browser == "brave" else ["open", url]
            result = subprocess.run(arguments, capture_output=True, timeout=10)
            if result.returncode != 0:
                print(f"無法開啟瀏覽器。請手動開啟 {url}", file=sys.stderr)
        else:
            import webbrowser
            webbrowser.open(url)
    except (OSError, subprocess.TimeoutExpired):
        print(f"無法開啟瀏覽器。請手動開啟 {url}", file=sys.stderr)


def check_port(host: str, port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError as exc:
            raise ValueError(f"連接埠 {port} 已被使用。請先關閉舊的 eCourse 視窗或其他佔用程式，再重新啟動。") from exc


def local_ready(host: str, port: int, host_header: str) -> bool:
    """Probe the bound listener directly without contacting the public base URL."""
    from urllib.request import build_opener, ProxyHandler
    connect_host = "127.0.0.1" if host in {"0.0.0.0", "::", "[::]"} else host
    if ":" in connect_host and not connect_host.startswith("["):
        connect_host = f"[{connect_host}]"
    request = Request(f"http://{connect_host}:{port}/health/ready", headers={"Host": host_header})
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=.5) as response:
            return response.status == 200 and json.loads(response.read(1024)).get("status") == "ok"
    except Exception:
        return False


def run_dev(settings: Settings, host: str, port: int, browser: str = "none", no_retry: bool = False, manual_sync_only: bool = False) -> int:
    require_initialized(settings)
    private_directory(settings.data_dir)
    lock_path = settings.data_dir / "launcher.lock"
    record_path = settings.data_dir / "launcher.json"
    public_host = urlsplit(settings.base_url).hostname or "localhost"
    with lock_path.open("a+") as lock:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                record = json.loads(record_path.read_text())
            except (OSError, ValueError):
                raise ValueError("eCourse 正在啟動，請稍後再試。")
            if (
                record.get("base_url") != settings.base_url
                or record.get("listener") != {"host": host, "port": port}
                or record.get("worker_mode", {"no_retry": False, "manual_sync_only": False}) != {"no_retry": no_retry, "manual_sync_only": manual_sync_only}
                or not local_ready(host, port, public_host)
            ):
                raise ValueError("eCourse 已在執行，但網站尚未就緒。請查看原啟動視窗。")
            print(f"eCourse 已在執行：{settings.base_url}", flush=True)
            open_browser(settings.base_url, browser)
            return 0
        try:
            check_port(host, port)
            record_path.write_text(json.dumps({
                "base_url": settings.base_url,
                "listener": {"host": host, "port": port},
                "worker_mode": {"no_retry": no_retry, "manual_sync_only": manual_sync_only},
                "pid": os.getpid(),
            }))
            os.chmod(record_path, 0o600)
            return _run_dev_processes(settings, host, port, browser, no_retry, manual_sync_only)
        finally:
            record_path.unlink(missing_ok=True)


def _run_dev_processes(settings: Settings, host: str, port: int, browser: str = "none", no_retry: bool = False, manual_sync_only: bool = False) -> int:
    require_initialized(settings)
    environment = os.environ.copy()
    environment.update(
        ECOURSE_DATA_DIR=str(settings.data_dir), ECOURSE_SECRET_DIR=str(settings.secret_dir),
        ECOURSE_BASE_URL=settings.base_url, ECOURSE_ALLOWED_HOSTS=",".join(settings.allowed_hosts),
        ECOURSE_ENV=settings.environment,
    )
    children = []
    stopping = False
    browser_opened = browser == "none"
    started = time.monotonic()
    public_host = urlsplit(settings.base_url).hostname or "localhost"

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        worker_arguments = ["worker"]
        if no_retry: worker_arguments.append("--no-retry")
        if manual_sync_only: worker_arguments.append("--manual-sync-only")
        for arguments in (worker_arguments, ["serve", "--host", host, "--port", str(port)]):
            children.append(subprocess.Popen([sys.executable, "-m", "ecourse.cli", *arguments], env=environment, start_new_session=True))
        print(f"eCourse 已啟動網站與獨立背景程序：{settings.base_url}\n按 Ctrl+C 同時停止。", flush=True)
        while not stopping:
            if any(child.poll() is not None for child in children):
                print("網站或背景程序已停止，正在關閉其餘程序。", file=sys.stderr)
                return 1
            if not browser_opened:
                if local_ready(host, port, public_host):
                    open_browser(settings.base_url, browser)
                    browser_opened = True
                elif time.monotonic() - started > 30:
                    print(f"網站尚未就緒。請稍後手動開啟 {settings.base_url}", file=sys.stderr)
                    browser_opened = True
            time.sleep(0.25)
        return 0
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                # Each child is a session/process-group leader. Kill the complete
                # group on timeout so Playwright browser helpers cannot survive.
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()
            else:
                # A child may have exited while one of its descendants remains.
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def server_settings(settings: Settings, host: str, port: int | None) -> tuple[Settings, int]:
    url = urlsplit(settings.base_url)
    port = port or url.port or (443 if url.scheme == "https" else 80)
    if not 1 <= port <= 65535:
        raise ValueError("連接埠必須介於 1 至 65535。")
    if settings.environment == "local" and host not in {"127.0.0.1", "localhost"}:
        raise ValueError("本機模式僅允許綁定 127.0.0.1 或 localhost。")
    if settings.environment == "local" and url.scheme == "http":
        settings = replace(settings, base_url=f"http://{url.hostname}:{port}")
    return settings, port


def tailscale_mobile_settings(settings: Settings, *, executable: str | None = None) -> Settings:
    """Resolve a private HTTPS origin from the signed-in local Tailscale node."""
    executable = executable or shutil.which("tailscale")
    if not executable:
        raise ValueError("找不到 Tailscale。請先安裝並在 Mac 登入 Tailscale。")
    result = subprocess.run([executable, "status", "--json"], capture_output=True, text=True, timeout=10)
    try:
        status = json.loads(result.stdout) if result.returncode == 0 else {}
    except json.JSONDecodeError:
        status = {}
    if status.get("BackendState") != "Running" or not status.get("Self", {}).get("Online"):
        raise ValueError("Tailscale 尚未連線。請先在 Mac 開啟 Tailscale 並登入，再重新執行 mobile.command。")
    hostname = str(status.get("Self", {}).get("DNSName") or "").rstrip(".").lower()
    suffix = str(status.get("MagicDNSSuffix") or status.get("CurrentTailnet", {}).get("MagicDNSSuffix") or "").rstrip(".").lower()
    valid_name = re.fullmatch(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]+", hostname)
    if not valid_name or not suffix or (hostname != suffix and not hostname.endswith(f".{suffix}")):
        raise ValueError("Tailscale 尚未提供可用的私人 HTTPS 主機名稱；請確認 MagicDNS 與 HTTPS 已啟用。")
    return Settings(
        settings.data_dir, settings.secret_dir,
        base_url=f"https://{hostname}", allowed_hosts=(hostname,), environment="production",
    )


def configure_tailscale_serve(port: int, *, executable: str | None = None) -> None:
    executable = executable or shutil.which("tailscale")
    if not executable:
        raise ValueError("找不到 Tailscale。")
    result = subprocess.run(
        [executable, "serve", "--bg", f"http://127.0.0.1:{port}"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ValueError(f"無法啟用 Tailscale Serve：{detail or '請確認 tailnet 已允許 HTTPS Serve。'}")


def main() -> int:
    parser = argparse.ArgumentParser(description="eCourse：本機課務整合網站")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("doctor", help="檢查 Python 與套件；不連線校方")
    check.add_argument("--browser", action="store_true", help="實際啟動 Chromium 並測試加密狀態還原")
    commands.add_parser("self-test", help="用臨時測試資料驗證加密，不建立正式設定")
    commands.add_parser("init-vault", help="僅初始化 P0 主金鑰；尚不建立網站擁有者或推播金鑰")
    commands.add_parser("init", help="安全建立本機帳號、主金鑰與資料庫")
    commands.add_parser("reset-password", help="重設 production 管理員 bootstrap 密碼")
    commands.add_parser("migrate", help="套用資料庫 migration；不改擁有者或金鑰")
    backup = commands.add_parser("backup", help="備份資料庫；主金鑰需另行保管")
    backup.add_argument("destination", type=Path)
    restore = commands.add_parser("restore", help="使用原主金鑰還原至空資料目錄")
    restore.add_argument("archive", type=Path)
    worker = commands.add_parser("worker", help="執行獨立背景服務")
    worker.add_argument("--no-retry", action="store_true", help="停用自動重試；每次連接僅一次登入嘗試")
    worker.add_argument("--manual-sync-only", action="store_true", help="停用週期同步，只執行手動排入的工作")
    for name, help_text in (("serve", "啟動網站 API"), ("dev", "同時啟動網站與獨立背景服務"), ("start", "建立本機帳號、啟動服務並開啟 Brave"), ("mobile", "透過 Tailscale Serve 以私人 HTTPS 提供手機存取")):
        server = commands.add_parser(name, help=help_text)
        server.add_argument("--host", default="127.0.0.1")
        server.add_argument("--port", type=int)
        if name != "serve":
            server.add_argument("--no-retry", action="store_true")
            server.add_argument("--manual-sync-only", action="store_true")
        if name in {"start", "mobile"}:
            server.add_argument("--browser", choices=["brave", "default", "none"], default="brave")
    args = parser.parse_args()
    try:
        if args.command == "doctor":
            return doctor(args.browser)
        if args.command == "self-test":
            return self_test()
        settings = Settings.from_env()
        if args.command == "backup":
            require_initialized(settings)
            info = create_backup(settings, args.destination)
            print(f"備份完成：{info.path}\n不含主金鑰；請另行保存原 .secrets/master-v1.key。")
            return 0
        if args.command == "restore":
            info = restore_backup(settings, args.archive)
            print(f"還原完成：{info.database_path}\n網站登入已撤銷，平台同步已暫停；啟動後請重新連接。")
            return 0
        if args.command == "init":
            return initialize(settings)
        if args.command == "mobile":
            settings = tailscale_mobile_settings(settings)
            port = args.port or 8765
            if args.host != "127.0.0.1":
                raise ValueError("手機模式的 eCourse 後端必須只監聽 127.0.0.1。")
            initialize(settings)
            configure_tailscale_serve(port)
            print(f"手機私人網址：{settings.base_url}\n請在已加入同一個 tailnet 的手機開啟此網址。", flush=True)
            return run_dev(settings, "127.0.0.1", port, browser=args.browser, no_retry=args.no_retry, manual_sync_only=args.manual_sync_only)
        if args.command in {"serve", "dev", "start"}:
            settings, port = server_settings(settings, args.host, args.port)
            if args.command == "start":
                initialize(settings)
                return run_dev(settings, args.host, port, browser=args.browser, no_retry=args.no_retry, manual_sync_only=args.manual_sync_only)
            return run_dev(settings, args.host, port, no_retry=args.no_retry, manual_sync_only=args.manual_sync_only) if args.command == "dev" else run_server(settings, args.host, port)
        if args.command == "worker":
            from ecourse.worker import run_worker
            require_initialized(settings)
            asyncio.run(run_worker(settings, retry_delays=() if args.no_retry else (1, 5, 15), periodic_sync=not args.manual_sync_only))
            return 0
        if args.command == "migrate":
            from ecourse.db import Database
            Vault(settings.secret_dir)
            private_directory(settings.data_dir)
            Database(settings).initialize()
            print("資料庫 migration 已完成。")
            return 0
        if args.command == "reset-password":
            from ecourse.auth.service import reset_owner_password
            if settings.environment == "local":
                raise ValueError("本機模式直接使用校務帳密登入，沒有獨立密碼可重設。")
            require_initialized(settings)
            reset_owner_password(settings, prompt_password())
            print("密碼已重設，所有裝置的網站登入均已撤銷。")
            return 0
        initialize_key(settings.secret_dir, settings.data_dir)
        print("P0 主金鑰已建立。尚未建立網站帳號、校方連線或推播設定。")
        return 0
    except (KeyboardInterrupt, EOFError):
        print("\n操作已取消。", file=sys.stderr)
        return 130
    except (BackupError, VaultError, ValueError, OSError) as error:
        message = str(error) if isinstance(error, (BackupError, VaultError, ValueError)) else "檔案操作失敗，請檢查目錄權限。"
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
