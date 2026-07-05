# Task: Weather CLI v2 — units, multi-city queries, and a config file

**Target repo:** `Kris0102/Test_env` (the weather-app test environment)
**Purpose:** A realistic, multi-file stress test for the MAS — exercises planning of a
backward-compatible epic, precise computed values / boundary cases (test-grounding), file
I/O, argv parsing, exit codes, and a multi-file deploy diff.

## Why this is a good stress test
- **Planner:** decompose a multi-file epic across `utils.js`, `weather.js`, `main.js`, a new
  `config.js`, and tests; honor backward-compat; make design calls (alias→`corrected` flag,
  unit precedence, tie-break order).
- **Coder / Tester / Reflector:** exact conversion math, exit codes, partial failures, config
  file I/O — lots of concrete I/O to ground tests on. The `--unit K` and malformed-config
  paths deliberately have NO clean example value, so they test the "assert structure for
  un-exampled cases" behaviour (ADR 0022).
- Built on real seams in the current code: `formatTemperature` ignores its `unit` arg;
  `main.js` hardcodes `inputCity = "Munich"`; there's no argument parsing.

## Notes on fairness
- Every asserted number is verifiable (`64.4`, `71.6`, `59`). Kelvin (a floating-point
  rounding trap on `x.15` values) is intentionally turned into an **`Unsupported unit`
  validation case** rather than a fragile expected value, so the spec stays honest.
- Intentionally near the edge of one run's capacity. If it hits the microtask cap, that's
  real signal about decomposition/convergence, not necessarily a defect.

---

## Full specification

Extend the existing CommonJS weather app (`utils.js`, `weather.js`, `main.js`) in its current
style (`"use strict"`, JSDoc, `module.exports`). All existing exports must be preserved and
the existing `tests/weather.test.js` must still pass.

**(1) Unit conversion — `utils.js`.** `formatTemperature(value, unit)` currently ignores
`unit`. Make it convert from Celsius (the `WEATHER_DB` base) to the target unit and round to
one decimal via `Math.round(x*10)/10`. Support `"C"` (unchanged: round the Celsius value) and
`"F"` (`value*9/5+32`). Examples: `formatTemperature(18,"C")` → `18`,
`formatTemperature(18,"F")` → `64.4`, `formatTemperature(22,"F")` → `71.6`,
`formatTemperature(15,"F")` → `59`. Any other unit throws `Error` with exactly
`Unsupported unit: <unit>` (e.g. `formatTemperature(18,"K")` throws `Unsupported unit: K`).
Keep the existing `TypeError` for a non-number value.

**(2) `getWeather` takes a unit — `weather.js`.** New signature `getWeather(city, unit = "C")`;
the returned object's `temperature` is in the requested unit (via `formatTemperature`) and its
`unit` field is that unit. Calling `getWeather(city)` with no unit MUST behave exactly as
today. Example: `getWeather("hamburg","F")` →
`{ city: "Hamburg", temperature: 59, condition: "Rainy", unit: "F", corrected: true }`.

**(3) Deterministic suggestions — `weather.js` `findCity`.** When several cities tie at the
same minimum Levenshtein distance in the suggestion range, the `"Did you mean"` message must
list ALL tied cities, **alphabetically**, joined by `" or "` — e.g.
`Unknown city: <query>. Did you mean Berlin or Munich?`. Behaviour for non-tie cases is
unchanged. (Supply concrete example inputs for the tie cases you construct.)

**(4) Real CLI — `main.js`.** Replace the hardcoded city. Parse `process.argv`: one or more
city names plus an optional `--unit C|F`. Print one line per city:
`Weather in <city>: <temp>°<unit>, <condition>`, and when the name was corrected also print
`corrected "<input>" -> "<canonical>"`. A city that fails lookup prints its error message to
**stderr** and does NOT abort the others. Exit code is `0` if every city succeeded, `1` if any
failed. With no city argument, print `Usage: node main.js <city> [<city> ...] [--unit C|F]` to
stderr and exit `1`. Examples: `node main.js Berlin` → `Weather in Berlin: 22°C, Sunny`;
`node main.js hamburg --unit F` → `Weather in Hamburg: 59°F, Rainy` then
`corrected "hamburg" -> "Hamburg"`; `node main.js Munich Tokyo` → Munich's line on stdout,
`Unknown city: Tokyo` on stderr, exit `1`.

**(5) Config file — new `config.js` + used by `main.js`.** If a `.weatherrc.json` exists in the
current working directory, load `{ "defaultUnit": "C"|"F", "aliases": { "<alias>":
"<CanonicalCity>" } }`. Precedence for the unit: explicit `--unit` › config `defaultUnit` ›
`"C"`. Aliases resolve BEFORE fuzzy matching and count as an exact resolution
(`corrected: false`). A missing config file is not an error (use defaults). A malformed config
prints `Invalid config: <reason>` to stderr and exits `1`. Example: with `.weatherrc.json` =
`{ "defaultUnit": "F", "aliases": { "muc": "Munich" } }`, `node main.js muc` →
`Weather in Munich: 64.4°F, Partly Cloudy` with no correction line.

**(6) Tests.** Extend `tests/` (jest) to cover: the C/F conversions and the unsupported-unit
throw; `getWeather` with a unit; the alphabetical multi-suggestion message; argv parsing incl.
multi-city, partial-failure exit codes, and the usage message; config precedence, alias
resolution, missing-config default, and malformed-config error. Keep all existing tests green.

---

## Condensed version (paste into the run's task field)

Extend the CommonJS weather app in its existing style ("use strict", JSDoc, module.exports);
preserve all existing exports and keep tests/weather.test.js passing. (1) utils.js: make
formatTemperature(value, unit) convert from Celsius and round to one decimal
(Math.round(x*10)/10) — support "C" (round the value) and "F" (value*9/5+32); examples
formatTemperature(18,"F")=64.4, formatTemperature(22,"F")=71.6, formatTemperature(15,"F")=59;
any other unit throws Error "Unsupported unit: <unit>" (e.g. "K"); keep the TypeError for a
non-number. (2) weather.js: getWeather(city, unit="C") returns temperature in the requested
unit and sets the unit field; getWeather(city) with no unit behaves exactly as before;
getWeather("hamburg","F") = { city:"Hamburg", temperature:59, condition:"Rainy", unit:"F",
corrected:true }. (3) weather.js findCity: on a tie at the minimum Levenshtein distance in the
suggestion range, the "Did you mean" message lists all tied cities alphabetically joined by
" or " (e.g. "Unknown city: <q>. Did you mean Berlin or Munich?"). (4) main.js: replace the
hardcoded city; parse process.argv for one or more cities plus optional --unit C|F; print
"Weather in <city>: <temp>°<unit>, <condition>" per city, plus 'corrected "<input>" ->
"<canonical>"' when corrected; a failed lookup prints its message to stderr and does not abort
the others; exit 0 if all succeed else 1; no city arg prints "Usage: node main.js <city>
[<city> ...] [--unit C|F]" to stderr and exits 1. (5) new config.js used by main.js: optional
.weatherrc.json in cwd { "defaultUnit":"C"|"F", "aliases":{"<alias>":"<CanonicalCity>"} };
unit precedence --unit › config.defaultUnit › "C"; aliases resolve before fuzzy matching and
count as exact (corrected:false); missing config = defaults; malformed config prints "Invalid
config: <reason>" to stderr and exits 1. (6) Extend jest tests to cover all of the above and
keep existing tests green.
