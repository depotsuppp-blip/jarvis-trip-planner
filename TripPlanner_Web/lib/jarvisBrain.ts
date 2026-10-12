/**
 * The one place the chat route asks "what does Jarvis say?": a POST to the
 * Python brain. The brain's side of the contract:
 *
 *   POST {JARVIS_BRAIN_URL}   default http://127.0.0.1:8000/chat
 *   body  { prompt: string,
 *           history: [{ role: "user" | "jarvis", content }],
 *           user_id?: string, app_url?: string }
 *   reply { reply: string }
 *
 *   history = earlier turns, oldest first, not including `prompt`.
 *   user_id = the signed-in person asking (User.id). Jarvis's trip tool acts on
 *             their behalf - it plans the trip for them and personalizes it from
 *             their Taste Profile - so it needs to know who they are.
 *   app_url = this web app's own origin (e.g. http://localhost:3000). The trip
 *             tool calls back into THIS instance (it is the one that has the
 *             route and the database the person is looking at) and builds the
 *             link it hands back from the same origin.
 *
 * Any failure throws a JarvisBrainError whose message is safe to show the
 * user; the route keeps their message saved and answers 502.
 */

export type ChatTurn = { role: "user" | "jarvis"; content: string };

/** Who is asking and where this app lives - see the contract above. */
export type ChatContext = { userId?: string; appUrl?: string };

const BRAIN_URL = process.env.JARVIS_BRAIN_URL ?? "http://127.0.0.1:8000/chat";
// Tool calls and the reasoning tier can take a while - and "plan a trip" is a
// tool call that itself runs a whole generation (two LLM calls, Places, Routes:
// ~15-30s) between the model's turns. Past this the turn fails.
const BRAIN_TIMEOUT_MS = 120_000;

export class JarvisBrainError extends Error {}

export async function askJarvis(
  message: string,
  history: ChatTurn[],
  context: ChatContext = {}
): Promise<string> {
  let response: Response;
  try {
    response = await fetch(BRAIN_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        prompt: message,
        history,
        // Left out when unknown, so the brain keeps its voice-assistant defaults.
        ...(context.userId ? { user_id: context.userId } : {}),
        ...(context.appUrl ? { app_url: context.appUrl } : {}),
      }),
      cache: "no-store",
      signal: AbortSignal.timeout(BRAIN_TIMEOUT_MS),
    });
  } catch (error) {
    const timedOut = error instanceof DOMException && error.name === "TimeoutError";
    console.error(`Jarvis brain unreachable at ${BRAIN_URL}:`, error);
    throw new JarvisBrainError(
      timedOut ? "Jarvis brain took too long to answer." : "Jarvis brain is offline."
    );
  }

  if (!response.ok) {
    console.error(`Jarvis brain answered ${response.status} ${response.statusText}`);
    throw new JarvisBrainError(`Jarvis brain returned an error (${response.status}).`);
  }

  const data: unknown = await response.json().catch(() => null);
  const reply = (data as { reply?: unknown } | null)?.reply;
  if (typeof reply !== "string" || !reply.trim()) {
    throw new JarvisBrainError("Jarvis brain sent an empty reply.");
  }
  return reply;
}
