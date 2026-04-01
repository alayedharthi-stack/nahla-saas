/**
 * Nahla AI — Storefront Tracking Snippet
 * Version: 1.0
 * Loaded async from: https://api.nahlaai.com/snippet.js
 *
 * Tracks storefront events and feeds them into the Nahla autopilot engine.
 * Supports: page_view, product_view, add_to_cart, begin_checkout, cart_abandon
 */
;(function (w, d) {
  'use strict';

  var TRACK_URL = 'https://api.nahlaai.com/track/event';
  var cfg = w.__nahla_config || {};
  var tenantId = cfg.tenant_id || '';
  var storeId  = cfg.store_id  || '';

  if (!tenantId) return; // safety: don't run without tenant config

  /* ── Utility ─────────────────────────────────────────────────────────── */

  function send(event_type, payload) {
    var body = JSON.stringify({
      event_type:  event_type,
      tenant_id:   tenantId,
      store_id:    storeId,
      payload:     payload || {},
      url:         w.location.href,
      referrer:    d.referrer,
      ts:          Date.now(),
    });
    // Use sendBeacon when available so events survive page navigation
    if (navigator.sendBeacon) {
      navigator.sendBeacon(TRACK_URL, new Blob([body], { type: 'application/json' }));
    } else {
      var xhr = new XMLHttpRequest();
      xhr.open('POST', TRACK_URL, true);
      xhr.setRequestHeader('Content-Type', 'application/json');
      xhr.send(body);
    }
  }

  /* ── Auto events ─────────────────────────────────────────────────────── */

  // Page view — fires on every page load
  send('page_view', { title: d.title });

  // Product view — detect Salla product page
  if (cfg.product_id) {
    send('product_view', {
      product_id:   cfg.product_id,
      product_name: cfg.product_name || '',
      price:        cfg.product_price || null,
    });
  }

  /* ── Salla event bus integration ────────────────────────────────────── */

  // Listen to Salla's global event bus if available
  function onSallaReady() {
    if (!w.Salla) return;

    // Add to cart
    w.Salla.event.on('cart.add', function (e) {
      send('add_to_cart', {
        product_id:   e && e.id,
        product_name: e && e.name,
        price:        e && e.price,
        quantity:     e && e.quantity,
      });
    });

    // Cart updated
    w.Salla.event.on('cart.update', function (e) {
      send('cart_update', {
        total:    e && e.total,
        items:    e && e.items_count,
      });
    });

    // Begin checkout
    w.Salla.event.on('checkout.start', function (e) {
      send('begin_checkout', {
        total: e && e.total,
        items: e && e.items_count,
      });
    });

    // Order completed
    w.Salla.event.on('order.created', function (e) {
      send('order_created', {
        order_id:     e && e.id,
        order_number: e && e.reference_id,
        total:        e && e.amounts && e.amounts.total && e.amounts.total.amount,
      });
    });
  }

  if (d.readyState === 'loading') {
    d.addEventListener('DOMContentLoaded', onSallaReady);
  } else {
    onSallaReady();
  }

  /* ── Cart abandonment signal ─────────────────────────────────────────── */

  // Fire abandon signal when user is about to leave with items in cart
  if (cfg.cart_items_count && cfg.cart_items_count > 0) {
    var abandonFired = false;
    function fireAbandon() {
      if (abandonFired) return;
      abandonFired = true;
      send('cart_abandon', {
        items:      cfg.cart_items_count,
        cart_total: cfg.cart_total || null,
        customer_phone: cfg.customer_phone || null,
      });
    }
    d.addEventListener('visibilitychange', function () {
      if (d.visibilityState === 'hidden') fireAbandon();
    });
  }

  /* ── Expose manual track API ─────────────────────────────────────────── */
  w.NahlaTrack = { send: send };

}(window, document));
