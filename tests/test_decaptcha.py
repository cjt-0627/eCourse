import asyncio
from importlib.resources import files

from ecourse.decaptcha import OAuthCaptchaSolver


class FakePage:
    def __init__(self, result):
        self.result = result
        self.scripts = []

    async def add_script_tag(self, *, content):
        self.scripts.append(content)

    async def evaluate(self, expression, encoded=None):
        if expression.startswith("Boolean"):
            return bool(self.scripts)
        assert "predictWithConfidence" in expression or "predictRgbaWithConfidence" in expression
        assert encoded
        return self.result


class FakeContext:
    def __init__(self, page):
        self.page = page
        self.closed = False

    async def new_page(self):
        return self.page

    async def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, result):
        self.page = FakePage(result)
        self.context = FakeContext(self.page)
        self.context_count = 0

    async def new_context(self):
        self.context_count += 1
        return self.context


class FakeImageLocator:
    async def evaluate(self, expression):
        assert "naturalWidth" in expression
        assert "getImageData" in expression
        assert "predictRgbaWithConfidence" not in expression
        return {"width": 150, "height": 80, "rgba": [255, 255, 255, 255]}


def test_bundled_model_and_runtime_are_deployable_package_resources():
    assets = files("ecourse.decaptcha") / "assets"
    assert (assets / "oauth_model.js").stat().st_size > 2_000_000
    assert "predictWithConfidence" in (assets / "oauth_runtime.js").read_text()
    assert (files("ecourse.decaptcha") / "THIRD_PARTY_NOTICES.txt").is_file()


def test_solver_reuses_local_browser_context_and_returns_calibrated_shape():
    async def scenario():
        browser = FakeBrowser({"candidate": "5004", "confidence": 0.987})
        solver = OAuthCaptchaSolver(browser)
        first = await solver.solve(b"synthetic-png")
        second = await solver.solve(b"another-synthetic-png")
        await solver.close()
        return browser, first, second

    browser, first, second = asyncio.run(scenario())
    assert first.candidate == second.candidate == "5004"
    assert first.confidence == second.confidence == 0.987
    assert first.solver_version == "ccxp-lite-oauth-1.9.0"
    assert browser.context_count == 1
    assert len(browser.page.scripts) == 2
    assert browser.context.closed is True


def test_solver_failure_falls_back_without_guessing():
    async def scenario():
        solver = OAuthCaptchaSolver(FakeBrowser({"candidate": "bad", "confidence": 1}))
        result = await solver.solve(b"synthetic-png")
        await solver.close()
        return result

    result = asyncio.run(scenario())
    assert result.candidate is None
    assert result.confidence == 0
    assert result.reason == "inference_failed:ValueError"


def test_page_element_inference_uses_natural_pixels_without_screenshot_resampling():
    async def scenario():
        browser = FakeBrowser({})
        browser.page.result = {"candidate": "9001", "confidence": 0.996}
        solver = OAuthCaptchaSolver(browser)
        result = await solver.solve_element(
            browser.page, FakeImageLocator(),
        )
        await solver.close()
        return browser, result

    browser, result = asyncio.run(scenario())
    assert result.candidate == "9001"
    assert result.confidence == 0.996
    assert len(browser.page.scripts) == 2
