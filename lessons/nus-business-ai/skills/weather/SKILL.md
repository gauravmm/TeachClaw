---
name: weather
description: Get current weather and short-range forecasts via free public APIs.
---

# Weather

Two free APIs, no keys needed. Use `web_fetch`. Both return JSON; the
tool already pretty-prints it for you.

## wttr.in (primary)

Compact current-weather + 3-day forecast in JSON:

- URL: `https://wttr.in/<location>?format=j1`
- `<location>` is a city name (URL-encode spaces: `New+York`),
  airport code (`JFK`), or `lat,lon`.

Example call:

```
{"name": "web_fetch", "arguments": {"url": "https://wttr.in/Singapore?format=j1"}}
```

Useful keys in the response:

- `current_condition[0]`: `temp_C`, `temp_F`, `humidity`, `windspeedKmph`,
  `weatherDesc[0].value`, `FeelsLikeC`.
- `weather[0..2]`: per-day forecast with `maxtempC`, `mintempC`,
  `astronomy` (sunrise/sunset), and `hourly` slices.

Default to `Singapore` when the user does not specify a location — that
is where the lecture is held.

## Open-Meteo (fallback)

Use when wttr.in is slow or returns an error.

- URL: `https://api.open-meteo.com/v1/forecast?latitude=<lat>&longitude=<lon>&current_weather=true`
- For Singapore: `latitude=1.3667&longitude=103.8`.

Example call:

```
{"name": "web_fetch", "arguments": {"url": "https://api.open-meteo.com/v1/forecast?latitude=1.3667&longitude=103.8&current_weather=true"}}
```

Returns `current_weather` with `temperature`, `windspeed`, `winddirection`,
`weathercode`. Weather-code lookup table:
https://open-meteo.com/en/docs (Variable section).

## Reply style

Provide the answer in one sentence with the relevant numbers and optionally an emoji.
Convert to the unit the user used; default to °C and km/h. Do not paste the raw JSON.
