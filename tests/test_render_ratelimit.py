"""Pemformat pesan dan pembatas laju."""

from __future__ import annotations

from astria_gateway.ratelimit import RateLimiter
from astria_gateway.render import render_event, render_result, truncate


def test_card_message_uses_only_given_fields_and_strips_control_chars() -> None:
    subject, body = render_event("card.verified", {"card_id": "IC-20261110-003\r\nBcc: x@y", "band": "TINGGI"})
    assert "\n" not in subject and "\r" not in subject
    assert body.splitlines() == [subject, "Band: TINGGI"]


def test_result_rendering_lists_and_empty() -> None:
    text = render_result("antrean", {"items": [{"card_id": "IC-20261110-003", "band": "KRITIS"}]})
    assert text.splitlines() == ["/antrean", "items:", "- card_id: IC-20261110-003, band: KRITIS"]
    assert render_result("handover", {}) == "/handover\n(kosong)"
    assert render_result("antrean", {"items": []}).splitlines()[-1] == "- (kosong)"


def test_truncate() -> None:
    assert truncate("abc", 3) == "abc"
    assert truncate("abcd", 3) == "ab…"


def test_rate_limiter_refills() -> None:
    clock = [0.0]
    limiter = RateLimiter(2, 60, clock=lambda: clock[0])
    assert [limiter.allow("a"), limiter.allow("a"), limiter.allow("a")] == [True, True, False]
    assert limiter.allow("b") is True
    clock[0] = 30.0
    assert limiter.allow("a") is True
    assert limiter.allow("a") is False
