import { config } from "dotenv";

// Plain `import "dotenv/config"` only reads a file literally named
// `.env` - it has no idea `.env.local` exists, unlike Next.js's own
// loader (which is why `npm run dev` sees DIRECT_URL fine but the
// Prisma CLI didn't). This project only has .env.local here, no plain
// .env, so this one explicit load is enough - if a plain .env with
// shared defaults is ever added back, load it first and .env.local
// second (still without `override: true`) so .env.local keeps winning.
// quiet: true - dotenv's own "injected env (N) from ..." banner prints
// to stdout, not stderr, which contaminates any command whose stdout is
// meant to be pure output (e.g. `prisma migrate diff --script > x.sql`
// - the banner ends up as an invalid first line inside the generated
// SQL file otherwise).
config({ path: ".env.local", quiet: true });

import { defineConfig, env } from "prisma/config";

// Prisma 7 moved the connection URL out of schema.prisma - CLI commands
// (migrate dev/deploy, db execute, studio) read it from here instead.
// This must be DIRECT_URL, not DATABASE_URL: migrate dev needs a plain,
// unpooled connection (it holds a session-level advisory lock and runs
// DDL), and Neon's pooled endpoint (the one DATABASE_URL points at, via
// PgBouncer) doesn't support that - see lib/prisma.ts, which is the
// running app's OWN connection and deliberately keeps using the pooled
// DATABASE_URL; this file only affects the CLI.
export default defineConfig({
  schema: "prisma/schema.prisma",
  migrations: {
    path: "prisma/migrations",
  },
  datasource: {
    url: env("DIRECT_URL"),
  },
});
