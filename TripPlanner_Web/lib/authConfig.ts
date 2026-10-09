/**
 * Checks the environment NextAuth needs and says plainly what is missing.
 * A missing secret or provider credential otherwise surfaces only as the
 * client's "Login failed", with the reason buried in NextAuth's own log.
 */

export interface AuthConfigStatus {
  /** Human-readable problems, worst first; empty when everything needed is set. */
  problems: string[];
  googleReady: boolean;
  emailReady: boolean;
}

const MIN_SECRET_LENGTH = 32;

export function getAuthConfigStatus(env: NodeJS.ProcessEnv = process.env): AuthConfigStatus {
  const set = (name: string) => Boolean(env[name]?.trim());
  const googleReady = set("GOOGLE_CLIENT_ID") && set("GOOGLE_CLIENT_SECRET");
  const emailReady = set("EMAIL_SERVER_HOST") && set("EMAIL_FROM");
  const problems: string[] = [];

  const secret = env.NEXTAUTH_SECRET?.trim() ?? "";
  if (!secret) {
    problems.push("NEXTAUTH_SECRET is not set: sessions cannot be signed, so every sign-in fails.");
  } else if (secret.length < MIN_SECRET_LENGTH) {
    problems.push(
      `NEXTAUTH_SECRET is only ${secret.length} characters; use at least ${MIN_SECRET_LENGTH} ` +
        "(node -e \"console.log(require('crypto').randomBytes(32).toString('hex'))\")."
    );
  }
  if (!set("NEXTAUTH_URL") && !set("VERCEL_URL")) {
    problems.push("NEXTAUTH_URL is not set: OAuth callbacks and magic links cannot be built.");
  }
  if (!googleReady) {
    problems.push("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not both set: \"Continue with Google\" will fail.");
  }
  if (!emailReady) {
    problems.push("EMAIL_SERVER_HOST / EMAIL_FROM are not set: \"Continue with Email\" will fail.");
  }
  if (!googleReady && !emailReady) {
    problems.unshift("No sign-in method is configured at all: nobody can sign in.");
  }
  return { problems, googleReady, emailReady };
}

let warned = false;

/** Logs each problem once per server process. Safe to call from module scope. */
export function warnAboutAuthConfig() {
  if (warned) return;
  warned = true;
  const { problems } = getAuthConfigStatus();
  if (problems.length === 0) return;
  console.warn(
    "[auth] Sign-in is not fully configured (this is what makes the client say \"Login failed\"):\n" +
      problems.map((problem) => `  - ${problem}`).join("\n") +
      "\n  See .env.example; restart the dev server after editing .env.local."
  );
}
