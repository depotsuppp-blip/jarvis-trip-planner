-- CreateSchema
CREATE SCHEMA IF NOT EXISTS "public";

-- CreateTable
CREATE TABLE "PollVote" (
    "id" TEXT NOT NULL,
    "tripId" TEXT NOT NULL,
    "name" TEXT NOT NULL,
    "lineUserId" TEXT NOT NULL DEFAULT '',
    "startDate" TEXT NOT NULL DEFAULT '',
    "endDate" TEXT NOT NULL DEFAULT '',
    "wishlist" TEXT NOT NULL DEFAULT '',
    "vibes" TEXT[] DEFAULT ARRAY[]::TEXT[],
    "submittedAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "anonId" TEXT NOT NULL DEFAULT '',
    "voterKey" TEXT NOT NULL,

    CONSTRAINT "PollVote_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "TripDraft" (
    "tripId" TEXT NOT NULL,
    "text" TEXT NOT NULL,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "TripDraft_pkey" PRIMARY KEY ("tripId")
);

-- CreateTable
CREATE TABLE "Poll" (
    "tripId" TEXT NOT NULL,
    "locked" BOOLEAN NOT NULL DEFAULT false,
    "lockedAt" TIMESTAMP(3),
    "generating" BOOLEAN NOT NULL DEFAULT false,
    "generatingStartedAt" TIMESTAMP(3),
    "lockedByLineUserId" TEXT NOT NULL DEFAULT '',

    CONSTRAINT "Poll_pkey" PRIMARY KEY ("tripId")
);

-- CreateTable
CREATE TABLE "action_logs" (
    "id" SERIAL NOT NULL,
    "intent_name" VARCHAR(100) NOT NULL,
    "status" VARCHAR(50) NOT NULL,
    "details" TEXT,
    "executed_at" TIMESTAMPTZ(6) DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "action_logs_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "chat_history" (
    "id" SERIAL NOT NULL,
    "role" VARCHAR(50) NOT NULL,
    "content" TEXT NOT NULL,
    "created_at" TIMESTAMPTZ(6) DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "chat_history_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "energy_logs" (
    "id" SERIAL NOT NULL,
    "chiller_id" VARCHAR(50),
    "energy_kwh" DECIMAL(10,2),
    "recorded_at" TIMESTAMPTZ(6) DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "energy_logs_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE INDEX "PollVote_tripId_idx" ON "PollVote"("tripId");

-- CreateIndex
CREATE UNIQUE INDEX "PollVote_tripId_voterKey_key" ON "PollVote"("tripId", "voterKey");

