"""Capture small, de-identified adapter fixtures from authorized saved sessions.

Raw protected HTML and storage state remain in memory.  Only selected DOM
subtrees are cloned, scrubbed in the browser, and then written to tests.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
from pathlib import Path
from urllib.parse import urljoin

from playwright.async_api import async_playwright

from ecourse.vault import Vault


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / ".data" / "ecourse.sqlite3"
SECRETS = ROOT / ".secrets"
OUTPUT = ROOT / "tests" / "fixtures" / "platforms"


def load_sessions() -> dict[str, dict]:
    rows = sqlite3.connect(DATA).execute(
        """select c.platform, c.owner_id, s.encrypted_state
           from platform_connections c join session_states s on s.connection_id=c.id
           where c.auth_state='connected'"""
    ).fetchall()
    vault = Vault(SECRETS)
    return {
        platform: vault.unseal(envelope, platform=platform, account_id=owner_id, purpose="session")
        for platform, owner_id, envelope in rows
    }


def sample_item_url(platform: str, kind: str, remote_status: str | None = None, *, newest: bool = False) -> str | None:
    status_clause = " and i.remote_status=?" if remote_status else ""
    params = (platform, kind, remote_status) if remote_status else (platform, kind)
    row = sqlite3.connect(DATA).execute(
        f"""select i.source_url from learning_items i
           join course_sources s on s.id=i.source_course_id
           join platform_connections c on c.id=s.connection_id
           where c.platform=? and i.kind=? and i.source_url is not null
           {status_clause} and i.source_url like '%/view.php%'
           order by cast(i.remote_item_id as integer) {'desc' if newest else 'asc'} limit 1""",
        params,
    ).fetchone()
    return row[0] if row else None


SCRUB_JS = r"""
({selectors, platform}) => {
  const wrapper = document.createElement('main');
  wrapper.dataset.fixturePlatform = platform;
  for (const selector of selectors) {
    for (const source of document.querySelectorAll(selector)) wrapper.append(source.cloneNode(true));
  }
  wrapper.querySelectorAll('script,style,noscript,svg,img,video,audio,iframe,input,textarea').forEach(n => n.remove());
  const allowed = new Set(['id','class','role','href','name','title','aria-label','aria-expanded','data-region','data-role']);
  for (const el of wrapper.querySelectorAll('*')) {
    for (const attr of [...el.attributes]) if (!allowed.has(attr.name)) el.removeAttribute(attr.name);
    for (const key of ['id','class','name','title','aria-label','data-region','data-role']) {
      if (el.hasAttribute(key)) el.setAttribute(key, el.getAttribute(key)
        .replace(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+/g, 'fixture@example.invalid')
        .replace(/[A-Fa-f0-9]{16,}/g, 'fixture-token')
        .replace(/\d{5,}/g, '101'));
    }
    if (el.hasAttribute('href')) {
      try {
        const u = new URL(el.getAttribute('href'), location.href);
        u.search = '';
        u.hash = '';
        let path = u.pathname.replace(/\b\d{3,}\b/g, '101');
        if (platform === 'elearn' && /\/course\/view\.php$/.test(path)) path += '?id=101';
        if (platform === 'elearn' && /\/mod\/(assign|quiz|forum|resource|page|url)\//.test(path)) path += '?id=9001';
        el.setAttribute('href', u.origin === location.origin ? path : '#external-removed');
      } catch { el.setAttribute('href', '#invalid-removed'); }
    }
  }
  const keep = /(課程|作業|測驗|公告|教材|繳交期限|開放繳交|已繳交|未繳交|此作業已繳交|尚未批改|已批改|已完成|已經完成|最後成績|收到成績|分數|成績|狀態|Submission status|Due date|Grading status|Grade|Submitted|Not submitted|completed|首頁|儀表板|Course|Assignment|Quiz|Announcement|Material)/i;
  for (const node of [...wrapper.querySelectorAll('*')].flatMap(el => [...el.childNodes]).filter(n => n.nodeType === Node.TEXT_NODE)) {
    const value = (node.textContent || '').replace(/\s+/g, ' ').trim();
    if (!value) continue;
    const parent = node.parentElement;
    const href = parent?.closest('a')?.getAttribute('href') || '';
    if (/homework|assign/i.test(href)) node.textContent = ' Fixture Assignment ';
    else if (/exam|quiz/i.test(href)) node.textContent = ' Fixture Quiz ';
    else if (/bulletin|forum/i.test(href)) node.textContent = ' Fixture Announcement ';
    else if (/material|media\/doc|resource/i.test(href)) node.textContent = ' Fixture Material ';
    else if (/\/course\/(?:101)?$|course\/view/i.test(href)) node.textContent = ' Fixture Course ';
    else if (/\d{4}[-\/]\d{1,2}[-\/]\d{1,2}/.test(value)) node.textContent = ' 2026-10-06 18:30 ';
    else if (!keep.test(value)) node.textContent = ' Fixture text ';
    else node.textContent = ' ' + value.replace(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+/g, 'fixture@example.invalid').replace(/\d{5,}/g, '101') + ' ';
  }
  return '<!doctype html>\n<html lang="zh-Hant"><body>' + wrapper.outerHTML + '</body></html>\n';
}
"""


async def capture(page, filename: str, selectors: list[str], platform: str) -> None:
    html = await page.evaluate(SCRUB_JS, {"selectors": selectors, "platform": platform})
    if re.search(r"ajaxAuth=|@[A-Za-z0-9.-]+\.(?:edu|tw|com)\b|\d{8,}", html, re.I):
        raise RuntimeError(f"fixture scrub failed: {filename}")
    (OUTPUT / filename).write_text(html, encoding="utf-8")


async def main() -> None:
    sessions = load_sessions()
    if set(sessions) != {"elearn", "eeclass"}:
        raise RuntimeError("both connected platform sessions are required")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, chromium_sandbox=True)

        elearn = await browser.new_context(storage_state=sessions["elearn"])
        page = await elearn.new_page()
        await page.goto("https://elearn.nthu.edu.tw/my/index.php", wait_until="domcontentloaded")
        await page.wait_for_function("() => document.querySelector(\"a[href*='/course/view.php?id=']\")", timeout=12_000)
        await capture(page, "elearn_dashboard.html", ["body"], "elearn")
        samples = {
            "elearn_assignment_submitted.html": sample_item_url("elearn", "assignment", "graded"),
            "elearn_assignment_open.html": sample_item_url("elearn", "assignment", "unknown", newest=True),
            "elearn_quiz_completed.html": sample_item_url("elearn", "quiz", "completed"),
        }
        for filename, assignment_url in samples.items():
            if assignment_url:
                await page.goto(assignment_url, wait_until="domcontentloaded")
                await page.wait_for_function(
                    "() => (document.querySelector('#region-main')?.innerText || '').trim().length > 20",
                    timeout=12_000,
                )
                await capture(page, filename, ["#region-main"], "elearn")
        await elearn.close()

        eeclass = await browser.new_context(storage_state=sessions["eeclass"])
        page = await eeclass.new_page()
        await page.goto("https://eeclass.nthu.edu.tw/dashboard", wait_until="domcontentloaded")
        await capture(page, "eeclass_dashboard.html", ["body"], "eeclass")
        course_href = await page.locator("a[href^='/course/']").evaluate_all(
            "els => els.map(e => e.getAttribute('href')).find(h => /^\\/course\\/\\d+$/.test(h))"
        )
        if not course_href:
            raise RuntimeError("eeclass course route not found")
        await page.goto("https://eeclass.nthu.edu.tw" + course_href, wait_until="domcontentloaded")
        await capture(page, "eeclass_course.html", ["body"], "eeclass")

        route_files = {
            "homeworkList": "eeclass_homework_list.html",
            "examList": "eeclass_exam_list.html",
            "bulletin": "eeclass_bulletin_list.html",
            "material": "eeclass_material_list.html",
        }
        course_id = re.search(r"(\d+)$", course_href).group(1)
        for route, filename in route_files.items():
            await page.goto(f"https://eeclass.nthu.edu.tw/course/{route}/{course_id}", wait_until="domcontentloaded")
            await capture(page, filename, ["body"], "eeclass")

        await page.goto(f"https://eeclass.nthu.edu.tw/course/homeworkList/{course_id}", wait_until="domcontentloaded")
        homework_href = await page.locator("a[href*='/course/homework/']").first.get_attribute("href")
        if homework_href:
            await page.goto(urljoin(page.url, homework_href), wait_until="domcontentloaded")
            await capture(page, "eeclass_homework.html", ["body"], "eeclass")
            report = page.locator("a[href*='/homework/report/']").first
            if await report.count():
                await page.goto(urljoin(page.url, await report.get_attribute("href")), wait_until="domcontentloaded")
                await capture(page, "eeclass_homework_report.html", ["body"], "eeclass")
        await eeclass.close()
        await browser.close()

    manifest = {"source": "authorized read-only capture", "raw_html_persisted": False, "files": sorted(path.name for path in OUTPUT.glob("*.html"))}
    (OUTPUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / ".cache" / "ms-playwright"))
    asyncio.run(main())
