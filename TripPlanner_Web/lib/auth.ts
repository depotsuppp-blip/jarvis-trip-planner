import type { NextAuthOptions } from "next-auth";
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
