from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from typing import Any
from urllib.parse import urljoin, urlparse

from playwright.async_api import Browser, Page, TimeoutError as PlaywrightTimeoutError

from ecourse.parsing.dates import parse_nthu_date
from ecourse.parsing.html import normalize_course_code, parse_courses, parse_eeclass_activity_page

from .base import AdapterStatus, CaptchaSolver, CourseItem, FetchResult, ItemDetail, SessionVerification
from .nthu_sso import NthuSsoMixin, SSO_HOST


class EeclassAdapter(NthuSsoMixin):
    platform = "eeclass"
    allow_protected_session_without_remote_id = True
    login_url = "https://eeclass.nthu.edu.tw/service/oauthRedir"
    protected_url = "https://eeclass.nthu.edu.tw/dashboard"
    courses_url = "https://eeclass.nthu.edu.tw/dashboard"

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

    async def fetch_course(self, storage_state: dict[str, Any], remote_course_id: str) -> FetchResult:
        if not remote_course_id.isdigit():
            return FetchResult((), (), False, ("invalid_remote_course_id",))
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
            body = "\n".join((await main.inner_text()).splitlines())
            due_text = _labeled_value(body, ("繳交期限", "截止時間", "截止日期", "Due date", "End time"))
            if item_ref.kind == "assignment":
                report = page.locator("a[href*='/homework/report/']").first
                if await report.count():
                    href = await report.get_attribute("href")
                    if href:
                        await self._goto(page, urljoin(page.url, href))
                        report_main = page.locator("#fs-content, #page-main, main, #content, .page-content, [role='main']").first
                        if await report_main.count():
                            body = "\n".join((await report_main.inner_text()).splitlines())
            due = parse_nthu_date(due_text)
            status_text = _labeled_value(body, ("繳交狀態", "提交狀態", "作答狀態", "狀態", "Submission status", "Status"))
            if not status_text and "此作業已繳交" in body:
                status_text = "此作業已繳交"
            status = _personal_status(status_text, item_ref.kind)
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


def _labeled_value(text: str, labels: tuple[str, ...]) -> str | None:
    for label in labels:
        match = re.search(rf"(?:^|\n)\s*{re.escape(label)}\s*[:：]?\s*([^\n]+)", text, re.I)
        if match:
            return match.group(1).strip()
    return None


def _personal_status(value: str | None, kind: str) -> str:
    folded = (value or "").casefold()
    if any(marker in folded for marker in ("未繳交", "未提交", "not submitted", "no attempt")):
        return "not_submitted"
    if any(marker in folded for marker in ("草稿", "draft", "進行中", "in progress")):
        return "draft"
    if any(marker in folded for marker in ("已繳交", "已提交", "submitted")):
        return "submitted"
    if any(marker in folded for marker in ("已完成", "finished", "completed")):
        return "completed" if kind == "quiz" else "submitted"
    return "unknown"
