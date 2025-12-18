# UL Transport for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/custom-components/hacs)
[![GitHub release](https://img.shields.io/github/release/AlexanderBabel/ul-transport.svg)](https://github.com/AlexanderBabel/ul-transport/releases)

A Home Assistant integration for real-time public transport departures from UL (Uppsala Läns Trafik). Designed to work with the [ha-departures-card](https://github.com/alex-jung/ha-departures-card) for a beautiful departure board display.

## Features

- 🔍 **UI-based setup** - Search and select stops directly in Home Assistant
- 🚌 **Real-time data** - Live departures with delays
- 📍 **Line filtering** - Monitor all or specific lines per stop
- ⏰ **5 departures** per line with planned and estimated times
- 🎨 **Official colors** - Includes UL line colors for cards
- 🔄 **Configurable refresh** - Set update interval (30-600 seconds)

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Click on "Integrations"
3. Click the three dots in the top right corner
4. Select "Custom repositories"
5. Add this repository URL: `https://github.com/AlexanderBabel/ul-transport`
6. Select category: "Integration"
7. Click "Add"
8. Search for "UL Transport" and install
9. Restart Home Assistant

### Manual Installation

1. Copy the `custom_components/ul_transport` folder to your Home Assistant's `custom_components` directory
2. Restart Home Assistant

## Configuration

1. **Settings** → **Devices & Services** → **+ Add Integration**
2. Search for **UL Transport**
3. Enter your stop name (e.g., "Centralstationen")
4. Select the stop from search results
5. Choose lines to monitor (leave empty for all)
6. Set update interval in seconds (default: 60, range: 30-600)

To modify settings later, click **Configure** on the integration.

## Sensors

Creates one sensor per **line + direction** combination.

**Naming**: `sensor.<stop>_line_<line>_to_<direction>`

**State**: Next departure time (ISO 8601 format)

**Key Attributes**:
- `line_name`, `line_id`, `direction` - Line information
- `line_color`, `text_color` - Official UL colors for display
- `transport` - Type (BUS, TRAM, TRAIN, etc.)
- `stop_name`, `area`, `latitude`, `longitude` - Location details
- `planned_departure_time`, `estimated_departure_time` - First departure
- `planned_departure_time_1` through `_4` - Next 4 departures
- `estimated_departure_time_1` through `_4` - Real-time for next 4

## Display with Departures Card

This integration is designed to work with [ha-departures-card](https://github.com/alex-jung/ha-departures-card):

```yaml
type: custom:departures-card
entities:
  - sensor.sommarro_uppsala_line_2_to_gamla_uppsala
  - sensor.sommarro_uppsala_line_7_to_arsta_fyrislund
```

The card automatically uses the `line_color` and `text_color` attributes to display lines with official UL colors.

## API & Data

**Endpoints**:
- Stop search: `https://www.ul.se/api/journey/stops`
- Departures: `https://api.ul.se/api/v3/stop/{stop_id}`
Configurable (30-600 seconds, default 60)
**Update interval**: 60 seconds

## Troubleshooting

**No stops found**: Check spelling or try a shorter search term

**Rate limit errors**: Increase the update interval or wait a few minutes between configuration changes

**Missing real-time data**: Not all departures have real-time updates (`estimated_departure_time` will be `null`)

## Support

[GitHub Issues](https://github.com/AlexanderBabel/ul-transport/issues)

## Credits

Developed by [@AlexanderBabel](https://github.com/AlexanderBabel) • Data by UL (Uppsala Läns Trafik) • MIT License
