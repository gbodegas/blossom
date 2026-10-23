# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Blossom saved to a Home Screen or the Dock: every page names the touch icon iOS and
iPadOS use and the manifest Safari on the Mac and other browsers read, and asks to open as
an app of its own. The manifest opens her week in its own window, and each icon is served
at the size it names, opaque, so no system fills a transparent corner with black."""

import io
import json
import pathlib
import re

import pytest
from PIL import Image

from blossom.settings import PACKAGE_ROOT
from tests.support import (
    HER_PAGE,
    PAGE_HEADERS,
    browser,
    client_for,
    elements_of,
    signed_in_household,
)

TOUCH_ICON = "/static/icons/apple-touch-icon.png"
MANIFEST = "/static/manifest.json"
WEB_APP = {
    "apple-mobile-web-app-capable": "yes",
    "mobile-web-app-capable": "yes",
    "apple-mobile-web-app-title": "Blossom",
    "apple-mobile-web-app-status-bar-style": "default",
    "theme-color": "#fdf5f7",
}
"""The lines that ask iOS, iPadOS and other browsers to open Blossom as an app, by name."""


def canvas() -> str:
    """The paper's color, as the stylesheet names it."""
    css = (PACKAGE_ROOT / "static" / "blossom.css").read_text(encoding="utf-8")
    found = re.search(r"--canvas:\s*(#[0-9a-f]{6});", css)
    assert found, "the stylesheet names no --canvas color"
    return found.group(1)


def head_of(page: str) -> tuple[dict[str, str], dict[str, str]]:
    """A page's links by rel and its metas by name."""
    found = elements_of(page)
    links = {e.attributes["rel"]: e.attributes.get("href", "") for e in found if e.tag == "link"}
    metas = {
        e.attributes["name"]: e.attributes.get("content", "")
        for e in found
        if e.tag == "meta" and "name" in e.attributes
    }
    return links, metas


def test_her_week_names_the_icon_the_manifest_and_the_web_app_lines() -> None:
    with browser() as client:
        page = client.get(HER_PAGE, headers=PAGE_HEADERS)
    assert page.status_code == 200
    links, metas = head_of(page.text)
    assert links["apple-touch-icon"] == TOUCH_ICON
    assert links["manifest"] == MANIFEST
    assert {name: metas.get(name) for name in WEB_APP} == WEB_APP


def test_the_sign_in_page_names_them_too(tmp_path: pathlib.Path) -> None:
    # The first page a saved app opens on a new device, before anyone has signed in.
    with client_for(signed_in_household(tmp_path)) as client:
        page = client.get("/sign-in", headers=PAGE_HEADERS)
    assert page.status_code == 200
    links, metas = head_of(page.text)
    assert links["apple-touch-icon"] == TOUCH_ICON
    assert links["manifest"] == MANIFEST
    assert {name: metas.get(name) for name in WEB_APP} == WEB_APP


def test_the_manifest_opens_her_week_in_its_own_window() -> None:
    with browser() as client:
        served = client.get(MANIFEST)
    assert served.status_code == 200
    assert served.headers["content-type"].split(";")[0] in {
        "application/json",
        "application/manifest+json",
    }
    manifest = json.loads(served.text)
    assert manifest["name"] == manifest["short_name"] == "Blossom"
    assert manifest["display"] == "standalone"
    assert manifest["start_url"] == HER_PAGE
    assert manifest["start_url"].startswith(manifest["scope"])
    assert manifest["background_color"] == manifest["theme_color"] == canvas()
    assert WEB_APP["theme-color"] == canvas()


@pytest.mark.parametrize(
    ("src", "size", "purpose"),
    [
        (TOUCH_ICON, 180, "touch"),
        ("/static/icons/icon-192.png", 192, "any"),
        ("/static/icons/icon-512.png", 512, "any"),
        ("/static/icons/icon-maskable-512.png", 512, "maskable"),
    ],
)
def test_each_icon_is_served_opaque_at_its_size(src: str, size: int, purpose: str) -> None:
    with browser() as client:
        served = client.get(src)
        manifest = json.loads(client.get(MANIFEST).text)
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/png"
    image = Image.open(io.BytesIO(served.content))
    assert image.size == (size, size)
    assert image.mode == "RGB", "no alpha channel: every pixel is opaque"
    if purpose != "touch":
        named = [icon for icon in manifest["icons"] if icon["src"] == src]
        sizes = f"{size}x{size}"
        assert named == [{"src": src, "sizes": sizes, "type": "image/png", "purpose": purpose}]


def test_the_manifest_names_only_the_icons_above() -> None:
    with browser() as client:
        manifest = json.loads(client.get(MANIFEST).text)
    assert sorted((icon["sizes"], icon["purpose"]) for icon in manifest["icons"]) == [
        ("192x192", "any"),
        ("512x512", "any"),
        ("512x512", "maskable"),
    ]
