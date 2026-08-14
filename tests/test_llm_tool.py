"""Tests for the Assist departures tool.

The card config in the result is the contract with the voice satellite: the
satellite draws whatever Lovelace config lands there, so a wrong stop_id shows
the wrong stop's board on the tablet without the spoken answer being wrong.
"""
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.ul_transport.const import DOMAIN
from custom_components.ul_transport.coordinator import ULTransportDataUpdateCoordinator
from custom_components.ul_transport.llm_tool import NextDeparturesTool

from .conftest import MOCK_STOP_ID, MOCK_STOP_NAME


def _stamp(minutes: float) -> str:
    when = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _departure(planned: float, estimated: float | None = None, line="2",
               towards="Uppsala Central") -> dict:
    return {
        "line": {"name": line, "towards": towards, "lineNo": int(line), "trafficType": 1},
        "departureDateTime": _stamp(planned),
        "realTimeDepartureDateTime": None if estimated is None else _stamp(estimated),
    }


class _ToolInput:
    """Stand-in for llm.ToolInput; only the args are read."""

    def __init__(self, **args):
        self.tool_args = args


@pytest.fixture
def hass_with_stop(hass):
    coordinator = ULTransportDataUpdateCoordinator(
        hass, MOCK_STOP_ID, MOCK_STOP_NAME, [], 60
    )
    coordinator.data = {
        "2_Uppsala Central": [_departure(4.05, 6.05), _departure(20.05)],
        "8_Gottsunda": [_departure(2.05, 2.05, line="8", towards="Gottsunda")],
    }
    hass.data[DOMAIN] = {"entry_id": coordinator, "map_loading": False}
    return hass


async def _call(hass, **args):
    return await NextDeparturesTool().async_call(hass, _ToolInput(**args), None)


async def test_answers_with_the_soonest_bus_first(hass_with_stop):
    result = await _call(hass_with_stop)

    assert result["stop"] == MOCK_STOP_NAME
    assert [d["line"] for d in result["departures"]] == ["8", "2", "2"]
    assert result["departures"][0]["in_minutes"] == 2
    # Six minutes out, two of them late.
    assert result["departures"][1]["in_minutes"] == 6
    assert result["departures"][1]["delay_minutes"] == 2
    # No real-time report is not the same as being on time.
    assert result["departures"][2]["delay_minutes"] is None


async def test_card_points_at_the_stop_that_was_answered(hass_with_stop):
    result = await _call(hass_with_stop)

    assert result["card"] == {
        "type": "custom:ul-transport-map",
        "stop_id": MOCK_STOP_ID,
        "content": "list",
        "list_count": 5,
        "card_module": "/ul_transport/ul-transport-map.js",
        "card_scale": 1.25,
    }


async def test_line_filters_both_the_answer_and_the_card(hass_with_stop):
    result = await _call(hass_with_stop, line="2")

    assert {d["line"] for d in result["departures"]} == {"2"}
    assert result["card"]["lines"] == ["2"]


async def test_a_misheard_stop_name_still_answers_the_only_stop(hass_with_stop):
    result = await _call(hass_with_stop, stop="central station")

    assert result["stop"] == MOCK_STOP_NAME


async def test_an_unknown_stop_lists_the_configured_ones(hass, hass_with_stop):
    other = ULTransportDataUpdateCoordinator(hass, 1, "Vaksala torg", [], 60)
    other.data = {}
    hass.data[DOMAIN]["other_entry"] = other

    result = await _call(hass_with_stop, stop="Gothenburg")

    assert "error" in result
    assert set(result["configured_stops"]) == {MOCK_STOP_NAME, "Vaksala torg"}


async def test_no_departures_still_hands_over_a_card(hass_with_stop):
    hass_with_stop.data[DOMAIN]["entry_id"].data = {}

    result = await _call(hass_with_stop)

    assert result["departures"] == []
    assert result["card"]["stop_id"] == MOCK_STOP_ID


async def test_nothing_configured_is_an_error_not_a_crash(hass):
    hass.data[DOMAIN] = {}

    result = await _call(hass)

    assert "error" in result


async def test_the_api_registers_and_withdraws(hass):
    """The setup path itself - a bad API definition breaks startup, not a query."""
    from homeassistant.helpers import llm

    from custom_components.ul_transport.llm_tool import (
        API_ID,
        async_register,
        async_unregister,
    )

    hass.data[DOMAIN] = {}
    async_register(hass)
    async_register(hass)  # second stop, same tool

    assert API_ID in [api.id for api in llm.async_get_apis(hass)]

    async_unregister(hass)
    assert API_ID not in [api.id for api in llm.async_get_apis(hass)]
