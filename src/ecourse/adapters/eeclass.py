from __future__ import annotations

import asyncio
import os
import re
from dataclasses import replace
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.async_api import Browser, Page, TimeoutError as PlaywrightTimeoutError

from ecourse.parsing.dates import parse_nthu_date
from ecourse.parsing.html import normalize_course_code, parse_courses, parse_eeclass_activity_page

from .base import AdapterStatus, CaptchaSolver, CourseItem, FetchResult, ItemDetail, SessionVerification
from .eeclass_http import EeclassHttp, NeedsBrowser, RateLimited, SessionExpired, UpstreamTimeout, html_to_text
from .nthu_sso import NthuSsoMixin, SSO_HOST

_MAIN_SELECTOR = "#fs-content, #page-main, main, #content, .page-content, [role='main']"
_COURSE_REGION_SELECTOR = "[data-role*='course'], .course-list, #course-list, [class*='courseList']"
_ROUTES = {
    "assignment": "homeworkList",
    "quiz": "examList",
    "announcement": "bulletin",
    "material": "material",
}


class EeclassAdapter(NthuSsoMixin):
    platform = "eeclass"
    allow_protected_session_without_remote_id = True
    login_url = "https://eeclass.nthu.edu.tw/service/oauthRedir"
    protected_url = "https://eeclass.nthu.edu.tw/dashboard"
    courses_url = "https://eeclass.nthu.edu.tw/dashboard"

    def __init__(self, browser, captcha_solver=None, timeout_ms: int = 30_000, *, use_http: bool | None = None, http_transport=None) -> None:
        super().__init__(browser, captcha_solver, timeout_ms)
        # Plain HTTP for the authenticated pages; the browser is the fallback.
        # ECOURSE_EECLASS_HTTP=0 turns the HTTP path off.
        self.use_http = os.environ.get("ECOURSE_EECLASS_HTTP", "1") != "0" if use_http is None else use_http
        self.http_transport = http_transport

    def _http(self, storage_state: dict[str, Any]) -> EeclassHttp | None:
        if not self.use_http:
            return None
        session = EeclassHttp(storage_state, transport=self.http_transport, timeout=min(self.timeout_ms / 1000, 25.0))
        return session if session.usable else None

    async def _verify_page(self, page: Page) -> SessionVerification:
        if urlparse(page.url).hostname != "eeclass.nthu.edu.tw" or urlparse(page.url).path == "/":
            await self._goto(page, self.protected_url)
        if urlparse(page.url).hostname == SSO_HOST or "/service/oauth" in urlparse(page.url).path.lower():
            return SessionVerification(AdapterStatus.SESSION_EXPIRED, error_code="session_expired")
        parsed = urlparse(page.url)
        if parsed.hostname == "eeclass.nthu.edu.tw" and await _has_official_sso_login_link(page):
            return SessionVerification(AdapterStatus.SESSION_EXPIRED, error_code="session_expired")
        if parsed.hostname != "eeclass.nthu.edu.tw" or not parsed.path.startswith("/dashboard"):
            return SessionVerification(AdapterStatus.PARSER_CHANGED, error_code="protected_page_not_reached")
        logout = page.locator("a[href*='logout'], [data-action='logout']")
        identity_links = page.locator("a[href*='/user/'], a[href*='/profile/'], [data-user-id]")
        if await logout.count() == 0 or await identity_links.count() == 0:
            return SessionVerification(
                AdapterStatus.CONNECTED, None,
                "受控 SSO context 已進入受保護的 eeclass dashboard",
            )
        remote_ids: set[str] = set()
        for index in range(await identity_links.count()):
            node = identity_links.nth(index)
            raw = (await node.get_attribute("data-user-id")) or (await node.get_attribute("href")) or ""
            match = re.search(r"(?:user|profile)(?:/info)?/(\d+)|[?&](?:id|userId)=(\d+)", raw, re.I)
            if match:
                remote_ids.add(next(value for value in match.groups() if value))
        if len(remote_ids) > 1:
            return SessionVerification(AdapterStatus.PARSER_CHANGED, error_code="identity_not_verified")
        remote_id = next(iter(remote_ids), None)
        return SessionVerification(AdapterStatus.CONNECTED, remote_id, "受保護 dashboard 同時顯示登出控制與平台 user/profile ID")

    async def _verify_expected_account(self, page: Page, expected_account: str) -> bool:
        profile = page.locator("a[href*='/user/'], a[href*='/profile/']").first
        if await profile.count():
            href = await profile.get_attribute("href")
            if href:
                await self._goto(page, urljoin(page.url, href))
        expected = expected_account.strip().casefold()
        selectors = (
            "[data-account]", "[data-username]", "[data-login-id]",
            "dt:has-text('帳號') + dd", "dt:has-text('Account') + dd",
            "th:has-text('帳號') + td", "th:has-text('Account') + td",
        )
        for selector in selectors:
            nodes = page.locator(selector)
            for index in range(await nodes.count()):
                node = nodes.nth(index)
                candidate = (
                    (await node.get_attribute("data-account"))
                    or (await node.get_attribute("data-username"))
                    or (await node.get_attribute("data-login-id"))
                    or (await node.inner_text())
                )
                if candidate.strip().casefold() == expected:
                    return True
        return False

    async def discover_courses(self, storage_state: dict[str, Any]) -> FetchResult:
        http = self._http(storage_state)
        if http is not None:
            try:
                result = await self._discover_courses_http(http)
            except NeedsBrowser:
                result = None
            finally:
                await http.aclose()
            if result is not None:
                return result
        context = await self.browser.new_context(storage_state=storage_state)
        page = await context.new_page()
        page.set_default_timeout(self.timeout_ms)
        try:
            await self._goto(page, self.courses_url)
            verification = await self._verify_page(page)
            if verification.status != AdapterStatus.CONNECTED:
                return FetchResult((), (), False, (verification.error_code or verification.status.value,))
            courses = parse_courses(await page.content(), page.url, self.platform)
            course_region = page.locator("[data-role*='course'], .course-list, #course-list, [class*='courseList']")
            if not courses and await course_region.count() == 0:
                return FetchResult((), (), False, ("parser_changed", "eeclass_protected_catalog_not_verified_with_real_account"))
            semaphore = asyncio.Semaphore(6)

            async def enrich(course):
                if course.course_code:
                    return course
                detail_page = await context.new_page()
                detail_page.set_default_timeout(self.timeout_ms)
                try:
                    async with semaphore:
                        await asyncio.wait_for(
                            self._goto(detail_page, f"https://eeclass.nthu.edu.tw/course/info/{course.remote_id}"),
                            timeout=6,
                        )
                    body = "\n".join((await detail_page.locator("body").inner_text()).splitlines())
                    code = normalize_course_code(_labeled_value(body, ("課程代碼", "課號", "科號", "Course code")))
                    return replace(course, course_code=code)
                except (PlaywrightTimeoutError, TimeoutError, ValueError):
                    return course
                finally:
                    await detail_page.close()

            courses = tuple(await asyncio.gather(*(enrich(course) for course in courses)))
            complete = all(course.course_code for course in courses) or not courses
            warnings = () if complete else ("eeclass_course_code_incomplete",)
            return FetchResult(courses, ("course_catalog", "course_codes"), complete, warnings)
        except PlaywrightTimeoutError:
            return FetchResult((), (), False, ("upstream_timeout",))
        finally:
            await context.close()

    async def _discover_courses_http(self, http: EeclassHttp) -> FetchResult:
        try:
            html, final = await http.get_text(self.courses_url)
        except SessionExpired:
            return FetchResult((), (), False, ("session_expired",))
        except UpstreamTimeout:
            return FetchResult((), (), False, ("upstream_timeout",))
        except RateLimited:
            return FetchResult((), (), False, ("upstream_rate_limited",))
        if not urlparse(final).path.startswith("/dashboard"):
            raise NeedsBrowser
        courses = parse_courses(html, final, self.platform)
        if not courses and BeautifulSoup(html, "html.parser").select_one(_COURSE_REGION_SELECTOR) is None:
            raise NeedsBrowser  # let the browser path decide whether this is a parser change

        async def enrich(course):
            if course.course_code:
                return course
            try:
                page_html, _final = await asyncio.wait_for(http.get_text(f"https://eeclass.nthu.edu.tw/course/info/{course.remote_id}"), timeout=6)
            except (TimeoutError, UpstreamTimeout, SessionExpired, RateLimited, NeedsBrowser, ValueError):
                return course
            body = html_to_text(BeautifulSoup(page_html, "html.parser"))
            code = normalize_course_code(_labeled_value(body, ("課程代碼", "課號", "科號", "Course code")))
            return replace(course, course_code=code)

        courses = tuple(await asyncio.gather(*(enrich(course) for course in courses)))
        complete = all(course.course_code for course in courses) or not courses
        warnings = () if complete else ("eeclass_course_code_incomplete",)
        return FetchResult(courses, ("course_catalog", "course_codes"), complete, warnings)

    async def _fetch_course_http(self, http: EeclassHttp, remote_course_id: str) -> FetchResult:
        async def fetch_list(kind: str, route: str):
            try:
                html, final = await http.get_text(f"https://eeclass.nthu.edu.tw/course/{route}/{remote_course_id}")
            except SessionExpired:
                return kind, (), False, "session_expired"
            except UpstreamTimeout:
                return kind, (), False, f"eeclass_{kind}_upstream_timeout"
            except RateLimited:
                return kind, (), False, "upstream_rate_limited"
            if BeautifulSoup(html, "html.parser").select_one("#fs-content, #page-main, body.body-course") is None:
                raise NeedsBrowser  # the rendered DOM may differ; let the browser path decide
            return kind, parse_eeclass_activity_page(html, final, remote_course_id, kind), True, None

        pages = await asyncio.gather(*(fetch_list(kind, route) for kind, route in _ROUTES.items()))
        items = tuple(item for _kind, records, _complete, _warning in pages for item in records)
        warnings = tuple(warning for _kind, _records, _complete, warning in pages if warning)
        coverage = tuple(kind for kind, _records, complete, _warning in pages if complete)
        return FetchResult(items, coverage, all(complete for _kind, _records, complete, _warning in pages), warnings)

    async def _fetch_item_detail_http(self, http: EeclassHttp, item_ref: CourseItem) -> FetchResult:
        try:
            if item_ref.kind == "announcement":
                return await self._announcement_detail_http(http, item_ref)
            html, final = await http.get_text(item_ref.url)
            soup = BeautifulSoup(html, "html.parser")
            main = soup.select_one(_MAIN_SELECTOR)
            if main is None:
                raise NeedsBrowser
            report_text = None
            if item_ref.kind == "assignment":
                link = soup.select_one("a[href*='/homework/report/']")
                href = link.get("href") if link else None
                if href:
                    report_html, _report_final = await http.get_text(urljoin(final, str(href)))
                    report_main = BeautifulSoup(report_html, "html.parser").select_one(_MAIN_SELECTOR)
                    if report_main is not None:
                        report_text = html_to_text(report_main)
            return _assignment_detail(item_ref, html_to_text(main), report_text)
        except SessionExpired:
            return FetchResult((), (), False, ("session_expired",))
        except UpstreamTimeout:
            return FetchResult((), (), False, ("upstream_timeout",))
        except RateLimited:
            return FetchResult((), (), False, ("upstream_rate_limited",))
        except ValueError:
            return FetchResult((), (), False, ("unexpected_redirect",))

    async def _announcement_detail_http(self, http: EeclassHttp, item_ref: CourseItem) -> FetchResult:
        list_html, list_final = await http.get_cached_text(item_ref.url)
        soup = BeautifulSoup(list_html, "html.parser")
        anchors = soup.select("a.fs-bulletin-item, .fs-bulletin-item a")
        candidates = [
            (index, node) for index, node in enumerate(anchors)
            if " ".join(node.get_text(" ", strip=True).split()) == item_ref.title
        ]
        suffix = str(item_ref.remote_id).rsplit(":", 1)[-1]
        wanted_index = int(suffix) - 1 if suffix.isdigit() else None
        target = next((node for index, node in candidates if index == wanted_index), None)
        target = target or (candidates[0][1] if candidates else None)
        if target is None:
            return FetchResult((), (), False, ("announcement_not_found",))
        data_url = target.get("data-url")
        content_url = urljoin(list_final, str(data_url)) if data_url else ""
        if not content_url or urlparse(content_url).path != "/bulletin/content/":
            raise NeedsBrowser
        html, _final = await http.get_text(content_url)
        document = BeautifulSoup(html, "html.parser")
        content_text = html_to_text(document.body or document, keep_paragraph_breaks=True) or None
        return FetchResult(
            (ItemDetail(item_ref, content_text, None),),
            ("announcement_detail",), bool(content_text),
            () if content_text else ("announcement_body_empty",),
        )

    async def fetch_course(self, storage_state: dict[str, Any], remote_course_id: str) -> FetchResult:
        if not remote_course_id.isdigit():
            return FetchResult((), (), False, ("invalid_remote_course_id",))
        http = self._http(storage_state)
        if http is not None:
            try:
                result = await self._fetch_course_http(http, remote_course_id)
            except NeedsBrowser:
                result = None
            finally:
                await http.aclose()
            if result is not None:
                return result
        context = await self.browser.new_context(storage_state=storage_state)
        page = await context.new_page()
        page.set_default_timeout(self.timeout_ms)
        try:
            await self._goto(page, f"https://eeclass.nthu.edu.tw/course/{remote_course_id}")
            if urlparse(page.url).hostname == SSO_HOST or "/service/oauth" in urlparse(page.url).path.lower():
                return FetchResult((), (), False, ("session_expired",))
            if await _has_official_sso_login_link(page):
                return FetchResult((), (), False, ("session_expired",))
            course_root = page.locator(
                "#fs-content, #page-main, body.body-course, [data-course-id], "
                "main, [role='main'], .page-content"
            )
            if await course_root.count() == 0:
                return FetchResult((), (), False, ("eeclass_course_root_unrecognized",))

            routes = {
                "assignment": "homeworkList",
                "quiz": "examList",
                "announcement": "bulletin",
                "material": "material",
            }

            async def fetch_list(kind: str, route: str):
                detail_page = await context.new_page()
                detail_page.set_default_timeout(self.timeout_ms)
                try:
                    await self._goto(detail_page, f"https://eeclass.nthu.edu.tw/course/{route}/{remote_course_id}")
                    if urlparse(detail_page.url).hostname == SSO_HOST or await _has_official_sso_login_link(detail_page):
                        return kind, (), False, "session_expired"
                    root = detail_page.locator("#fs-content, #page-main, body.body-course")
                    if await root.count() == 0:
                        return kind, (), False, f"eeclass_{kind}_root_unrecognized"
                    records = parse_eeclass_activity_page(await detail_page.content(), detail_page.url, remote_course_id, kind)
                    return kind, records, True, None
                except PlaywrightTimeoutError:
                    return kind, (), False, f"eeclass_{kind}_upstream_timeout"
                finally:
                    await detail_page.close()

            pages = await asyncio.gather(*(fetch_list(kind, route) for kind, route in routes.items()))
            items = tuple(item for _kind, records, _complete, _warning in pages for item in records)
            warnings = tuple(warning for _kind, _records, _complete, warning in pages if warning)
            coverage = tuple(kind for kind, _records, complete, _warning in pages if complete)
            complete = all(complete for _kind, _records, complete, _warning in pages)
            return FetchResult(items, coverage, complete, warnings)
        except PlaywrightTimeoutError:
            return FetchResult((), (), False, ("upstream_timeout",))
        finally:
            await context.close()

    async def fetch_item_detail(self, storage_state: dict[str, Any], item_ref: CourseItem) -> FetchResult:
        parsed = urlparse(item_ref.url)
        if parsed.scheme != "https" or parsed.hostname != "eeclass.nthu.edu.tw":
            return FetchResult((), (), False, ("invalid_item_ref",))
        http = self._http(storage_state)
        if http is not None:
            try:
                result = await self._fetch_item_detail_http(http, item_ref)
            except NeedsBrowser:
                result = None
            finally:
                await http.aclose()
            if result is not None:
                return result
        context = await self.browser.new_context(storage_state=storage_state)
        page = await context.new_page()
        page.set_default_timeout(self.timeout_ms)
        try:
            await self._goto(page, item_ref.url)
            if urlparse(page.url).hostname == SSO_HOST or await _has_official_sso_login_link(page):
                return FetchResult((), (), False, ("session_expired",))
            main = page.locator("#fs-content, #page-main, main, #content, .page-content, [role='main']").first
            if await main.count() == 0:
                return FetchResult((), (), False, ("parser_changed",))
            if item_ref.kind == "announcement":
                announcements = page.locator("a.fs-bulletin-item, .fs-bulletin-item a")
                candidates = []
                for index in range(await announcements.count()):
                    node = announcements.nth(index)
                    title = " ".join((await node.inner_text()).split())
                    if title == item_ref.title:
                        candidates.append((index, node))
                suffix = str(item_ref.remote_id).rsplit(":", 1)[-1]
                wanted_index = int(suffix) - 1 if suffix.isdigit() else None
                target = next((node for index, node in candidates if index == wanted_index), None)
                target = target or (candidates[0][1] if candidates else None)
                if target is None:
                    return FetchResult((), (), False, ("announcement_not_found",))
                await target.click()
                modal = page.locator("#mod_bulletin_contentModal_course_" + str(item_ref.remote_id).split(":", 1)[0])
                if await modal.count() == 0:
                    modal = page.locator(".mod_bulletin .modal:visible, .modal:visible").last
                iframe = modal.locator("iframe.fs-modal-iframe").first
                await iframe.wait_for(state="attached")
                handle = await iframe.element_handle()
                frame = await handle.content_frame() if handle else None
                if frame is None:
                    return FetchResult((), (), False, ("announcement_frame_missing",))
                await frame.wait_for_load_state("domcontentloaded")
                frame_body = frame.locator("body").first
                await frame_body.wait_for(state="visible")
                content_text = "\n".join((await frame_body.inner_text()).splitlines()).strip() or None
                return FetchResult(
                    (ItemDetail(item_ref, content_text, None),),
                    ("announcement_detail",), bool(content_text),
                    () if content_text else ("announcement_body_empty",),
                )
            main_text = "\n".join((await main.inner_text()).splitlines())
            report_text = None
            if item_ref.kind == "assignment":
                report = page.locator("a[href*='/homework/report/']").first
                if await report.count():
                    href = await report.get_attribute("href")
                    if href:
                        await self._goto(page, urljoin(page.url, href))
                        report_main = page.locator("#fs-content, #page-main, main, #content, .page-content, [role='main']").first
                        if await report_main.count():
                            report_text = "\n".join((await report_main.inner_text()).splitlines())
            return _assignment_detail(item_ref, main_text, report_text)
        except PlaywrightTimeoutError:
            return FetchResult((), (), False, ("upstream_timeout",))
        except ValueError:
            return FetchResult((), (), False, ("unexpected_redirect",))
        finally:
            await context.close()


def create_adapter(browser: Browser, captcha_solver: CaptchaSolver | None = None) -> EeclassAdapter:
    return EeclassAdapter(browser, captcha_solver)


async def _has_official_sso_login_link(page: Page) -> bool:
    links = page.locator("a[href*='/service/oauthRedir']")
    for index in range(await links.count()):
        href = await links.nth(index).get_attribute("href")
        target = urlparse(urljoin(page.url, href or ""))
        if (
            target.scheme == "https"
            and target.hostname == "eeclass.nthu.edu.tw"
            and target.path.lower() == "/service/oauthredir"
        ):
            return True
    return False


def _assignment_detail(item_ref: CourseItem, main_text: str, report_text: str | None) -> FetchResult:
    """Personal status, due date and grade from a homework/exam page (and its report page)."""
    due_text = _labeled_value(main_text, ("繳交期限", "截止時間", "截止日期", "Due date", "End time"))
    body = main_text if report_text is None else report_text
    due = parse_nthu_date(due_text)
    status_text = _labeled_value(body, ("繳交狀態", "提交狀態", "作答狀態", "狀態", "Submission status", "Status"))
    status = _personal_status(status_text, item_ref.kind)
    # The report page's generic "狀態" row is the grading state, so the
    # explicit submitted banner must win when it did not decide.
    if status == "unknown" and "此作業已繳交" in body:
        status = "submitted"
        status_text = status_text or "此作業已繳交"
    grade = _labeled_value(body, ("成績", "得分", "Grade", "Score"))
    if grade and grade.strip().casefold() in {"-", "尚未評分", "未評分", "not graded"}:
        grade = None
    if status == "submitted" and grade:
        status = "graded"
    updated = replace(
        item_ref, due_at=due.due_at, due_date=due.due_date,
        date_precision=due.precision, source_date_text=due.source_text,
        remote_status=status, remote_status_text=status_text,
        status_evidence="personal_submission_status" if status != "unknown" else None,
        grade_text=grade,
    )
    return FetchResult((updated,), (f"{item_ref.kind}_detail",), True)


def _labeled_value(text: str, labels: tuple[str, ...]) -> str | None:
    for label in labels:
        # The label must end at a word boundary so "成績" does not match "成績比重".
        match = re.search(rf"(?:^|\n)\s*{re.escape(label)}(?!\w)\s*[:：]?\s*([^\n]+)", text, re.I)
        if match:
            return match.group(1).strip()
    return None


def _personal_status(value: str | None, kind: str) -> str:
    folded = (value or "").casefold()
    if any(marker in folded for marker in ("未繳交", "未提交", "not submitted", "no attempt")):
        return "not_submitted"
    if any(marker in folded for marker in ("草稿", "draft", "進行中", "in progress")):
        return "draft"
    if "已批改" in folded or "已評分" in folded:
        return "graded"
    if any(marker in folded for marker in ("尚未批改", "待批改", "未批改", "批改中")):
        # A grading state only exists once the work has been handed in.
        return "submitted"
    if any(marker in folded for marker in ("已繳交", "已提交", "submitted")):
        return "submitted"
    if any(marker in folded for marker in ("已完成", "finished", "completed")):
        return "completed" if kind == "quiz" else "submitted"
    return "unknown"
