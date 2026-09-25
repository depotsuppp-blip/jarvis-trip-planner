import type { DefaultSession } from "next-auth";

// The adapter user id set by lib/auth.ts's session() callback isn't part
// of next-auth's built-in Session type - augment it here rather than
// casting `session.user` at every call site.
declare module "next-auth" {
  interface Session {
    user: {
      id: string;
    } & DefaultSession["user"];
  }
}
