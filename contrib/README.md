# Departures on a voice satellite

`llm_tool.py` exposes **UL Transport departures** as an Assist LLM API. Its
answer carries a `card` key:

```json
{
  "stop": "Centralstationen",
  "departures": [{"line": "8", "in_minutes": 2, "...": "..."}],
  "card": {"type": "custom:ul-transport-map", "stop_id": 740020565, "content": "list",
           "list_count": 5, "card_module": "/ul_transport/ul-transport-map.js",
           "card_scale": 1.25}
}
```

Any assistant ignores that key harmlessly. The
[Voice Satellite card](https://github.com/jxlarrea/voice-satellite-card-integration)
does not know about it yet - the patch here teaches it to draw whatever
Lovelace card a tool result names, so the map comes up on the tablet while the
LLM reads the times out. It is written to be the upstream pull request: nothing
in it mentions UL Transport.

## Turning the tool on

Settings → Devices & services → your conversation agent → **Configure**, and
tick *UL Transport departures* under the exposed APIs. Then ask "when does the
next bus go?".

Speech works from here. The picture needs the patch below.

## Patching the satellite card

```bash
git clone https://github.com/jxlarrea/voice-satellite-card-integration
cd voice-satellite-card-integration
git apply ../ul-transport/contrib/voice-satellite-lovelace-panel.patch
npm ci && npm run build
HA_DEPLOY_TARGET=/path/to/homeassistant/config node scripts/deploy-ha.js
```

Restart Home Assistant, then reload the tablet.

**Bump the version on every rebuild.** Home Assistant serves the bundle as
`voice-satellite-card.js?v=<INTEGRATION_VERSION>`, and its service worker caches
it under that URL - a rebuilt bundle at the same version keeps serving the old
code to every browser, with no way to tell from the UI. `npm run build` copies
`package.json`'s version into `const.py`, so bump it there first:

```bash
npm version --no-git-tag-version 2026.8.8-lovelace3 && npm run build
```

Then restart HA. The local build is on `2026.8.8-lovelace7`; the patch itself
touches only `src/`, so the version bump never reaches the upstream diff.

This overwrites the HACS copy of `voice_satellite`, so HACS will offer to
"update" back to the released version and undo it. Re-deploy after any such
update, until the patch is upstream.

**A version bump is not always enough.** The service worker served a stale
bundle here twice even after the URL changed. If the fix does not appear, clear
site data for the Home Assistant origin on that browser, or check what is
actually running from its console:

```js
document.querySelector('voice-satellite-card').ui.showLovelacePanel.toString().includes('card_module')
```

## If the panel shows a red error card

That is `hui-error-card`: the card type in the config was not defined when the
panel tried to build it. The patch waits five seconds for a `custom:` element
to register, which covers a page that is still loading its resources - but not
a page where the resource never arrives. Reload it; if it persists, check that
`/ul_transport/ul-transport-map.js` is being served.

## What the patch adds

A tool result with a `card` object gets it mounted in the media panel, with
`hass` kept current so live cards keep updating. Two optional keys sit
alongside it:

- `card_height` - a height for cards that fill their container rather than
  sizing to their content. A map needs one. The departures tool does not set
  it: `content: list` sizes to its rows (158px for two, 286px for five), and a
  fixed height only buys dead space under a short list.
- `card_scale` - how much bigger to draw the card than a dashboard would.
  Applied as `zoom`, so the card reflows at the larger size rather than being
  stretched, and the panel itself is widened from the 25% a featured image gets
  to 34%. The departures tool asks for `1.25`; raise that one number in
  `llm_tool.py` if the tablet is further away. Widening the panel means the
  chat has to stop sooner too - every skin reserves `32.5% + 40px` for a 25%
  panel inset by 7.5%, so a `has-lovelace` rule reserves `41.5% + 40px`
  instead, or the last words of each line sit behind the card.
- `card_module` - where the card's JS lives. The satellite also runs on pages
  that are not dashboards, above all its own `/voice-satellite` panel, and
  those load neither Lovelace's card helpers nor the dashboard resources that
  define custom cards. With a module named, a `custom:` card is imported and
  built directly - which is all `createCardElement` does for one anyway. The
  helpers are still used for built-in card types where they exist.

Everything else is unchanged: existing weather, financial, image and video
payloads still take their own paths.
