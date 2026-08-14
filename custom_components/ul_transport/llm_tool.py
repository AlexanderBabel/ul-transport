"""Assist tool: "when does the next bus leave?"

The answer is spoken from `departures`. `card` is for a voice satellite with a
screen: the Voice Satellite card draws any Lovelace config a tool result puts
there, so the map card comes up on the tablet while the LLM reads the times.
Assistants without a screen ignore it - it costs them a few tokens and nothing
else.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm

from .const import DOMAIN, TRAFFIC_TYPE_MAPPING
from .coordinator import ULTransportDataUpdateCoordinator
# The same "which timestamp counts" and "how many minutes is that" rules the
# sensors answer with; a tool that rounded differently would contradict them.
from .sensor import _departure, _minutes_until, _parse

API_ID = f"{DOMAIN}_departures"
API_NAME = "UL Transport departures"
API_PROMPT = (
    "Use get_next_departures for anything about buses, trains or departures "
    "from the stops this household has configured."
)

# Enough for "and the one after that?" without filling the context with a
# timetable the user did not ask for.
MAX_DEPARTURES = 5


def _coordinators(hass: HomeAssistant) -> list[ULTransportDataUpdateCoordinator]:
    """Every configured stop. hass.data[DOMAIN] also holds the map runtime."""
    return [
        value
        for value in hass.data.get(DOMAIN, {}).values()
        if isinstance(value, ULTransportDataUpdateCoordinator)
    ]


def _pick(
    coordinators: list[ULTransportDataUpdateCoordinator], stop: str | None
) -> ULTransportDataUpdateCoordinator | None:
    """The stop the question is about.

    Substring rather than exact: speech-to-text gives "Vaksala torg" for a stop
    configured as "Uppsala Vaksala torg". With one stop configured a name that
    matches nothing is a mishearing, not a different stop, so it still answers.
    """
    if stop:
        wanted = stop.casefold()
        for coordinator in coordinators:
            if wanted in coordinator.stop_name.casefold():
                return coordinator
    if len(coordinators) == 1:
        return coordinators[0]
    return None


class NextDeparturesTool(llm.Tool):
    """The next departures from a configured stop."""

    name = "get_next_departures"
    description = (
        "Get the next bus, train or tram departures from a public transport "
        "stop in the Uppsala (UL) region."
    )
    parameters = vol.Schema(
        {
            vol.Optional("stop"): str,
            vol.Optional("line"): str,
        }
    )

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> dict[str, Any]:
        """Answer, and hand a screen the map card for the same stop."""
        coordinators = _coordinators(hass)
        if not coordinators:
            return {"error": "No UL Transport stops are configured."}

        stop = tool_input.tool_args.get("stop")
        coordinator = _pick(coordinators, stop)
        if coordinator is None:
            return {
                "error": f"No configured stop matches {stop!r}.",
                "configured_stops": [c.stop_name for c in coordinators],
            }

        line = tool_input.tool_args.get("line")
        departures = [
            departure
            for group in (coordinator.data or {}).values()
            for departure in group
            if _departure(departure) is not None
            and (line is None or str(departure["line"]["name"]).strip() == line)
        ]
        departures.sort(key=_departure)

        card: dict[str, Any] = {
            "type": "custom:ul-transport-map",
            "stop_id": coordinator.stop_id,
            "content": "list",
            "list_count": MAX_DEPARTURES,
            # A satellite showing this outside a dashboard - on its own panel -
            # has no Lovelace to have loaded the card, so say where it lives.
            "card_module": f"/{DOMAIN}/ul-transport-map.js",
            # Dashboard type sizes are for a screen at arm's length. This is
            # read from wherever the tablet is on the wall.
            "card_scale": 1.25,
        }
        if line is not None:
            card["lines"] = [line]

        if not departures:
            return {
                "stop": coordinator.stop_name,
                "departures": [],
                "card": card,
                "instruction": "Say that nothing is departing from this stop soon.",
            }

        return {
            "stop": coordinator.stop_name,
            "departures": [_summary(d) for d in departures[:MAX_DEPARTURES]],
            "card": card,
            "instruction": (
                "Answer with the next departure, and the one after it if the "
                "first is very soon. Say the delay only when the bus is late. "
                "Speak naturally - do not read the list out verbatim."
            ),
        }


def _summary(departure: dict[str, Any]) -> dict[str, Any]:
    """One departure, small enough to send every turn."""
    line = departure["line"]
    planned = _parse(departure.get("departureDateTime"))
    estimated = _parse(departure.get("realTimeDepartureDateTime"))
    return {
        "line": str(line["name"]).strip(),
        "direction": line["towards"].strip(),
        "transport": TRAFFIC_TYPE_MAPPING.get(line.get("trafficType", 0), "UNKNOWN"),
        "in_minutes": _minutes_until(_departure(departure)),
        # Positive is late. Absent when the bus has no real-time report at all,
        # which is not the same as being exactly on time.
        "delay_minutes": (
            round((estimated - planned).total_seconds() / 60)
            if estimated and planned
            else None
        ),
    }


class ULTransportAPI(llm.API):
    """The tool, as something selectable in the Assist pipeline."""

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass=hass, id=API_ID, name=API_NAME)

    async def async_get_api_instance(
        self, llm_context: llm.LLMContext
    ) -> llm.APIInstance:
        return llm.APIInstance(
            api=self,
            api_prompt=API_PROMPT,
            llm_context=llm_context,
            tools=[NextDeparturesTool()],
        )


def async_register(hass: HomeAssistant) -> None:
    """Expose the tool once, however many stops are configured."""
    if hass.data[DOMAIN].get("llm_api"):
        return
    hass.data[DOMAIN]["llm_api"] = llm.async_register_api(hass, ULTransportAPI(hass))


def async_unregister(hass: HomeAssistant) -> None:
    """Withdraw the tool when the last stop goes."""
    if unregister := hass.data[DOMAIN].pop("llm_api", None):
        unregister()
