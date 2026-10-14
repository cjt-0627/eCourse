"""Local CAPTCHA inference using the already-required Playwright browser."""

from __future__ import annotations

import asyncio
import base64
import math
import re
import time
from importlib.resources import files

from ecourse.adapters.base import CaptchaSolution


class OAuthCaptchaSolver:
    """Run the bundled ccxp-lite OAuth model entirely inside local Chromium."""

    version = "ccxp-lite-oauth-1.9.0"

    def __init__(self, browser):
        self.browser = browser
        self._context = None
        self._page = None
        self._lock = asyncio.Lock()

    async def _page_for_inference(self):
        if self._page is not None:
            return self._page
        context = await self.browser.new_context()
        page = await context.new_page()
        assets = files(__package__) / "assets"
        try:
            await page.add_script_tag(content=(assets / "oauth_model.js").read_text())
            await page.add_script_tag(content=(assets / "oauth_runtime.js").read_text())
        except Exception:
            await context.close()
            raise
        self._context, self._page = context, page
        return page

    async def solve(self, image: bytes) -> CaptchaSolution:
        started = time.perf_counter()
        try:
            if not image:
                raise ValueError("empty image")
            encoded = base64.b64encode(image).decode("ascii")
            async with self._lock:
                page = await self._page_for_inference()
                result = await page.evaluate(
                    """async encoded => {
                        const raw = atob(encoded);
                        const bytes = Uint8Array.from(raw, char => char.charCodeAt(0));
                        return await globalThis.CCXP_LITE.oauthDecaptcha.predictWithConfidence(bytes);
                    }""",
                    encoded,
                )
            return self._solution(result, started)
        except Exception as exc:
            return CaptchaSolution(None, 0.0, self.version, _elapsed_ms(started), f"inference_failed:{type(exc).__name__}")

    async def solve_element(self, page, image_locator) -> CaptchaSolution:
        """Infer from the image's natural pixels in its SSO page.

        This avoids CSS/device-scale screenshot resampling, which changes the
        150x80 input the model was trained on.
        """
        started = time.perf_counter()
        try:
            async with self._lock:
                pixels = await image_locator.evaluate(
                    """image => {
                        const width = image.naturalWidth;
                        const height = image.naturalHeight;
                        if (!width || !height) throw new Error('captcha image is not decoded');
                        const canvas = document.createElement('canvas');
                        canvas.width = width;
                        canvas.height = height;
                        const context = canvas.getContext('2d', {willReadFrequently: true});
                        context.drawImage(image, 0, 0, width, height);
                        const rgba = context.getImageData(0, 0, width, height).data;
                        return {width, height, rgba: Array.from(rgba)};
                    }""",
                )
                # Keep model execution in a private blank page. Injecting a
                # script into the school's SSO page can be blocked by its CSP
                # and needlessly modifies third-party page state.
                inference_page = await self._page_for_inference()
                result = await inference_page.evaluate(
                    """pixels => globalThis.CCXP_LITE.oauthDecaptcha.predictRgbaWithConfidence(
                        pixels.width, pixels.height, pixels.rgba
                    )""",
                    pixels,
                )
            return self._solution(result, started)
        except Exception as exc:
            return CaptchaSolution(None, 0.0, self.version, _elapsed_ms(started), f"inference_failed:{type(exc).__name__}")

    def _solution(self, result, started: float) -> CaptchaSolution:
        candidate = result.get("candidate") if isinstance(result, dict) else None
        confidence = float(result.get("confidence", 0)) if isinstance(result, dict) else 0.0
        if re.fullmatch(r"[0-9]{4}", candidate or "") is None or not math.isfinite(confidence):
            raise ValueError("invalid model output")
        return CaptchaSolution(candidate, max(0.0, min(1.0, confidence)), self.version, _elapsed_ms(started))

    async def close(self) -> None:
        async with self._lock:
            context, self._context, self._page = self._context, None, None
            if context is not None:
                await context.close()


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.perf_counter() - started) * 1000))


__all__ = ["OAuthCaptchaSolver"]
