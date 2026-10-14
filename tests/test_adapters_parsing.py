import asyncio
from datetime import datetime
from pathlib import Path

import pytest

from ecourse.adapters.base import DatePrecision
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from ecourse.adapters.elearn import ElearnAdapter, _grade_text, _is_explicit_empty_catalog, _labeled_value, _quiz_status, _submission_status
from ecourse.adapters.eeclass import _labeled_value as _eeclass_labeled_value, _personal_status
from ecourse.parsing import parse_course_content, parse_courses, parse_eeclass_activity_page, parse_moodle_forum_pages, parse_nthu_date
from ecourse.parsing.html import normalize_course_code


ELEARN_COURSES = """
<main data-region="courses-view">
  <a class="aalink coursename" href="/course/view.php?id=101"><span>資料結構</span></a>
  <a href="/course/view.php?id=102">線性代數</a>
  <a href="/course/view.php?id=101">資料結構</a>
</main>
"""

EECLASS_COURSES = """
<section class="course-list">
  <a class="course" href="/course/home/501">編譯器</a>
  <a href="https://eeclass.nthu.edu.tw/course/info/502">作業系統</a>
</section>
"""

ELEARN_CONTENT = """
<main id="page-course-view-topics">
 <a href="/mod/assign/view.php?id=9001">作業一</a>
 <a href="/mod/quiz/view.php?id=9002">期中測驗</a>
 <a href="/mod/forum/discuss.php?d=9003">最新公告</a>
 <a href="/mod/resource/view.php?id=9004">第一週教材</a>
 <a href="/mod/assign/view.php?id=9001"><span>作業一</span></a>
</main>
"""

FIXTURES = Path(__file__).parent / "fixtures" / "platforms"


def test_parse_elearn_complete_course_catalog_and_deduplicate() -> None:
    records = parse_courses(ELEARN_COURSES, "https://elearn.nthu.edu.tw/my/courses.php", "elearn")
    assert [(row.remote_id, row.title) for row in records] == [("101", "資料結構"), ("102", "線性代數")]


def test_parse_eeclass_known_course_routes() -> None:
    records = parse_courses(EECLASS_COURSES, "https://eeclass.nthu.edu.tw/dashboard", "eeclass")
    assert [row.remote_id for row in records] == ["501", "502"]


def test_real_eeclass_dashboard_fixture_uses_course_root_route() -> None:
    html = (FIXTURES / "eeclass_dashboard.html").read_text()
    records = parse_courses(html, "https://eeclass.nthu.edu.tw/dashboard", "eeclass")
    assert records
    assert records[0].url == "https://eeclass.nthu.edu.tw/course/101"


@pytest.mark.parametrize(
    ("filename", "kind", "path"),
    [
        ("eeclass_homework_list.html", "assignment", "/course/homework/101"),
        ("eeclass_bulletin_list.html", "announcement", "/course/bulletin/101"),
        ("eeclass_material_list.html", "material", "/media/doc/101"),
    ],
)
def test_real_eeclass_activity_fixtures_cover_protected_routes(filename, kind, path) -> None:
    html = (FIXTURES / filename).read_text()
    records = parse_eeclass_activity_page(html, f"https://eeclass.nthu.edu.tw/course/{kind}/101", "101", kind)
    assert records
    assert any(path in record.url for record in records)


def test_real_eeclass_report_fixture_proves_personal_submitted_ungraded_state() -> None:
    from bs4 import BeautifulSoup

    html = (FIXTURES / "eeclass_homework_report.html").read_text()
    body = BeautifulSoup(html, "html.parser").get_text("\n", strip=True)
    assert "此作業已繳交" in body
    assert _eeclass_labeled_value(body, ("狀態",)) == "尚未批改"
    assert _personal_status("此作業已繳交", "assignment") == "submitted"


def test_real_elearn_dashboard_and_status_fixtures_regress_live_structure() -> None:
    from bs4 import BeautifulSoup

    dashboard = (FIXTURES / "elearn_dashboard.html").read_text()
    assert parse_courses(dashboard, "https://elearn.nthu.edu.tw/my/index.php", "elearn")
    assert BeautifulSoup(dashboard, "html.parser").select_one("a[href='/login/logout.php']")

    open_body = BeautifulSoup(
        (FIXTURES / "elearn_assignment_open.html").read_text(), "html.parser"
    ).get_text("\n", strip=True)
    assert _submission_status(open_body) == ("not_submitted", "personal_submission_status")

    submitted_body = BeautifulSoup(
        (FIXTURES / "elearn_assignment_submitted.html").read_text(), "html.parser"
    ).get_text("\n", strip=True)
    assert _submission_status(submitted_body) == ("submitted", "personal_submission_status")

    completed_quiz = BeautifulSoup(
        (FIXTURES / "elearn_quiz_completed.html").read_text(), "html.parser"
    ).get_text("\n", strip=True)
    assert _quiz_status(completed_quiz) == ("completed", "personal_attempt_status")


def test_course_catalog_extracts_official_code_from_attributes_and_row_context() -> None:
    elearn = parse_courses(
        '<a class="course" data-course-code="11510 CS 135601" href="/course/view.php?id=1">程式設計…</a>',
        "https://elearn.nthu.edu.tw/my/courses.php", "elearn",
    )
    eeclass = parse_courses(
        '<tr><td>課程代碼：11510CS135601</td><td><a href="/course/home/2">程式設計</a></td></tr>',
        "https://eeclass.nthu.edu.tw/dashboard", "eeclass",
    )
    assert elearn[0].course_code == eeclass[0].course_code == "11510CS135601"
    assert normalize_course_code("CHE 5001 分子工程") == "CHE5001"
    assert normalize_course_code("11510PHYS 113301-07 [元課程]") == "11510PHYS113301"
    assert normalize_course_code("2026 離散數學") is None


def test_login_form_is_not_misread_as_empty_catalog() -> None:
    login = '<form><input name="id"><input name="password"><img id="captcha_image"></form>'
    assert parse_courses(login, "https://oauth.ccxp.nthu.edu.tw/v1.1/authorize.php", "elearn") == ()


def test_parse_elearn_course_content_by_source_ids() -> None:
    content = parse_course_content(ELEARN_CONTENT, "https://elearn.nthu.edu.tw/course/view.php?id=101", "elearn", "101")
    assert [(row.kind, row.remote_id) for row in content.items] == [
        ("assignment", "9001"), ("quiz", "9002"),
        ("announcement", "9003"), ("material", "9004"),
    ]
    assert all(row.date_precision == DatePrecision.UNKNOWN for row in content.items)


def test_moodle_forum_container_is_not_faked_as_an_announcement() -> None:
    content = parse_course_content(
        '<a href="/mod/forum/view.php?id=77">公告討論區</a>',
        "https://elearn.nthu.edu.tw/course/view.php?id=101", "elearn", "101",
    )
    assert content.items == ()


def test_moodle_forum_pagination_stays_on_same_proven_forum() -> None:
    html = """
      <a href="/mod/forum/view.php?id=77&page=1">2</a>
      <a href="https://elearn.nthu.edu.tw/mod/forum/view.php?id=77&page=2">3</a>
      <a href="/mod/forum/view.php?id=88&page=1">別的討論區</a>
      <a href="https://evil.example/mod/forum/view.php?id=77&page=3">外站</a>
      <a href="/course/view.php?id=77&page=4">非討論區</a>
    """
    assert parse_moodle_forum_pages(html, "https://elearn.nthu.edu.tw/mod/forum/view.php?id=77", "77") == (
        "https://elearn.nthu.edu.tw/mod/forum/view.php?id=77&page=1",
        "https://elearn.nthu.edu.tw/mod/forum/view.php?id=77&page=2",
    )


class _FakeLocator:
    def __init__(self, count: int):
        self._count = count

    async def count(self) -> int:
        return self._count


class _FakeForumPage:
    def __init__(self, pages: dict[str, str], timeout_url: str | None = None):
        self.pages = pages
        self.timeout_url = timeout_url
        self.url = ""
        self.html = ""

    async def load(self, url: str) -> None:
        if url == self.timeout_url:
            raise PlaywrightTimeoutError("fixture timeout")
        self.url = url
        self.html = self.pages[url]

    def locator(self, selector: str) -> _FakeLocator:
        if (
            selector == "body.notloggedin"
            or "page-login-index" in selector
            or "/auth/oauth2/login.php" in selector
        ):
            return _FakeLocator(0)
        recognized = "page-mod-forum-view" in self.html or "forumheaderlist" in self.html
        return _FakeLocator(1 if recognized else 0)

    async def content(self) -> str:
        return self.html


class _FixtureElearnAdapter(ElearnAdapter):
    async def _goto(self, page, url: str) -> None:
        await page.load(url)


def test_forum_followup_reads_multiple_pages_and_deduplicates() -> None:
    first = "https://elearn.nthu.edu.tw/mod/forum/view.php?id=77"
    second = "https://elearn.nthu.edu.tw/mod/forum/view.php?id=77&page=1"
    page = _FakeForumPage({
        first: '<body id="page-mod-forum-view"><a href="/mod/forum/discuss.php?d=1">公告一</a><a href="/mod/forum/view.php?id=77&page=1">下一頁</a></body>',
        second: '<body id="page-mod-forum-view"><a href="/mod/forum/discuss.php?d=1">公告一</a><a href="/mod/forum/discuss.php?d=2">公告二</a></body>',
    })
    adapter = _FixtureElearnAdapter(browser=object())
    complete, items, warnings = asyncio.run(adapter._fetch_forum_discussions(page, first, "101"))
    assert complete is True
    assert [(item.remote_id, item.title) for item in items] == [("1", "公告一"), ("2", "公告二")]
    assert warnings == ()


def test_forum_timeout_keeps_already_fetched_discussions_as_partial() -> None:
    first = "https://elearn.nthu.edu.tw/mod/forum/view.php?id=77"
    second = "https://elearn.nthu.edu.tw/mod/forum/view.php?id=77&page=1"
    page = _FakeForumPage({
        first: '<body id="page-mod-forum-view"><a href="/mod/forum/discuss.php?d=1">已讀公告</a><a href="/mod/forum/view.php?id=77&page=1">下一頁</a></body>',
    }, timeout_url=second)
    adapter = _FixtureElearnAdapter(browser=object())
    complete, items, warnings = asyncio.run(adapter._fetch_forum_discussions(page, first, "101"))
    assert complete is False
    assert [item.remote_id for item in items] == ["1"]
    assert warnings == ("elearn_forum_upstream_timeout",)


def test_forum_page_limit_keeps_records_and_marks_partial() -> None:
    first = "https://elearn.nthu.edu.tw/mod/forum/view.php?id=77"
    second = "https://elearn.nthu.edu.tw/mod/forum/view.php?id=77&page=1"
    page = _FakeForumPage({
        first: '<body id="page-mod-forum-view"><a href="/mod/forum/discuss.php?d=1">公告</a><a href="/mod/forum/view.php?id=77&page=1">下一頁</a></body>',
    })
    adapter = _FixtureElearnAdapter(browser=object())
    adapter.max_forum_pages = 1
    complete, items, warnings = asyncio.run(adapter._fetch_forum_discussions(page, first, "101"))
    assert complete is False
    assert [item.remote_id for item in items] == ["1"]
    assert warnings == ("elearn_forum_page_limit",)


def test_only_explicit_moodle_empty_text_is_complete() -> None:
    assert _is_explicit_empty_catalog("No courses") is True
    assert _is_explicit_empty_catalog("You are not enrolled in any courses") is True
    assert _is_explicit_empty_catalog("載入中…") is False
    assert _is_explicit_empty_catalog("") is False


@pytest.mark.parametrize(
    ("source", "precision", "expected"),
    [
        ("2026-10-04 00:00", DatePrecision.EXACT, "2026-10-03T16:00:00+00:00"),
        ("2026/10/04 23:59", DatePrecision.EXACT, "2026-10-04T15:59:00+00:00"),
        ("2026年10月4日", DatePrecision.DATE_ONLY, "2026-10-04"),
        ("2026年 09月 10日(週四) 12:00", DatePrecision.EXACT, "2026-09-10T04:00:00+00:00"),
        ("期限未提供", DatePrecision.UNKNOWN, None),
        ("10/04 09:00", DatePrecision.UNKNOWN, None),
        ("下週二", DatePrecision.UNKNOWN, None),
    ],
)
def test_date_precision_never_invents_missing_components(source, precision, expected) -> None:
    parsed = parse_nthu_date(source)
    assert parsed.precision == precision
    actual = parsed.due_at.isoformat() if isinstance(parsed.due_at, datetime) else parsed.due_date.isoformat() if parsed.due_date else None
    assert actual == expected
    assert parsed.source_text == source


def test_invalid_calendar_date_remains_unknown() -> None:
    parsed = parse_nthu_date("2026-02-30 12:00")
    assert parsed.precision == DatePrecision.UNKNOWN
    assert parsed.due_at is None


def test_assignment_personal_submission_evidence_is_conservative() -> None:
    page = "Due date: 2026-10-04 23:59\nSubmission status: Submitted for grading\nGrading status: Not graded"
    assert _labeled_value(page, ("Due date",)) == "2026-10-04 23:59"
    assert _submission_status(page) == ("submitted", "personal_submission_status")
    assert _submission_status("Grading status: Graded") == ("unknown", None)
    assert _submission_status("繳交狀態\t已繳交，等待評分中") == ("submitted", "personal_submission_status")
    assert _submission_status("繳交狀態\n目前尚無任何作業繳交") == ("not_submitted", "personal_submission_status")


def test_draft_is_not_treated_as_submitted() -> None:
    assert _submission_status("繳交狀態：草稿（未繳交）") == ("draft", "personal_submission_status")


def test_grade_and_quiz_completion_require_personal_evidence() -> None:
    assert _grade_text("Grade: 87.5 / 100") == "87.5 / 100"
    assert _grade_text("評分狀態：尚未評分\n成績：-") is None
    assert _grade_text("評分狀態\n尚未評分") is None
    assert _quiz_status("作答狀態：已完成") == ("completed", "personal_attempt_status")
    assert _quiz_status("成績：100") == ("unknown", None)
    actual_quiz = "作答狀態\t成績 / 6.00\t回顧作答紀錄\n已經完成\n已經提交2026年 09月 22日\n這個測驗您的最後成績是6.00/6.00"
    assert _quiz_status(actual_quiz) == ("completed", "personal_attempt_status")
    assert _grade_text(actual_quiz) == "6.00/6.00"


def test_eeclass_personal_submission_status_and_course_code_labels() -> None:
    body = "課程代碼：11510CS135601\n繳交狀態：已繳交\n成績：92"
    assert _eeclass_labeled_value(body, ("課程代碼",)) == "11510CS135601"
    assert _personal_status("未繳交", "assignment") == "not_submitted"
    assert _personal_status("已繳交", "assignment") == "submitted"
    assert _personal_status("已完成", "quiz") == "completed"


def test_course_catalog_ignores_platform_tutorials_and_help_overlay():
    html = """<ul class="overlaylist"><li><a href="/course/view.php?id=99">教師指南</a></li></ul>
    <a href="/course/view.php?id=98">操作教學影片</a>
    <main><a href="/course/view.php?id=101">影片製作</a></main>"""
    records = parse_courses(html, "https://elearn.nthu.edu.tw/my/courses.php", "elearn")
    assert [(row.remote_id, row.title) for row in records] == [("101", "影片製作")]
