"""End-to-end setup of a config entry.

The unit tests build coordinators and entities by hand, which never exercises
async_setup_entry itself - where the coordinator is handed to the platforms,
the stop's device is created, and the options listener is wired up.
"""

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ul_transport.const import (
    CONF_ENTRY_TYPE,
    CONF_GTFS_REALTIME_KEY,
    CONF_GTFS_STATIC_KEY,
    CONF_SCAN_INTERVAL,
    CONF_SELECTED_LINES,
    CONF_STOP_ID,
    CONF_STOP_NAME,
    DOMAIN,
    ENTRY_TYPE_TRAFIKLAB,
    TRAFIKLAB_UNIQUE_ID,
)
from custom_components.ul_transport.coordinator import (
    ULTransportDataUpdateCoordinator,
    trafiklab_keys,
)

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
    """Only the stop. The quota meters hang off the Trafiklab entry instead."""
    await _setup(hass, entry)

    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert len(devices) == 1
    assert devices[0].identifiers == {(DOMAIN, str(MOCK_STOP_ID))}
    assert devices[0].name == MOCK_STOP_NAME


async def test_every_entity_lands_on_that_device(hass, entry):
    await _setup(hass, entry)

    registry = er.async_get(hass)
    entities = er.async_entries_for_config_entry(registry, entry.entry_id)
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    device_id = next(
        d.id for d in devices if d.identifiers == {(DOMAIN, str(MOCK_STOP_ID))}
    )

    unique_ids = {e.unique_id for e in entities}
    assert f"{MOCK_STOP_ID}_refresh" in unique_ids
    assert f"{MOCK_STOP_ID}_next_departure" in unique_ids
    assert f"{MOCK_STOP_ID}_last_update" in unique_ids
    assert f"{MOCK_STOP_ID}_3_Nyby_in" in unique_ids
    # A stop no longer carries the quota meters at all.
    assert not [e for e in entities if e.unique_id.endswith("_quota")]

    for entity in entities:
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
    assert f"{MOCK_STOP_NAME} Line 3 to Nyby" in names
    assert f"{MOCK_STOP_NAME} Refresh" in names


async def test_an_options_change_reloads_exactly_once(hass, entry):
    """Two update listeners were registered, so every save reloaded twice."""
    await _setup(hass, entry)

    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload:
        hass.config_entries.async_update_entry(
            entry, options={CONF_SELECTED_LINES: ["3_Nyby"]}
        )
        await hass.async_block_till_done()

    assert reload.call_count == 1


async def test_unload_releases_the_entry(hass, entry):
    await _setup(hass, entry)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert DOMAIN not in hass.data or "map" not in hass.data[DOMAIN]


def _trafiklab(hass, static="static-key", realtime="rt-key"):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=TRAFIKLAB_UNIQUE_ID,
        title="Trafiklab API",
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_TRAFIKLAB,
            CONF_GTFS_STATIC_KEY: static,
            CONF_GTFS_REALTIME_KEY: realtime,
        },
    )
    entry.add_to_hass(hass)
    return entry


async def test_the_keys_entry_owns_the_quota_meters(hass):
    """One pair per install, on the device for the key they actually watch."""
    keys = _trafiklab(hass)

    with patch("custom_components.ul_transport._async_serve_card", AsyncMock()):
        assert await hass.config_entries.async_setup(keys.entry_id)
        await hass.async_block_till_done()

    entities = er.async_entries_for_config_entry(er.async_get(hass), keys.entry_id)
    assert {e.unique_id for e in entities} == {
        f"{DOMAIN}_realtime_quota",
        f"{DOMAIN}_static_quota",
    }
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), keys.entry_id)
    assert [d.identifiers for d in devices] == [{(DOMAIN, "trafiklab")}]


async def test_removing_a_stop_leaves_the_keys_alone(hass, entry):
    """The bug this split exists to fix.

    The keys used to live on whichever stop was set up first, so removing that
    stop took the live map down for every other stop on the install.
    """
    keys = _trafiklab(hass)
    # Setting up either entry sets the domain up, and that sets up both - so
    # the stop's session has to be patched around the whole thing.
    with (
        patch("custom_components.ul_transport._async_serve_card", AsyncMock()),
        patch(
            "custom_components.ul_transport.coordinator.async_get_clientsession",
            return_value=build_session(),
        ),
        patch("custom_components.ul_transport._async_setup_map", AsyncMock()),
    ):
        assert await hass.config_entries.async_setup(keys.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED

        await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()

    assert hass.config_entries.async_get_entry(keys.entry_id) is not None
    assert trafiklab_keys(hass) == ("static-key", "rt-key")


async def test_keys_left_on_a_stop_move_to_their_own_entry(hass):
    """Upgrading from 2.2 or earlier, where a stop was the only place for them."""
    legacy = MockConfigEntry(
        domain=DOMAIN,
        unique_id=str(MOCK_STOP_ID),
        title=MOCK_STOP_NAME,
        data={
            CONF_STOP_ID: MOCK_STOP_ID,
            CONF_STOP_NAME: MOCK_STOP_NAME,
            CONF_SELECTED_LINES: [],
            CONF_SCAN_INTERVAL: 60,
        },
        options={
            CONF_GTFS_STATIC_KEY: "old-static",
            CONF_GTFS_REALTIME_KEY: "old-rt",
        },
    )
    legacy.add_to_hass(hass)

    with (
        patch("custom_components.ul_transport._async_serve_card", AsyncMock()),
        patch(
            "custom_components.ul_transport.coordinator.async_get_clientsession",
            return_value=build_session(),
        ),
        patch("custom_components.ul_transport._async_setup_map", AsyncMock()),
    ):
        assert await async_setup_component(hass, DOMAIN, {})
        await hass.async_block_till_done()

    # Adopted, with the keys intact...
    assert trafiklab_keys(hass) == ("old-static", "old-rt")
    # ...and gone from the stop, so there is only ever one place to edit them.
    assert CONF_GTFS_STATIC_KEY not in legacy.options
    assert CONF_GTFS_REALTIME_KEY not in legacy.options


async def test_migration_is_not_re_run_when_the_keys_already_moved(hass):
    """A second restart must not make a rival entry."""
    _trafiklab(hass)
    legacy = MockConfigEntry(
        domain=DOMAIN,
        unique_id=str(MOCK_STOP_ID),
        data={CONF_STOP_ID: MOCK_STOP_ID, CONF_STOP_NAME: MOCK_STOP_NAME},
        options={CONF_GTFS_STATIC_KEY: "stale", CONF_GTFS_REALTIME_KEY: "stale"},
    )
    legacy.add_to_hass(hass)

    with (
        patch("custom_components.ul_transport._async_serve_card", AsyncMock()),
        patch(
            "custom_components.ul_transport.coordinator.async_get_clientsession",
            return_value=build_session(),
        ),
        patch("custom_components.ul_transport._async_setup_map", AsyncMock()),
    ):
        assert await async_setup_component(hass, DOMAIN, {})
        await hass.async_block_till_done()

    keys_entries = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_TRAFIKLAB
    ]
    assert len(keys_entries) == 1
    # The stale copy is still stripped, so it cannot be edited by mistake.
    assert CONF_GTFS_STATIC_KEY not in legacy.options
