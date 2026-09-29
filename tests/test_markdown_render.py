"""Markdown → HTML renderer tests (D6.1 port to the v2 suite).

The gateways compose markdown text; the transport derives formatted_body
HTML from it (gateways.markdown_render). Element renders bold/code;
clients that ignore formatted_body fall back to the plain body, which
carries identical content.
"""

import html

from data_broker.gateways.markdown_render import text_to_html


def test_bold_and_code():
    h = text_to_html("**Approved** `revoke 1`")
    assert "<strong>Approved</strong>" in h
    assert "<code>revoke 1</code>" in h


def test_escapes_all_html_first():
    h = text_to_html("path <script>alert(1)</script> **bold**")
    assert "<script>" not in h
    assert html.escape("<script>alert(1)</script>") in h
    assert "<strong>bold</strong>" in h


def test_fence_becomes_code_block():
    h = text_to_html("```\napprove 1\n```")
    assert "<pre><code>" in h and "approve 1" in h
    assert "**" not in h


def test_no_nested_markup_inside_fence():
    h = text_to_html("```\n**not bold**\n```")
    assert "<strong>" not in h


def test_newlines_become_br():
    h = text_to_html("line1\nline2")
    assert "<br/>" in h


def test_injection_via_room_resource_escaped():
    # room content (paths, reasons) must never become tags
    h = text_to_html("granted: `!room<b>x` **<img src=x onerror=y>**")
    assert "<b>" not in h
    assert "<img" not in h
    assert html.escape("<img src=x onerror=y>") in h
