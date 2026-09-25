"use client";

import { SessionProvider } from "next-auth/react";
import type { ReactNode } from "react";

/**
 * Makes useSession()/signIn()/signOut() work anywhere in the tree.
 * SessionProvider relies on React context, which server components can't
 * provide - mounted once here so the root layout (a server component)
 * doesn't itself need "use client".
 */
export function AuthWrapper({ children }: { children: ReactNode }) {
  return <SessionProvider>{children}</SessionProvider>;
}
