---
version: 2
slug: "tripplanner-web-app-trip-swipe-id-page-tsx"
primary_target: "TripPlanner_Web/app/trip/swipe/[id]/page.tsx"
related_targets: ["TripPlanner_Web/components/trip/SwipeDeck.tsx"]
---

# Surface brief: swipe step (/trip/swipe/[id])

## Scope and mode
Mode: Operate. The visitor completes one task. Targets: `TripPlanner_Web/app/trip/swipe/[id]/page.tsx` and `TripPlanner_Web/components/trip/SwipeDeck.tsx` (the shared `PageHeader` no longer serves this page: it has its own bar). Restyled on 2026-10-12 into the Modern Editorial dialect (DESIGN.md, "Swipe (Editorial)") when the step became the global taste quiz: light theme, a second dialect beside the Boarding Pass Desk, the same one the plan view speaks.

## Audience, job, task, proof, constraints
- Audience: a signed-in participant on a phone (LINE in-app browser or open web), one-handed, a minute or two.
- Job: tell the product which kinds of place they like in general (nature or shopping, history or nightlife), without typing.
- Task: answer 24 cards (eight themes of three, from cities around the world); undo one slip; know it is saved; know where to go next (back to the trip poll).
- Proof and content: only real Google Places data (name, rating, count, Google's own type label, photo with its author credit) plus our own city and theme for each card. Nothing invented; address dropped from the card. The page asks nothing about a destination.
- Constraints: Google rules (photo author credit, "Google Maps" attribution unaltered, store place ids only). Thai and English names. Picks are private: the only privacy claim is "Only you can see what you picked." Plan generation does not use swipes or the Taste Profile yet, so no copy may promise that (no "used for your plans"). Someone who has finished the quiz is sent back to the trip when they open it again, and there is no retake. DESIGN.md and PRODUCT.md bind: flat fills, no blur or scrim on cards, no gradients, no indigo/violet, Deep Signal Rose only on Like and the one primary action, Muted or darker for readable text, 6px radius, no shadows.

## Chosen direction and memorable moment
One ruled card on stark white: the photo on top, a hairline, then the place name and one row of "city · theme" (where in the world, and which taste this card measures) with the rating at the right. Pass is neutral, Like is the single Deep Signal Rose block. Memorable moment: the stamp (LIKE / PASS) inking in as the card is pulled off the stack, a one-step Undo that lays the card back from the side it left, and a finish card that tallies likes theme by theme.

## Unresolved decisions
- White on Deep Signal Rose measures 4.53:1 (it clears AA for the 14-16px labels used).
- Undo only changes what the person sees until they answer again; the earlier answer stays saved server-side (no API change in scope).
- Each card is saved as it is answered, per trip (`UserSwipeAction`, with the theme as `category`); finishing saves the whole set as the person's Taste Profile (`TasteSwipe`) and sets `hasCompletedTasteProfile`. Whether to offer a retake is undecided.
- Whether to localize interface copy (Thai) is undecided product-wide.

## Direction contract
THESIS: The swipe step is one ruled card on a white page where the photo decides and the line under the name says where in the world it is. It refuses the dating-app arrangement: gradient scrim over the photo, glass chips, a rainbow of hearts, a confetti finish; and it no longer dresses as a boarding pass.
OWN-WORLD: Paper ground; a Paper card with a Hairline edge, 6px corners and no shadow; a 1px rule between photo and text; solid Paper and Ink chips on the photo; 6px-radius LIKE and PASS stamps; 52px labelled buttons; Deep Signal Rose only on Like (and the primary action, and the focus ring); Star Amber only on the rating star.
STORY: A signed-in friend answers 24 real places from around the world in a minute, trusts that a slip can be undone, sees that their picks are private and saved, sees what they like theme by theme, and leaves knowing where to go next: back to the trip.
FIRST VIEWPORT: At 390x844 with no scroll: the white bar ("Your travel taste", "Places from around the world"); a counter row "0 of 24" and "0 liked" over a 2px progress line; the card (photo about 390px tall, a rule, the name, "Rome · History" with the rating at the right) with two cards peeking beneath; Undo, Pass and Like in one 52px row (Like the only rose block); a one-line hint and "Google Maps".
FORM: A photo-led card with a ruled text block, kept from the earlier "photo first" decision but redrawn without the ticket (no perforation, notches, shadow or circle controls). Not rolled from concept-seed, so there is no seed key; the incumbent system pins the world.
FINISH: Checked on 2026-10-12 with scripted headless-Chrome runs against a stub backend (layout at 320x568 to 1280x800, contrast, drag, keyboard, undo, save failure, error, empty and signed-out states, Thai and long names) and `next build`. The Impeccable finish reviewer was not run, and the look has not been seen on live Places photos.
