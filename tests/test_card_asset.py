"""The card file is shipped as-is, so its syntax is a runtime constraint.

Home Assistant falls back to a legacy bundle on anything below roughly Chrome
109, and still renders fine there. A card written in syntax those engines cannot
parse fails silently: the module never runs, the element is never defined, and
the dashboard shows a bare error box - with the reason hidden if the user
looking at it is not an administrator. Nothing else in the suite would catch it.
"""

from __future__ import annotations

from pathlib import Path
import re

CARD = Path(__file__).parent.parent / "custom_components" / "ul_transport" / "www"
SOURCE = CARD / "ul-transport-map.js"


def _code_only(text: str) -> str:
    """The file with its comments taken out."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", text)


def test_no_syntax_newer_than_es2017():
    code = _code_only(SOURCE.read_text())
    assert "??" not in code, "nullish coalescing - use or(a, b)"
    assert not re.search(r"\?\.", code), "optional chaining - spell the check out"


def test_runs_as_a_plain_script():
    """No imports or exports, so it loads via <script> as well as import()."""
    code = _code_only(SOURCE.read_text())
    assert not re.search(r"^\s*(import|export)\s", code, flags=re.MULTILINE)


def test_definitions_are_not_gated_on_a_lookup():
    """`customElements.define` must always be attempted.

    Gating it on `customElements.get` means a wrong answer skips the definition
    silently and the card goes missing with nothing logged anywhere. Home
    Assistant hands this file to the browser by three routes, so a repeat run is
    normal - catching the redefine is the failure mode worth having.
    """
    code = _code_only(SOURCE.read_text())
    assert "customElements.define(" in code
    assert "customElements.get(" not in code, "define must not be conditional"
