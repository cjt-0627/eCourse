import asyncio
from datetime import UTC, datetime, timedelta

from ecourse.adapters.base import AdapterStatus, CaptchaSolution, LoginChallenge, LoginResult, ManualCaptchaSolver
from ecourse.adapters.elearn import ElearnAdapter, _page_session_expired
from ecourse.adapters.eeclass import EeclassAdapter
from ecourse.adapters.nthu_sso import (
    ALLOWED_HOSTS,
    AUTOMATIC_CAPTCHA_CONFIDENCE,
    CAPTCHA_SOLVER_TIMEOUT_SECONDS,
    CHALLENGE_TTL,
    MAX_CAPTCHA_IMAGES_PER_LOGIN,
    MAX_CAPTCHA_SUBMISSIONS,
    SSO_HOST,
)
from ecourse.adapters.nthu_sso import LoginAttempt, NthuSsoMixin


def test_login_security_limits_are_explicit() -> None:
    assert CHALLENGE_TTL.total_seconds() == 300
    assert MAX_CAPTCHA_SUBMISSIONS == 2
    assert AUTOMATIC_CAPTCHA_CONFIDENCE == 0.95
    assert CAPTCHA_SOLVER_TIMEOUT_SECONDS == 5
    assert MAX_CAPTCHA_IMAGES_PER_LOGIN == 4
    assert ALLOWED_HOSTS == {SSO_HOST, "elearn.nthu.edu.tw", "eeclass.nthu.edu.tw"}


def test_default_solver_requires_human_input() -> None:
    result = asyncio.run(ManualCaptchaSolver().solve(b"anonymous-image"))
    assert result.candidate is None
    assert result.reason == "manual_input_required"


def test_automatic_captcha_only_accepts_high_confidence_four_ascii_digits() -> None:
    class Solver:
        def __init__(self, candidate, confidence):
            self.candidate = candidate
            self.confidence = confidence

        async def solve(self, _image):
            return CaptchaSolution(self.candidate, self.confidence, "ccxp-lite-test", 1)

    accepted = NthuSsoMixin(object(), Solver("8004", 0.95))
    unicode_digits = NthuSsoMixin(object(), Solver("８００４", 0.99))
    low_confidence = NthuSsoMixin(object(), Solver("8004", 0.949))
    non_finite = NthuSsoMixin(object(), Solver("8004", float("nan")))

    assert asyncio.run(accepted._automatic_captcha_candidate(b"image")) == "8004"
    assert asyncio.run(unicode_digits._automatic_captcha_candidate(b"image")) is None
    assert asyncio.run(low_confidence._automatic_captcha_candidate(b"image")) is None
    assert asyncio.run(non_finite._automatic_captcha_candidate(b"image")) is None


def test_automatic_captcha_solver_failure_falls_back_to_human_challenge() -> None:
    class BrokenSolver:
        async def solve(self, _image):
            raise RuntimeError("local model unavailable")

    adapter = NthuSsoMixin(object(), BrokenSolver())

    assert asyncio.run(adapter._automatic_captcha_candidate(b"image")) is None


def test_automatic_captcha_refreshes_low_confidence_image_then_accepts() -> None:
    class Adapter(NthuSsoMixin):
        solved = []
        refreshed = 0

        async def _automatic_captcha_candidate(self, image, **_kwargs):
            self.solved.append(image)
            return "8004" if image == b"fresh-1" else None

        async def _refresh_captcha_for_solver(self, _attempt, _previous):
            self.refreshed += 1
            return f"fresh-{self.refreshed}".encode()

    adapter = Adapter(browser=object())
    attempt = LoginAttempt(
        "attempt", "challenge", "elearn", "account", "password", 1,
        object(), object(), datetime.now(UTC) + timedelta(minutes=5),
    )

    candidate, final_image = asyncio.run(
        adapter._automatic_captcha_with_refresh(attempt, b"initial", object())
    )

    assert candidate == "8004"
    assert final_image == b"fresh-1"
    assert adapter.solved == [b"initial", b"fresh-1"]
    assert adapter.refreshed == 1
    assert attempt.submissions == 0


def test_automatic_captcha_refresh_is_bounded_before_manual_fallback() -> None:
    class Adapter(NthuSsoMixin):
        solved = 0
        refreshed = 0

        async def _automatic_captcha_candidate(self, _image, **_kwargs):
            self.solved += 1
            return None

        async def _refresh_captcha_for_solver(self, _attempt, _previous):
            self.refreshed += 1
            return f"fresh-{self.refreshed}".encode()

    adapter = Adapter(browser=object())
    attempt = LoginAttempt(
        "attempt", "challenge", "elearn", "account", "password", 1,
        object(), object(), datetime.now(UTC) + timedelta(minutes=5),
    )

    candidate, final_image = asyncio.run(
        adapter._automatic_captcha_with_refresh(attempt, b"initial", object())
    )

    assert candidate is None
    assert final_image == b"fresh-3"
    assert adapter.solved == MAX_CAPTCHA_IMAGES_PER_LOGIN
    assert adapter.refreshed == MAX_CAPTCHA_IMAGES_PER_LOGIN - 1
    assert attempt.submissions == 0


def test_automatic_solver_prefers_natural_page_pixels_when_supported() -> None:
    from ecourse.adapters.base import CaptchaSolution

    class Solver:
        async def solve(self, _image):
            raise AssertionError("screenshot path must not be used")

        async def solve_element(self, page, locator):
            assert page == "sso-page" and locator == "captcha-image"
            return CaptchaSolution("9001", 0.99, "fixture", 1)

    adapter = NthuSsoMixin(browser=object(), captcha_solver=Solver())
    assert asyncio.run(adapter._automatic_captcha_candidate(
        b"scaled-screenshot", page="sso-page", image_locator="captcha-image",
    )) == "9001"


def test_login_result_does_not_contain_credentials() -> None:
    names = set(LoginResult.__dataclass_fields__)
    assert not {"account", "password", "cookie", "context"} & names
    assert LoginChallenge.__dataclass_fields__["image"]
    assert AdapterStatus.CONNECTED != AdapterStatus.CHALLENGE_REQUIRED


def test_sensitive_login_values_are_redacted_from_dataclass_repr() -> None:
    challenge = LoginChallenge(
        "challenge", "image_captcha", b"secret-image",
        datetime.now(UTC) + timedelta(minutes=5),
    )
    result = LoginResult(
        AdapterStatus.CONNECTED,
        storage_state={"cookies": [{"value": "secret-cookie"}]},
    )
    solution = CaptchaSolution("8004", 0.99, "ccxp-lite-test", 1)

    assert "secret-image" not in repr(challenge)
    assert "secret-cookie" not in repr(result)
    assert "8004" not in repr(solution)


def test_timeout_diagnostic_has_bounded_safe_stage() -> None:
    result = LoginResult(
        AdapterStatus.UPSTREAM_TIMEOUT,
        error_code="upstream_timeout",
        message="校方登入步驟逾時（open_sso）",
        retryable=True,
        diagnostic_stage="open_sso",
    )
    assert result.diagnostic_stage in {"open_platform", "open_sso", "sso_form", "captcha_image", "captcha_ready"}
    assert "https://" not in (result.message or "")
    assert "?" not in (result.message or "")


def test_discard_attempt_is_immediate_idempotent_and_clears_secrets() -> None:
    class Context:
        closed = 0
        async def close(self):
            self.closed += 1

    class Adapter(NthuSsoMixin):
        pass

    context = Context()
    adapter = Adapter(browser=object())
    attempt = LoginAttempt(
        attempt_id="adapter-attempt", challenge_id="challenge", platform="elearn",
        expected_account="sensitive-account", password="sensitive-password", generation=4,
        context=context, page=object(), expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    attempt.answered_request_ids["request"] = LoginResult(AdapterStatus.CHALLENGE_REQUIRED)
    adapter._attempts[attempt.attempt_id] = attempt

    rendered = repr(attempt)
    assert "sensitive-account" not in rendered
    assert "sensitive-password" not in rendered

    assert asyncio.run(adapter.discard_attempt(attempt.attempt_id)) is True
    assert asyncio.run(adapter.discard_attempt(attempt.attempt_id)) is False
    assert context.closed == 1
    assert attempt.expected_account == attempt.password == ""
    assert attempt.answered_request_ids == {}


def test_successful_controlled_sso_binds_stable_remote_profile_id() -> None:
    class Context:
        closed = 0
        async def storage_state(self): return {"cookies": [{"name": "session", "value": "opaque"}]}
        async def close(self): self.closed += 1

    class Adapter(NthuSsoMixin):
        async def _verify_page(self, _page):
            from ecourse.adapters.base import SessionVerification
            return SessionVerification(AdapterStatus.CONNECTED, "profile-42", "protected profile")

        async def _verify_expected_account(self, *_args):
            raise AssertionError("Moodle does not expose the SSO login name")

    context = Context()
    adapter = Adapter(browser=object())
    attempt = LoginAttempt(
        attempt_id="adapter-attempt", challenge_id="challenge", platform="elearn",
        expected_account="student-id", password="secret", generation=1,
        context=context, page=object(), expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    adapter._attempts[attempt.attempt_id] = attempt

    result = asyncio.run(adapter._finish_login(attempt))

    assert result.status == AdapterStatus.CONNECTED
    assert result.remote_account_id == "profile-42"
    assert result.storage_state == {"cookies": [{"name": "session", "value": "opaque"}]}
    assert "profile ID" in result.evidence
    assert context.closed == 1
    assert attempt.attempt_id not in adapter._attempts
    assert attempt.expected_account == attempt.password == ""
    assert attempt.answered_request_ids == {}


def test_controlled_sso_can_bind_protected_session_to_credential_fingerprint() -> None:
    class Context:
        async def storage_state(self): return {"cookies": [{"name": "session", "value": "opaque"}]}
        async def close(self): pass

    class Adapter(NthuSsoMixin):
        allow_protected_session_without_remote_id = True

        async def _verify_page(self, _page):
            from ecourse.adapters.base import SessionVerification
            return SessionVerification(AdapterStatus.CONNECTED, None, "protected route")

    adapter = Adapter(browser=object())
    attempt = LoginAttempt(
        attempt_id="adapter-attempt", challenge_id="challenge", platform="elearn",
        expected_account="student-id", password="secret", generation=1,
        context=Context(), page=object(), expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    adapter._attempts[attempt.attempt_id] = attempt

    result = asyncio.run(adapter._finish_login(attempt))

    assert result.status == AdapterStatus.CONNECTED
    assert result.remote_account_id is None
    assert result.storage_state["cookies"]
    assert "protected page" in result.evidence


class _IdentityNode:
    def __init__(self, **attributes):
        self.attributes = attributes

    async def get_attribute(self, name):
        return self.attributes.get(name)


class _IdentityLocator:
    def __init__(self, nodes=()):
        self.nodes = tuple(nodes)

    async def count(self):
        return len(self.nodes)

    def nth(self, index):
        return self.nodes[index]


class _IdentityPage:
    url = "https://elearn.nthu.edu.tw/my/index.php"

    def __init__(
        self, *, logout=True, logged_out=False, profiles=(), identity_nodes=(),
        moodle_userid=None, sesskey=None,
    ):
        self.logout = logout
        self.logged_out = logged_out
        self.profiles = tuple(profiles)
        self.identity_nodes = tuple(identity_nodes)
        self.moodle_userid = moodle_userid
        self.sesskey = sesskey

    def locator(self, selector):
        if selector == "a[href*='/login/logout.php']":
            return _IdentityLocator((_IdentityNode(href="/login/logout.php"),) if self.logout else ())
        if selector == "body.notloggedin":
            return _IdentityLocator((_IdentityNode(),) if self.logged_out else ())
        if selector.startswith("[data-region='user-menu'] a"):
            return _IdentityLocator(_IdentityNode(href=href) for href in self.profiles)
        if selector.startswith("body[data-userid]"):
            return _IdentityLocator(_IdentityNode(**{"data-userid": value}) for value in self.identity_nodes)
        # An unrelated data-userid elsewhere on a page must never be consulted.
        if selector == "[data-userid]":
            return _IdentityLocator((_IdentityNode(**{"data-userid": "999"}),))
        return _IdentityLocator()

    async def evaluate(self, expression):
        assert expression == "() => window.M && M.cfg ? {userid: M.cfg.userid, sesskey: M.cfg.sesskey} : null"
        if self.moodle_userid is None and self.sesskey is None:
            return None
        return {"userid": self.moodle_userid, "sesskey": self.sesskey}


def test_elearn_identity_accepts_controlled_dom_userid_without_profile_link() -> None:
    adapter = ElearnAdapter(browser=object())
    page = _IdentityPage(identity_nodes=("0042",))

    result = asyncio.run(adapter._verify_page(page))

    assert result.status == AdapterStatus.CONNECTED
    assert result.remote_account_id == "42"


def test_elearn_identity_accepts_moodle_runtime_userid() -> None:
    adapter = ElearnAdapter(browser=object())
    page = _IdentityPage(moodle_userid=73)

    result = asyncio.run(adapter._verify_page(page))

    assert result.status == AdapterStatus.CONNECTED
    assert result.remote_account_id == "73"


def test_elearn_identity_accepts_logged_in_moodle_session_without_logout_anchor() -> None:
    adapter = ElearnAdapter(browser=object())
    page = _IdentityPage(
        logout=False,
        moodle_userid=73,
        sesskey="stable-session-proof",
    )

    result = asyncio.run(adapter._verify_page(page))

    assert result.status == AdapterStatus.CONNECTED
    assert result.remote_account_id == "73"


def test_elearn_identity_rejects_conflicting_controlled_sources() -> None:
    adapter = ElearnAdapter(browser=object())
    page = _IdentityPage(
        profiles=("https://elearn.nthu.edu.tw/user/profile.php?id=42",),
        moodle_userid=73,
    )

    result = asyncio.run(adapter._verify_page(page))

    assert result.status == AdapterStatus.PARSER_CHANGED
    assert result.error_code == "identity_not_verified"


def test_elearn_identity_accepts_protected_route_without_exposed_userid() -> None:
    adapter = ElearnAdapter(browser=object())

    no_identity = asyncio.run(adapter._verify_page(_IdentityPage()))
    no_logout = asyncio.run(adapter._verify_page(_IdentityPage(logout=False, moodle_userid=42)))

    assert no_identity.status == AdapterStatus.CONNECTED
    assert no_identity.remote_account_id is None
    assert no_logout.status == AdapterStatus.CONNECTED
    assert no_logout.remote_account_id == "42"


def test_elearn_identity_explicit_logged_out_marker_fails_closed() -> None:
    adapter = ElearnAdapter(browser=object())

    logged_out = asyncio.run(adapter._verify_page(
        _IdentityPage(logout=False, logged_out=True, moodle_userid=42, sesskey="session-proof")
    ))

    assert logged_out.status == AdapterStatus.SESSION_EXPIRED
    assert logged_out.error_code == "session_expired"


def test_elearn_session_expiry_detects_same_url_moodle_login_form() -> None:
    class Page:
        url = "https://elearn.nthu.edu.tw/course/view.php?id=101"

        def locator(self, selector):
            if selector.startswith("body#page-login-index form"):
                return _IdentityLocator((_IdentityNode(),))
            return _IdentityLocator()

    assert asyncio.run(_page_session_expired(Page())) is True


def test_elearn_session_expiry_does_not_treat_normal_course_page_as_expired() -> None:
    class Page:
        url = "https://elearn.nthu.edu.tw/course/view.php?id=101"

        def locator(self, _selector):
            return _IdentityLocator()

    assert asyncio.run(_page_session_expired(Page())) is False


def test_elearn_session_expiry_accepts_only_exact_same_origin_oauth_link() -> None:
    class Page:
        url = "https://elearn.nthu.edu.tw/"

        def __init__(self, href):
            self.href = href

        def locator(self, selector):
            if selector == "a[href*='/auth/oauth2/login.php']":
                return _IdentityLocator((_IdentityNode(href=self.href),))
            return _IdentityLocator()

    official = Page("/auth/oauth2/login.php?id=5")
    wrong_provider = Page("/auth/oauth2/login.php?id=4")
    external = Page("https://evil.example/auth/oauth2/login.php?id=5")

    assert asyncio.run(_page_session_expired(official)) is True
    assert asyncio.run(_page_session_expired(wrong_provider)) is False
    assert asyncio.run(_page_session_expired(external)) is False


def test_elearn_exact_logged_out_landing_redirect_is_session_expired() -> None:
    class Page:
        def __init__(self, url):
            self.url = url

        def locator(self, _selector):
            return _IdentityLocator()

    assert asyncio.run(_page_session_expired(Page("https://elearn.nthu.edu.tw/?redirect=0"))) is True
    assert asyncio.run(_page_session_expired(Page("https://elearn.nthu.edu.tw/"))) is False
    assert asyncio.run(_page_session_expired(Page("https://elearn.nthu.edu.tw/?redirect=1"))) is False
    assert asyncio.run(_page_session_expired(Page("https://evil.example/?redirect=0"))) is False


def test_eeclass_accepts_protected_dashboard_without_exposed_profile_id() -> None:
    class EmptyLocator:
        async def count(self): return 0

    class Page:
        url = "https://eeclass.nthu.edu.tw/dashboard"
        def locator(self, _selector): return EmptyLocator()

    result = asyncio.run(EeclassAdapter(browser=object())._verify_page(Page()))

    assert result.status == AdapterStatus.CONNECTED
    assert result.remote_account_id is None


def test_eeclass_landing_with_exact_same_origin_sso_link_is_session_expired() -> None:
    class Page:
        url = "https://eeclass.nthu.edu.tw/welcome"

        def __init__(self, href):
            self.href = href

        def locator(self, selector):
            if selector == "a[href*='/service/oauthRedir']":
                return _IdentityLocator((_IdentityNode(href=self.href),))
            return _IdentityLocator()

    official = asyncio.run(EeclassAdapter(browser=object())._verify_page(Page("/service/oauthRedir")))
    external = asyncio.run(EeclassAdapter(browser=object())._verify_page(
        Page("https://evil.example/service/oauthRedir")
    ))

    assert official.status == AdapterStatus.SESSION_EXPIRED
    assert official.error_code == "session_expired"
    assert external.status == AdapterStatus.PARSER_CHANGED
    assert external.error_code == "protected_page_not_reached"
