# Accessibility test script (WCAG 2.2 AA target, BFSG)

Run before every release and before a hotel goes live. Record the date, the browser
and the outcome per row; a failed row is a defect.

## Widget

| # | Check | How | Expected |
|---|---|---|---|
| 1 | Inert until opened | load the page with the loader script, open devtools | no cookie, no `localStorage`, no request to the service beyond the script fetch until the launcher is clicked |
| 2 | Keyboard only | Tab to the launcher, Enter | the panel opens, focus lands in the input; Tab cycles through language menu, close, log, quick replies, input, send, footer links |
| 3 | Escape | press Esc in the panel | the panel closes and focus returns to the launcher |
| 4 | Visible focus | Tab through every control | a 3 px outline on every focused element (white on the dark header) |
| 5 | Live region | send a message with a screen reader running | the reply is announced (`role="log"`, `aria-live="polite"`) |
| 6 | Labels | inspect with the accessibility tree | input, send, close and language menu have names; every message carries `lang` |
| 7 | Contrast | check text and UI colours with a contrast tool (brand tokens in `gc-widget.css`) | text at least 4.5:1, controls at least 3:1, in light and dark mode |
| 8 | Zoom and reflow | 200 percent zoom and a 320 px wide viewport | no horizontal scrolling, all controls reachable |
| 9 | Targets | measure quick replies and buttons | at least 24 by 24 px |
| 10 | Reduced motion | enable `prefers-reduced-motion` | no smooth scrolling animation |
| 11 | No CAPTCHA | send several messages | rate limits answer with a plain message, never a puzzle |
| 12 | AI label | open the panel | the persistent "AI" label and the privacy link are visible |

## Hotel admin

| # | Check | How | Expected |
|---|---|---|---|
| 13 | Keyboard operation | log in, open the inbox, reply, approve a KB entry with the keyboard only | every action completes |
| 14 | Skip link and headings | screen reader outline | one `h1` per page, a skip link to the content |
| 15 | Forms | inspect the login, reply, KB and questionnaire forms (85 fields, grouped under headings) | every field has a label; errors are announced near the field or in the alert list at the top |
| 16 | Tables | inbox and reservations | header cells, row scope, the SLA countdown readable as text |
| 17 | Language | switch the admin to English | `lang` attribute changes, all labels translated |
| 18 | Phone first | the inbox at 360 px | conversation list and thread usable without horizontal scrolling |
| 19 | Contrast | admin colours (`admin.css`) | 4.5:1 text |

## Automated helpers

`pytest -m browser tests/browser` (Playwright) covers rows 1, 2, 3, 8 and the CORS
matrix when a browser is available; the contrast of the brand tokens is asserted in
`tests/integration/test_widget.py`.

## Accessibility statement

The template lives in the legal drafts (`legal/drafts.yaml`, `accessibility`); the
hotel publishes it after the lawyer review with the date of the last test run.
