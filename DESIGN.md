---
name: Trip Planner
description: "Boarding-pass cards on a quiet desk: light Bento/Airbnb minimalism with one rare rose accent, and an equal dark theme."
colors:
  desk-mist: "#f8fafc"
  paper: "#ffffff"
  hairline: "#e2e8f0"
  perforation: "#cad5e2"
  stub-grey: "#f1f5f9"
  ink: "#0f172b"
  ink-strong: "#314158"
  ink-body: "#45556c"
  muted: "#62748e"
  faint: "#90a1b9"
  signal-rose: "#ff2056"
  signal-rose-deep: "#ec003f"
  rose-wash: "#fff1f2"
  rose-mist: "#ffe4e6"
  rose-edge: "#ffccd3"
  wayfinding-blue: "#155dfc"
  blue-wash: "#eff6ff"
  focus-blue: "#51a2ff"
  focus-halo: "#dbeafe"
  go-green: "#007a55"
  go-wash: "#ecfdf5"
  go-edge: "#a4f4cf"
  alert-red: "#c10007"
  alert-wash: "#fef2f2"
  alert-edge: "#ffc9c9"
  alert-icon: "#fb2c36"
  star-amber: "#ffb900"
  night-desk: "#0d0d0d"
  night-card: "#1a1a1a"
  night-inset: "#141414"
  night-field: "#222222"
  night-hairline: "rgba(255, 255, 255, 0.1)"
  night-ink: "#ffffff"
  night-body: "#e4e4e7"
  night-muted: "#9f9fa9"
  night-button: "#e4e4e7"
  night-button-ink: "#18181b"
typography:
  display:
    fontFamily: "Geist, Arial, Helvetica, sans-serif"
    fontSize: "3rem"
    fontWeight: 800
    lineHeight: 1
    letterSpacing: "normal"
  headline:
    fontFamily: "Geist, Arial, Helvetica, sans-serif"
    fontSize: "1.5rem"
    fontWeight: 700
    lineHeight: 1.333
    letterSpacing: "-0.025em"
  title:
    fontFamily: "Geist, Arial, Helvetica, sans-serif"
    fontSize: "1.125rem"
    fontWeight: 600
    lineHeight: 1.556
    letterSpacing: "-0.025em"
  body:
    fontFamily: "Geist, Arial, Helvetica, sans-serif"
    fontSize: "0.875rem"
    fontWeight: 400
    lineHeight: 1.429
    letterSpacing: "normal"
  body-lg:
    fontFamily: "Geist, Arial, Helvetica, sans-serif"
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: "normal"
  button:
    fontFamily: "Geist, Arial, Helvetica, sans-serif"
    fontSize: "0.875rem"
    fontWeight: 600
    lineHeight: 1.429
    letterSpacing: "normal"
  button-lg:
    fontFamily: "Geist, Arial, Helvetica, sans-serif"
    fontSize: "1rem"
    fontWeight: 600
    lineHeight: 1.5
    letterSpacing: "normal"
  label:
    fontFamily: "Geist, Arial, Helvetica, sans-serif"
    fontSize: "0.75rem"
    fontWeight: 600
    lineHeight: 1.333
    letterSpacing: "0.05em"
  label-micro:
    fontFamily: "Geist, Arial, Helvetica, sans-serif"
    fontSize: "0.6875rem"
    fontWeight: 500
    lineHeight: 1.45
    letterSpacing: "0.1em"
  eyebrow:
    fontFamily: "Geist, Arial, Helvetica, sans-serif"
    fontSize: "0.6875rem"
    fontWeight: 600
    lineHeight: 1.45
    letterSpacing: "0.2em"
  code:
    fontFamily: "Geist Mono, ui-monospace, Menlo, Consolas, monospace"
    fontSize: "1.125rem"
    fontWeight: 700
    lineHeight: 1.556
    letterSpacing: "normal"
rounded:
  xl: "12px"
  2xl: "16px"
  3xl: "24px"
  full: "9999px"
spacing:
  "2": "8px"
  "3": "12px"
  "4": "16px"
  "5": "20px"
  "6": "24px"
  "8": "32px"
components:
  button-primary:
    backgroundColor: "{colors.signal-rose}"
    textColor: "{colors.paper}"
    typography: "{typography.button-lg}"
    rounded: "{rounded.full}"
    padding: "14px 16px"
  button-primary-hover:
    backgroundColor: "{colors.signal-rose-deep}"
  button-secondary:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink-strong}"
    typography: "{typography.button}"
    rounded: "{rounded.full}"
    padding: "14px 16px"
  button-secondary-hover:
    backgroundColor: "{colors.desk-mist}"
  button-disabled:
    backgroundColor: "{colors.stub-grey}"
    textColor: "{colors.faint}"
    typography: "{typography.button-lg}"
    rounded: "{rounded.full}"
    padding: "16px 24px"
  button-night-secondary:
    backgroundColor: "{colors.night-button}"
    textColor: "{colors.night-button-ink}"
    typography: "{typography.button-lg}"
    rounded: "{rounded.full}"
    padding: "16px 20px"
  card:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
    rounded: "{rounded.3xl}"
    padding: "{spacing.5}"
  card-gate:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
    rounded: "{rounded.3xl}"
    padding: "{spacing.8}"
  card-night:
    backgroundColor: "{colors.night-card}"
    textColor: "{colors.night-ink}"
    rounded: "{rounded.3xl}"
    padding: "{spacing.4}"
  tile-inset:
    backgroundColor: "{colors.desk-mist}"
    textColor: "{colors.ink}"
    rounded: "{rounded.2xl}"
    padding: "{spacing.3}"
  tile-night:
    backgroundColor: "{colors.night-inset}"
    textColor: "{colors.night-body}"
    rounded: "{rounded.2xl}"
    padding: "{spacing.3}"
  input-field:
    backgroundColor: "{colors.desk-mist}"
    textColor: "{colors.ink}"
    typography: "{typography.body-lg}"
    rounded: "{rounded.2xl}"
    padding: "14px 16px"
  input-night:
    backgroundColor: "{colors.night-field}"
    textColor: "{colors.night-ink}"
    typography: "{typography.body-lg}"
    rounded: "{rounded.2xl}"
    padding: "16px"
  chip-vibe:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink-body}"
    typography: "{typography.button}"
    rounded: "{rounded.full}"
    padding: "10px 16px"
  chip-vibe-selected:
    backgroundColor: "{colors.signal-rose}"
    textColor: "{colors.paper}"
  pill-status-go:
    backgroundColor: "{colors.go-wash}"
    textColor: "{colors.go-green}"
    typography: "{typography.label-micro}"
    rounded: "{rounded.full}"
    padding: "4px 12px"
  alert-error:
    backgroundColor: "{colors.alert-wash}"
    textColor: "{colors.alert-red}"
    typography: "{typography.button}"
    rounded: "{rounded.2xl}"
    padding: "12px 16px"
---

# Design System: Trip Planner

## Overview

**Creative North Star: "The Boarding Pass Desk"**

Everything in Trip Planner arrives as a document laid on a clean desk. The desk is a pale slate wash (Desk Mist); the documents are white cards with a hairline edge: a boarding pass for the poll, a day card for each itinerary day, a summary card for the locked plan. Screens are Bento grids of these cards, each holding exactly one idea, so a group of friends can read a trip at a glance on a phone. Structure comes from the cards and the grid, not from decoration. There is no chrome to look at, only the content and the next thing to do.

The palette is almost entirely slate and white. One vivid rose (Signal Rose) is the only voice in the system, and it is rare on purpose: it marks the single next action and its echoes. Blue helps you find your way and never asks for a click; green, red and amber report status. A dark theme sits beside the light one as an equal, with the same grid, radii and accent on a near-black desk. Type is Geist: friendly sans for sentences, Geist Mono for ticket codes, and small tracked uppercase labels as the "field names" of every document.

The tone is friendly and warm, crisp and clear, and playful where it counts: soft pill controls and 24px corners, strict labels and big numerals, a plane glyph, ticket notches and the LIKE / PASS stamps on the swipe deck. The user's confirmed visual rejection is AI-slop: template-looking decoration, with purple or indigo gradients as the named example. Light and dark are both official; today the theme is chosen per route (light: login, poll, swipe; dark: home, dashboard, draft board).

**Key Characteristics:**
- Pale desk (#f8fafc) with white 24px cards, a 1px slate hairline and a whisper shadow.
- One rare accent, Signal Rose (#ff2056), on at most about 10% of any screen; the same accent in both themes.
- Bento grids: a two-column dashboard grid where hero and wishlist cells span both columns, and a 12-column plan grid on large screens.
- Tracked uppercase micro-labels and monospace codes give every card its "printed document" feel.
- Pill controls that compress slightly when pressed; no gradients, no glow, no glass except sticky bars.
- Measured contrast: faint grey and white-on-rose fall short of AA and are restricted (see Colors).

## Colors

A slate-and-paper neutral system carries everything; one vivid rose speaks rarely, blue only points the way, and green, red and amber are reserved for status. Hex values are computed from the Tailwind v4 palette the project uses.

### Primary
- **Signal Rose** (#ff2056): the system's only accent. The single primary button per view, Like, selected chips, progress fill, the plane glyph, timeline dots, the day-number badge. Pressed and hover: **Deep Signal Rose** (#ec003f). Tints for soft containers and rings: **Rose Wash** (#fff1f2), **Rose Mist** (#ffe4e6), **Rose Edge** (#ffccd3).

### Secondary
- **Wayfinding Blue** (#155dfc): never an action, only orientation: the boarding-pass eyebrow, links such as "Change", destination and map icon tiles on **Blue Wash** (#eff6ff). Input focus uses **Focus Blue** (#51a2ff) for the border and **Focus Halo** (#dbeafe) for the 4px ring.

### Tertiary
- **Go Green** (#007a55 on **Go Wash** #ecfdf5, edge **#a4f4cf**): open, locked and saved states, such as the "Poll Open" and "Locked" pills.
- **Alert Red** (#c10007 on **Alert Wash** #fef2f2, edge **#ffc9c9**, icon **#fb2c36**): errors only.
- **Star Amber** (#ffb900): the rating star and nothing else.

### Neutral
- **Desk Mist** (#f8fafc): the page desk and the inset fill for fields and mini tiles.
- **Paper** (#ffffff): every card surface.
- **Hairline** (#e2e8f0): the 1px edge of every card, input and divider.
- **Perforation** (#cad5e2): dashed ticket tear lines.
- **Stub Grey** (#f1f5f9): disabled fills and quiet chips.
- **Ink** (#0f172b), **Ink Strong** (#314158), **Ink Body** (#45556c), **Muted** (#62748e), **Faint** (#90a1b9): the five text steps, darkest to lightest.

### Night Theme (an equal sibling)
- **Night Desk** (#0d0d0d) is the page; **Night Card** (#1a1a1a) the card; **Night Inset** (#141414) a tile inside a card; **Night Field** (#222222, focus #262626) a text area. Edges are **Night Hairline** (rgba(255, 255, 255, 0.1)).
- Text: **Night Ink** (#ffffff) for headings and numerals, **Night Body** (#e4e4e7), **Night Muted** (#9f9fa9). The secondary button is **Night Button** (#e4e4e7) with **Night Button Ink** (#18181b).
- The accent is the same Signal Rose. The dark dashboard still shows indigo (the "Total Joined" number and the Top Vibes bars) and the dark sticky button a slate-to-indigo gradient; both are drift to replace, not precedent.

### Named Rules
**The One Voice Rule.** Signal Rose covers at most about 10% of any screen: one primary action plus its echoes. It is never a section background and never appears as two competing primaries.

**The Same Accent Rule.** Light and dark share one accent. No indigo, violet or purple anywhere in the system.

**The Readable Ink Rule.** Contrast is measured, not assumed: Ink on white 17.8:1; Ink Strong 10.4:1; Ink Body 7.6:1; Muted 4.8:1 on white and 4.6:1 on Desk Mist (the lightest text allowed for captions), but only 4.35:1 on Stub Grey, so text on that fill uses Ink Body; Faint 2.6:1 (placeholders and decorative icons only, never text that must be read); Night Muted 6.6:1 on Night Card, but #71717b only 3.6:1 (do not use it for text). White on Signal Rose is 3.75:1: it clears only the 3:1 large-text bar, so today's rose-filled labels (14 to 16px semibold) fall short of the 4.5:1 AA bar for text that size. Rose-filled text is therefore limited to button labels, never body copy; where AA conformance is required, fill the button with Deep Signal Rose (#ec003f), which measures 4.53:1 with white. Whether the resting fill changes is an open brand decision. Go Green text uses #007a55 (5.1:1 on Go Wash), not the lighter #009966 (3.65:1).

## Typography

**Display Font:** Geist (with Arial, Helvetica, sans-serif)
**Body Font:** Geist (same stack)
**Label/Mono Font:** Geist Mono (with ui-monospace, Menlo, Consolas), for codes only

**Character:** One geometric grotesque doing every job, so hierarchy comes from weight, size and case rather than from a second typeface. It reads like printed travel documents: sentence-case for people, tracked small caps for the form fields, monospace for the codes.

### Hierarchy
- **Display** (800, 3rem, line-height 1): a single hero numeral such as the dashboard "Total Joined" count.
- **Headline** (700, 1.5rem, 1.333, -0.025em): screen and gate titles ("Join the Trip", page titles).
- **Title** (600, 1.125rem, 1.556, -0.025em): card and section names, the destination, a place name (up to 1.25rem bold on the swipe card, at default letter-spacing and a 1.4 line height so Thai tone marks are not clipped).
- **Body** (400 to 500, 0.875rem, 1.429): all interface text and captions; 1rem (1.5) for typed input and form-submit buttons.
- **Button** (600, 0.875rem, or 1rem for submit and sticky buttons).
- **Label** (600, 0.75rem, 0.05em, uppercase): form labels and card eyebrows.
- **Label Micro** (500 to 600, 0.6875rem, 0.1em, uppercase): the ticket field names such as CREW, TRIP CODE, DATES, TOTAL JOINED.
- **Eyebrow** (600, 0.6875rem, 0.2em, uppercase, Wayfinding Blue): the document title line, such as "Digital Boarding Pass".
- **Code** (Geist Mono, 700, 1.125rem; up to 1.5rem on a boarding pass): trip codes.

### Named Rules
**The Form-Field Rule.** Every label under 12px is uppercase with widened tracking, like the printed field names of a ticket. Sentence-case micro-text is not part of the system.

**The Mono Means Code Rule.** Geist Mono appears only where the content is a code or an id (the trip code). It is never decoration.

## Layout

Mobile first. Pages are a single centered column, `max-w-md` (448px), with 16px side gutters and 24px top and bottom padding; sections are separated by 32px (`space-y-8`), form fields by 16 to 24px. Gate screens (login) centre one `max-w-sm` (384px) card in a full-height pale desk with `py-12`. Because the base page background is dark, a light page must paint its own full-height Desk Mist wrapper.

The Bento Grid is the structural idea, and it appears in two forms:
- **Dashboard grid:** `grid-cols-2` with a 16px gap inside the 448px column. The hero (Overview) spans both columns; two half-width cells (Top Vibes, Voters) share the next row; the Wishlist Summary spans both columns again. Card padding is 16px.
- **Locked-plan grid:** the container widens to `max-w-7xl`; at `lg` a 12-column grid with a 24px gap puts the summary card in 4 columns and the map in 8; the day cards below flow into 1, 2 (`md`) or 3 (`xl`) columns.

Rules of the grid: one idea per cell; a grid uses one gutter (16px on the dashboard, 24px on the plan); spans are half, full, or 4/12 and 8/12; cells in a row stretch to equal height (`h-full`) instead of leaving holes. Sticky chrome: a translucent header (blurred) at the top, and a fixed bottom action bar padded with `max(0.75rem, env(safe-area-inset-bottom))`; pages that have the bar reserve 128px (`pb-32`) so content clears it. The spacing steps in use are 8, 12, 16, 20, 24 and 32px on a 4px base. Controls are 48 to 56px tall and the swipe action buttons are 64px (Undo is 48px); chips are 40px.

## Elevation & Depth

A hybrid that leads with line, not shadow. A card rests on the desk by its 1px Hairline plus the faintest shadow; only gate cards and the swipe stack lift higher, and only the rose button carries a coloured shadow. In the dark theme depth is a 10% white hairline plus a deep black shadow. Nothing glows.

### Shadow Vocabulary
- **Card rest** (`box-shadow: 0 1px 3px 0 rgb(0 0 0 / 0.1), 0 1px 2px -1px rgb(0 0 0 / 0.1)`): every content card, with a 1px Hairline.
- **Gate float** (`box-shadow: 0 20px 25px -5px rgb(15 23 43 / 0.05), 0 8px 10px -6px rgb(15 23 43 / 0.05)`): the login card.
- **Stack lift** (same shape at 0.10 alpha): swipe cards.
- **CTA tint** (`box-shadow: 0 4px 6px -1px rgb(255 32 86 / 0.2), 0 2px 4px -2px rgb(255 32 86 / 0.2)`): rose buttons; the sticky bar button uses a larger 0.25 version.
- **Field inset** (`box-shadow: inset 0 2px 4px 0 rgb(15 23 43 / 0.05)`): text inputs.
- **Focus halo** (`box-shadow: 0 0 0 4px #dbeafe` on fields, `0 0 0 4px #ffccd3` on rose buttons): keyboard focus.
- **Night card** (`box-shadow: 0 25px 50px -12px rgb(0 0 0 / 0.4)`, with a 1px Night Hairline): every dark card.

### Named Rules
**The Whisper Rule.** On the light desk a shadow never exceeds 10% opacity (the rose CTA tint is the one exception). If the shadow is noticed before the border, it is too strong.

**The Flat Fill Rule.** Surfaces, buttons, badges and text are flat fills. No gradients, no glow. Blur is allowed only on sticky bars. The one permitted gradient is the 28px hairline grid on the map placeholder, which is a pattern, not a fill.

## Shapes

A soft, document-like form language with exactly four radii. Pills (9999px) for every button, chip, badge and status; 24px panels for every card; 16px tiles for fields, mini tiles, alerts and icon tiles; 12px for the swipe stamps. Circles for avatars, day numbers and the swipe buttons (64px, 48px for Undo). Every card has a 1px Hairline edge; dashed Perforation lines mean a ticket tear, and a boarding pass shows it with two 20px notches in Desk Mist at the ends of the divider. Photos clip to the card radius. There are no sharp corners and no mixed radii inside one component.

## Components

### Buttons
- **Shape:** capsule (9999px); 48px tall in cards (14px 16px), 52 to 56px for form-submit and the sticky bar.
- **Primary:** Signal Rose fill, white 600 label (14px in cards, 16px for submit and sticky), CTA tint shadow. One per view. White on this rose is 3.75:1 (see The Readable Ink Rule).
- **Hover / Focus / Press:** fill deepens to Deep Signal Rose; keyboard focus adds a 4px Rose Edge halo; press compresses to 0.98 (`active:scale-[0.98]`); disabled drops to 50% opacity.
- **Secondary:** Paper fill, 1px Hairline, Ink Strong 600 label, Card-rest shadow; hover fills Desk Mist (the Google sign-in button). In the dark theme it is Night Button with Night Button Ink.
- **Disabled (sticky bar):** Stub Grey fill, Hairline edge, Faint label.

### Chips
- **Style:** pill, 40px tall, 14px 500 label, Paper fill with a Hairline edge and Ink Body text.
- **State:** selected is a Signal Rose fill with a white label, a leading check mark and a rose tint shadow; they toggle on tap and press to 0.97.

### Cards / Containers
- **Corner Style:** 24px (`rounded-3xl`).
- **Background:** Paper on Desk Mist; Night Card on Night Desk.
- **Shadow Strategy:** Card rest by default; Gate float or Stack lift where a card must lift; Night card in dark.
- **Border:** 1px Hairline (Night Hairline in dark).
- **Internal Padding:** 20px content cards; 16px dashboard cells; 32px (40px from `sm`) gate cards. Mini tiles inside use Desk Mist, a Hairline edge, 16px corners and 12px padding.

### Inputs / Fields
- **Style:** Desk Mist fill, 1px Hairline, 16px corners, 14px 16px padding, 16px Ink text, Faint placeholder, Field inset shadow; a tracked uppercase Label sits above. Dark: Night Field fill, Night Hairline edge.
- **Focus:** the border turns Focus Blue, the fill turns white and a 4px Focus Halo appears. Dark focus brightens the border to 25% white and the fill to #262626, with a 4px 5%-white halo.
- **Error / Disabled:** errors use an Alert banner (below), not a red field; submit buttons dim to 50%.

### Navigation
- **Header:** sticky, translucent (white at 80% in light, black at 30% in dark) with a heavy backdrop blur, a bottom hairline, a 600 18px title and a second line that is either the "Trip ID" with the id in monospace or a page subtitle (the swipe step shows "Near <destination>" with a blue "Change" link there); a 40px circular avatar placeholder sits at the right.
- **Bottom action bar:** fixed, a similar translucent surface (white at 90% in light, black at 40% in dark) with a top hairline, holding one full-width pill (the sticky primary button).

### Status Pill and Alert
- **Status pill:** 11px 600 uppercase label in Go Green on Go Wash with a Go Edge border, pill-shaped (Poll Open, Locked, Saved). Dark pills use an emerald tint at 10% fill with a 20% edge.
- **Alert:** a 16px-corner banner of Alert Wash with an Alert Edge border, a red icon and a 14px 500 Alert Red message; it sits above the controls it concerns and uses `role="alert"`.

### Boarding Pass (signature)
The poll's heart and the north star made literal. A white 24px card with a blue Eyebrow ("Digital Boarding Pass"), a bold title, a green status pill, then a ticket row: CREW at the left, a Signal Rose plane glyph over a dashed Perforation line in the middle, TRIP CODE in monospace at the right. A dashed divider with two 20px Desk Mist notches separates the stub (dates, status), and a strip of 32 Hairline-coloured bars of varying width forms the barcode.

### Bento Hero (signature)
The dashboard's opening cell: a Night Card spanning both columns with an uppercase Overview label and a status pill, trip code and dates as label-and-value pairs on the left, and one Display numeral with a TOTAL JOINED micro-label on the right. The numeral is the screen's single accent moment (Signal Rose, replacing the current indigo).

### Swipe Ticket (signature)
The swipe deck's card is a ticket: a Paper 24px card with a 1px Hairline and the Stack lift shadow, the photo on top and a stub below, and between them a dashed Perforation with a half-moon Desk Mist notch bitten out of each edge (the boarding pass's tear, built the same way). The photo is Stub Grey while it loads, and stays a flat Stub Grey with a quiet "No photo" label, and no credit, if it fails. It carries a Paper type chip with a Hairline edge (Google's own category label, Label Micro, uppercase) at the top left and, once the photo is in, a credit chip at the bottom left ("PHOTO · author" on Ink at 70%, the author linked, as Google requires). Nothing is laid over the photo: no scrim, no blur, no gradient. The stub has a fixed height with room for a two-line name, so the tear line sits at the same height on every card; the name (Title, up to 1.25rem bold) and a Star Amber rating with a Muted count ("No ratings yet" when there is none) sit together in the middle of it. The top card tilts up to 14 degrees with the finger and is stamped LIKE (Signal Rose) or PASS (Ink Strong) on opaque Paper in a 3px-bordered 12px-radius box, and two more cards show 10px of themselves each beneath it as a stack, the nearer one rising as the top card leaves.

Under the stack are three controls with 12px Muted labels: Undo (a 48px ghost circle with a Hairline edge, at half strength while there is nothing to undo), Pass (a 64px Paper circle with a Hairline edge and an Ink Strong X) and Like (a 64px Signal Rose circle with a white outlined heart and the CTA tint). Like is the one rose block of the view; the progress fill is its quiet echo. A mouse-and-keyboard device trades the swipe hint for key caps (left arrow Pass, right arrow Like, backspace Undo). While the deck loads, a skeleton of the same ticket (tear line, three circles) holds the layout and pulses in opacity only. The finish card is the same ticket: a headline with a Saved status pill, one sentence of counts, the tear line, up to four liked places (name and category) and the one privacy line, then a full-width rose "Back to trip" above two quieter actions. Under reduced motion the flight and tilt become a 120ms fade in place and the stamp stays.

## Do's and Don'ts

### Do:
- **Do** build every screen from one-idea cards: white 24px cards on Desk Mist (light) or Night Card on Night Desk (dark), arranged in a Bento grid with a single gutter.
- **Do** use Signal Rose once per view for the primary action and let its echoes (Like, selected chip, progress, plane glyph) stay small.
- **Do** give each card a 1px Hairline and at most the Card rest shadow; lift only gate and stack cards, at 10% or less.
- **Do** write labels as tracked uppercase small text and show ids and codes in Geist Mono.
- **Do** make every control a pill that compresses to 0.98 on press, with a 4px focus-visible halo.
- **Do** keep the same structure in the dark theme: same radii, same grid, same accent, same hierarchy.
- **Do** use Muted (#62748e) or darker for any text a person must read; reserve Faint for placeholders and decorative icons.
- **Do** use lucide line icons at 16 to 24px for interface icons; emoji appear only inside content labels such as vibe chips, and the boarding pass uses its single plane glyph.
- **Do** keep gate screens free of raw trip ids; an id is shown only where it is the content (a "Trip code" field).

### Don't:
- **Don't** use gradient fills on surfaces, buttons, badges or text, and never purple or indigo ones: that is the named anti-reference. Known offender: the dark sticky button's `from-slate-100 to-indigo-100`.
- **Don't** use indigo, violet or purple anywhere. Known offenders: the dashboard's `text-indigo-300` numeral (flagged by the detector at `app/trip/dashboard/[id]/page.tsx:143`) and `bg-indigo-500` bars; replace with Signal Rose.
- **Don't** let rose become a background, a banner or a second primary in the same view.
- **Don't** lay a scrim, blur or gradient over a place photo: the type and credit chips are solid, and a missing photo is a flat Stub Grey.
- **Don't** add glow, neon, glass cards or coloured shadows other than the rose CTA tint.
- **Don't** mix radii inside a card or introduce sharp corners.
- **Don't** put any text other than a button label on Signal Rose, and don't set captions in Faint. Known offenders: `text-slate-400` captions (2.6:1), the `text-emerald-600` cost line (3.65:1), and `text-zinc-500` micro-labels on dark (3.6:1).
- **Don't** hard-code the dark hexes (#1A1A1A, #222222, #141414, #262626) as one-off arbitrary values in new code; reference the Night tokens.
