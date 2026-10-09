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
- Proof and content: only the stored itinerary (destination, dates, headcount, budget, weather, per-day stops with travel legs, cost estimates and coordinates). Nothing invented. Estimates carry "~" and costs are per person. There is no basemap, so the map is a route sketch and says so.
- Constraints: the owner's brief: Modern Editorial; crisp white; slate/charcoal borders; strict black and dark-grey text; #EC003F (Tailwind rose-600) only for active states and primary actions; 50/50 split with a sticky map on the right; day tabs in one left column; one-row sticky summary; no long day headings; subtle prices; days without stops are hidden. "Google Maps" attribution text stays where Places data shows without a Google map. English copy; Thai text must render. Light theme.

## Chosen direction and memorable moment
A timetable beside its map. One calm column of days, one sticky route, no cards. Memorable moment: the stop numbers are also the map's pins, so hovering a stop lights its pin and switching day redraws that day's route.

## Unresolved decisions
- A real basemap (Phase 2) must keep the sketch's contract: numbered pins that match the list.
- The generator writes no short day title yet; the view shows `title` when present, otherwise just "Day N".
- Stop sentences stay as the model wrote them; making them shorter is a Stage 2 prompt change.
- Whether Editorial becomes the whole app's world is the owner's call; PRODUCT.md still says warm and playful.

## Direction contract
THESIS: The plan is read, not browsed: a single ruled column of days beside a sticky route, with no cards and no paragraph under any heading. It refuses the pastel Bento wall of day cards and an accent splashed on every dot.
OWN-WORLD: White ground, ink headings, Ink Strong body, Muted meta, 1px slate hairlines as the only structure, radii 0 and 6px. Rose-600 appears only on the selected day's underline, the lit stop and its pin, focus rings and the swap spinner. Numbered nodes on a thin spine carry the order; 11px tracked micro-labels carry travel time and cost; Geist on a hard ladder (22 / 15 / 13 / 11) with tabular numerals.
STORY: The traveler sees destination, dates and budget in one row, picks a day, reads its stops in order, and watches the route redraw; a stop under the pointer lights its pin.
FIRST VIEWPORT: At 1440x900, a 56px white bar (destination and a Locked dot; dates, travelers, budget, weather) over a 50/50 split. Left: Day tabs with a rose underline, the "Day 1" heading with one meta line, the numbered timeline. Right, full-bleed to the edge: a hairline-gridded route sketch whose pins match the list. On a phone: bar, tabs, list, with a List/Map switch.
FORM: user-pinned by the brief (Modern Editorial split-screen with day tabs); not rolled from concept-seed, so there is no seed key. Signature move: stop numbers double as map pins, and each day change redraws that day's route in about 320ms.
FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
