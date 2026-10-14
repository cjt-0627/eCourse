from ecourse import cli
from ecourse.auth.service import SCHOOL_AUTH_DISABLED_PASSWORD, create_session, authenticate_session, verify_owner
from ecourse.config import Settings
from ecourse.db import Database
from ecourse.db.models import Owner


def test_init_can_restart_without_replacing_credentials_or_key(tmp_path, monkeypatch, capsys):
    settings = Settings(tmp_path / "data", tmp_path / "secrets")
    monkeypatch.setattr("builtins.input", lambda _: (_ for _ in ()).throw(AssertionError("local init must not prompt")))
    assert cli.initialize(settings) == 0
    db = Database(settings)
    with db.session() as session:
        owner = session.query(Owner).one()
        owner_id = owner.id
        assert owner.login_name == "school-owner"
        assert owner.password_hash == SCHOOL_AUTH_DISABLED_PASSWORD
    assert verify_owner(db, "school-owner", "anything") is None
    original_key = (settings.secret_dir / "master-v1.key").read_bytes()
    raw, _ = create_session(db, owner_id)
    monkeypatch.setattr("builtins.input", lambda _: (_ for _ in ()).throw(AssertionError("must not prompt again")))
    assert cli.initialize(settings) == 0
    assert (settings.secret_dir / "master-v1.key").read_bytes() == original_key
    assert authenticate_session(db, raw) is not None
    assert "校務帳密登入" in capsys.readouterr().out


def test_concurrent_first_start_initializes_without_prompt(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    settings = Settings(tmp_path / "data", tmp_path / "secrets")
    barrier = Barrier(2)
    prompts = 0

    def login_name(_prompt):
        nonlocal prompts
        prompts += 1
        return "local-owner"

    monkeypatch.setattr("builtins.input", login_name)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: "synthetic-password-123")

    def start():
        barrier.wait()
        return cli.initialize(settings)

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert [future.result() for future in (pool.submit(start), pool.submit(start))] == [0, 0]
    assert prompts == 0


def test_production_init_keeps_explicit_admin_bootstrap(tmp_path, monkeypatch):
    settings = Settings(
        tmp_path / "data", tmp_path / "secrets",
        base_url="https://course.example", allowed_hosts=("course.example",), environment="production",
    )
    monkeypatch.setattr("builtins.input", lambda _: "admin")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: "synthetic-password-123")
    assert cli.initialize(settings) == 0
    assert verify_owner(Database(settings), "admin", "synthetic-password-123") is not None


def test_password_prompt_refuses_pipe(monkeypatch):
    import pytest
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    with pytest.raises(ValueError, match="互動式終端"):
        cli.prompt_password()


def test_launcher_rejects_occupied_port_before_starting_worker(tmp_path, monkeypatch):
    import pytest
    settings = Settings(tmp_path / "data", tmp_path / "secrets")
    monkeypatch.setattr(cli, "require_initialized", lambda _: None)
    monkeypatch.setattr(cli, "check_port", lambda *_: (_ for _ in ()).throw(ValueError("連接埠已被使用")))
    monkeypatch.setattr(cli, "_run_dev_processes", lambda *_: (_ for _ in ()).throw(AssertionError("must not start a worker")))
    with pytest.raises(ValueError, match="連接埠"):
        cli.run_dev(settings, "127.0.0.1", 8765)
    assert not (settings.data_dir / "launcher.json").exists()


def test_repeated_launcher_reuses_only_same_ready_instance(tmp_path, monkeypatch):
    import fcntl
    import json
    import pytest
    settings = Settings(tmp_path / "data", tmp_path / "secrets")
    settings.data_dir.mkdir(mode=0o700)
    monkeypatch.setattr(cli, "require_initialized", lambda _: None)
    calls = []
    monkeypatch.setattr(cli, "open_browser", lambda *args: calls.append(args))
    monkeypatch.setattr(cli, "local_ready", lambda *_: True)
    monkeypatch.setattr(cli, "_run_dev_processes", lambda *_: (_ for _ in ()).throw(AssertionError("no second worker")))
    with (settings.data_dir / "launcher.lock").open("a+") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        record = settings.data_dir / "launcher.json"
        record.write_text(json.dumps({
            "base_url": settings.base_url,
            "listener": {"host": "127.0.0.1", "port": 8765},
        }))
        assert cli.run_dev(settings, "127.0.0.1", 8765, "brave") == 0
        assert calls == [(settings.base_url, "brave")]
        record.write_text(json.dumps({"base_url": "http://localhost:9999"}))
        with pytest.raises(ValueError, match="已在執行"):
            cli.run_dev(settings, "127.0.0.1", 8765, "brave")
        record.write_text(json.dumps({
            "base_url": settings.base_url,
            "listener": {"host": "127.0.0.1", "port": 9999},
        }))
        with pytest.raises(ValueError, match="已在執行"):
            cli.run_dev(settings, "127.0.0.1", 8765, "brave")
        assert len(calls) == 1


def test_launcher_releases_record_and_lock_after_failure(tmp_path, monkeypatch):
    import pytest
    settings = Settings(tmp_path / "data", tmp_path / "secrets")
    monkeypatch.setattr(cli, "require_initialized", lambda _: None)
    monkeypatch.setattr(cli, "check_port", lambda *_: None)
    monkeypatch.setattr(cli, "_run_dev_processes", lambda *_: (_ for _ in ()).throw(RuntimeError("launch failed")))
    with pytest.raises(RuntimeError):
        cli.run_dev(settings, "127.0.0.1", 8765)
    assert not (settings.data_dir / "launcher.json").exists()
    monkeypatch.setattr(cli, "_run_dev_processes", lambda *_: 0)
    assert cli.run_dev(settings, "127.0.0.1", 8765) == 0


def test_brave_launch_uses_argument_list_and_failure_is_nonfatal(monkeypatch, capsys):
    from types import SimpleNamespace
    calls = []
    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(cli.subprocess, "run", lambda arguments, **kwargs: calls.append(arguments) or SimpleNamespace(returncode=1))
    cli.open_browser("http://localhost:8765", "brave")
    assert calls == [["open", "-a", "Brave Browser", "http://localhost:8765"]]
    assert "手動開啟" in capsys.readouterr().err


def test_ready_probe_uses_listener_and_allowed_host_header(monkeypatch):
    import urllib.request

    requests = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, _limit):
            return b'{"status":"ok"}'

    class Opener:
        def open(self, request, timeout):
            requests.append((request, timeout))
            return Response()

    monkeypatch.setattr(urllib.request, "build_opener", lambda *_: Opener())
    assert cli.local_ready("0.0.0.0", 9443, "course.example") is True
    request, timeout = requests[0]
    assert request.full_url == "http://127.0.0.1:9443/health/ready"
    assert request.get_header("Host") == "course.example"
    assert timeout == .5


def test_dev_cleanup_signals_process_groups_after_children_exit(tmp_path, monkeypatch):
    from types import SimpleNamespace

    settings = Settings(tmp_path / "data", tmp_path / "secrets")
    monkeypatch.setattr(cli, "require_initialized", lambda _: None)
    processes = [SimpleNamespace(pid=4101, poll=lambda: 1, wait=lambda **_: 1),
                 SimpleNamespace(pid=4102, poll=lambda: 1, wait=lambda **_: 1)]
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *_args, **_kwargs: processes.pop(0))
    signals = []
    monkeypatch.setattr(cli.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    assert cli._run_dev_processes(settings, "127.0.0.1", 8765) == 1
    assert signals == [(4101, cli.signal.SIGTERM), (4102, cli.signal.SIGTERM)]


def test_restricted_validation_flags_reach_worker_child(tmp_path, monkeypatch):
    from types import SimpleNamespace
    settings = Settings(tmp_path / "data", tmp_path / "secrets")
    monkeypatch.setattr(cli, "require_initialized", lambda _: None)
    calls = []
    def spawn(arguments, **kwargs):
        calls.append(arguments)
        return SimpleNamespace(pid=4100+len(calls), poll=lambda: 1, wait=lambda **_: 1)
    monkeypatch.setattr(cli.subprocess, "Popen", spawn)
    monkeypatch.setattr(cli.os, "killpg", lambda *_: None)
    assert cli._run_dev_processes(settings, "127.0.0.1", 8765, no_retry=True, manual_sync_only=True) == 1
    assert calls[0][-3:] == ["worker", "--no-retry", "--manual-sync-only"]
    assert "--no-retry" not in calls[1]
