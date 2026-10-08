# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Travelers on the open web: people who sign in with Google or email to plan a trip, alone or with friends. LINE is one way in, not the only one. The person who starts a group trip (the organizer) does it through Jarvis, the owner's voice assistant, which drops a link into a LINE chat; everyone else opens that link, signs in, and takes part without installing anything.

Roles inside a trip:

- **Organizer:** starts the trip, holds the private admin link, and locks the plan.
- **Participant:** joins from a shared link, votes on dates, adds wishlist places and vibes, swipes on suggested places, and reads the plan.
- **Solo traveler:** plans alone on a draft board, or lets the planner decide everything.

## Product Purpose

Turns "we should go somewhere" into a locked itinerary the whole group had a hand in, without a long chat thread: dates are agreed by vote, taste is gathered from votes and swipes on real places, and an AI-written plan is built from all of it. The product's own name for success is "No One Left Behind": no participant's preferences get dropped.

## Positioning

Four claims the user confirmed another trip planner could not truthfully make:

- **Everyone shapes the plan.** Date votes and swipes on places both feed one itinerary.
- **Starts in LINE, by voice.** Jarvis turns a spoken request into a shared link in a LINE chat; friends join without installing anything.
- **Grounded in real places.** Itineraries use real venues, travel times, costs and weather instead of invented names.
- **Group and solo in one tool.** A poll for groups still deciding; a draft board for someone who already has places in mind.

## Operating Context

- Trip Planner is a separate application from Jarvis (a Python voice assistant). Jarvis only creates trips, generates links into this app (`/trip/poll/[id]`, `/trip/draft/[id]`) and pushes them over LINE; it renders no UI of its own.
- Group flow as built today: the organizer asks Jarvis by voice, a poll link goes to LINE, participants sign in and add dates, wishlist and vibes, the organizer locks the poll, and the generated itinerary then appears for everyone on the same page.
- Entry points: a link opened in LINE's in-app browser (LIFF), or the open web with sign-in.
- The organizer's authority is a private admin link sent only to them; locking a plan and swapping a stop both require it.

## Capabilities and Constraints

Built today:

- **Consensus poll** (`/trip/poll/[id]`): each participant adds a name, date range, wishlist places and vibe tags; everyone's entries are listed; the organizer locks the poll and generates the plan.
- **Generated itinerary:** destination inferred from the group's input; per-day stops with real venues, travel time between stops, per-person cost estimates and weather (the map is still a placeholder). The organizer can swap a stop for a live nearby alternative.
- **Dashboard** (`/trip/dashboard/[id]`): participants, top vibes and a wishlist summary.
- **Swipe deck** (`/trip/swipe/[id]?destination=`): Like or Pass on about 20 Google Places suggestions; answers are saved per signed-in user and trip. Plan generation does not use them yet.
- **Sign-in:** Google or email magic link; required for poll, dashboard and swipe (the organizer's admin link bypasses it for poll and dashboard).
- **Solo draft board** (`/trip/draft/[id]`): free-text board; its "Generate Itinerary" button is still a placeholder.

In the data model but with no UI yet: trip participants and friendships ("Past Trip Mates").

Terminology: poll, draft board, organizer, lock & generate, vibes, wishlist, swipe deck, "No One Left Behind".

Constraints:

- Google Places rules: every Google photo carries its author's credit, "Google Maps" attribution appears wherever Places data is shown without a Google map, and only place IDs may be stored (no other Places content may be cached or kept).
- Interface copy is English only today; whether to localize it is undecided.
- The README still calls LINE's in-app browser the only realistic entry point; the user has since confirmed the open web is the primary audience.

## Brand Commitments

- Name: "Trip Planner".
- Stated by the user: the interface follows Airbnb / Bento minimalism and avoids AI-slop such as purple gradients; new UI matches "our existing light-mode Bento Grid / Airbnb aesthetic" (white cards, soft shadows). Light and dark are both official themes (confirmed 2026-10-08). The visual system itself is recorded in DESIGN.md, not here.
- Personality, chosen by the user: friendly and warm, crisp and clear, playful and lively.
- No logo or brand assets exist yet (`public/` holds only framework defaults).

## Evidence on Hand

No user research, testimonials, usage data, screenshots or press exist in the repo; future work must not invent any. The only real content is what the product itself generates (polls, plans) and Google Places data.

## Product Principles

1. **No one's preferences get dropped.** Every participant's votes and swipes should visibly shape the outcome.
2. **Joining costs almost nothing.** A link and, at most, a sign-in; never an install.
3. **Plans are real, or they say so.** Name only places, times, costs and weather that came from real data; show "unavailable" rather than guess.
4. **Group and solo are equals.** Neither path is an afterthought.
5. **Honor the data and the languages.** Respect Google's attribution and storage rules, and handle Thai and English content everywhere.

## Accessibility & Inclusion

Users and content span Thai and English: names, places and typed input can be in either script. No formal accessibility standard has been set.
