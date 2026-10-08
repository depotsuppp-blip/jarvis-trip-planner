import { getServerSession, type NextAuthOptions } from "next-auth";
import GoogleProvider from "next-auth/providers/google";
import EmailProvider from "next-auth/providers/email";
import { PrismaAdapter } from "@auth/prisma-adapter";
import { prisma } from "@/lib/prisma";

/**
 * Shared NextAuth config - imported by app/api/auth/[...nextauth]/route.ts
 * and by any server-side code that needs getServerSession(authOptions) to
 * find out who's signed in. Database session strategy (the adapter's
 * Session model, not a JWT) so a session can be revoked server-side by
 * deleting its row, and so User.id is stable and available for the
 * TripParticipant/Friendship/UserSwipeAction rows in schema.prisma.
 */
export const authOptions: NextAuthOptions = {
  adapter: PrismaAdapter(prisma),
  session: {
    strategy: "database",
  },
  providers: [
    GoogleProvider({
      clientId: process.env.GOOGLE_CLIENT_ID ?? "",
      clientSecret: process.env.GOOGLE_CLIENT_SECRET ?? "",
    }),
    EmailProvider({
      server: {
        host: process.env.EMAIL_SERVER_HOST,
        port: Number(process.env.EMAIL_SERVER_PORT ?? 587),
        auth: {
          user: process.env.EMAIL_SERVER_USER,
          pass: process.env.EMAIL_SERVER_PASSWORD,
        },
      },
      from: process.env.EMAIL_FROM,
    }),
  ],
  // Replaces every NextAuth-rendered screen with our own login UI
  // (components/auth/LoginScreen.tsx, served standalone at app/login/
  // page.tsx), so its default unstyled pages never appear. signIn AND
  // error both need setting: NextAuth sends an OAuth failure
  // (OAuthCallback, OAuthSignin, OAuthAccountNotLinked, ...) to
  // pages.signIn as ?callbackUrl=...&error=..., but everything else - an
  // expired or already-used magic link ("Verification"), a bad server
  // config ("Configuration") - to pages.error as a bare ?error=....
  // verifyRequest isn't navigated to today (LoginScreen calls
  // signIn("email") with redirect: false and shows its own "Check your
  // inbox" state), but pointing it here keeps the default page
  // unreachable if that ever changes.
  pages: {
    signIn: "/login",
    error: "/login",
    verifyRequest: "/login",
  },
  callbacks: {
    // Adapter's default session callback only forwards name/email/image -
    // attach the adapter user id too, since every Phase 2 model above
    // (TripParticipant.userId, Friendship.requesterId/addresseeId,
    // UserSwipeAction.userId) keys off it.
    session({ session, user }) {
      if (session.user) {
        session.user.id = user.id;
      }
      return session;
    },
  },
};

/**
 * The signed-in user's id, or null for a signed-out caller - the check
 * every route handler that must answer 401 makes before doing anything
 * (see app/api/trip/swipe/route.ts). Reads the database session behind
 * the session cookie, so it costs one session lookup per call.
 */
export async function getSessionUserId(): Promise<string | null> {
  const session = await getServerSession(authOptions);
  return session?.user?.id ?? null;
}
