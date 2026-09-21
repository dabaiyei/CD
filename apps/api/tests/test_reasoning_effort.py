"""Model declares which thinking levels it takes; the agent picks one of them."""
import pytest

from app.services.agent_runtime import resolve_max_tokens, resolve_reasoning_effort


def model(*levels, default="", max_tokens=None):
    capabilities = {"reasoning_efforts": list(levels)}
    if default:
        capabilities["default_reasoning_effort"] = default
    if max_tokens is not None:
        capabilities["max_tokens"] = max_tokens
    return capabilities


def test_agent_picks_a_level_the_model_declares():
    assert resolve_reasoning_effort({"reasoning_effort": "high"}, model("low", "high")) == "high"


def test_agent_level_the_model_does_not_support_is_dropped():
    # Sending it anyway would look like a working setting that the provider ignores.
    assert resolve_reasoning_effort({"reasoning_effort": "xhigh"}, model("low", "medium")) is None


def test_agent_falls_back_to_the_models_own_default():
    assert resolve_reasoning_effort({}, model("low", "high", default="high")) == "high"
    # A default the model does not list is not silently trusted.
    assert resolve_reasoning_effort({}, model("low", default="xhigh")) is None


def test_models_that_declare_nothing_keep_the_previous_behaviour():
    # A model with no declared levels accepts any valid level, so existing setups
    # do not silently lose their setting after this change.
    assert resolve_reasoning_effort({"reasoning_effort": "medium"}, {}) == "medium"
    assert resolve_reasoning_effort({}, {}) is None


def test_an_unknown_level_is_rejected_even_without_a_declaration():
    assert resolve_reasoning_effort({"reasoning_effort": "ultra"}, {}) is None


def test_thinking_can_be_explicitly_disabled():
    assert resolve_reasoning_effort({"reasoning_effort": "none"}, model("low", "high")) == "none"


@pytest.mark.parametrize("value", ["", "  ", None])
def test_a_blank_agent_value_defers_to_the_model_default(value):
    # Blank must mean "unset", not "override with nothing".
    assert resolve_reasoning_effort({"reasoning_effort": value}, model("high", default="high")) == "high"
    assert resolve_reasoning_effort({"reasoning_effort": value}, model("high")) is None


def test_levels_are_case_insensitive():
    assert resolve_reasoning_effort({"reasoning_effort": "HIGH"}, model("high")) == "high"


def test_max_tokens_prefers_the_agent_override_then_the_model_default():
    assert resolve_max_tokens({"max_tokens": 8000}, model(max_tokens=4000)) == 8000
    assert resolve_max_tokens({}, model(max_tokens=4000)) == 4000
    assert resolve_max_tokens({}, {}) is None


def test_max_tokens_ignores_placeholder_values():
    for value in (0, -1, True, "many", None):
        assert resolve_max_tokens({"max_tokens": value}, {}) is None
        assert resolve_max_tokens({}, {"max_tokens": value}) is None


def test_max_tokens_ceiling_caps_a_generous_setting():
    assert resolve_max_tokens({"max_tokens": 20000}, {}, ceiling=4000) == 4000
    assert resolve_max_tokens({"max_tokens": 1000}, {}, ceiling=4000) == 1000
