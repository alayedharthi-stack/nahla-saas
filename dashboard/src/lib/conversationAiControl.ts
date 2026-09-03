/**
 * Conversation AI-control projection.
 *
 * Queue / staff-request flags and AI pause are independent:
 *
 *   AI_ON  <=> aiPaused === false
 *   AI_OFF <=> aiPaused === true
 *
 * needsHuman / handoffActive / status=human must not drive the
 * Pause / Start AI toggle. Those flags are queue + supervision.
 *
 * Canonical persisted sources (no new markers):
 *   AI_TOGGLE_SOURCE          = aiPaused
 *   QUEUE_SOURCE               = needsHuman | handoffActive
 *   EXPLICIT_TAKEOVER_SOURCE  = takenOverBy | takenOverAt | status=human
 */

export type ConversationAiControlState = {
  aiPaused?: boolean
  needsHuman?: boolean
  handoffActive?: boolean
  status?: string | null
  takenOverAt?: string | null
  takenOverBy?: string | null
}

export type AiToggleKind = 'pause' | 'resume'
export type AiResumeCallPath = 'resumeConversationAI'
export type EndSupervisionCallPath = 'returnHandoffToAI'

export function isAiPaused(c: ConversationAiControlState): boolean {
  return !!c.aiPaused
}

export function aiToggleKind(c: ConversationAiControlState): AiToggleKind {
  return isAiPaused(c) ? 'resume' : 'pause'
}

export function isQueueActive(c: ConversationAiControlState): boolean {
  return !!c.needsHuman || !!c.handoffActive
}

export function isExplicitTakeover(c: ConversationAiControlState): boolean {
  return !!c.takenOverBy || !!c.takenOverAt || c.status === 'human'
}

/** Menu "إنهاء الإشراف" — queue and/or explicit merchant takeover. */
export function isSupervisionActive(c: ConversationAiControlState): boolean {
  return isQueueActive(c) || c.status === 'human'
}

/**
 * Start AI must resume pause only. It must never clear a customer-request
 * queue via returnHandoffToAI.
 */
export function aiResumeCallPath(
  c: ConversationAiControlState,
): AiResumeCallPath | null {
  return isAiPaused(c) ? 'resumeConversationAI' : null
}

export function endSupervisionCallPath(
  c: ConversationAiControlState,
): EndSupervisionCallPath | null {
  return isSupervisionActive(c) ? 'returnHandoffToAI' : null
}

export function isConversationAiOn(c: ConversationAiControlState): boolean {
  return !isAiPaused(c)
}
