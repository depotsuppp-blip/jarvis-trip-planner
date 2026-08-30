/**
 * Verifies a LINE LIFF ID token against LINE's own verification endpoint
 * and returns the trusted LINE user id (claims.sub) on success. Returns
 * null on any failure - missing config, network error, non-2xx, or a
 * claim that doesn't check out - so every caller gets a clean
 * "unauthenticated" signal rather than having to distinguish failure
 * modes itself.
 *
 * Shared by every route that must know WHO is calling before mutating
 * data - app/api/poll/[id]/route.ts (voting) and
 * app/api/trigger-jarvis/route.ts (locking a poll, which spends real
 * LLM quota) - so this check exists exactly once.
 */
export async function verifyLineIdToken(idToken: string): Promise<string | null> {
  const clientId = process.env.LINE_CHANNEL_ID;
  if (!clientId) {
    console.error("LINE_CHANNEL_ID is not configured; cannot verify ID tokens.");
    return null;
  }

  let response: Response;
  try {
    response = await fetch("https://api.line.me/oauth2/v2.1/verify", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ id_token: idToken, client_id: clientId }),
    });
  } catch (err) {
    // TEMPORARY DIAGNOSTIC - see the other early returns below. Remove
    // once real mobile LINE sessions verify successfully.
    console.error("verifyLineIdToken: network error calling LINE's verify endpoint:", err);
    return null;
  }

  if (!response.ok) {
    const body = await response.text().catch(() => "");
    console.error(`verifyLineIdToken: LINE verify endpoint returned ${response.status}: ${body}`);
    return null;
  }

  const claims = await response.json().catch(() => null);
  if (!claims || typeof claims.sub !== "string" || !claims.sub) {
    console.error("verifyLineIdToken: response body has no valid sub claim:", claims);
    return null;
  }
  if (claims.aud !== clientId) {
    // Far and away the most likely cause of a persistent, every-attempt
    // failure on a real device while every other check passes: LIFF ID
    // format is "{channelId}-{suffix}" (see NEXT_PUBLIC_LIFF_ID), so
    // LINE_CHANNEL_ID must equal that prefix exactly - if it's set to a
    // different channel's id, or has stray whitespace, every real token
    // fails right here, every time, while the code itself is correct.
    console.error(
      `verifyLineIdToken: aud mismatch - token aud="${claims.aud}", configured LINE_CHANNEL_ID="${clientId}"`
    );
    return null;
  }
  if (claims.iss !== "https://access.line.me") {
    console.error(`verifyLineIdToken: unexpected iss claim: "${claims.iss}"`);
    return null;
  }
  if (typeof claims.exp !== "number" || claims.exp * 1000 <= Date.now()) {
    console.error(
      `verifyLineIdToken: token expired or missing exp claim (exp=${claims.exp}, now=${Date.now()})`
    );
    return null;
  }

  return claims.sub;
}

/**
 * Pulls and verifies the LINE ID token from a standard
 * `Authorization: Bearer <token>` header. Returns the trusted
 * lineUserId, or null if the header is missing/malformed or the token
 * doesn't check out.
 */
export async function verifyBearerLineToken(
  authorizationHeader: string | null
): Promise<string | null> {
  const [scheme, idToken] = (authorizationHeader || "").split(" ");
  if (scheme !== "Bearer" || !idToken) {
    return null;
  }
  return verifyLineIdToken(idToken);
}
