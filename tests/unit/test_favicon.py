"""Tests for the inline SVG favicon shared by the SPA and the login page."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote

import pytest

_UI_DIR = Path(__file__).resolve().parents[2] / "src" / "stemtrace" / "server" / "ui"
_INDEX_HTML = _UI_DIR / "frontend" / "index.html"
_LOGIN_HTML = _UI_DIR / "templates" / "login.html"

_ICON_HREF_RE = re.compile(r'<link rel="icon" type="image/svg\+xml" href="([^"]+)"')
_DATA_PREFIX = "data:image/svg+xml,"
_SVG_TAG = "{http://www.w3.org/2000/svg}svg"


def _icon_href(path: Path) -> str:
    """Return the single SVG icon href declared in an HTML file."""
    hrefs = _ICON_HREF_RE.findall(path.read_text())
    assert len(hrefs) == 1, f"expected one SVG icon link in {path.name}, got {hrefs}"
    return str(hrefs[0])


def test_favicon_hrefs_in_index_and_login_are_identical() -> None:
    """The SPA and login page embed exactly the same favicon."""
    assert _icon_href(_INDEX_HTML) == _icon_href(_LOGIN_HTML)


@pytest.mark.parametrize("path", [_INDEX_HTML, _LOGIN_HTML], ids=lambda p: p.name)
def test_favicon_href_decodes_to_well_formed_svg(path: Path) -> None:
    """Each favicon is an inline data URI that decodes to a parseable SVG."""
    href = _icon_href(path)
    assert href.startswith(_DATA_PREFIX)

    root = ET.fromstring(unquote(href[len(_DATA_PREFIX) :]))

    assert root.tag == _SVG_TAG
    assert root.attrib["viewBox"] == "0 0 32 32"


@pytest.mark.parametrize("path", [_INDEX_HTML, _LOGIN_HTML], ids=lambda p: p.name)
def test_html_never_references_missing_vite_svg(path: Path) -> None:
    """The old, non-existent /vite.svg favicon is gone everywhere."""
    assert "/vite.svg" not in path.read_text()
