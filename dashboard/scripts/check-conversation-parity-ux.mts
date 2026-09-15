import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import {
  isConversationNearBottom,
  preservedHistoryScrollTop,
  shouldAutoScrollForNewMessage,
} from '../src/lib/conversationScroll.ts'


const root = fileURLToPath(new URL('..', import.meta.url))
const conversations = readFileSync(`${root}/src/pages/Conversations.tsx`, 'utf8')
const renderer = readFileSync(`${root}/src/components/conversations/MessagePresentationCard.tsx`, 'utf8')
const orders = readFileSync(`${root}/src/components/conversations/CustomerOrdersDrawer.tsx`, 'utf8')

assert.equal(isConversationNearBottom({ scrollTop: 920, scrollHeight: 1_500, clientHeight: 520 }), true)
assert.equal(isConversationNearBottom({ scrollTop: 300, scrollHeight: 1_500, clientHeight: 520 }), false)

assert.equal(preservedHistoryScrollTop({
  previousScrollTop: 140,
  previousScrollHeight: 1_200,
  nextScrollHeight: 1_850,
}), 790, 'prepending history must retain the same visual anchor')

assert.equal(shouldAutoScrollForNewMessage({
  wasNearBottom: true,
  operatorPausedAutoScroll: false,
}), true, 'new messages follow an operator already at the latest message')
assert.equal(shouldAutoScrollForNewMessage({
  wasNearBottom: false,
  operatorPausedAutoScroll: true,
}), false, 'new messages do not steal an operator reading older history')

assert.match(conversations, /pinConversationToLatestAfterLayout/)
assert.match(conversations, /data-new-messages-affordance/)
assert.match(conversations, /data-sticky-customer-header/)
assert.match(conversations, /selected\.phone/)
assert.match(conversations, /<Phone[\s\S]*selected\.phone[\s\S]*WhatsApp/)
assert.match(conversations, /data-customer-orders-trigger/)
assert.match(conversations, /<CustomerOrdersDrawer/)

assert.match(renderer, /data-presentation-kind/)
assert.match(renderer, /loading="lazy"/)
assert.match(renderer, /onError=/)
assert.match(renderer, /data-presentation-action/)
assert.match(renderer, /presentation\.template\?\.footer/)

assert.match(orders, /data-customer-orders-drawer="read-only"/)
assert.match(orders, /conversationCustomerOrders/)
assert.doesNotMatch(orders, /cancelOrder|confirmCod|updateOrder|createShipment/)

console.log('conversation parity UX contract: ok')
