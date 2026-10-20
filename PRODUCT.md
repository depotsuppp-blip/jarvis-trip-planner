# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Travelers on the open web: people who sign in with Google or email to plan a trip, alone or with friends. LINE is one way in, not the only one. The person who starts a group trip (the organizer) does it through Jarvis, the owner's voice assistant, which drops a link into a LINE chat; everyone else opens that link, signs in, and takes part without installing anything.

Roles inside a trip:

- **Organizer:** starts the trip, holds the private admin link, and locks the plan.
- **Participant:** joins from a shared link, votes on dates, adds wishlist places and vibes, answers a one-time taste quiz (a swipe deck of 24 places from around the world; once per person, not once per trip), and reads the plan.
- **Solo traveler:** plans alone on a draft board, or lets the planner decide everything.

## Product Purpose

Turns "we should go somewhere" into a locked itinerary the whole group had a hand in, without a long chat thread: dates are agreed by vote, taste is gathered from votes and from a one-time swipe quiz on real places around the world, and an AI-written plan is built from all of it. The product's own name for success is "No One Left Behind": no participant's preferences get dropped.

## Positioning

Four claims the user confirmed another trip planner could not truthfully make:

- **Everyone shapes the plan.** Date votes and swipes on places both feed one itinerary.
- **Starts in LINE, by voice.** Jarvis turns a spoken request into a shared link in a LINE chat; friends join without installing anything.
- **Grounded in real places.** Itineraries use real venues, travel times, costs and weather instead of invented names.
- **Group and solo in one tool.** A poll for groups still deciding; a draft board for someone who already has places in mind.

## Operating Context

- Trip Planner is a separate application from Jarvis (a Python voice assistant). Jarvis only creates trips, generates links into this app (`/trip/poll/[id]`, `/trip/draft/[id]`) and delivers them (over LINE from the microphone, in the reply from the web chat); it renders no UI of its own.
- Group flow as built today: the organizer asks Jarvis by voice, a poll link goes to LINE, participants sign in and add dates, wishlist and vibes (a first vote carries them straight on to the swipe deck), the organizer locks the poll - with the "Lock & Generate Plan" button on their private admin link, or by asking Jarvis to finalize it (see "Chat flow" and the organizer's authority below) - and the generated itinerary, built around the voters' Taste Profiles, then appears for everyone on the same page.
- Chat flow: a signed-in person types "plan a trip to Chiang Mai" in the Jarvis hub (`/jarvis`); Jarvis asks this app to plan it (`POST /api/trip/create`, server-to-server) and answers with the link. The plan is built at once for that person alone, from their Taste Profile when they have one, and is already locked: the length and the head count are the ones they gave (Jarvis asks for any it does not have - the destination, how many days and how many people - and never assumes a default), no dates, no admin link. Saying it is a group trip creates a poll instead and answers with its voting link; the organizer's private link still goes to LINE only, never into the chat. Once the votes are in, "finalize it" locks that poll from the chat (Jarvis checks the vote count first and asks to confirm) and answers with the plan's link - `POST /api/poll/[id]/finalize`, server-to-server, no admin token needed; the poll records who made it (`Poll.createdByUserId`) and only that person may lock it from the chat. By voice the same call is made for a poll that was made by voice, and the plan's link goes to LINE. Jarvis only ever gives a trip link that a tool returned during that same request (`link_guard.py` checks every trip link and trip id in a reply before it is sent): a link from earlier in the conversation is never reused for a new trip, and when the planner fails it says so and gives no link.
- Entry points: a link opened in LINE's in-app browser (LIFF), or the open web with sign-in.
- The organizer's authority is a private admin link sent only to them; locking a plan and swapping a stop both require it. A plan made straight from the chat has no admin link, so its stops cannot be swapped. The one other way to lock a poll is asking Jarvis, and it is deliberately narrower: from the web chat only the person who created the poll in the chat may, and by voice only a poll that was made by voice (the voting link is public, so anyone signed in could otherwise lock someone else's poll by asking Jarvis to). A poll made from the chat cannot be locked by voice, nor one made by voice from the chat; the admin link works for every poll.

## Capabilities and Constraints

Built today:

- **Consensus poll** (`/trip/poll/[id]`): each signed-in participant adds a date range, wishlist places and vibe tags, named after their account (no name to type; one entry per account, which a later submit replaces); everyone's entries are listed; the organizer locks the poll and generates the plan. A participant's first vote on a trip sends them on to the swipe deck, unless they have already finished the taste quiz. An organizer who opens their admin link without signing in can lock but must sign in to vote.
- **Generated itinerary:** destination inferred from the group's input (or given, when Jarvis plans a trip from the chat); built around the Taste Profiles of the people it is for - the themes they liked most shape what kinds of place each day holds, and the themes they did not like are treated as neutral, never as dislikes (see "Swipe deck" below); per-day stops with real venues, travel time between stops, per-person cost estimates and weather. Beside the plan sits a real Google map: each day's stops as numbered pins joined by a route line, matching the numbers in the list (it needs a Maps JavaScript API browser key; without one the pane says so and the plan reads on its own). The organizer can swap a stop for a live nearby alternative.
- **Dashboard** (`/trip/dashboard/[id]`): participants, top vibes and a wishlist summary.
- **Swipe deck / taste quiz** (`/trip/swipe/[id]`; its cards come from `GET /api/places/deck`): a one-time onboarding quiz that learns a person's general taste (nature or shopping? history or nightlife?), not where to go on one trip, so it asks nothing about a destination. The deck is 24 cards: eight curated themes (for example historical landmarks in Rome, quiet nature spots in Vancouver, night markets in Taipei; the list is `DECK_CATEGORIES` in `lib/placeDeck.ts`), three real places per theme, dealt in a fixed order and fetched fresh on every visit. Like or Pass on each card; signed-in only. Finishing the deck saves the answers as the person's Taste Profile (`TasteSwipe`, one row per place) and marks the quiz done (`hasCompletedTasteProfile`); after that the poll no longer sends them to it, and opening it sends them straight back to the trip.
  - **Plan generation reads the Taste Profile** (`lib/tasteSummary.ts`): positive signals only: per theme, liked over answered, and a theme with at least a third of its answers liked counts (the top three are used). A pass says almost nothing - three sample photos can simply be unappealing - so a theme with no likes is neutral and is never avoided or left out. Someone who liked nearly everything, or nothing, says nothing that stands out and adds no signal. A group's profile is its voters' answers added together, and the prompt says how many of the travelers have one. It is one paragraph in each of the two prompts: what to build the days from (Stage 1) and how to choose between the real candidates (Stage 2). With no profile the prompts are exactly what they were. Only a finished quiz counts (`TasteSwipe`), not a half-answered one.
  - Not finished: each card is also saved as it is answered, per trip (`UserSwipeAction`, with its theme as `category`) so a refresh resumes, which means someone who has not finished and opens the quiz from a second trip's link answers the same cards again; there is no way to retake the quiz once it is done; and nothing tells the person, on the plan page, that their taste shaped it (Jarvis says so in the chat).
- **Jarvis hub** (`/jarvis`): the signed-in person's chat with Jarvis as an agentic dashboard - a sidebar of their sessions (the 50 most recent, grouped by day, `GET /api/chat/sessions`; one opens with `GET /api/chat?conversationId=`; "New session" starts one with `newConversation: true`) and of the agents and tools Jarvis has, beside one conversation, with the orb and the Talk button in a dock at the bottom centre. A reply is text, or - when a tool answers with more than words - a JSON object in the same stored string that the page draws as a widget in the same column: `{ "type": "widget", "tool": "calendar", "data": {...} }`, or `{ "text": "...", "widget": { "tool", "data" } }`, or `{ "text": "...", "widgets": [...] }` (`lib/jarvis/messageContent.ts` has the contract; what the person typed is never parsed). Only `system_status` has a view so far; any other tool is drawn as a placeholder that shows its raw data. Not real yet: the microphone (Talk plays a simulated turn), and the status of every agent but the brain; previews made on the page (`/status`, the test bench) are labelled Sample and are neither sent nor saved.
- **Sign-in:** Google or email magic link; required for poll, dashboard and swipe (the organizer's admin link bypasses it for poll and dashboard).
- **Solo draft board** (`/trip/draft/[id]`): free-text board; its "Generate Itinerary" button is still a placeholder.

In the data model but with no UI yet: trip participants and friendships ("Past Trip Mates"). The taste quiz's per-person record, one row per answered place (`TasteSwipe`: the place id, our own theme label and Like or Pass), and the user's one-time "already finished" flag (`hasCompletedTasteProfile`) are written when the quiz is finished (`POST /api/taste-profile`) and read when a plan is generated; the record holds no Google content beyond the place id. A poll or chat-made trip also records who made it from the chat (`Poll.createdByUserId`, empty for polls made by voice or before it existed); it only decides who may lock a poll through Jarvis.

Terminology: poll, draft board, organizer, lock & generate, vibes, wishlist, swipe deck (the taste quiz), Taste Profile (what the quiz learns about one person), "No One Left Behind".

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

1. **No one's preferences get dropped.** Every participant's votes and taste-quiz answers should visibly shape the outcome.
2. **Joining costs almost nothing.** A link and, at most, a sign-in; never an install.
3. **Plans are real, or they say so.** Name only places, times, costs and weather that came from real data; show "unavailable" rather than guess.
4. **Group and solo are equals.** Neither path is an afterthought.
5. **Honor the data and the languages.** Respect Google's attribution and storage rules, and handle Thai and English content everywhere.

## Accessibility & Inclusion

Users and content span Thai and English: names, places and typed input can be in either script. No formal accessibility standard has been set.
