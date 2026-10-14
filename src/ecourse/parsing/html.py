from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup

from ecourse.adapters.base import Course, CourseContent, CourseItem


class _AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[tuple[str, str, dict[str, str]]] = []
        self._href: str | None = None
        self._attrs: dict[str, str] = {}
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a" and self._href is None:
            self._attrs = {key: value or "" for key, value in attrs}
            self._href = self._attrs.get("href")
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            text = " ".join("".join(self._text).split())
            self.anchors.append((self._href, text, self._attrs))
            self._href, self._attrs, self._text = None, {}, []


def _remote_id(url: str, platform: str) -> str | None:
    parsed = urlparse(url)
    query_id = parse_qs(parsed.query).get("id", [None])[0]
    if platform == "elearn" and query_id and "/course/" in parsed.path:
        return query_id
    if platform == "eeclass":
        match = re.search(r"/course/(?:home|info|dashboard)?/?(\d+)(?:/|$)", parsed.path)
        if match:
            return match.group(1)
    return None


def is_platform_tutorial(title: str, platform: str) -> bool:
    return platform == "elearn" and "".join(title.split()) == "操作教學影片"


def parse_courses(html: str, base_url: str, platform: str) -> tuple[Course, ...]:
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, Course] = {}
    for anchor in soup.find_all("a", href=True):
        # NTHU's global help overlay links to tutorial courses, not enrollments.
        if platform == "elearn" and anchor.find_parent(class_="overlaylist"):
            continue
        href = str(anchor.get("href") or "")
        attrs = {str(key): str(value) for key, value in anchor.attrs.items() if isinstance(value, str)}
        visible_text = " ".join(anchor.get_text(" ", strip=True).split())
        accessible_text = " ".join(str(anchor.get("title") or anchor.get("aria-label") or "").split())
        text = accessible_text if len(accessible_text) > len(visible_text) else visible_text
        url = urljoin(base_url, href)
        remote_id = _remote_id(url, platform)
        classes = attrs.get("class", "")
        if not remote_id or not text or is_platform_tutorial(text, platform):
            continue
        if platform == "elearn" and not ("course" in classes or "/course/view.php" in url):
            continue
        container = anchor.find_parent(["article", "li", "tr", "div"])
        context = " ".join(container.get_text(" ", strip=True).split()) if container else text
        candidates = [
            anchor.get("data-course-code"), anchor.get("data-code"),
            container.get("data-course-code") if container else None,
            container.get("data-code") if container else None,
            accessible_text, text, context,
        ]
        code = next((value for candidate in candidates if (value := normalize_course_code(candidate))), None)
        found.setdefault(remote_id, Course(remote_id, text, url, course_code=code))
    return tuple(found.values())


def normalize_course_code(value: object) -> str | None:
    """Extract an explicit NTHU course code without matching names by similarity."""
    if not isinstance(value, str):
        return None
    text = " ".join(value.upper().split())
    labeled = re.search(
        r"(?:課程代碼|課號|科號|COURSE\s*CODE)\s*[:：]?\s*"
        r"((?:\d{5}\s*)?[A-Z]{2,6}\s*\d{4,6}(?:-\d{1,2})?)",
        text,
    )
    if labeled:
        return _clean_course_code(labeled.group(1))
    # Platform course cards commonly prefix or suffix the title with the same
    # official code (for example 11510MATH101001 or CS 1356).
    boundary = re.search(
        r"(?:^|[\[(_\-\s])((?:\d{5}\s*)?[A-Z]{2,6}\s*\d{4,6}(?:-\d{1,2})?)(?=$|[\])_\-\s])",
        text,
    )
    return _clean_course_code(boundary.group(1)) if boundary else None


def _clean_course_code(value: str) -> str:
    # Moodle may append a teaching-group suffix such as ``-07`` after the
    # official 15-byte code. It is not part of the cross-platform course code.
    return re.sub(r"-\d{1,2}$", "", re.sub(r"\s+", "", value))


def parse_course_content(html: str, base_url: str, platform: str, course_id: str) -> CourseContent:
    parser = _AnchorParser()
    parser.feed(html)
    found: dict[tuple[str, str], CourseItem] = {}
    patterns = {
        "assignment": ("/mod/assign/", "/homework/", "/assignment/"),
        "quiz": ("/mod/quiz/", "/quiz/", "/exam/"),
        # A Moodle forum/view link is a container, not one announcement. Only a
        # concrete discussion is emitted as an announcement record.
        "announcement": ("/mod/forum/discuss.php", "/bulletin/", "/announcement/"),
        "material": ("/mod/resource/", "/mod/page/", "/mod/url/", "/material/"),
    }
    for href, text, _attrs in parser.anchors:
        if not text:
            continue
        url = urljoin(base_url, href)
        kind = next((name for name, fragments in patterns.items() if any(fragment in url for fragment in fragments)), None)
        if kind is None:
            continue
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        remote_id = query.get("id", [None])[0] or query.get("d", [None])[0] or parsed.path.rstrip("/").rsplit("/", 1)[-1]
        key = (kind, remote_id)
        found.setdefault(key, CourseItem(remote_id=remote_id, kind=kind, title=text, url=url))
    return CourseContent(course_id, tuple(found.values()))


def parse_eeclass_activity_page(html: str, base_url: str, course_id: str, kind: str) -> tuple[CourseItem, ...]:
    """Parse one authenticated eeclass course list route.

    eeclass exposes activities on separate routes rather than on the course
    landing page. Bulletin entries are JavaScript modal triggers with no
    stable detail URL, so their list position is retained as a scoped ID.
    """
    route_fragments = {
        "assignment": ("/course/homework/",),
        "quiz": ("/course/exam/",),
        "material": ("/media/doc/",),
    }
    if kind not in {*route_fragments, "announcement"}:
        raise ValueError("unsupported eeclass activity kind")
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, CourseItem] = {}
    if kind == "announcement":
        anchors = soup.select("a.fs-bulletin-item, .fs-bulletin-item a")
        if not anchors:
            anchors = soup.select("a[href*='/course/bulletin/']")
        for index, anchor in enumerate(anchors):
            title = " ".join(anchor.get_text(" ", strip=True).split())
            if not title:
                continue
            raw_id = next((str(anchor.get(key)) for key in ("data-id", "data-sn", "data-bulletin-id") if anchor.get(key)), None)
            remote_id = raw_id or f"{course_id}:{index + 1}"
            found.setdefault(remote_id, CourseItem(remote_id, kind, title, urljoin(base_url, f"/course/bulletin/{course_id}")))
        return tuple(found.values())
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "")
        url = urljoin(base_url, href)
        parsed = urlparse(url)
        if parsed.hostname != "eeclass.nthu.edu.tw" or not any(fragment in parsed.path for fragment in route_fragments[kind]):
            continue
        title = " ".join(anchor.get_text(" ", strip=True).split())
        match = re.search(r"/(\d+)/?$", parsed.path)
        if not title or not match:
            continue
        found.setdefault(match.group(1), CourseItem(match.group(1), kind, title, url))
    return tuple(found.values())


def parse_moodle_forum_pages(html: str, base_url: str, forum_id: str) -> tuple[str, ...]:
    """Return only pagination URLs proven to belong to the current Moodle forum."""
    parser = _AnchorParser()
    parser.feed(html)
    found: dict[str, None] = {}
    for href, _text, _attrs in parser.anchors:
        url = urljoin(base_url, href)
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        if (
            parsed.scheme == "https"
            and parsed.hostname == "elearn.nthu.edu.tw"
            and parsed.path == "/mod/forum/view.php"
            and query.get("id", [None])[0] == forum_id
            and "page" in query
        ):
            found[url] = None
    return tuple(found)
