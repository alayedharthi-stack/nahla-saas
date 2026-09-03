/**
 * Cases A–F: AI toggle is aiPaused only. Queue is not AI_OFF.
 *
 * Run: npm run check:ai-toggle-projection   (from dashboard/)
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import {
  aiResumeCallPath,
  aiToggleKind,
  endSupervisionCallPath,
  isConversationAiOn,
  isExplicitTakeover,
  isQueueActive,
  isSupervisionActive,
  type ConversationAiControlState,
} from '../src/lib/conversationAiControl.ts'

type Case = {
  id: string
  state: ConversationAiControlState
  expect: {
    toggle: 'pause' | 'resume'
    queue: boolean
    takeover: boolean
    resumeCall: 'resumeConversationAI' | null
    endSupervision: 'returnHandoffToAI' | null
  }
}

const cases: Case[] = [
  {
    id: 'A-normal-ai',
    state: { aiPaused: false, needsHuman: false, handoffActive: false, status: 'active' },
    expect: {
      toggle: 'pause',
      queue: false,
      takeover: false,
      resumeCall: null,
      endSupervision: null,
    },
  },
  {
    id: 'B-queue-only-staff-request',
    state: { aiPaused: false, needsHuman: true, handoffActive: true, status: 'active' },
    expect: {
      toggle: 'pause',
      queue: true,
      takeover: false,
      resumeCall: null,
      endSupervision: 'returnHandoffToAI',
    },
  },
  {
    id: 'C-explicit-pause',
    state: { aiPaused: true, needsHuman: false, handoffActive: false, status: 'active' },
    expect: {
      toggle: 'resume',
      queue: false,
      takeover: false,
      resumeCall: 'resumeConversationAI',
      endSupervision: null,
    },
  },
  {
    id: 'D-explicit-takeover',
    state: {
      aiPaused: false,
      needsHuman: true,
      handoffActive: true,
      status: 'human',
      takenOverAt: '2026-09-03T05:00:00.000Z',
      takenOverBy: 'user:27',
    },
    expect: {
      toggle: 'pause',
      queue: true,
      takeover: true,
      resumeCall: null,
      endSupervision: 'returnHandoffToAI',
    },
  },
  {
    id: 'E-queued-plus-explicit-stop-ai',
    state: { aiPaused: true, needsHuman: true, handoffActive: true, status: 'active' },
    expect: {
      toggle: 'resume',
      queue: true,
      takeover: false,
      resumeCall: 'resumeConversationAI',
      endSupervision: 'returnHandoffToAI',
    },
  },
  {
    id: 'F-end-supervision-target',
    state: {
      aiPaused: false,
      needsHuman: true,
      handoffActive: true,
      status: 'human',
      takenOverAt: '2026-09-03T05:00:00.000Z',
      takenOverBy: 'dashboard:handoff',
    },
    expect: {
      toggle: 'pause',
      queue: true,
      takeover: true,
      resumeCall: null,
      endSupervision: 'returnHandoffToAI',
    },
  },
]

let failed = 0

function check(ok: boolean, msg: string) {
  if (!ok) {
    failed += 1
    console.error(`FAIL ${msg}`)
  }
}

for (const c of cases) {
  check(aiToggleKind(c.state) === c.expect.toggle, `${c.id} toggle=${aiToggleKind(c.state)} expected=${c.expect.toggle}`)
  check(isQueueActive(c.state) === c.expect.queue, `${c.id} queue=${isQueueActive(c.state)} expected=${c.expect.queue}`)
  check(isExplicitTakeover(c.state) === c.expect.takeover, `${c.id} takeover=${isExplicitTakeover(c.state)} expected=${c.expect.takeover}`)
  check(aiResumeCallPath(c.state) === c.expect.resumeCall, `${c.id} resumeCall=${aiResumeCallPath(c.state)} expected=${c.expect.resumeCall}`)
  check(endSupervisionCallPath(c.state) === c.expect.endSupervision, `${c.id} endSupervision=${endSupervisionCallPath(c.state)} expected=${c.expect.endSupervision}`)
  check(isConversationAiOn(c.state) === (c.expect.toggle === 'pause'), `${c.id} aiOn mismatch`)
}

check(isSupervisionActive(cases[1].state) === true, 'B queue-only still exposes End Supervision')
check(aiResumeCallPath(cases[1].state) !== 'returnHandoffToAI' as never, 'B Start AI must not call returnHandoffToAI')
check(aiResumeCallPath(cases[4].state) === 'resumeConversationAI', 'E Start AI resumes pause only')
check(isQueueActive(cases[4].state) === true, 'E Start AI must leave queue flags independent')

const page = readFileSync(join(import.meta.dirname, '..', 'src', 'pages', 'Conversations.tsx'), 'utf8')
check(!/intelligenceOff\s*=\s*humanTakeover/.test(page), 'Conversations.tsx must not collapse queue into intelligenceOff')
check(page.includes('aiToggleKind'), 'Conversations.tsx must use aiToggleKind for Pause/Start AI')
check(page.includes('resumeConversationAI'), 'Start AI must call resumeConversationAI')
check(/if \(inTakeover\) \{\s*await featureRealityApi\.returnHandoffToAI/.test(page) === false, 'resumeIntelligenceForSelected must not call returnHandoffToAI')
check(page.includes('endHumanSupervisionForSelected'), 'End Supervision control must remain')
check(page.includes('returnHandoffToAI'), 'End Supervision must still call returnHandoffToAI')
check(page.includes('aiToggleKind(selected)') && page.includes('sm:hidden'), 'mobile reply bar must use the same toggle truth')

if (failed) {
  console.error(`\n${failed} check(s) failed`)
  process.exit(1)
}

console.log('ok  conversation AI toggle projection (cases A–F, mobile/desktop source guard)')
