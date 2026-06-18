/** Display plan feature text without leading emoji prefixes. */
export function displayPlanFeature(text: string): string {
  const trimmed = text.trim()
  const arabic = trimmed.match(/[\u0621-\u064A].*/u)
  return arabic ? arabic[0].trim() : trimmed
}

/** True when a feature string still contains pictographic emoji. */
export function featureHasEmoji(text: string): boolean {
  return /\p{Extended_Pictographic}/u.test(text)
}
