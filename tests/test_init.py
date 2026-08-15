"""End-to-end setup of a config entry.

The unit tests build coordinators and entities by hand, which never exercises
async_setup_entry itself - where the coordinator is handed to the platforms,
the stop's device is created, and the options listener is wired up.
"""

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ul_transport.const import (
    CONF_SCAN_INTERVAL,
    CONF_SELECTED_LINES,
    CONF_STOP_ID,
    CONF_STOP_NAME,
    DOMAIN,
)
from custom_components.ul_transport.coordinator import ULTransportDataUpdateCoordinator

from .conftest import MOCK_STOP_ID, MOCK_STOP_NAME, build_session


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    """A stop with no Trafiklab keys, so the live map stays out of the way."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=str(MOCK_STOP_ID),
        title=MOCK_STOP_NAME,
        data={
            CONF_STOP_ID: MOCK_STOP_ID,
            CONF_STOP_NAME: MOCK_STOP_NAME,
            CONF_SELECTED_LINES: [],
            CONF_SCAN_INTERVAL: 60,
        },
    )
    entry.add_to_hass(hass)
    return entry


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> bool:
    # The card is served over hass.http, which a bare test instance has no
    # route table for; it is additive to the sensors and tested separately.
    with (
        patch("custom_components.ul_transport._async_serve_card", AsyncMock()),
        patch(
            "custom_components.ul_transport.coordinator.async_get_clientsession",
            return_value=build_session(),
        ),
    ):
        result = await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return result


async def test_sets_up_and_hands_the_coordinator_to_the_platforms(hass, entry):
    assert await _setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert isinstance(entry.runtime_data, ULTransportDataUpdateCoordinator)
    assert entry.runtime_data.stop_id == MOCK_STOP_ID


async def test_creates_one_device_for_the_stop(hass, entry):
    await _setup(hass, entry)

    devices = dr.async_entries_for_config_entry(
        dr.async_get(hass), entry.entry_id
    )
    assert len(devices) == 1
    assert devices[0].name == MOCK_STOP_NAME
    assert devices[0].identifiers == {(DOMAIN, str(MOCK_STOP_ID))}


async def test_every_entity_lands_on_that_device(hass, entry):
    await _setup(hass, entry)

    registry = er.async_get(hass)
    entities = er.async_entries_for_config_entry(registry, entry.entry_id)
    device_id = dr.async_entries_for_config_entry(
        dr.async_get(hass), entry.entry_id
    )[0].id

    unique_ids = {e.unique_id for e in entities}
    assert f"{MOCK_STOP_ID}_refresh" in unique_ids
    assert f"{MOCK_STOP_ID}_next_departure" in unique_ids
    assert f"{MOCK_STOP_ID}_last_update" in unique_ids
    assert f"{MOCK_STOP_ID}_2_Uppsala Central_in" in unique_ids

    # The account-wide request counter belongs to no single stop.
    for entity in entities:
        if entity.unique_id == f"{DOMAIN}_api_requests":
            assert entity.device_id is None
        else:
            assert entity.device_id == device_id


async def test_the_stop_name_prefixes_the_entity_names(hass, entry):
    """has_entity_name composes "<stop> <entity>" - the pre-2.0.1 friendly name."""
    await _setup(hass, entry)

    names = {
        state.attributes.get("friendly_name")
        for state in hass.states.async_all()
        if state.entity_id.startswith(("sensor.", "button."))
    }
    assert f"{MOCK_STOP_NAME} Next departure" in names
    assert f"{MOCK_STOP_NAME} Line 2 to Uppsala Central" in names
    assert f"{MOCK_STOP_NAME} Refresh" in names


async def test_an_options_change_reloads_exactly_once(hass, entry):
    """Two update listeners were registered, so every save reloaded twice."""
    await _setup(hass, entry)

    with patch.object(
        hass.config_entries, "async_reload", AsyncMock()
    ) as reload:
        hass.config_entries.async_update_entry(
            entry, options={CONF_SELECTED_LINES: ["2_Uppsala Central"]}
        )
        await hass.async_block_till_done()

    assert reload.call_count == 1


async def test_unload_releases_the_entry(hass, entry):
    await _setup(hass, entry)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert DOMAIN not in hass.data or "map" not in hass.data[DOMAIN]
