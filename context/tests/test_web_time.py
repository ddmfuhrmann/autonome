from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from starlette.requests import Request

from autonome_context import web

SAO_PAULO = ZoneInfo("America/Sao_Paulo")


def doc(*args):
    return {"created_at": datetime(*args, tzinfo=UTC)}


def request_with(cookie: str | None) -> Request:
    headers = [(b"cookie", f"tz={cookie}".encode())] if cookie is not None else []
    return Request({"type": "http", "headers": headers})


def test_days_are_grouped_in_the_viewers_zone():
    now = datetime(2026, 11, 2, 2, 0, tzinfo=UTC)  # 23:00 on the 1st in São Paulo
    today, yesterday = doc(2026, 11, 2, 1, 30), doc(2026, 11, 1, 2, 30)  # 22:30 on the 1st, 23:30 on Oct 31
    groups = web.day_groups([today, yesterday], now=now, tz=SAO_PAULO)
    assert [name for name, _ in groups] == ["Today", "Yesterday"]
    assert groups[0][1] == [today] and groups[1][1] == [yesterday]
    assert [name for name, _ in web.day_groups([today], now=now)] == ["Today"]  # UTC by default


@pytest.mark.parametrize("cookie", [None, "", "Mars/Olympus", "../../etc/passwd", "<script>", "A" * 300])
def test_a_bad_tz_cookie_means_utc(cookie):
    assert web.viewer_tz(request_with(cookie)) is UTC


def test_a_valid_tz_cookie_is_used():
    assert web.viewer_tz(request_with("America/Sao_Paulo")).key == "America/Sao_Paulo"


def test_when_and_clock_emit_time_tags_with_escaped_text():
    t = datetime(2026, 11, 2, 1, 30, 5, tzinfo=UTC)
    assert web.when(t) == '<time datetime="2026-11-02T01:30:05Z" data-fmt="when">Nov 02, 01:30</time>'
    assert web.clock(t) == '<time datetime="2026-11-02T01:30:05Z" data-fmt="clock">01:30:05</time>'
    assert web.when(None) == "" and web.clock(None) == ""
    assert web.when("2026-11-02T01:30:05Z") == web.when(t)


def test_ago_is_plain_text_and_only_the_old_date_fallback_is_a_time_tag():
    assert web.ago(datetime.now(UTC) - timedelta(hours=2)) == "2h ago"
    assert web.ago(datetime(2020, 11, 2, 1, 30, 5, tzinfo=UTC)) == (
        '<time datetime="2020-11-02T01:30:05Z" data-fmt="day">Nov 02</time>')
    assert web.ago(None) == ""


def test_hm_is_a_time_tag_and_when_text_is_plain_utc():
    t = datetime(2026, 11, 2, 1, 30, 5, tzinfo=UTC)
    assert web.hm(t) == '<time datetime="2026-11-02T01:30:05Z" data-fmt="hm">01:30</time>'
    assert web.hm(None) == ""
    assert web.when_text(t) == "Nov 02, 01:30" and "<" not in web.when_text(t)
    assert web.when_text(t.astimezone(SAO_PAULO)) == "Nov 02, 01:30"  # always UTC, whatever the zone it came in
    assert web.when_text(None) == ""


def test_templates_never_format_dates_themselves():
    templates = Path(web.__file__).parent / "templates"
    assert not [p.name for p in templates.glob("*.html") if "strftime" in p.read_text()]
