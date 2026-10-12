---
version: 1
slug: "tripplanner-web-app-trip-poll-id-page-tsx"
primary_target: "TripPlanner_Web/app/trip/poll/[id]/page.tsx"
related_targets: ["TripPlanner_Web/components/trip/PlanView.tsx","TripPlanner_Web/components/trip/TripBar.tsx","TripPlanner_Web/components/trip/DayTabs.tsx","TripPlanner_Web/components/trip/TimelineList.tsx","TripPlanner_Web/components/trip/MapPanel.tsx"]
---

# Surface brief: locked plan view (/trip/poll/[id], after lock)

## Scope and mode
Mode: Operate. The visitor reads a locked itinerary one day at a time. Target: the locked-plan branch of `TripPlanner_Web/app/trip/poll/[id]/page.tsx`; the vote form shown before lock is untouched. A redesign of this surface only, decided with the owner on 2026-10-09: every other page keeps the Boarding Pass Desk for now, so DESIGN.md gains a Plan (Editorial) section instead of being replaced.

## Audience, job, task, proof, constraints
- Audience: a signed-in traveler, or the organizer on the admin link, on a phone in LINE's browser or on a desktop, after the plan is locked.
- Job: understand the trip at a glance, then read one day in order and see where it goes.
- Task: pick a day, read its stops, glance at travel time and cost, see the route; the organizer can swap a stop.
- Proof and content: only the stored itinerary (destination, dates, headcount, budget, weather, per-day stops with travel legs, cost estimates and coordinates). Nothing invented. Estimates carry "~" and costs are per person. The map is Google Maps (revised 2026-10-10; before that there was no basemap and the pane was a route sketch that said so).
- Constraints: the owner's brief: Modern Editorial; crisp white; slate/charcoal borders; strict black and dark-grey text; #EC003F (Tailwind rose-600) only for active states and primary actions; 50/50 split with a sticky map on the right; day tabs in one left column; one-row sticky summary; no long day headings; subtle prices; days without stops are hidden. "Google Maps" attribution text stays where Places data shows without a Google map (the real map draws Google's own). English copy; Thai text must render. Light theme.
- Revision 2026-10-10, the owner's feedback on the first build ("too bland, pale and unfinished, like a wireframe"; keep the layout and the numbered sequence): the left timeline sits on a light grey panel (slate-50) beside a flush map; stop names bolder and darker; #EC003F also on the stop numbers (solid discs, white numbers, list and map), the route line, the tab underline and hover states; a real Google map with numbered pins and a route line replaces the sketch. This supersedes "only for active states and primary actions" and "crisp white" above.

## Chosen direction and memorable moment
A timetable beside its map. One calm column of days, one sticky route, no cards. Memorable moment: the stop numbers are also the map's pins, so hovering a stop lights its pin and switching day redraws that day's route.

## Unresolved decisions
- Resolved 2026-10-10: the real basemap is Google Maps and keeps the sketch's contract (numbered pins that match the list). Seen on Google's real tiles on 2026-10-12 (the owner's browser key, Maps JavaScript API on; pins, route, lit-pin chip, press-a-pin and day changes all as designed; the default basemap is busy but the pins and route read clearly). Still open: a Map ID with a muted style (see TripPlanner_Web/.env.example).
- The generator writes no short day title yet; the view shows `title` when present, otherwise just "Day N".
- Stop sentences stay as the model wrote them; making them shorter is a Stage 2 prompt change.
- Whether Editorial becomes the whole app's world is the owner's call; PRODUCT.md still says warm and playful.

## Direction contract
THESIS: The plan is read, not browsed: a single ruled column of days beside a sticky route, with no cards and no paragraph under any heading. It refuses the pastel Bento wall of day cards and an accent splashed on every dot.
OWN-WORLD: (revised 2026-10-10) a white bar, a Desk Mist timeline panel and a flush map; ink headings, bold ink stop names over Ink Body captions, Muted meta, 1px slate hairlines, radii 6 and 12px (the lit stop's sheet). Rose-600 on the stop numbers (solid discs, in the list and as map pins), the route line, the selected day's underline, the spine in its pale tint, halos, focus rings and the swap spinner. Numbered nodes on a thin spine carry the order; 11px tracked micro-labels carry cost; Geist on a hard ladder (28 / 17 / 14 / 13 / 12 / 11) with tabular numerals.
STORY: The traveler sees destination, dates and budget in one row, picks a day, reads its stops in order, and watches the route redraw; a stop under the pointer lights its pin.
FIRST VIEWPORT: At 1440x900, a 56px white bar (destination and a Locked dot; dates, travelers, budget, weather) over a 50/50 split. Left: Day tabs with a rose underline, the "Day 1" heading with one meta line, the numbered timeline. Right, full-bleed to the edge: a Google map whose numbered pins and route line match the list. On a phone: bar, tabs, list, with a List/Map switch.
FORM: user-pinned by the brief (Modern Editorial split-screen with day tabs); not rolled from concept-seed, so there is no seed key. Signature move: stop numbers double as map pins, and each day change redraws that day's route in about 320ms.
FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
