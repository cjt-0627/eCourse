from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urljoin, urlparse
from uuid import uuid4

from playwright.async_api import Browser, BrowserContext, Page, TimeoutError as PlaywrightTimeoutError

from .base import (
    AdapterStatus,
    CaptchaSolver,
    LoginChallenge,
    LoginResult,
    ManualCaptchaSolver,
    SessionVerification,
)

SSO_HOST = "oauth.ccxp.nthu.edu.tw"
ALLOWED_HOSTS = {SSO_HOST, "elearn.nthu.edu.tw", "eeclass.nthu.edu.tw"}
CHALLENGE_TTL = timedelta(minutes=5)
MAX_CAPTCHA_SUBMISSIONS = 2
AUTOMATIC_CAPTCHA_CONFIDENCE = 0.95
CAPTCHA_SOLVER_TIMEOUT_SECONDS = 5
MAX_CAPTCHA_IMAGES_PER_LOGIN = 4


@dataclass(slots=True)
class LoginAttempt:
    attempt_id: str
    challenge_id: str
    platform: str
    expected_account: str = field(repr=False)
    password: str = field(repr=False)
    generation: int
    context: BrowserContext = field(repr=False)
    page: Page = field(repr=False)
    expires_at: datetime
    submissions: int = 0
    answered_request_ids: dict[str, LoginResult] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


class NthuSsoMixin:
    platform: str
    login_url: str
    protected_url: str

    def __init__(self, browser: Browser, captcha_solver: CaptchaSolver | None = None, timeout_ms: int = 30_000) -> None:
        self.browser = browser
        self.captcha_solver = captcha_solver or ManualCaptchaSolver()
        self.timeout_ms = timeout_ms
        self._attempts: dict[str, LoginAttempt] = {}
        self._lock = asyncio.Lock()

    async def begin_login(
        self, account: str, password: str, storage_state: dict[str, Any] | None = None,
        connection_generation: int = 0,
    ) -> LoginResult:
        if not account or not password:
            return LoginResult(AdapterStatus.CREDENTIALS_INVALID, error_code="credentials_invalid", message="帳號與密碼不可空白")
        if storage_state:
            verification = await self.verify_session(storage_state)
            if verification.status == AdapterStatus.CONNECTED:
                return LoginResult(
                    AdapterStatus.CONNECTED, remote_account_id=verification.remote_account_id,
                    storage_state=verification.storage_state or storage_state,
                    evidence=verification.evidence,
                )
            if verification.status != AdapterStatus.SESSION_EXPIRED:
                return LoginResult(
                    verification.status, remote_account_id=verification.remote_account_id,
                    error_code=verification.error_code, message="既有校方 session 無法安全驗證",
                    retryable=verification.status in {AdapterStatus.UPSTREAM_TIMEOUT, AdapterStatus.UPSTREAM_UNAVAILABLE},
                )
        async with self._lock:
            await self.expire_attempts()
            if self._attempts:
                return LoginResult(AdapterStatus.UPSTREAM_UNAVAILABLE, error_code="login_busy", message="已有登入挑戰等待處理", retryable=True)
            context = await self.browser.new_context(storage_state=storage_state)
            page = await context.new_page()
            page.set_default_timeout(self.timeout_ms)
            stage = "open_platform"
            try:
                async with asyncio.timeout(90):
                    await self._goto(page, self.login_url)
                    stage = "open_sso"
                    if urlparse(page.url).hostname != SSO_HOST:
                        selector = (
                            "a[href*='/auth/oauth2/login.php?id=5']" if self.platform == "elearn"
                            else "a[href*='/service/oauthRedir']"
                        )
                        await page.locator(selector).first.click(timeout=self.timeout_ms)
                    stage = "sso_form"
                    await self._wait_for_sso_form(page)
                    await page.locator("#id, input[name=id]").first.fill(account)
                    await page.locator("#password, input[name=password]").first.fill(password)
                    stage = "captcha_image"
                    image_locator = page.locator("#captcha_image, img[alt*='CAPTCHA']").first
                    image = await image_locator.screenshot(timeout=self.timeout_ms)
                attempt = LoginAttempt(
                    attempt_id=str(uuid4()), challenge_id=str(uuid4()), platform=self.platform,
                    expected_account=account, password=password, generation=connection_generation, context=context, page=page,
                    expires_at=datetime.now(UTC) + CHALLENGE_TTL,
                )
                self._attempts[attempt.attempt_id] = attempt
                candidate, image = await self._automatic_captcha_with_refresh(
                    attempt, image, image_locator
                )
                if candidate is not None:
                    # Automatic and human answers share answer_challenge, so
                    # the same two-submission budget and failure classifier
                    # apply. A rejected model answer leaves one human attempt.
                    return await self.answer_challenge(
                        attempt.attempt_id, attempt.challenge_id, candidate
                    )
                return self._challenge_result(attempt, image)
            except PlaywrightTimeoutError:
                await context.close()
                return self._timeout_result(stage)
            except TimeoutError:
                await context.close()
                return self._timeout_result(stage, overall=True)
            except Exception as exc:
                await context.close()
                return LoginResult(
                    AdapterStatus.PARSER_CHANGED, error_code="parser_changed",
                    message=f"登入頁結構無法辨識（{stage}）：{type(exc).__name__}", diagnostic_stage=stage,
                )

    async def _automatic_captcha_candidate(self, image: bytes, *, page=None, image_locator=None) -> str | None:
        """Run an injected local solver without making login depend on it."""
        try:
            solve_element = getattr(self.captcha_solver, "solve_element", None)
            inference = solve_element(page, image_locator) if solve_element and page is not None and image_locator is not None else self.captcha_solver.solve(image)
            solution = await asyncio.wait_for(inference, timeout=CAPTCHA_SOLVER_TIMEOUT_SECONDS)
            candidate = solution.candidate
            confidence = float(solution.confidence)
        except Exception:
            # Missing/corrupt local models and inference failures fall back to
            # the existing in-memory human challenge. Never submit a guess.
            return None
        if (
            not isinstance(candidate, str)
            or re.fullmatch(r"[0-9]{4}", candidate) is None
            or not math.isfinite(confidence)
            or confidence < AUTOMATIC_CAPTCHA_CONFIDENCE
        ):
            return None
        return candidate

    async def _automatic_captcha_with_refresh(
        self, attempt: LoginAttempt, image: bytes, image_locator,
    ) -> tuple[str | None, bytes]:
        """Try a bounded set of fresh images without submitting weak guesses."""
        current_image = image
        for image_number in range(MAX_CAPTCHA_IMAGES_PER_LOGIN):
            candidate = await self._automatic_captcha_candidate(
                current_image, page=attempt.page, image_locator=image_locator,
            )
            if candidate is not None:
                return candidate, current_image
            if image_number + 1 >= MAX_CAPTCHA_IMAGES_PER_LOGIN:
                break
            refreshed = await self._refresh_captcha_for_solver(attempt, current_image)
            if refreshed is None:
                break
            current_image = refreshed
        return None, current_image

    async def _refresh_captcha_for_solver(
        self, attempt: LoginAttempt, previous_image: bytes,
    ) -> bytes | None:
        """Get one demonstrably different official CAPTCHA or stop safely."""
        page = attempt.page
        refresh = page.locator(
            "a[title='Refresh Image'], a:has(img[alt='Refresh Image'])"
        ).first
        try:
            if await refresh.count() > 0:
                old_src = await page.locator("#captcha_image").get_attribute("src")
                await refresh.click(timeout=min(self.timeout_ms, 3_000))
                try:
                    await page.wait_for_function(
                        "old => document.querySelector('#captcha_image')?.getAttribute('src') !== old",
                        arg=old_src,
                        timeout=min(self.timeout_ms, 3_000),
                    )
                except PlaywrightTimeoutError:
                    # The server can reuse the src while changing image bytes.
                    pass
            else:
                # Reload only the already validated official SSO form, then
                # restore credentials from this in-memory login transaction.
                await self._assert_allowed_url(page.url)
                if urlparse(page.url).hostname != SSO_HOST:
                    return None
                await page.reload(wait_until="domcontentloaded", timeout=self.timeout_ms)
                await self._wait_for_sso_form(page)
                await page.locator("#id, input[name=id]").first.fill(attempt.expected_account)
                await page.locator("#password, input[name=password]").first.fill(attempt.password)
            fresh = await page.locator(
                "#captcha_image, img[alt*='CAPTCHA']"
            ).first.screenshot(timeout=self.timeout_ms)
            return fresh if fresh != previous_image else None
        except Exception:
            # Refresh is an optimization. DOM changes, a missing control, or a
            # reload failure must preserve the current manual challenge.
            return None

    async def answer_challenge(
        self, attempt_id: str, challenge_id: str, answer: str, request_id: str | None = None,
    ) -> LoginResult:
        attempt = self._attempts.get(attempt_id)
        if attempt is None:
            return LoginResult(AdapterStatus.CHALLENGE_REQUIRED, error_code="challenge_expired", message="登入挑戰不存在或已失效")
        async with attempt.lock:
            if attempt.challenge_id != challenge_id:
                return LoginResult(AdapterStatus.CHALLENGE_REQUIRED, error_code="challenge_expired", message="登入挑戰不存在或已失效")
            if request_id and request_id in attempt.answered_request_ids:
                return attempt.answered_request_ids[request_id]
            if datetime.now(UTC) >= attempt.expires_at:
                await self._discard_attempt(attempt_id)
                return LoginResult(AdapterStatus.CHALLENGE_REQUIRED, error_code="challenge_expired", message="登入挑戰已超過五分鐘")
            if not answer or len(answer) > 32:
                return LoginResult(AdapterStatus.CHALLENGE_REQUIRED, attempt_id=attempt_id, error_code="captcha_rejected", message="驗證碼格式無效")
            attempt.submissions += 1
            try:
                await attempt.page.locator("#captcha_code, input[name=captcha]").first.fill(answer)
                await attempt.page.locator("button[name=action][value=login], button[type=submit], input[type=submit]").first.click()
                await attempt.page.wait_for_load_state("domcontentloaded")
                await self._assert_allowed_url(attempt.page.url)
                if urlparse(attempt.page.url).hostname == SSO_HOST:
                    result = await self._classify_failed_submission(attempt)
                else:
                    result = await self._finish_login(attempt)
            except PlaywrightTimeoutError:
                result = LoginResult(AdapterStatus.UPSTREAM_TIMEOUT, attempt_id=attempt_id, error_code="upstream_timeout", message="校方回應逾時", retryable=True)
                await self._discard_attempt(attempt_id)
            except Exception as exc:
                result = LoginResult(AdapterStatus.PARSER_CHANGED, attempt_id=attempt_id, error_code="parser_changed", message=f"登入回應無法辨識：{type(exc).__name__}")
                await self._discard_attempt(attempt_id)
            if request_id:
                attempt.answered_request_ids[request_id] = result
            return result

    async def refresh_challenge(self, attempt_id: str, challenge_id: str) -> LoginResult:
        attempt = self._attempts.get(attempt_id)
        if attempt is None:
            return LoginResult(AdapterStatus.CHALLENGE_REQUIRED, error_code="challenge_expired", message="登入挑戰不存在或已失效")
        async with attempt.lock:
            if attempt.challenge_id != challenge_id or datetime.now(UTC) >= attempt.expires_at:
                await self._discard_attempt(attempt_id)
                return LoginResult(AdapterStatus.CHALLENGE_REQUIRED, error_code="challenge_expired", message="登入挑戰不存在或已失效")
            try:
                old_src = await attempt.page.locator("#captcha_image").get_attribute("src")
                await attempt.page.locator("a[title='Refresh Image'], a:has(img[alt='Refresh Image'])").first.click()
                await attempt.page.wait_for_function(
                    "old => document.querySelector('#captcha_image')?.getAttribute('src') !== old",
                    arg=old_src,
                )
                image = await attempt.page.locator("#captcha_image").screenshot()
                attempt.challenge_id = str(uuid4())
                attempt.expires_at = datetime.now(UTC) + CHALLENGE_TTL
                return self._challenge_result(attempt, image)
            except PlaywrightTimeoutError:
                return LoginResult(AdapterStatus.UPSTREAM_TIMEOUT, attempt_id=attempt_id, error_code="upstream_timeout", retryable=True)

    async def _classify_failed_submission(self, attempt: LoginAttempt) -> LoginResult:
        body = (await attempt.page.locator("body").inner_text()).lower()
        if any(text in body for text in ("密碼錯誤", "帳號或密碼", "invalid password", "incorrect password", "帳號不存在")):
            await self._discard_attempt(attempt.attempt_id)
            return LoginResult(AdapterStatus.CREDENTIALS_INVALID, error_code="credentials_invalid", message="校方拒絕帳號或密碼")
        if any(text in body for text in ("two-factor", "兩步驟", "二階段", "一次性密碼", "otp")):
            await self._discard_attempt(attempt.attempt_id)
            return LoginResult(AdapterStatus.MFA_REQUIRED, error_code="mfa_required", message="校方要求額外身分驗證")
        if any(text in body for text in ("帳號已鎖定", "account locked", "too many attempts", "嘗試次數過多")):
            await self._discard_attempt(attempt.attempt_id)
            return LoginResult(AdapterStatus.CREDENTIALS_INVALID, error_code="account_locked", message="校方帳號已鎖定或嘗試次數過多")
        if attempt.submissions >= MAX_CAPTCHA_SUBMISSIONS:
            await self._discard_attempt(attempt.attempt_id)
            return LoginResult(AdapterStatus.CHALLENGE_REQUIRED, error_code="captcha_rejected", message="驗證碼已達兩次提交上限，請重新開始登入")
        await attempt.page.locator("#id, input[name=id]").first.fill(attempt.expected_account)
        await attempt.page.locator("#password, input[name=password]").first.fill(attempt.password)
        image = await attempt.page.locator("#captcha_image, img[alt*='CAPTCHA']").first.screenshot()
        attempt.challenge_id = str(uuid4())
        attempt.expires_at = datetime.now(UTC) + CHALLENGE_TTL
        return self._challenge_result(attempt, image, error_code="captcha_rejected")

    async def _finish_login(self, attempt: LoginAttempt) -> LoginResult:
        verification = await self._verify_page(attempt.page)
        if verification.status != AdapterStatus.CONNECTED:
            await self._discard_attempt(attempt.attempt_id)
            return LoginResult(verification.status, error_code=verification.error_code or "identity_not_verified", message="未能在受保護頁確認遠端身分")
        # eLearn's protected profile currently exposes a stable numeric profile
        # id but does not expose the SSO login name.  This login still started
        # in a new isolated context and this adapter filled the expected account
        # into the official SSO form itself.  Bind the first successful login to
        # that remote profile id; the worker persists it and rejects every later
        # session whose protected page reports a different id.
        if not verification.remote_account_id and not getattr(self, "allow_protected_session_without_remote_id", False):
            await self._discard_attempt(attempt.attempt_id)
            return LoginResult(
                AdapterStatus.PARSER_CHANGED, error_code="identity_not_verified",
                message="受保護頁未提供穩定的 profile ID；不保存 session",
            )
        state = await attempt.context.storage_state()
        attempt.expected_account = ""
        attempt.password = ""
        attempt.answered_request_ids.clear()
        await attempt.context.close()
        self._attempts.pop(attempt.attempt_id, None)
        return LoginResult(
            AdapterStatus.CONNECTED, remote_account_id=verification.remote_account_id,
            storage_state=state,
            evidence=(
                "isolated controlled SSO login bound to protected-page profile ID"
                if verification.remote_account_id
                else "isolated controlled SSO login verified on protected page"
            ),
        )

    async def verify_session(self, storage_state: dict[str, Any]) -> SessionVerification:
        context = await self.browser.new_context(storage_state=storage_state)
        page = await context.new_page()
        page.set_default_timeout(self.timeout_ms)
        try:
            await self._goto(page, self.protected_url)
            verification = await self._verify_page(page)
            if verification.status == AdapterStatus.CONNECTED:
                verification = replace(
                    verification,
                    storage_state=await context.storage_state(),
                )
            return verification
        except PlaywrightTimeoutError:
            return SessionVerification(AdapterStatus.UPSTREAM_TIMEOUT, error_code="upstream_timeout")
        except Exception:
            return SessionVerification(AdapterStatus.PARSER_CHANGED, error_code="parser_changed")
        finally:
            await context.close()

    async def _goto(self, page: Page, url: str) -> None:
        await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        await self._assert_allowed_url(page.url)

    async def _assert_allowed_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
            raise ValueError("unexpected_sso_redirect")

    async def _wait_for_sso_form(self, page: Page) -> None:
        await page.wait_for_url(re.compile(r"^https://oauth\.ccxp\.nthu\.edu\.tw/v1\.1/authorize\.php(?:\?|$)"))
        parsed = urlparse(page.url)
        if parsed.scheme != "https" or parsed.hostname != SSO_HOST or parsed.path != "/v1.1/authorize.php":
            raise ValueError("unexpected_sso_form_url")
        form = page.locator("form:has(#id):has(#password):has(#captcha_code)").first
        await form.wait_for()
        method = (await form.get_attribute("method") or "get").lower()
        action = urljoin(page.url, await form.get_attribute("action") or page.url)
        action_url = urlparse(action)
        if method != "post" or action_url.scheme != "https" or action_url.hostname != SSO_HOST or action_url.path != "/v1.1/authorize.php":
            raise ValueError("unsafe_sso_form_action")
        await form.locator("#captcha_image, img[alt*='CAPTCHA']").first.wait_for()

    def _challenge_result(self, attempt: LoginAttempt, image: bytes, error_code: str = "captcha_required") -> LoginResult:
        return LoginResult(
            AdapterStatus.CHALLENGE_REQUIRED, attempt_id=attempt.attempt_id,
            challenge=LoginChallenge(attempt.challenge_id, "image_captcha", image, attempt.expires_at),
            error_code=error_code, message="需要人工輸入校方圖片驗證碼",
            diagnostic_stage="captcha_ready",
        )

    def _timeout_result(self, stage: str, overall: bool = False) -> LoginResult:
        scope = "整體登入流程" if overall else "校方登入步驟"
        return LoginResult(
            AdapterStatus.UPSTREAM_TIMEOUT, error_code="upstream_timeout",
            message=f"{scope}逾時（{stage}）", retryable=True, diagnostic_stage=stage,
        )

    async def expire_attempts(self) -> None:
        expired = [key for key, value in self._attempts.items() if datetime.now(UTC) >= value.expires_at]
        for attempt_id in expired:
            await self._discard_attempt(attempt_id)

    async def discard_attempt(self, attempt_id: str) -> bool:
        """Cancel one worker-owned login transaction after generation invalidation.

        Popping before waiting on the attempt lock prevents any new answer/refresh
        lookup from finding the transaction while an in-flight operation drains.
        """
        attempt = self._attempts.pop(attempt_id, None)
        if attempt is None:
            return False
        attempt.expected_account = ""
        attempt.password = ""
        attempt.answered_request_ids.clear()
        # Close first: Playwright aborts any in-flight navigation immediately.
        # Waiting for the lock first could leave a stale generation alive for a
        # full upstream timeout.
        await attempt.context.close()
        async with attempt.lock:
            pass
        return True

    async def close(self) -> None:
        for attempt_id in list(self._attempts):
            await self._discard_attempt(attempt_id)

    async def _discard_attempt(self, attempt_id: str) -> None:
        attempt = self._attempts.pop(attempt_id, None)
        if attempt:
            attempt.expected_account = ""
            attempt.password = ""
            attempt.answered_request_ids.clear()
            await attempt.context.close()

    async def _verify_page(self, page: Page) -> SessionVerification:
        raise NotImplementedError

    async def _verify_expected_account(self, page: Page, expected_account: str) -> bool:
        raise NotImplementedError
