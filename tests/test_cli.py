import json
import subprocess
import sys


def run(*args):
    return subprocess.run([sys.executable, "-m", "ecourse.cli", *args], text=True, capture_output=True)


def test_doctor_reports_unverified_school_login():
    result = run("doctor")
    assert result.returncode == 0
    report = json.loads(result.stdout)
    assert "不驗證校方登入" in report["school_login"]


def test_self_test_needs_no_persistent_setup():
    result = run("self-test")
    assert result.returncode == 0
    assert "通過" in result.stdout
