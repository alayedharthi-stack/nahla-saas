/**
 * Order-updates settings: persisted individual flags vs effective-after-master.
 *
 * Run: npm run check:order-updates-settings   (from dashboard/)
 */
import {
  LEGACY_DEFAULT_ON_KEYS,
  ORDER_UPDATE_SERVICE_KEYS,
  effectiveEnabledAfterMaster,
  isMasterEnabled,
  isServiceEnabled,
  patchPayloadForIndividual,
  patchPayloadForMaster,
  persistedIndividualEnabled,
  type OrderUpdatesSettings,
} from '../src/api/orderUpdates.ts'

function assert(cond: unknown, msg: string): asserts cond {
  if (!cond) throw new Error(msg)
}

const persistedOn: OrderUpdatesSettings = {
  enabled: true,
  flags: {
    order_confirmation: true,
    shipping_tracking: true,
    cod_confirmation: false,
    payment_pending: false,
  },
  services: {
    order_confirmation: { enabled: true },
    shipping_tracking: { enabled: true },
  },
}

assert(isMasterEnabled(persistedOn) === true, 'master defaults from enabled')
assert(isServiceEnabled(persistedOn, 'order_confirmation') === true, 'persisted confirmation ON')
assert(isServiceEnabled(persistedOn, 'shipping_tracking') === true, 'persisted shipping ON')
assert(persistedIndividualEnabled(persistedOn, 'order_confirmation') === true, 'flags win over services')

const masterOff: OrderUpdatesSettings = {
  ...persistedOn,
  enabled: false,
  effective: {
    order_confirmation: false,
    shipping_tracking: false,
    cod_confirmation: false,
    payment_pending: false,
    payment_confirmed: false,
    order_preparing: false,
    order_ready: false,
    out_for_delivery: false,
    order_delivered: false,
    order_cancelled: false,
    order_refunded: false,
  },
}

assert(isMasterEnabled(masterOff) === false, 'master OFF')
assert(
  persistedIndividualEnabled(masterOff, 'order_confirmation') === true,
  'master OFF must not rewrite persisted confirmation',
)
assert(
  persistedIndividualEnabled(masterOff, 'shipping_tracking') === true,
  'master OFF must not rewrite persisted shipping',
)
assert(
  effectiveEnabledAfterMaster(masterOff, 'order_confirmation') === false,
  'effective confirmation OFF when master OFF',
)
assert(
  effectiveEnabledAfterMaster(masterOff, 'shipping_tracking') === false,
  'effective shipping OFF when master OFF',
)

const masterOnAgain: OrderUpdatesSettings = { ...masterOff, enabled: true, effective: undefined }
assert(
  effectiveEnabledAfterMaster(masterOnAgain, 'order_confirmation') === true,
  'master ON restores persisted confirmation',
)
assert(
  effectiveEnabledAfterMaster(masterOnAgain, 'shipping_tracking') === true,
  'master ON restores persisted shipping',
)

const individualPatch = patchPayloadForIndividual('order_confirmation', true)
assert(individualPatch.enabled === undefined, 'individual PATCH must not send master')
assert(
  Object.keys(individualPatch.services ?? {}).join(',') === 'order_confirmation',
  'individual PATCH sends only the changed key',
)
assert(individualPatch.flags === undefined, 'individual PATCH must not send a full flags snapshot')

const masterPatch = patchPayloadForMaster(false)
assert(masterPatch.enabled === false, 'master PATCH sends enabled only')
assert(masterPatch.services === undefined, 'master PATCH must not send individual services')
assert(masterPatch.flags === undefined, 'master PATCH must not send flags snapshot')

assert(LEGACY_DEFAULT_ON_KEYS.includes('order_confirmation'), 'legacy confirmation default')
assert(ORDER_UPDATE_SERVICE_KEYS.length === 11, 'all 11 service keys typed')

console.log('check:order-updates-settings OK')
