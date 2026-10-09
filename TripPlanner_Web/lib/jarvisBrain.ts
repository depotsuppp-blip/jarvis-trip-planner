/**
 * The one place the chat route asks "what does Jarvis say?": a POST to the
 * Python brain. The brain's side of the contract:
 *
 *   POST {JARVIS_BRAIN_URL}   default http://127.0.0.1:8000/chat
 *   body  { prompt: string, history: [{ role: "user" | "jarvis", content }] }
 *         (history = earlier turns, oldest first, not including `prompt`)
 *   reply { reply: string }
 *
 * Any failure throws a JarvisBrainError whose message is safe to show the
 * user; the route keeps their message saved and answers 502.
 */

export type ChatTurn = { role: "user" | "jarvis"; content: string };

const BRAIN_URL = process.env.JARVIS_BRAIN_URL ?? "http://127.0.0.1:8000/chat";
// Tool calls and the reasoning tier can take a while; past this the turn fails.
const BRAIN_TIMEOUT_MS = 60_000;

export class JarvisBrainError extends Error {}

export async function askJarvis(message: string, history: ChatTurn[]): Promise<string> {
  let response: Response;
  try {
    response = await fetch(BRAIN_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt: message, history }),
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
