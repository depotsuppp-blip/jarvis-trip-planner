# Trip Planner

The standalone web app for Jarvis's Trip Planner feature. Deliberately a
separate application from Subtrack and from Jarvis itself - Jarvis only
generates links into this app (`/trip/poll/[id]`, `/trip/draft/[id]`) and
pushes them over LINE; it never renders any UI of its own.

Built mobile-first: the only realistic entry point is a link opened
inside LINE's in-app browser, not a desktop visit to the bare domain.

## Routes

- `app/trip/poll/[id]/page.tsx` - consensus mode. Friends submit a name,
  a date range, and a wishlist; everyone's submissions are listed below
  the form. "Lock & Generate Plan" is a placeholder for now.
- `app/trip/draft/[id]/page.tsx` - solo draft mode. One big textarea for
  the trip leader to dump links/ideas into, autosaved on demand.
  "Generate Itinerary" is a placeholder for now.
- `app/api/poll/[id]/route.ts` / `app/api/draft/[id]/route.ts` - the data
  backing both pages (GET to read, POST to write).

Both placeholder buttons currently just `alert("Will trigger Jarvis AI in
the next phase")` - the actual hand-off back to Jarvis's Gemini-based
Planner Agent (see `plugins/trip_planner.py` in the Jarvis project) is
what "the next phase" refers to, and does not exist yet.

## Data

`lib/store.ts` is a mock database: flat JSON files under `.data/`
(gitignored, created on first write), keyed by trip id. Good enough to
develop against; swap the functions in that one file for a real database
later without touching any route handler or page.

## Getting Started

```bash
cd TripPlanner_Web
npm run dev
```

Then visit e.g. `http://localhost:3000/trip/poll/test-123` or
`http://localhost:3000/trip/draft/test-123` - any string works as the id
while there is no real trip-creation flow yet.
