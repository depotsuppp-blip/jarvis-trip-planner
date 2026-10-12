-- AlterTable
ALTER TABLE "User" ADD COLUMN     "hasCompletedTasteProfile" BOOLEAN NOT NULL DEFAULT false;

-- CreateTable
CREATE TABLE "TasteSwipe" (
    "id" TEXT NOT NULL,
    "userId" TEXT NOT NULL,
    "googlePlaceId" TEXT NOT NULL,
    "category" TEXT NOT NULL,
    "action" "SwipeAction" NOT NULL,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "TasteSwipe_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE UNIQUE INDEX "TasteSwipe_userId_googlePlaceId_key" ON "TasteSwipe"("userId", "googlePlaceId");

-- AddForeignKey
ALTER TABLE "TasteSwipe" ADD CONSTRAINT "TasteSwipe_userId_fkey" FOREIGN KEY ("userId") REFERENCES "User"("id") ON DELETE CASCADE ON UPDATE CASCADE;

