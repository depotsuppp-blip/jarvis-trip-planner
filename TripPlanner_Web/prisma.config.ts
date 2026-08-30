import { defineConfig } from "prisma/config";

// Prisma 7 moved the connection URL out of schema.prisma - CLI commands
// (db push / db execute / studio) read it from here instead, taken from
// whatever already has DATABASE_URL in the environment the CLI is
// invoked from. The running app doesn't use this file at all - it builds
// its own adapter in lib/prisma.ts.
export default defineConfig({
  schema: "prisma/schema.prisma",
  datasource: {
    url: process.env.DATABASE_URL,
  },
});
