/**
 * Longest voter name the poll will store - the cap the old free-text
 * "Your name" field enforced with maxLength.
 */
export const MAX_VOTER_NAME_LENGTH = 100;

/**
 * What a signed-in voter is called in the poll's list: their account name,
 * else the part of their email before the "@" (an email-link sign-in has no
 * name), else "Traveler". Never the full email - the list is shown to
 * everyone on the trip.
 *
 * Shared by POST /api/poll/[id], which stores the result, and the poll page,
 * which shows it under "Voting as", so the two can't drift apart.
 */
export function voterDisplayName(user: {
  name?: string | null;
  email?: string | null;
}): string {
  const name = user.name?.replace(/\s+/g, " ").trim();
  if (name) return name.slice(0, MAX_VOTER_NAME_LENGTH);

  const emailName = user.email?.split("@")[0]?.trim();
  if (emailName) return emailName.slice(0, MAX_VOTER_NAME_LENGTH);

  return "Traveler";
}
