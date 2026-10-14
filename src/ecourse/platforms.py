from __future__ import annotations

from urllib.parse import urlsplit


PLATFORM_HOME = {
    "elearn": "https://elearn.nthu.edu.tw/my/index.php",
    "eeclass": "https://eeclass.nthu.edu.tw/dashboard",
}

PLATFORM_HOST = {
    "elearn": "elearn.nthu.edu.tw",
    "eeclass": "eeclass.nthu.edu.tw",
}

BRAVE_EXTENSION_ID = "amecmikkjicakbcboajnfmbhekmkpahk"


def managed_platform_url(platform: str, target_url: str | None = None) -> str:
    """Return a strict official-platform HTTPS URL suitable for session handoff."""
    if platform not in PLATFORM_HOME:
        raise ValueError("不支援的平台。")
    value = target_url or PLATFORM_HOME[platform]
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname == PLATFORM_HOST[platform]
            and not parsed.username
            and not parsed.password
            and parsed.port in {None, 443}
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("平台網址不在允許範圍內。")
    return value
