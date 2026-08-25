"""Config and options flow.

The flow is the only part of the integration a user drives by hand, and the
only place the Trafiklab keys can be typed - so the cases that matter are the
ones where a wrong answer silently loses configuration.

Since 2.3 the keys live in an entry of their own rather than on whichever stop
happened to be set up first. The tests that matter most here are the ones
pinning that down: one home for the keys, and stops that come and go without
touching them.
"""

from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ul_transport.config_flow import CannotConnect
from custom_components.ul_transport.const import (
    CONF_ENTRY_TYPE,
    CONF_GTFS_REALTIME_KEY,
    CONF_GTFS_STATIC_KEY,
    CONF_SCAN_INTERVAL,
    CONF_SELECTED_LINES,
    CONF_STOP_ID,
    CONF_STOP_NAME,
    DOMAIN,
    ENTRY_TYPE_STOP,
    ENTRY_TYPE_TRAFIKLAB,
    TRAFIKLAB_UNIQUE_ID,
)

from .conftest import MOCK_STOP_ID, MOCK_STOP_NAME

SEARCH_HIT = [
    {"id": MOCK_STOP_ID, "name": MOCK_STOP_NAME, "type": 0},
    # An address, which has no departure board - the flow must drop it.
    {"id": 999, "name": "Kungsgatan 1", "type": 1},
]
BOARD = (MOCK_STOP_NAME, ["3_Nyby", "5_Stenhagen"])
MODULE = "custom_components.ul_transport.config_flow"


def _patch(search=SEARCH_HIT, board=BOARD):
    return (
        patch(f"{MODULE}._async_get_json", return_value=search),
        patch(f"{MODULE}.async_get_board", return_value=board),
    )


def trafiklab_entry(hass, **kwargs):
    """The keys, in the entry that owns them."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=TRAFIKLAB_UNIQUE_ID,
        title="Trafiklab API",
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_TRAFIKLAB,
            CONF_GTFS_STATIC_KEY: "static-key",
            CONF_GTFS_REALTIME_KEY: "rt-key",
        },
        **kwargs,
    )
    entry.add_to_hass(hass)
    return entry


async def _start(hass):
    """Open the flow. Returns the menu, or the stop search once keys exist."""
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def _to_line_step(hass):
    """Walk the stop branch as far as the line picker."""
    search, board = _patch()
    with search, board:
        result = await _start(hass)
        if result["type"] is FlowResultType.MENU:
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"next_step_id": "stop"}
            )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"stop_query": "central"}
        )
        return await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_STOP_ID: str(MOCK_STOP_ID)}
        )


class TestTheFirstQuestion:
    """The live map is offered first, and explained, but never forced."""

    async def test_offers_the_map_before_anything_else(self, hass):
        result = await _start(hass)

        assert result["type"] is FlowResultType.MENU
        assert set(result["menu_options"]) == {"trafiklab", "stop"}

    async def test_skipping_goes_straight_to_the_stop_search(self, hass):
        result = await _start(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "stop"}
        )

        assert result["step_id"] == "stop"

    async def test_stops_asking_once_the_keys_have_a_home(self, hass):
        """Asking again would be a dead end - there is only one place for them."""
        trafiklab_entry(hass)

        result = await _start(hass)

        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "stop"


class TestTrafiklabEntry:
    async def test_creates_its_own_entry(self, hass):
        result = await _start(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "trafiklab"}
        )
        with patch(f"{MODULE}.async_validate_keys", return_value=None):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_GTFS_STATIC_KEY: " s-key ", CONF_GTFS_REALTIME_KEY: "r-key"},
            )

        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["data"] == {
            CONF_ENTRY_TYPE: ENTRY_TYPE_TRAFIKLAB,
            # Trimmed: a key pasted with a stray space fails much later and much
            # more quietly.
            CONF_GTFS_STATIC_KEY: "s-key",
            CONF_GTFS_REALTIME_KEY: "r-key",
        }

    async def test_a_rejected_key_is_named(self, hass):
        """The error lands on the field, so it is clear which of the two is wrong."""
        from custom_components.ul_transport.config_flow import InvalidKey

        result = await _start(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "trafiklab"}
        )
        with patch(
            f"{MODULE}.async_validate_keys",
            side_effect=InvalidKey(CONF_GTFS_REALTIME_KEY),
        ):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_GTFS_STATIC_KEY: "s", CONF_GTFS_REALTIME_KEY: "bad"},
            )

        assert result["errors"] == {CONF_GTFS_REALTIME_KEY: "invalid_key"}
        assert result["step_id"] == "trafiklab"

    async def test_trafiklab_outage_is_not_a_rejected_key(self, hass):
        result = await _start(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "trafiklab"}
        )
        with patch(f"{MODULE}.async_validate_keys", side_effect=CannotConnect("boom")):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_GTFS_STATIC_KEY: "s", CONF_GTFS_REALTIME_KEY: "r"},
            )

        assert result["errors"] == {"base": "cannot_connect"}

    async def test_a_second_set_of_keys_aborts(self, hass):
        """One home for them, so a rival entry must never be created."""
        trafiklab_entry(hass)

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        # The menu is gone, so reach the step the way a stale browser tab would.
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_IMPORT},
            data={CONF_GTFS_STATIC_KEY: "s", CONF_GTFS_REALTIME_KEY: "r"},
        )

        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "already_configured"


class TestImportedKeys:
    """Keys an older version left on a stop are adopted, not re-validated."""

    async def test_import_creates_the_entry(self, hass):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_IMPORT},
            data={CONF_GTFS_STATIC_KEY: "old-s", CONF_GTFS_REALTIME_KEY: "old-r"},
        )

        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["data"][CONF_ENTRY_TYPE] == ENTRY_TYPE_TRAFIKLAB
        assert result["data"][CONF_GTFS_STATIC_KEY] == "old-s"

    async def test_import_does_not_call_trafiklab(self, hass):
        """An outage during a restart must not be what loses working keys."""
        with patch(f"{MODULE}.async_validate_keys") as validate:
            await hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": config_entries.SOURCE_IMPORT},
                data={CONF_GTFS_STATIC_KEY: "s", CONF_GTFS_REALTIME_KEY: "r"},
            )

        validate.assert_not_called()


class TestStopEntry:
    async def test_search_only_offers_real_stops(self, hass):
        """Addresses and POIs have no GTFS counterpart and no departures."""
        trafiklab_entry(hass)
        search, _ = _patch()
        with search:
            result = await _start(hass)
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
            {CONF_SELECTED_LINES: ["3_Nyby"], CONF_SCAN_INTERVAL: 120},
        )

        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["title"] == MOCK_STOP_NAME
        assert result["data"] == {
            CONF_ENTRY_TYPE: ENTRY_TYPE_STOP,
            CONF_STOP_ID: MOCK_STOP_ID,
            CONF_STOP_NAME: MOCK_STOP_NAME,
            CONF_SELECTED_LINES: ["3_Nyby"],
            CONF_SCAN_INTERVAL: 120,
        }

    async def test_carries_no_keys(self, hass):
        """A stop that held keys is exactly the bug this release fixes."""
        result = await _to_line_step(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

        assert CONF_GTFS_STATIC_KEY not in result["data"]
        assert CONF_GTFS_REALTIME_KEY not in result["data"]

    async def test_no_lines_picked_means_every_line(self, hass):
        """Empty stays empty, so a line that only runs at 07:00 still appears."""
        result = await _to_line_step(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

        assert result["data"][CONF_SELECTED_LINES] == []

    @pytest.mark.parametrize(
        ("side_effect", "expected"),
        [(CannotConnect("boom"), "cannot_connect"), (RuntimeError("nope"), "unknown")],
    )
    async def test_a_broken_api_is_an_error_not_a_crash(
        self, hass, side_effect, expected
    ):
        trafiklab_entry(hass)
        with patch(f"{MODULE}._async_get_json", side_effect=side_effect):
            result = await _start(hass)
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"stop_query": "central"}
            )

        assert result["errors"] == {"base": expected}

    async def test_nothing_matched(self, hass):
        trafiklab_entry(hass)
        search, _ = _patch(search=[])
        with search:
            result = await _start(hass)
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"stop_query": "atlantis"}
            )

        assert result["errors"] == {"base": "no_stops_found"}

    async def test_the_same_stop_twice_aborts(self, hass):
        MockConfigEntry(domain=DOMAIN, unique_id=str(MOCK_STOP_ID)).add_to_hass(hass)

        result = await _to_line_step(hass)

        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "already_configured"


def _stop(hass, stop_id=MOCK_STOP_ID, name=MOCK_STOP_NAME, options=None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=str(stop_id),
        title=name,
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_STOP,
            CONF_STOP_ID: stop_id,
            CONF_STOP_NAME: name,
            CONF_SELECTED_LINES: [],
            CONF_SCAN_INTERVAL: 60,
        },
        options=options or {},
    )
    entry.add_to_hass(hass)
    return entry


class TestStopOptions:
    async def test_never_offers_the_key_fields(self, hass):
        """There is one place to type them, and a stop is not it."""
        entry = _stop(hass)
        _, board = _patch()
        with board:
            result = await hass.config_entries.options.async_init(entry.entry_id)

        fields = {str(key) for key in result["data_schema"].schema}
        assert CONF_GTFS_STATIC_KEY not in fields
        assert CONF_GTFS_REALTIME_KEY not in fields

    async def test_saves_lines_and_interval(self, hass):
        entry = _stop(hass)
        _, board = _patch()
        with board:
            result = await hass.config_entries.options.async_init(entry.entry_id)
            result = await hass.config_entries.options.async_configure(
                result["flow_id"],
                {CONF_SELECTED_LINES: ["5_Stenhagen"], CONF_SCAN_INTERVAL: 300},
            )

        assert result["data"] == {
            CONF_SELECTED_LINES: ["5_Stenhagen"],
            CONF_SCAN_INTERVAL: 300,
        }

    async def test_saving_a_stop_leaves_the_keys_alone(self, hass):
        keys_entry = trafiklab_entry(hass)
        entry = _stop(hass)

        _, board = _patch()
        with board:
            result = await hass.config_entries.options.async_init(entry.entry_id)
            await hass.config_entries.options.async_configure(
                result["flow_id"], {CONF_SELECTED_LINES: [], CONF_SCAN_INTERVAL: 60}
            )

        assert keys_entry.data[CONF_GTFS_STATIC_KEY] == "static-key"

    async def test_a_line_that_stopped_running_can_still_be_deselected(self, hass):
        """It is missing from the board, so only the saved selection offers it."""
        entry = _stop(hass, options={CONF_SELECTED_LINES: ["99_Gone"]})
        _, board = _patch()
        with board:
            result = await hass.config_entries.options.async_init(entry.entry_id)

        selector = result["data_schema"].schema[CONF_SELECTED_LINES]
        labels = {o["value"]: o["label"] for o in selector.config["options"]}
        assert "99_Gone" in labels
        assert "(no departures)" in labels["99_Gone"]

    async def test_a_dead_api_still_lets_the_form_open(self, hass):
        entry = _stop(hass)
        with patch(f"{MODULE}.async_get_board", side_effect=CannotConnect("boom")):
            result = await hass.config_entries.options.async_init(entry.entry_id)

        assert result["errors"] == {"base": "cannot_connect"}
        assert result["step_id"] == "init"


class TestTrafiklabOptions:
    async def test_edits_the_keys(self, hass):
        entry = trafiklab_entry(hass)

        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["step_id"] == "keys"

        with patch(f"{MODULE}.async_validate_keys", return_value=None):
            result = await hass.config_entries.options.async_configure(
                result["flow_id"],
                {CONF_GTFS_STATIC_KEY: "new-s", CONF_GTFS_REALTIME_KEY: "new-r"},
            )

        assert result["data"] == {
            CONF_GTFS_STATIC_KEY: "new-s",
            CONF_GTFS_REALTIME_KEY: "new-r",
        }

    async def test_prefills_what_is_stored(self, hass):
        entry = trafiklab_entry(hass)

        result = await hass.config_entries.options.async_init(entry.entry_id)

        defaults = {
            str(key): key.default()
            for key in result["data_schema"].schema
            if key.default
        }
        assert defaults[CONF_GTFS_STATIC_KEY] == "static-key"

    async def test_a_rejected_key_does_not_overwrite_a_working_one(self, hass):
        from custom_components.ul_transport.config_flow import InvalidKey

        entry = trafiklab_entry(hass)

        result = await hass.config_entries.options.async_init(entry.entry_id)
        with patch(
            f"{MODULE}.async_validate_keys",
            side_effect=InvalidKey(CONF_GTFS_STATIC_KEY),
        ):
            result = await hass.config_entries.options.async_configure(
                result["flow_id"],
                {CONF_GTFS_STATIC_KEY: "typo", CONF_GTFS_REALTIME_KEY: "rt-key"},
            )

        assert result["errors"] == {CONF_GTFS_STATIC_KEY: "invalid_key"}
        assert entry.data[CONF_GTFS_STATIC_KEY] == "static-key"


class TestKeyValidation:
    """The one place a typo is cheap to catch.

    Wrong keys otherwise fail much later and much more quietly: the sensors
    carry on working and the map simply never appears.
    """

    @staticmethod
    def _session(status_by_url):
        """A session whose GET answers per URL, like the shared one does."""
        from unittest.mock import AsyncMock, MagicMock

        def get(url, **kwargs):
            response = AsyncMock()
            response.status = next(
                status for fragment, status in status_by_url.items() if fragment in url
            )
            context = AsyncMock()
            context.__aenter__ = AsyncMock(return_value=response)
            context.__aexit__ = AsyncMock(return_value=False)
            return context

        session = MagicMock()
        session.get = MagicMock(side_effect=get)
        return session

    async def _validate(self, hass, session):
        from custom_components.ul_transport.config_flow import async_validate_keys

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            await async_validate_keys(hass, "static", "realtime")

    async def test_two_good_keys_pass(self, hass):
        await self._validate(hass, self._session({"gtfs/ul": 200, "gtfs-rt": 200}))

    @pytest.mark.parametrize("status", [401, 403])
    async def test_a_refused_static_key_names_the_static_field(self, hass, status):
        from custom_components.ul_transport.config_flow import InvalidKey

        with pytest.raises(InvalidKey) as err:
            await self._validate(
                hass, self._session({"gtfs/ul": status, "gtfs-rt": 200})
            )

        assert err.value.field == CONF_GTFS_STATIC_KEY

    async def test_keys_swapped_between_the_boxes_are_caught(self, hass):
        """The commonest mistake: each key is valid, but for the other dataset."""
        from custom_components.ul_transport.config_flow import InvalidKey

        with pytest.raises(InvalidKey) as err:
            await self._validate(hass, self._session({"gtfs/ul": 200, "gtfs-rt": 403}))

        assert err.value.field == CONF_GTFS_REALTIME_KEY

    async def test_a_server_error_is_not_blamed_on_the_key(self, hass):
        with pytest.raises(CannotConnect):
            await self._validate(hass, self._session({"gtfs/ul": 500, "gtfs-rt": 200}))

    async def test_a_timeout_is_not_blamed_on_the_key(self, hass):
        from unittest.mock import MagicMock

        session = MagicMock()
        session.get = MagicMock(side_effect=TimeoutError())
        with pytest.raises(CannotConnect):
            await self._validate(hass, session)

    async def test_the_static_body_is_never_downloaded(self, hass):
        """It is ~26 MB. The status alone says whether the key works."""
        session = self._session({"gtfs/ul": 200, "gtfs-rt": 200})
        await self._validate(hass, session)

        for call in session.get.call_args_list:
            context = session.get.side_effect(*call.args, **call.kwargs)
            response = await context.__aenter__()
            response.read.assert_not_called()
            response.json.assert_not_called()

    async def test_both_calls_are_counted_against_the_quota(self, hass):
        """Trafiklab counts them, so the meters have to as well."""
        from custom_components.ul_transport.quota import request_quota

        await self._validate(hass, self._session({"gtfs/ul": 200, "gtfs-rt": 200}))

        totals = request_quota(hass).totals()
        assert totals["gtfs_static"] == 1
        assert totals["rt_TripUpdates"] == 1
