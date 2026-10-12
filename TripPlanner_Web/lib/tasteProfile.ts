/**
 * Browser-side check the poll and swipe pages share: has the signed-in person
 * already finished the taste quiz? Asks GET /api/taste-profile, which reads
 * User.hasCompletedTasteProfile (set when POST /api/taste-profile saves a
 * finished deck). The quiz is once per person, so a person who has done it is
 * not sent to it again - see app/trip/poll/[id]/page.tsx (after a first vote)
 * and app/trip/swipe/[id]/page.tsx (on arrival).
 *
 * Fails OPEN: an error, a timeout or a signed-out answer all count as "not
 * finished". Asking someone the quiz twice is a small cost; shutting them out
 * of it because a request hiccuped is not.
 */
export async function hasCompletedTasteQuiz(timeoutMs = 8000): Promise<boolean> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch("/api/taste-profile", { cache: "no-store", signal: controller.signal });
    if (!response.ok) return false;
    const data: unknown = await response.json().catch(() => null);
    return (data as { completed?: unknown } | null)?.completed === true;
  } catch {
    return false;
  } finally {
    clearTimeout(timer);
  }
}
