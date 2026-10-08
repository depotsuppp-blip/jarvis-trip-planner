---
version: 1
slug: "tripplanner-web-app-trip-swipe-id-page-tsx"
primary_target: "TripPlanner_Web/app/trip/swipe/[id]/page.tsx"
related_targets: ["TripPlanner_Web/components/trip/SwipeDeck.tsx","TripPlanner_Web/components/PageHeader.tsx"]
---

# Surface brief: swipe step (/trip/swipe/[id])

## Scope and mode
Mode: Operate. The visitor completes one task. Targets: `TripPlanner_Web/app/trip/swipe/[id]/page.tsx`, `TripPlanner_Web/components/trip/SwipeDeck.tsx`, and the shared `TripPlanner_Web/components/PageHeader.tsx` (gains an optional subtitle; its other callers are untouched). Refinement of an existing surface inside the incumbent system in DESIGN.md ("The Boarding Pass Desk"), light theme, not a new world.

## Audience, job, task, proof, constraints
- Audience: a signed-in participant on a phone (LINE in-app browser or open web), one-handed, a minute or two.
- Job: tell the planner which places they would love, without typing.
- Task: answer about 20 cards; undo one slip; know it is saved; know where to go next (back to the trip poll).
- Proof and content: only real Google Places data (name, rating, count, Google's own category label, photo with its author credit). Nothing invented; address dropped from the card.
- Constraints: Google rules (photo author credit, "Google Maps" attribution unaltered, store place IDs only). Thai and English names. Picks are private: the only privacy claim is "Only you can see what you picked." Plan generation does not use swipes yet, so no copy may promise it does. DESIGN.md and PRODUCT.md bind: flat fills, no blur or scrim on cards, no gradients, no indigo/violet, one rose primary per view, Muted or darker for readable text, radii 12/16/24/pill only.

## Chosen direction and memorable moment
Photo-led boarding-pass card: photo on top, a dashed Perforation with two 20px notches, a ticket stub carrying name and rating. Pass and Like are equal-size 64px circles; Pass is neutral (red is reserved for errors), Like is the single rose block. Memorable moment: the stamp (LIKE / PASS) inking in as the ticket is pulled off the stack, and a one-step Undo that lays the ticket back from the side it left.

## Unresolved decisions
- White on Signal Rose measures 3.75:1 (known brand decision in DESIGN.md); kept as is for "Back to trip".
- Undo only changes what the person sees until they answer again; the earlier answer stays saved server-side (no API change in scope).
- Whether to localize interface copy (Thai) is undecided product-wide.

## Direction contract
THESIS: The swipe step is one boarding-pass ticket on a bare desk where the photo decides and everything else stays quiet. It refuses the dating-app arrangement: gradient scrim over the photo, glass chips, a rainbow of hearts, a confetti finish.
OWN-WORLD: Desk Mist ground; a Paper ticket with Hairline edge and Stack-lift shadow; a dashed Perforation with two 20px Desk Mist notches between photo and stub; uppercase Label-Micro chips; 12px-radius LIKE and PASS stamps; 64px circle controls; Signal Rose only on Like (and the progress fill); Star Amber only on the rating star.
STORY: A signed-in friend answers about twenty real places in a minute, trusts that a slip can be undone, sees that their picks are private and saved, and leaves knowing where to go next: back to the trip.
FIRST VIEWPORT: At 390x844 with no scroll: the shared sticky header ("Pick your places", "Near X" with a Change link); a counter row "7 OF 20" and "3 LIKED" over a 6px progress bar; the ticket (photo about 60 percent, perforation, name and rating stub) with two cards peeking beneath; Undo (48), Pass (64), Like (64, the only rose block) with labels; a one-line hint and "Google Maps" at the bottom.
FORM: Photo-led ticket card with a stub, chosen by the user in the shape interview ("photo first plus ticket stub"). Not rolled from concept-seed, so there is no seed key; the incumbent system pins the world.
FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
