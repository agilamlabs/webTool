"""v1.6.6 backward-compat for BaseAction parent class.

Every existing Action input class now inherits from BaseAction instead
of BaseModel. The Pydantic v2 discriminated union (Field(discriminator=
"action")) dispatches on the ``action: Literal[...]`` field, not on class
identity, so the change MUST be transparent to:
  * Existing JSON callers that omit ``tab_id``.
  * TypeAdapter[Action] parsing.
"""

from __future__ import annotations

from pydantic import TypeAdapter
from web_agent.models import (
    Action,
    BaseAction,
    ClickInput,
    ClickXYInput,
    FillInput,
    NavigateInput,
    ScreenshotInput,
)


def test_existing_action_json_without_tab_id_still_parses() -> None:
    """v1.6.5 callers building action JSON did not include tab_id. Those
    payloads must still round-trip cleanly through TypeAdapter[Action]."""
    legacy_payloads = [
        {"action": "click", "selector": "#submit"},
        {"action": "fill", "selector": "input[name=q]", "value": "test"},
        {"action": "screenshot", "full_page": True},
        {"action": "navigate", "url": "https://example.com"},
    ]
    adapter = TypeAdapter(Action)
    for raw in legacy_payloads:
        parsed = adapter.validate_python(raw)
        # tab_id default is None and is NOT serialized back into the JSON
        # by default. Inherited from BaseAction.
        assert parsed.tab_id is None, parsed
        # Re-serialize and re-parse to confirm round-trip.
        re_parsed = adapter.validate_python(parsed.model_dump(exclude_none=True))
        assert type(re_parsed) is type(parsed)


def test_action_with_tab_id_roundtrips_through_typeadapter() -> None:
    """v1.6.6 callers may set tab_id; it must survive a JSON round trip."""
    payload = {
        "action": "click",
        "selector": "#go",
        "tab_id": "popup-abc123",
    }
    adapter = TypeAdapter(Action)
    parsed = adapter.validate_python(payload)
    assert isinstance(parsed, ClickInput)
    assert parsed.tab_id == "popup-abc123"

    # Round trip
    re_parsed = adapter.validate_python(parsed.model_dump())
    assert re_parsed.tab_id == "popup-abc123"


def test_all_existing_actions_inherit_base_action() -> None:
    """Sanity: every legacy action class is a BaseAction subclass."""
    for cls in (ClickInput, FillInput, ScreenshotInput, NavigateInput):
        assert issubclass(cls, BaseAction), cls
        assert "tab_id" in cls.model_fields, cls


def test_new_coord_actions_inherit_base_action() -> None:
    """New v1.6.6 coord-click actions also inherit BaseAction."""
    assert issubclass(ClickXYInput, BaseAction)
    assert "tab_id" in ClickXYInput.model_fields
