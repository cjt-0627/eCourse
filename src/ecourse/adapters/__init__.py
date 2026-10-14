from __future__ import annotations

from playwright.async_api import Browser

from .base import *  # noqa: F403


def create_platform_adapters(browser: Browser, captcha_solver=None) -> dict[str, object]:
    """Worker registry factory; the caller owns the Playwright Browser."""
    # Keep implementations lazy: parsing.dates imports adapters.base for its
    # enums, while the implementations import the date parser.
    from .eeclass import EeclassAdapter
    from .elearn import ElearnAdapter

    return {
        "elearn": ElearnAdapter(browser, captcha_solver),
        "eeclass": EeclassAdapter(browser, captcha_solver),
    }


__all__ = ["create_platform_adapters"]
