import type { Metadata } from "next";
import { LoginScreen } from "@/components/auth/LoginScreen";
import { getAuthConfigStatus, warnAboutAuthConfig } from "@/lib/authConfig";

export const metadata: Metadata = {
  title: "Sign in | Jarvis",
};

/**
 * Standalone home for LoginScreen, and the page NextAuth sends people to
 * instead of its own built-in sign-in/error screens - see `pages` in
 * lib/auth.ts. A failed sign-in lands here as /login?error=<Code> (plus
 * ?callbackUrl= when the failure came from an OAuth provider), which
 * LoginScreen turns into a friendly inline message and uses to send the
 * visitor back to their trip after a retry.
 *
 * The trip poll/dashboard pages render the same LoginScreen inline instead
 * of redirecting here, so a signed-out visitor never loses their place; the
 * "/" fallback below only applies when nothing says where they came from
 * (e.g. an expired magic link, which NextAuth reports without a
 * callbackUrl).
 */
export default function LoginPage() {
  warnAboutAuthConfig();
  const { googleReady, emailReady } = getAuthConfigStatus();
  return <LoginScreen fallbackCallbackUrl="/" googleReady={googleReady} emailReady={emailReady} />;
}
