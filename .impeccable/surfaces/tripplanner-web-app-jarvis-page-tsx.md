---
version: 1
slug: "tripplanner-web-app-jarvis-page-tsx"
primary_target: "TripPlanner_Web/app/jarvis/page.tsx"
related_targets: ["TripPlanner_Web/components/jarvis/JarvisHub.tsx","TripPlanner_Web/components/jarvis/Sidebar.tsx","TripPlanner_Web/components/jarvis/Composer.tsx","TripPlanner_Web/components/jarvis/OrbStage.tsx","TripPlanner_Web/components/jarvis/DynamicRenderer.tsx"]
---

# Surface brief: Jarvis hub (/jarvis)

## Scope and mode
Mode: Operate. The visitor is in a task (talking to an assistant). Targets: `TripPlanner_Web/app/jarvis/page.tsx` (a server component that mounts the hub), `TripPlanner_Web/components/jarvis/*` (the screen) and `TripPlanner_Web/lib/jarvis/*` (the state behind it, no React). Restructured on 2026-10-20 from one chat column into an agentic dashboard; documented in DESIGN.md, "Jarvis Hub (Editorial)". The incumbent look (white, hairlines, 6px corners, ink text, Deep Signal Rose only for focus) is kept; the owner pinned "Modern Editorial: clean white space, crisp typography, high contrast".

## Audience, job, task, proof, constraints
- Audience: the signed-in owner and travelers who plan trips by chatting with Jarvis, on a laptop or a phone.
- Job: ask Jarvis for something and get an answer they can act on - a trip link today, a tool's result (a calendar, a status board) tomorrow.
- Task: write or speak a request; see what Jarvis is doing; read the reply (text or a widget); come back to an earlier session; know which agents and tools are available.
- Proof and content: every message is what was said or stored. Anything the page makes up (the widget previews, the simulated voice turn, the sample statuses) is labelled Sample. The agents list does not claim a health it cannot know.
- Constraints: Next 16 / React 19, Tailwind 4. The orb (`Orb.tsx`, thinking-orbs) is not touched: one orb, two homes (36px over the dock, 224px centred on the stage), positioned by handing it a rectangle, never through state. The Python brain is not touched; the only server changes are additive Next routes (`GET /api/chat/sessions`, `GET /api/chat?conversationId=`, `newConversation` on `POST /api/chat`). Interface copy is English only. Contrast measured on the real render, never smaller than 12px for text that must be read (Muted and darker).

## Chosen direction and memorable moment
A Desk Mist sidebar (sessions, agents and tools, a folded test bench) beside one white conversation column, the dock at its bottom centre. Memorable moment: the orb. It lives inside the dock as part of the input, swells to the middle of the conversation while Jarvis listens or thinks (the conversation frosts, the sidebar stays crisp and shows the agent working), and glides home - and it keeps its frames because the chat's state lives outside React and the orb has a store of its own.

## Unresolved decisions
- Two sessions were given this spec at once. This branch (`feat/jarvis-hub-agentic`, worktree `.claude/worktrees/jarvis-hub`) is one implementation; another was built in the main tree. The owner picks one.
- The widget wire format (a JSON object in the stored reply string) is defined on the Next side only; the Python brain does not emit it yet. Whether the brain should emit it as a string in `reply` or as a structured field is open (the page reads both).
- Agents and tools: only the brain's state is live. Where the real list comes from (the brain's tool registry, MCP) is open.
- Session management beyond list, open and new (rename, delete, search) is not designed.

## Direction contract
THESIS: The hub is one conversation with its history and its helpers beside it, and a tool's answer is part of the conversation, not a pop-up. It refuses the chat-app arrangement of bubbles with avatars, a floating action orb, a card for every result and a modal for every task.
OWN-WORLD: Paper conversation column; Desk Mist sidebar ruled off by one Hairline; a 56px bar on each side sharing one rule; an Ink bubble for the person and an unboxed reply under a byline for Jarvis; one 6px ruled frame for every widget; Deep Signal Rose only for selection, caret, focus and the dot beside the open session; Go Green and Alert Red for state, with a word and an icon beside each.
STORY: A person opens the hub, sees yesterday's sessions and which agents are there, asks for something by typing or by tapping Talk, watches the orb take the stage while Jarvis works, and gets text or a ruled widget back in the same column - then picks up another session without losing their place.
FIRST VIEWPORT: At 1440x900: a 288px Desk Mist sidebar (wordmark and collapse button, "New session", Sessions grouped by day with the open one on a white sheet, Agents & tools with a dot and a word each, the test bench and the account at the foot); a 56px top bar (the session's title at the left, the orb's state word at the right); the conversation column, 672px, with the person's Ink bubble and Jarvis's reply and a System status widget under it; the dock at the bottom centre of the stage with the small orb inside it at the left, then the field, Send and Talk.
FORM: A sidebar-and-stage app shell, the category's standard for an assistant, rendered in the Editorial dialect. Not rolled from concept-seed (the owner pinned the world and the structure), so there is no seed key.
FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance. Status 2026-10-20: DESIGN.md written; no raster ships; verified by scripted headless-Chrome runs (desktop 1440x900, tablet 820, phone 390, signed in and out, a stand-in brain, real routes and Postgres, React render counts) and `next build`; the Impeccable finish reviewer was NOT run (no subagent was authorized) and the sidecar `.impeccable/design.json` was NOT regenerated.
