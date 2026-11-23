# UI 5 design spec ("Friendly")

The team picked this look for the front-end overhaul. This folder is the design hand-off:

- `mockups/` has 20 screenshots: 5 desktop and 5 phone screens, each in light and dark.
- `mockup-html/` has the same screens as static HTML, so exact sizes, colors and spacing can be read off. They are hand-made mockups with fake data (street names, numbers, routes). Copy the styling, not the markup or the numbers.
- `fonts/` has Nunito (SIL Open Font License, see `OFL-LICENSE.txt`).

## What does not change

This is a front-end reskin and re-layout. Everything under `safety/`, the API, the data and the tests' expectations stay the same.

- **No backend changes.** Use only the data the current front end already gets from `/api/v1/...`. If a mockup shows something the API does not provide, leave that bit out and list it in the PR. One known case: the per-stretch route coloring.
- **Color thresholds stay.** Keep `RAMP_HINGES` (green up to 1.5×, yellow at 1.5×, orange 2×, red 3×), the zero/no-data fill and anything `tests/test_constants_parity.py` checks. Only the colors change. The legend ranges in the mockups are illustrative. The real legend must be built from the hinges.
- **Every current feature stays**, including the ones listed below.
- **Layering and security stay.** Cells stay under street names (`cellsBeforeId()`). The CSP in `safety/api/security.py` only allows `'self'` fonts and no Google Fonts. Self-host Nunito from `web/fonts/` and don't loosen the CSP.
- **Existing behavior stays.** Keep the escape-by-default templating (`web/html.js`), the service worker and the accessibility basics: labels, `aria-*`, keyboard use and focus states.

## Feature checklist (all must exist after the change)

| Feature | Where it lives in UI 5 |
|---|---|
| Map of all cities, resolution follows zoom | Full-bleed map behind everything |
| City picker (moves the map only) | Desktop: Cities screen and city chip. Phone: Cities tab |
| Violent / Non-violent | Pill toggle, orange when active |
| Show cells on/off | "Color the map" switch |
| Cell details: relative risk (e.g. 0.78×, "22% below/above the city average"), hour + day label in city time, 24-hour chart with now + quieter window, 12-hour outlook sentence, 7-day chart + sentence, "usual pattern, not a forecast", hour chart needs 48+ incidents | Desktop: bottom card. Phone: bottom sheet |
| How is this calculated (methodology) | "Help" rail/tab, opens the "How do we figure this out?" dialog |
| Address search with suggestions (Photon) | Big rounded search, numbered suggestions with Quieter/Typical/Busier chips |
| Locate me + locate note when outside coverage | Locate button. Note as a friendly bubble |
| Right-click drops pin (phone: press and hold) with nearest address; right-click sets start while directions are open | Dropped-pin card / sheet |
| Lower-crime direction compass (8 sectors, ~1.5 km, user location if in the city on screen, else map center) | Compass card on Explore and full "Near me" screen |
| Directions: To / From / Use my location, Drive / Walk / Bike / Transit, route cards (time, miles, activity vs city average, lower-activity first, fastest), why text, Start, Steps, caveats, attribution | Directions panel (desktop) / route sheet (phone) |
| Live turn-by-turn: instruction, ETA, End, Reroute from here, Recenter, Steps | Phone turn-by-turn screen; same on desktop in the panel |
| First-load progress (`/api/v1/progress`) | Progress bar on the Cities screen + small status pill |
| Light and dark mode | Sun/moon switch on rail (desktop) and map button (phone). Default follows `prefers-color-scheme`, user choice remembered in `localStorage` (wrapped in try/catch) |

## Layout

- **Desktop (≥ 768 px):**
  - A floating left rail, 84 px wide, 16 px from the edges, radius 30. It holds the logo, Explore, Directions, Near me, Cities and Help, with the light/dark switch at the bottom.
  - Floating cards sit over the map: a search box at the top center, a "What would you like to see?" panel on the right, and the selected-area card along the bottom.
- **Phone (< 768 px):**
  - A floating bottom tab bar (radius 30) with the same five items.
  - Panels become bottom sheets (radius 30, drag handle).
  - The search box is a pill at the top.
  - During turn-by-turn the tab bar hides.
- Use **one** breakpoint in both CSS and JS (replace the current mix of 600 and 900 px), plus `env(safe-area-inset-*)`, `100dvh` and 44 px minimum tap targets.

## Design tokens

Font: **Nunito** 500 to 900 (headings 900, labels 800, body 600 to 700).

| Token | Light | Dark |
|---|---|---|
| land (page/map background) | `#fbf4ea` | `#221c22` |
| panel | `#ffffff` | `#2c242b` |
| soft (inputs, chips, idle buttons) | `#fdf0e3` | `#3a3038` |
| border | `#f0dfcd` | `#4a3e47` |
| text | `#3a2b26` | `#fbeee4` |
| muted | `#8a766b` | `#bba79c` |
| primary (actions, selection) | `#f06a3f` | `#ff8a5c` |
| calm bg / text | `#e4f5ec` / `#23845f` | `rgba(76,196,149,.16)` / `#7fdcb5` |
| typical bg / text | `#fff3d1` / `#94690a` | `rgba(255,209,102,.16)` / `#ffd166` |
| busy bg / text | `#fde2e6` / `#b93850` | `rgba(232,90,114,.18)` / `#ff9aab` |
| shadow | `0 10px 30px rgba(120,70,40,.14)` | `0 10px 30px rgba(0,0,0,.45)` |
| my-location blue | `#4b7be5` | `#8fb0ff` |

Map cell ramp colors (light and dark): `#3fae86` → `#a8d47e` → `#ffd166` → `#ff9a6b` → `#e85a72`. Map these onto the existing ramp steps and hinges. Fill opacity is about 0.5 in light and 0.46 in dark. Street names must stay readable.

Shapes:
- Cards have radius 24 to 36.
- Buttons, inputs and chips are full pills.
- Map cells stay **hexagons**. Only the UI around them is rounded.

## Voice

Use plain, warm and short wording, for example:
- "Where are you headed?"
- "Take me here"
- "Let's go"
- "Want a calmer walk? Head northwest"

Keep the exact numbers visible next to the friendly words (0.78×, minutes, miles). Keep the honesty lines: "Patterns, not predictions" and "Based on past reports".

The small smiling shield ("buddy") posts short tips in chat bubbles. Tips only restate data the app already has, such as the quieter-window sentence, the route "why" text or the locate note. No made-up advice.

Smiley faces on cells and routes follow the same bands as the colors.
