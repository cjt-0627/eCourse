import json

from ecourse.course_catalog import CourseCatalog, build_index, format_periods

ROWS = [
    {"科號": "11510CS  135500", "授課教師": "王大明\tWANG, DA-MING\n", "教室與上課時間": "DELTA103\tT3T4\nDELTA104\tR3R4\n"},
    {"科號": "11510MATH101001", "授課教師": "李一\tLEE, YI\n張二\tCHANG, ER\n", "教室與上課時間": "GEN201\tM3M4MnM5W9\n"},
]


def test_build_index_and_format():
    index = build_index(ROWS)
    assert index["11510CS135500"]["slots"] == ["T3", "T4", "R3", "R4"]
    assert format_periods(index["11510CS135500"]["slots"]) == "二 3-4、四 3-4"
    assert format_periods(index["11510MATH101001"]["slots"], "en") == "Mon 3-5, Wed 9"


def test_catalog_describe_uses_cache_and_short_codes(tmp_path):
    catalog = CourseCatalog(tmp_path / "catalog.json", url="https://example.invalid", fetcher=lambda _url: ROWS)
    catalog.refresh()
    assert json.loads((tmp_path / "catalog.json").read_text(encoding="utf-8"))["courses"]
    fresh = CourseCatalog(tmp_path / "catalog.json", url="")
    assert fresh.describe("11510CS135500") == {"teacher": "王大明", "periods": "二 3-4、四 3-4"}
    assert fresh.describe("CS 135500", "en") == {"teacher": "WANG, DA-MING", "periods": "Tue 3-4, Thu 3-4"}
    assert fresh.describe("11510MATH101001")["teacher"] == "李一、張二"
    assert fresh.describe("11510EE000000") == {}
