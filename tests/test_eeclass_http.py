"""The eeclass adapter reads authenticated pages over plain HTTP, with the browser as fallback."""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from bs4 import BeautifulSoup

from ecourse.adapters.base import CourseItem
from ecourse.adapters.eeclass import EeclassAdapter, _labeled_value
from ecourse.adapters.eeclass_http import html_to_text, is_challenge_page

FIXTURES = Path(__file__).parent / "fixtures" / "platforms"
BASE = "https://eeclass.nthu.edu.tw"


class NoBrowser:
    """Fails loudly if the HTTP path ever falls back to the browser."""

    async def new_context(self, **_kwargs):
        raise RuntimeError("browser used")


def state(value: str = "v1"):
    return {"cookies": [
        {"name": "eeclass_session", "value": value, "domain": "eeclass.nthu.edu.tw", "path": "/"},
        {"name": "sso", "value": "must-not-be-sent", "domain": "oauth.ccxp.nthu.edu.tw", "path": "/"},
    ]}


def adapter(handler, browser=None):
    return EeclassAdapter(browser or NoBrowser(), use_http=True, http_transport=httpx.MockTransport(handler))


def page(body: str) -> httpx.Response:
    return httpx.Response(200, html=f"<html><body class='body-course'><div id='fs-content'>{body}</div></body></html>")


def test_html_to_text_keeps_table_rows_as_label_value_lines():
    soup = BeautifulSoup(
        "<table><tr><th>繳交期限</th><td>2026-09-15 18:30</td></tr><tr><th>成績比重</th><td>0%</td></tr></table>"
        "<dl><dt>狀態</dt><dd>尚未批改</dd></dl><script>ignored()</script>", "html.parser",
    )
    text = html_to_text(soup)
    prose = html_to_text(BeautifulSoup("<div>人氣: 3</div><p>第一段</p><p>第二段</p>", "html.parser"), keep_paragraph_breaks=True)
    assert prose.split("\n") == ["人氣: 3", "", "第一段", "", "第二段"]
    assert "繳交期限\t2026-09-15 18:30" in text and "ignored" not in text
    assert _labeled_value(text, ("繳交期限",)) == "2026-09-15 18:30"
    assert _labeled_value(text, ("成績",)) is None  # "成績比重" is not a grade
    assert _labeled_value(text, ("狀態",)) == "尚未批改"


def test_challenge_page_is_recognised():
    assert is_challenge_page("<html>Please enable JavaScript to view the page content. Your support ID is: 1</html>")
    assert not is_challenge_page("<html>" + "x" * 30_000 + "</html>")


def test_course_lists_are_fetched_over_http_without_a_browser():
    seen = []
    routes = {
        "/course/homeworkList/101": "eeclass_homework_list.html",
        "/course/examList/101": "eeclass_exam_list.html",
        "/course/bulletin/101": "eeclass_bulletin_list.html",
        "/course/material/101": "eeclass_material_list.html",
    }

    def handler(request):
        seen.append(request)
        return httpx.Response(200, text=(FIXTURES / routes[request.url.path]).read_text())

    result = asyncio.run(adapter(handler).fetch_course(state(), "101"))
    assert result.complete and set(result.coverage) == {"assignment", "quiz", "announcement", "material"}
    assert {item.kind for item in result.records} >= {"assignment", "announcement", "material"}
    assert len(seen) == 4
    assert all(request.headers["cookie"] == "eeclass_session=v1" for request in seen)  # no SSO cookie leaks


def test_expired_session_is_reported_without_using_the_browser():
    def handler(request):
        if request.url.host == "oauth.ccxp.nthu.edu.tw":
            return httpx.Response(200, html="<html>login</html>")
        return httpx.Response(302, headers={"location": "https://oauth.ccxp.nthu.edu.tw/v1.1/authorize.php"})

    result = asyncio.run(adapter(handler).fetch_course(state(), "101"))
    assert not result.complete and "session_expired" in result.warnings


def test_bot_challenge_falls_back_to_the_browser_instead_of_failing_or_bypassing():
    challenge = "<html><body>Please enable JavaScript to view the page content. Your support ID is: 9</body></html>"
    with pytest.raises(RuntimeError, match="browser used"):
        asyncio.run(adapter(lambda request: httpx.Response(200, html=challenge)).fetch_course(state(), "101"))


def test_session_without_eeclass_cookies_uses_the_browser():
    with pytest.raises(RuntimeError, match="browser used"):
        asyncio.run(adapter(lambda request: page("x")).fetch_course({"cookies": []}, "101"))


def test_timeout_is_a_partial_result_not_a_browser_retry():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    result = asyncio.run(adapter(handler).fetch_course(state(), "101"))
    assert not result.complete
    assert "eeclass_assignment_upstream_timeout" in result.warnings


def test_assignment_status_comes_from_the_report_page_over_http():
    homework = (FIXTURES / "eeclass_homework.html").read_text()
    report = (FIXTURES / "eeclass_homework_report.html").read_text()

    def handler(request):
        if request.url.path.startswith("/homework/report/"):
            return httpx.Response(200, text=report)
        return httpx.Response(200, text=homework)

    ref = CourseItem(remote_id="101", kind="assignment", title="作業", url=f"{BASE}/course/homework/101")
    result = asyncio.run(adapter(handler).fetch_item_detail(state(), ref))
    (updated,) = result.records
    assert result.complete
    assert updated.remote_status == "submitted"
    assert updated.remote_status_text == "尚未批改"


BULLETIN_LIST = """
<html><body class="body-course"><div id="fs-content">
<a href="####" class="fs-bulletin-item" data-url="/bulletin/content/?id=1&pageId=course.101&ajaxAuth=a">第一則</a>
<a href="####" class="fs-bulletin-item" data-url="/bulletin/content/?id=2&pageId=course.101&ajaxAuth=b">第二則</a>
</div></body></html>
"""


def test_announcement_body_is_read_from_its_content_url_and_the_list_is_fetched_once():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/course/bulletin/101":
            return httpx.Response(200, html=BULLETIN_LIST)
        assert request.url.path == "/bulletin/content/"
        return httpx.Response(200, html=f"<html><head><title>公告</title></head><body><p>內文 {request.url.params['id']}</p></body></html>")

    http_adapter = adapter(handler)
    refs = [CourseItem(remote_id=f"101:{n}", kind="announcement", title=title, url=f"{BASE}/course/bulletin/101") for n, title in ((1, "第一則"), (2, "第二則"))]

    async def run():
        return await asyncio.gather(*(http_adapter.fetch_item_detail(state("announcements"), ref) for ref in refs))

    first, second = asyncio.run(run())
    assert first.records[0].content_text == "內文 1" and first.complete
    assert second.records[0].content_text == "內文 2"
    assert calls.count("/course/bulletin/101") == 1
    assert calls.count("/bulletin/content/") == 2


def test_announcement_content_url_must_stay_on_the_bulletin_endpoint():
    html = BULLETIN_LIST.replace("/bulletin/content/?id=1&pageId=course.101&ajaxAuth=a", "/user/secret?id=1")
    ref = CourseItem(remote_id="101:1", kind="announcement", title="第一則", url=f"{BASE}/course/bulletin/101")
    with pytest.raises(RuntimeError, match="browser used"):
        asyncio.run(adapter(lambda request: httpx.Response(200, html=html)).fetch_item_detail(state("unsafe"), ref))


def test_dashboard_courses_and_codes_over_http():
    dashboard = (FIXTURES / "eeclass_dashboard.html").read_text()

    def handler(request):
        if request.url.path == "/dashboard":
            return httpx.Response(200, text=dashboard)
        return httpx.Response(200, html="<html><body><table><tr><th>課程代碼</th><td>11510CS135601</td></tr></table></body></html>")

    result = asyncio.run(adapter(handler).discover_courses(state()))
    assert result.records and result.complete
    assert all(course.course_code == "11510CS135601" for course in result.records)
