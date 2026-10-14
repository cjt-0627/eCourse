from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

from playwright.async_api import Browser, Error as PlaywrightError, Page, TimeoutError as PlaywrightTimeoutError

from ecourse.parsing.dates import parse_nthu_date
from ecourse.parsing.html import parse_course_content, parse_courses, parse_moodle_forum_pages

from .base import AdapterStatus, CaptchaSolver, CourseItem, FetchResult, ItemDetail, SessionVerification
from .nthu_sso import NthuSsoMixin, SSO_HOST


class ElearnAdapter(NthuSsoMixin):
    platform = "elearn"
    allow_protected_session_without_remote_id = True
    login_url = "https://elearn.nthu.edu.tw/"
    protected_url = "https://elearn.nthu.edu.tw/my/index.php"
    courses_url = "https://elearn.nthu.edu.tw/my/courses.php"
    max_forum_containers = 8
    max_forum_pages = 5

    async def _verify_page(self, page: Page) -> SessionVerification:
        if urlparse(page.url).hostname != "elearn.nthu.edu.tw" or "/my/" not in urlparse(page.url).path:
            await self._goto(page, self.protected_url)
        if await _page_session_expired(page):
            return SessionVerification(AdapterStatus.SESSION_EXPIRED, error_code="session_expired")
        parsed = urlparse(page.url)
        if parsed.hostname != "elearn.nthu.edu.tw" or not parsed.path.startswith("/my/"):
            return SessionVerification(AdapterStatus.PARSER_CHANGED, error_code="protected_page_not_reached")
        logout = page.locator("a[href*='/login/logout.php']")
        has_logout = await logout.count() > 0

        # Current Moodle themes can keep logout inside a JS-driven menu without
        # rendering an anchor.  On the protected /my/ route, a non-guest
        # M.cfg.userid plus Moodle's per-session key is the stable equivalent.
        # Explicit logged-out markup always wins and fails closed.
        has_logged_out_body = await page.locator("body.notloggedin").count() > 0
        moodle_config: object = None
        try:
            moodle_config = await page.evaluate(
                "() => window.M && M.cfg ? {userid: M.cfg.userid, sesskey: M.cfg.sesskey} : null"
            )
        except Exception:
            # A locked-down page may prevent evaluation. The visible logout
            # path can still prove authentication.
            pass
        config = moodle_config if isinstance(moodle_config, dict) else {}
        sesskey = config.get("sesskey")
        config_user_id = _unique_moodle_userid([config.get("userid")])
        has_session_markers = (
            not has_logged_out_body
            and config_user_id is not None
            and int(config_user_id) > 1
            and isinstance(sesskey, str)
            and 1 <= len(sesskey) <= 256
            and not sesskey.isspace()
        )
        if has_logged_out_body:
            return SessionVerification(AdapterStatus.SESSION_EXPIRED, error_code="session_expired")

        # Moodle versions expose the signed-in user's stable numeric id in
        # different places.  Keep these selectors tied to the current-user UI:
        # a broad ``[data-userid]`` or profile link could identify a classmate.
        candidates: list[object] = []
        profiles = page.locator(
            "[data-region='user-menu'] a[href*='/user/profile.php?id='], "
            ".usermenu a[href*='/user/profile.php?id='], "
            "#user-menu-toggle[href*='/user/profile.php?id=']"
        )
        for index in range(await profiles.count()):
            href = await profiles.nth(index).get_attribute("href") or ""
            match = re.search(r"[?&]id=(\d+)(?:&|$)", href)
            if match:
                candidates.append(match.group(1))
        identity_nodes = page.locator(
            "body[data-userid], [data-region='user-menu'][data-userid], "
            "#user-menu-toggle[data-userid]"
        )
        for index in range(await identity_nodes.count()):
            candidates.append(await identity_nodes.nth(index).get_attribute("data-userid"))
        candidates.append(config.get("userid"))

        remote_user_id = _unique_moodle_userid(candidates)
        if remote_user_id is None:
            # Some eLearn themes expose neither a current-user link nor
            # M.cfg.userid. Reaching the protected /my/ route in the isolated
            # SSO context still proves that the credential entered by this
            # adapter authenticated. The worker binds the saved session to the
            # credential's blind fingerprint and clears it on account changes.
            if any(_unique_moodle_userid([candidate]) is not None for candidate in candidates):
                return SessionVerification(AdapterStatus.PARSER_CHANGED, error_code="identity_not_verified")
            return SessionVerification(
                AdapterStatus.CONNECTED,
                None,
                "受控 SSO context 已進入受保護的 eLearn /my/ 頁面",
            )
        return SessionVerification(
            AdapterStatus.CONNECTED,
            remote_user_id,
            "受保護儀表板顯示 Moodle 認證狀態與穩定 user ID",
        )

    async def _verify_expected_account(self, page: Page, expected_account: str) -> bool:
        profile = page.locator("a[href*='/user/profile.php?id=']").first
        href = await profile.get_attribute("href")
        if not href:
            return False
        await self._goto(page, urljoin(page.url, href))
        expected = expected_account.strip().casefold()
        selectors = (
            "[data-username]", "[data-account]", "[data-fieldtype='username'] .content",
            "dt:has-text('帳號') + dd", "dt:has-text('Username') + dd",
            ".profile_tree li:has-text('帳號') .content",
        )
        for selector in selectors:
            nodes = page.locator(selector)
            for index in range(await nodes.count()):
                node = nodes.nth(index)
                candidate = (await node.get_attribute("data-username")) or (await node.get_attribute("data-account")) or (await node.inner_text())
                if candidate.strip().casefold() == expected:
                    return True
        return False

    async def discover_courses(self, storage_state: dict[str, Any]) -> FetchResult:
        context = await self.browser.new_context(storage_state=storage_state)
        page = await context.new_page()
        page.set_default_timeout(self.timeout_ms)
        try:
            await self._goto(page, self.courses_url)
            verification = await self._verify_page(page)
            if verification.status != AdapterStatus.CONNECTED:
                return FetchResult((), (), False, (verification.error_code or verification.status.value,))
            try:
                await page.wait_for_function(
                    """() => {
                        if (document.querySelector("a[href*='/course/view.php?id=']")) return true;
                        const known = ['no courses', 'you are not enrolled in any courses', '沒有課程', '無課程', '尚未選修任何課程'];
                        return [...document.querySelectorAll("[data-region='empty-message'], [data-region='courses-view'] .alert-info, .block_myoverview .alert-info")]
                            .some(node => known.includes((node.textContent || '').trim().toLowerCase().replace(/\\s+/g, ' ')));
                    }""",
                    timeout=self.timeout_ms,
                )
            except PlaywrightTimeoutError:
                return FetchResult((), (), False, ("course_catalog_not_resolved",))
            courses = parse_courses(await page.content(), page.url, self.platform)
            if not courses:
                empty_nodes = page.locator("[data-region='empty-message'], [data-region='courses-view'] .alert-info, .block_myoverview .alert-info")
                empty_texts = [await empty_nodes.nth(index).inner_text() for index in range(await empty_nodes.count())]
                if not any(_is_explicit_empty_catalog(text) for text in empty_texts):
                    return FetchResult((), (), False, ("parser_changed",))
            if await _catalog_has_more(page):
                return FetchResult(courses, ("course_catalog_page",), False, ("course_catalog_pagination_not_expanded",))
            return FetchResult(courses, ("course_catalog",), True)
        except PlaywrightTimeoutError:
            return FetchResult((), (), False, ("upstream_timeout",))
        except ValueError:
            return FetchResult((), (), False, ("unexpected_redirect",))
        finally:
            await context.close()

    async def fetch_course(self, storage_state: dict[str, Any], remote_course_id: str) -> FetchResult:
        if not remote_course_id.isdigit():
            return FetchResult((), (), False, ("invalid_remote_course_id",))
        context = await self.browser.new_context(storage_state=storage_state)
        page = await context.new_page()
        page.set_default_timeout(self.timeout_ms)
        try:
            await self._goto(page, f"https://elearn.nthu.edu.tw/course/view.php?id={remote_course_id}")
            if await _page_session_expired(page):
                return FetchResult((), (), False, ("session_expired",))
            try:
                await page.wait_for_function(
                    "() => document.querySelectorAll('a[href]').length > 0",
                    timeout=min(self.timeout_ms, 3_000),
                )
            except PlaywrightTimeoutError:
                pass
            try:
                course_html = await page.content()
            except PlaywrightError:
                await page.wait_for_load_state("domcontentloaded", timeout=self.timeout_ms)
                await self._assert_allowed_url(page.url)
                if await _page_session_expired(page):
                    return FetchResult((), (), False, ("session_expired",))
                course_html = await page.content()
            content = parse_course_content(course_html, page.url, self.platform, remote_course_id)
            course_root = page.locator(
                "#page-course-view-topics, #page-course-view-weeks, "
                "body[id^='page-course-view-'], #region-main, #page-content, "
                "main[role='main'], .course-content, [data-region='course-content']"
            )
            if await course_root.count() == 0:
                if not content.items:
                    anchor_paths: set[str] = set()
                    anchors = page.locator("a[href]")
                    for index in range(min(await anchors.count(), 80)):
                        href = await anchors.nth(index).get_attribute("href")
                        target = urlparse(urljoin(page.url, href or ""))
                        if target.scheme == "https" and target.hostname == "elearn.nthu.edu.tw":
                            anchor_paths.add(target.path)
                    final_path = urlparse(page.url).path
                    safe_paths = "|".join(sorted(anchor_paths)[:16]) or "none"
                    return FetchResult(
                        (), (), False,
                        ("parser_changed", f"final_path:{final_path}", f"anchor_paths:{safe_paths}"),
                    )
                # NTHU occasionally serves a theme without Moodle's usual
                # outer container ids.  Concrete same-origin activity links
                # remain useful and are individually validated below.
                root_warning = ("course_root_unrecognized",)
            else:
                root_warning = ()
            forum_containers = page.locator("a[href*='/mod/forum/view.php']")
            forum_urls: list[str] = []
            for index in range(await forum_containers.count()):
                href = await forum_containers.nth(index).get_attribute("href")
                url = urljoin(page.url, href or "")
                parsed = urlparse(url)
                if parsed.scheme == "https" and parsed.hostname == "elearn.nthu.edu.tw" and parsed.path == "/mod/forum/view.php" and parse_qs(parsed.query).get("id", [None])[0]:
                    if url not in forum_urls:
                        forum_urls.append(url)
            items = {(item.kind, item.remote_id): item for item in content.items}
            forum_limit_exceeded = len(forum_urls) > self.max_forum_containers
            complete = not forum_limit_exceeded
            warnings: list[str] = list(root_warning)
            complete = complete and not root_warning
            if forum_limit_exceeded:
                warnings.append("elearn_forum_container_limit")
            for forum_url in forum_urls[: self.max_forum_containers]:
                forum_complete, forum_items, forum_warnings = await self._fetch_forum_discussions(page, forum_url, remote_course_id)
                complete = complete and forum_complete
                warnings.extend(forum_warnings)
                for item in forum_items:
                    items[(item.kind, item.remote_id)] = item
            return FetchResult(
                tuple(items.values()), ("assignments", "quizzes", "announcements", "materials"),
                complete, tuple(dict.fromkeys(warnings)),
            )
        except PlaywrightTimeoutError:
            return FetchResult((), (), False, ("upstream_timeout",))
        except ValueError:
            return FetchResult((), (), False, ("unexpected_redirect",))
        finally:
            await context.close()

    async def _fetch_forum_discussions(self, page: Page, forum_url: str, course_id: str) -> tuple[bool, tuple[CourseItem, ...], tuple[str, ...]]:
        forum_id = parse_qs(urlparse(forum_url).query).get("id", [None])[0]
        if not forum_id:
            return False, (), ("elearn_forum_invalid_id",)
        queue = [forum_url]
        seen: set[str] = set()
        items: dict[str, CourseItem] = {}
        complete = True
        warnings: list[str] = []
        while queue and len(seen) < self.max_forum_pages:
            url = queue.pop(0)
            if url in seen:
                continue
            try:
                await self._goto(page, url)
            except PlaywrightTimeoutError:
                complete = False
                warnings.append("elearn_forum_upstream_timeout")
                break
            except ValueError:
                complete = False
                warnings.append("unexpected_redirect")
                break
            if await _page_session_expired(page):
                return False, tuple(items.values()), ("session_expired",)
            forum_root = page.locator("#page-mod-forum-view, .forumheaderlist, [data-region='forum-index'], .discussion-list")
            if await forum_root.count() == 0:
                complete = False
                warnings.append("elearn_forum_parser_changed")
                break
            seen.add(url)
            html = await page.content()
            parsed = parse_course_content(html, page.url, self.platform, course_id)
            for item in parsed.items:
                if item.kind == "announcement":
                    items[item.remote_id] = item
            for next_url in parse_moodle_forum_pages(html, page.url, forum_id):
                if next_url not in seen and next_url not in queue:
                    queue.append(next_url)
        if queue:
            complete = False
            warnings.append("elearn_forum_page_limit")
        return complete, tuple(items.values()), tuple(warnings)

    async def fetch_item_detail(self, storage_state: dict[str, Any], item_ref: CourseItem) -> FetchResult:
        parsed_url = urlparse(item_ref.url)
        allowed_paths = ("/mod/assign/", "/mod/quiz/", "/mod/forum/", "/mod/resource/", "/mod/page/", "/mod/url/")
        if parsed_url.scheme != "https" or parsed_url.hostname != "elearn.nthu.edu.tw" or not any(path in parsed_url.path for path in allowed_paths):
            return FetchResult((), (), False, ("invalid_item_ref",))
        context = await self.browser.new_context(storage_state=storage_state)
        page = await context.new_page()
        page.set_default_timeout(self.timeout_ms)
        try:
            await self._goto(page, item_ref.url)
            if await _page_session_expired(page):
                return FetchResult((), (), False, ("session_expired",))
            region = page.locator("#region-main").first
            await region.wait_for(state="visible")
            body = "\n".join((await region.inner_text()).splitlines())
            updated = item_ref
            if item_ref.kind == "assignment":
                due_text = _labeled_value(body, ("Due date", "截止日期", "截止時間", "到期"))
                due = parse_nthu_date(due_text)
                status, evidence = _submission_status(body)
                grade_text = _grade_text(body)
                grading_status = _labeled_value(body, ("Grading status", "評分狀態", "成績狀態"))
                if status == "submitted" and grade_text:
                    status = "graded"
                updated = replace(
                    updated, due_at=due.due_at, due_date=due.due_date,
                    date_precision=due.precision, source_date_text=due.source_text,
                    remote_status=status, remote_status_text=_labeled_value(body, ("Submission status", "繳交狀態", "提交狀態")),
                    status_evidence=evidence, grade_text=grade_text,
                    grading_status_text=grading_status,
                )
            elif item_ref.kind == "quiz":
                close_text = _labeled_value(body, ("Closes", "測驗關閉", "結束時間", "關閉"))
                closes = parse_nthu_date(close_text)
                status, evidence = _quiz_status(body)
                quiz_status_text = _quiz_attempt_status_text(body)
                updated = replace(
                    updated, closes_at=closes.due_at, closes_date=closes.due_date,
                    closes_precision=closes.precision, closes_source_text=closes.source_text,
                    remote_status=status, remote_status_text=quiz_status_text,
                    status_evidence=evidence, grade_text=_grade_text(body),
                )
            content_text = None
            published_at = None
            if item_ref.kind == "announcement":
                content = page.locator(
                    "[id^='post-content-'], .forumpost .post-content-container, "
                    ".forumpost [data-region='post-content'], .forumpost .content, .posting.fullpost"
                ).first
                await content.wait_for(state="visible")
                if await content.count():
                    content_text = "\n".join((await content.inner_text()).splitlines()).strip() or None
                time_node = page.locator("time[datetime]").first
                if await time_node.count():
                    raw_time = await time_node.get_attribute("datetime")
                    try:
                        published_at = datetime.fromisoformat((raw_time or "").replace("Z", "+00:00"))
                    except ValueError:
                        published_at = None
            detail = ItemDetail(updated, content_text, published_at)
            complete = item_ref.kind != "announcement" or content_text is not None
            return FetchResult((detail,), (f"{item_ref.kind}_detail",), complete, () if complete else ("parser_changed",))
        except PlaywrightTimeoutError:
            return FetchResult((), (), False, ("upstream_timeout",))
        except ValueError:
            return FetchResult((), (), False, ("unexpected_redirect",))
        finally:
            await context.close()


def create_adapter(browser: Browser, captcha_solver: CaptchaSolver | None = None) -> ElearnAdapter:
    return ElearnAdapter(browser, captcha_solver)


async def _page_session_expired(page: Page) -> bool:
    """Recognize Moodle's redirect and same-URL logged-out representations."""
    parsed = urlparse(page.url)
    if parsed.hostname == SSO_HOST or "/login/" in parsed.path:
        return True
    if parsed.hostname != "elearn.nthu.edu.tw":
        return False
    # The live site sends an expired /my/ session to this exact logged-out
    # landing URL before it renders an OAuth link. Keep the match narrow so
    # unrelated root pages still fail closed as unverified.
    if (
        parsed.scheme == "https"
        and parsed.path == "/"
        and parse_qs(parsed.query) == {"redirect": ["0"]}
        and not parsed.fragment
    ):
        return True
    if await page.locator("body.notloggedin").count() > 0:
        return True
    oauth_links = page.locator("a[href*='/auth/oauth2/login.php']")
    for index in range(await oauth_links.count()):
        href = await oauth_links.nth(index).get_attribute("href")
        target = urlparse(urljoin(page.url, href or ""))
        if (
            target.scheme == "https"
            and target.hostname == "elearn.nthu.edu.tw"
            and target.path == "/auth/oauth2/login.php"
            and parse_qs(target.query).get("id") == ["5"]
        ):
            return True
    login_form = page.locator(
        "body#page-login-index form[action*='/login/index.php'], "
        "body.notloggedin form#login"
    )
    return await login_form.count() > 0


def _unique_moodle_userid(values: list[object]) -> str | None:
    """Return one unambiguous positive numeric Moodle user id."""
    normalized: set[str] = set()
    for value in values:
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            candidate = str(value) if value > 0 else None
        elif isinstance(value, str) and value.isascii() and value.isdigit():
            number = int(value)
            candidate = str(number) if number > 0 else None
        else:
            candidate = None
        if candidate is not None:
            normalized.add(candidate)
    if len(normalized) != 1:
        return None
    return normalized.pop()


def _labeled_value(text: str, labels: tuple[str, ...]) -> str | None:
    for label in labels:
        match = re.search(rf"(?:^|\n)\s*{re.escape(label)}\s*[:：]?\s*([^\n]+)", text, re.I)
        if match:
            return match.group(1).strip()
    return None


def _submission_status(text: str) -> tuple[str, str | None]:
    value = (_labeled_value(text, ("Submission status", "繳交狀態", "提交狀態")) or "").casefold()
    # The live Moodle table renders a heading and a row label with the same
    # text before the actual value.  If the first labeled value is another
    # label, scan the following lines but still require an explicit personal
    # status marker.
    markers = (
        "submitted for grading", "已繳交供評分", "已提交供評分", "已繳交", "已提交",
        "draft", "草稿", "no attempt", "not submitted", "尚未繳交", "未提交",
        "目前尚無任何作業繳交",
    )
    if not any(marker in value for marker in markers):
        labels = {"submission status", "繳交狀態", "提交狀態"}
        lines = [line.strip().casefold() for line in text.splitlines() if line.strip()]
        for index, line in enumerate(lines):
            if line not in labels:
                continue
            candidate = next(
                (following for following in lines[index + 1:index + 4]
                 if following not in labels and any(marker in following for marker in markers)),
                None,
            )
            if candidate:
                value = candidate
                break
    if any(marker in value for marker in ("submitted for grading", "已繳交供評分", "已提交供評分", "已繳交", "已提交")):
        return "submitted", "personal_submission_status"
    if any(marker in value for marker in ("draft", "草稿")):
        return "draft", "personal_submission_status"
    if any(marker in value for marker in ("no attempt", "not submitted", "尚未繳交", "未提交", "目前尚無任何作業繳交")):
        return "not_submitted", "personal_submission_status"
    return "unknown", None


def _grade_text(text: str) -> str | None:
    # ``評分`` is intentionally excluded: it is a prefix of ``評分狀態`` and
    # caused "尚未評分" to be mistaken for a real grade on the live page.
    value = _labeled_value(text, ("Grade", "成績", "Your final grade", "你的最終成績"))
    if not value:
        final_grade = re.search(r"(?:您的最後成績是|你的最終成績(?:是|為)?|your final grade is)\s*([^\n]+)", text, re.I)
        value = final_grade.group(1).strip() if final_grade else None
    if not value:
        return None
    folded = value.casefold()
    if any(marker in folded for marker in ("not graded", "not yet graded", "尚未評分", "未評分", "-")) and folded.strip() in {
        "not graded", "not yet graded", "尚未評分", "未評分", "-"
    }:
        return None
    return value


def _quiz_status(text: str) -> tuple[str, str | None]:
    value = (_quiz_attempt_status_text(text) or "").casefold()
    if any(marker in value for marker in ("finished", "submitted", "已完成", "已經完成", "已繳交", "已提交", "已經提交")):
        return "completed", "personal_attempt_status"
    if any(marker in value for marker in ("in progress", "進行中", "未完成")):
        return "draft", "personal_attempt_status"
    return "unknown", None


def _quiz_attempt_status_text(text: str) -> str | None:
    labeled = _labeled_value(text, ("State", "測驗狀態", "作答狀態"))
    if labeled and any(marker in labeled.casefold() for marker in ("已經提交", "已經完成", "已完成", "已提交", "submitted", "finished", "completed", "in progress", "進行中")):
        return labeled
    for line in text.splitlines():
        value = line.strip()
        if any(marker in value.casefold() for marker in ("已經提交", "已經完成", "submitted", "finished")):
            return value
    return None


def _is_explicit_empty_catalog(text: str) -> bool:
    return " ".join(text.split()).casefold() in {
        "no courses", "you are not enrolled in any courses",
        "沒有課程", "無課程", "尚未選修任何課程",
    }


async def _catalog_has_more(page: Page) -> bool:
    controls = page.locator(
        "a[rel='next'], [data-region='paging-bar'] a[aria-label*='Next'], "
        "[data-region='paging-bar'] a[aria-label*='下一'], "
        "button[data-action='load-more'], [data-action='load-more']:not([disabled]), "
        "[data-region='more-courses'] button"
    )
    for index in range(await controls.count()):
        control = controls.nth(index)
        if await control.is_visible() and not await control.is_disabled():
            return True
    return False
