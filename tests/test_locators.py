"""Tests for LocatorSpec and SelectorLike on action models."""

from __future__ import annotations

import json

import pytest
from pydantic import TypeAdapter, ValidationError

from web_agent.models import (
    Action,
    ClickInput,
    FillInput,
    HoverInput,
    LocatorSpec,
    SelectInput,
    TypeInput,
)


class TestLocatorSpec:
    def test_role_only(self) -> None:
        spec = LocatorSpec(role="button")
        assert spec.role == "button"
        assert not spec.is_empty()

    def test_role_with_name(self) -> None:
        spec = LocatorSpec(role="button", role_name="Submit")
        assert spec.role_name == "Submit"

    def test_label_only(self) -> None:
        spec = LocatorSpec(label="Email")
        assert spec.label == "Email"

    def test_test_id_only(self) -> None:
        spec = LocatorSpec(test_id="login-btn")
        assert spec.test_id == "login-btn"

    def test_empty_returns_true(self) -> None:
        spec = LocatorSpec()
        assert spec.is_empty()

    def test_json_round_trip(self) -> None:
        spec = LocatorSpec(role="link", role_name="Home", text="Click")
        restored = LocatorSpec.model_validate_json(spec.model_dump_json())
        assert restored == spec


class TestActionInputAcceptsBothFormats:
    def test_click_with_string_selector(self) -> None:
        c = ClickInput(selector="button#submit")
        assert isinstance(c.selector, str)
        assert c.selector == "button#submit"

    def test_click_with_locator_spec(self) -> None:
        c = ClickInput(selector=LocatorSpec(role="button", role_name="Submit"))
        assert isinstance(c.selector, LocatorSpec)
        assert c.selector.role == "button"

    def test_click_with_dict_locator(self) -> None:
        c = ClickInput(selector={"role": "button", "role_name": "Submit"})
        assert isinstance(c.selector, LocatorSpec)
        assert c.selector.role_name == "Submit"

    def test_fill_with_label_locator(self) -> None:
        f = FillInput(selector={"label": "Email"}, value="me@example.com")
        assert isinstance(f.selector, LocatorSpec)
        assert f.selector.label == "Email"

    def test_type_with_test_id(self) -> None:
        t = TypeInput(selector={"test_id": "search"}, text="hello")
        assert isinstance(t.selector, LocatorSpec)
        assert t.selector.test_id == "search"

    def test_select_with_string_selector(self) -> None:
        s = SelectInput(selector="select#country", value="US")
        assert s.selector == "select#country"

    def test_hover_with_locator_spec(self) -> None:
        h = HoverInput(selector={"text": "Menu"})
        assert isinstance(h.selector, LocatorSpec)


class TestDiscriminatedUnionWithLocators:
    def test_actions_list_accepts_mixed_selectors(self) -> None:
        raw = [
            {"action": "fill", "selector": "#search", "value": "q"},
            {
                "action": "click",
                "selector": {"role": "button", "role_name": "Search"},
            },
        ]
        adapter = TypeAdapter(list[Action])
        actions = adapter.validate_python(raw)
        assert isinstance(actions[0].selector, str)
        assert isinstance(actions[1].selector, LocatorSpec)

    def test_action_serializes_locator_spec(self) -> None:
        c = ClickInput(selector={"role": "button", "role_name": "OK"})
        data = json.loads(c.model_dump_json())
        # selector should serialize as a nested object
        assert isinstance(data["selector"], dict)
        assert data["selector"]["role"] == "button"
