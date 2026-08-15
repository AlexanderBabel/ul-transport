"""Config and options flow.

The flow is the only part of the integration a user drives by hand, and the
only place the Trafiklab keys can be typed - so the cases that matter are the
ones where a wrong answer silently loses configuration.
"""

from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ul_transport.config_flow import CannotConnect
from custom_components.ul_transport.const import (
    CONF_GTFS_REALTIME_KEY,
    CONF_GTFS_STATIC_KEY,
    CONF_SCAN_INTERVAL,
    CONF_SELECTED_LINES,
    CONF_STOP_ID,
    CONF_STOP_NAME,
    DOMAIN,
)

from .conftest import MOCK_STOP_ID, MOCK_STOP_NAME

SEARCH_HIT = [
    {"id": MOCK_STOP_ID, "name": MOCK_STOP_NAME, "type": 0},
    # An address, which has no departure board - the flow must drop it.
    {"id": 999, "name": "Kungsgatan 1", "type": 1},
]
BOARD = (MOCK_STOP_NAME, ["2_Uppsala Central", "8_Gottsunda"])


def _patch(search=SEARCH_HIT, board=BOARD):
    module = "custom_components.ul_transport.config_flow"
    return (
        patch(f"{module}._async_get_json", return_value=search),
        patch(f"{module}.async_get_board", return_value=board),
    )


async def _to_line_step(hass):
    """Walk the flow as far as the line picker."""
    search, board = _patch()
    with search, board:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"stop_query": "central"}
        )
        return await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_STOP_ID: str(MOCK_STOP_ID)}
        )


class TestConfigFlow:
    async def test_search_only_offers_real_stops(self, hass):
        """Addresses and POIs have no GTFS counterpart and no departures."""
        search, _ = _patch()
        with search:
            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": config_entries.SOURCE_USER}
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"stop_query": "central"}
            )

        assert result["step_id"] == "select_stop"
        options = result["data_schema"].schema[CONF_STOP_ID].container
        assert options == {str(MOCK_STOP_ID): MOCK_STOP_NAME}

    async def test_creates_the_entry(self, hass):
        result = await _to_line_step(hass)
        assert result["step_id"] == "select_lines"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_SELECTED_LINES: ["2_Uppsala Central"], CONF_SCAN_INTERVAL: 120},
        )

        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["title"] == MOCK_STOP_NAME
        assert result["data"] == {
            CONF_STOP_ID: MOCK_STOP_ID,
            CONF_STOP_NAME: MOCK_STOP_NAME,
            CONF_SELECTED_LINES: ["2_Uppsala Central"],
            CONF_SCAN_INTERVAL: 120,
        }

    async def test_no_lines_picked_means_every_line(self, hass):
        """Empty stays empty, so a line that only runs at 07:00 still appears."""
        result = await _to_line_step(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

        assert result["data"][CONF_SELECTED_LINES] == []

    async def test_a_dead_api_is_an_error_not_a_crash(self, hass):
        module = "custom_components.ul_transport.config_flow"
        with patch(f"{module}._async_get_json", side_effect=CannotConnect("boom")):
            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": config_entries.SOURCE_USER}
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"stop_query": "central"}
            )

        assert result["errors"] == {"base": "cannot_connect"}

    async def test_an_unexpected_error_does_not_escape(self, hass):
        module = "custom_components.ul_transport.config_flow"
        with patch(f"{module}._async_get_json", side_effect=RuntimeError("nope")):
            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": config_entries.SOURCE_USER}
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"stop_query": "central"}
            )

        assert result["errors"] == {"base": "unknown"}

    async def test_nothing_matched(self, hass):
        search, _ = _patch(search=[])
        with search:
            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": config_entries.SOURCE_USER}
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"stop_query": "atlantis"}
            )

        assert result["errors"] == {"base": "no_stops_found"}

    async def test_the_same_stop_twice_aborts(self, hass):
        MockConfigEntry(domain=DOMAIN, unique_id=str(MOCK_STOP_ID)).add_to_hass(hass)

        result = await _to_line_step(hass)

        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "already_configured"


def _entry(hass, stop_id=MOCK_STOP_ID, name=MOCK_STOP_NAME, options=None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=str(stop_id),
        title=name,
        data={
            CONF_STOP_ID: stop_id,
            CONF_STOP_NAME: name,
            CONF_SELECTED_LINES: [],
            CONF_SCAN_INTERVAL: 60,
        },
        options=options or {},
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def keys():
    return {CONF_GTFS_STATIC_KEY: "static-key", CONF_GTFS_REALTIME_KEY: "rt-key"}


class TestOptionsFlow:
    async def test_offers_the_key_fields_on_the_entry_that_holds_them(self, hass, keys):
        entry = _entry(hass, options=keys)
        _, board = _patch()
        with board:
            result = await hass.config_entries.options.async_init(entry.entry_id)

        fields = {str(key) for key in result["data_schema"].schema}
        assert CONF_GTFS_STATIC_KEY in fields
        assert CONF_GTFS_REALTIME_KEY in fields

    async def test_hides_the_key_fields_on_every_other_stop(self, hass, keys):
        """The keys are account-wide, so they are edited where they were typed."""
        holder = _entry(hass, options=keys)
        other = _entry(hass, stop_id=1, name="Vaksala torg")

        _, board = _patch()
        with board:
            result = await hass.config_entries.options.async_init(other.entry_id)

        fields = {str(key) for key in result["data_schema"].schema}
        assert CONF_GTFS_STATIC_KEY not in fields
        assert result["description_placeholders"]["keys"].endswith(f"{holder.title}.")

    async def test_saving_another_stop_does_not_blank_the_keys(self, hass, keys):
        """The fields are not on this form, so it must not write empties over them."""
        holder = _entry(hass, options=keys)
        other = _entry(hass, stop_id=1, name="Vaksala torg", options=dict(keys))

        _, board = _patch()
        with board:
            result = await hass.config_entries.options.async_init(other.entry_id)
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], {CONF_SELECTED_LINES: [], CONF_SCAN_INTERVAL: 60}
            )

        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert CONF_GTFS_STATIC_KEY not in result["data"]
        assert holder.options[CONF_GTFS_STATIC_KEY] == "static-key"

    async def test_saves_lines_interval_and_keys(self, hass):
        entry = _entry(hass)
        _, board = _patch()
        with board:
            result = await hass.config_entries.options.async_init(entry.entry_id)
            result = await hass.config_entries.options.async_configure(
                result["flow_id"],
                {
                    CONF_SELECTED_LINES: ["8_Gottsunda"],
                    CONF_SCAN_INTERVAL: 300,
                    CONF_GTFS_STATIC_KEY: "s",
                    CONF_GTFS_REALTIME_KEY: "r",
                },
            )

        assert result["data"] == {
            CONF_SELECTED_LINES: ["8_Gottsunda"],
            CONF_SCAN_INTERVAL: 300,
            CONF_GTFS_STATIC_KEY: "s",
            CONF_GTFS_REALTIME_KEY: "r",
        }

    async def test_a_line_that_stopped_running_can_still_be_deselected(self, hass):
        """It is missing from the board, so only the saved selection offers it."""
        entry = _entry(hass, options={CONF_SELECTED_LINES: ["99_Gone"]})
        _, board = _patch()
        with board:
            result = await hass.config_entries.options.async_init(entry.entry_id)

        selector = result["data_schema"].schema[CONF_SELECTED_LINES]
        labels = {o["value"]: o["label"] for o in selector.config["options"]}
        assert "99_Gone" in labels
        assert "(no departures)" in labels["99_Gone"]

    async def test_a_dead_api_still_lets_the_form_open(self, hass):
        entry = _entry(hass)
        module = "custom_components.ul_transport.config_flow"
        with patch(f"{module}.async_get_board", side_effect=CannotConnect("boom")):
            result = await hass.config_entries.options.async_init(entry.entry_id)

        assert result["errors"] == {"base": "cannot_connect"}
        assert result["step_id"] == "init"
