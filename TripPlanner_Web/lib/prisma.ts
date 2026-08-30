import { PrismaPg } from "@prisma/adapter-pg";
import { PrismaClient } from "@prisma/client";

// Next.js reloads route modules on every file change in dev, which would
// otherwise open a fresh PrismaClient (and a fresh DB connection) per
// reload. Stashing it on globalThis survives that reload and is a no-op
// in production, where each serverless instance only ever sees this
// module evaluated once.
const globalForPrisma = globalThis as unknown as { prisma?: PrismaClient };

function createPrismaClient(): PrismaClient {
  const adapter = new PrismaPg({ connectionString: process.env.DATABASE_URL });
  return new PrismaClient({ adapter });
}

export const prisma = globalForPrisma.prisma ?? createPrismaClient();

if (process.env.NODE_ENV !== "production") {
  globalForPrisma.prisma = prisma;
}
