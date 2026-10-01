# Salla sales widgets: one-time app snippet

Salla injects a published App Snippet into stores that install the Nahla app. Enter **JavaScript only** in the Salla Partner Portal snippet editor, then publish the app update. The dashboard toggle controls the tenant bundle on subsequent storefront page loads.

```javascript
(function () {
  if (document.querySelector('script[data-nahla-universal]')) return;
  var script = document.createElement('script');
  script.src = 'https://api.nahlah.ai/merchant/widgets/salla-auto.js';
  script.defer = true;
  script.dataset.nahlaUniversal = '';
  document.head.appendChild(script);
})();
```

The universal script reads Salla's store ID, including the `salla-<id>` theme class, and requests `/merchant/widgets/salla/<id>/nahla-widgets.js`. The API resolves that ID through `Integration.external_store_id`, with a legacy `config.store_id` fallback. Never serve a bundle for a store without a verified Salla integration.

Verification for a merchant:

1. Confirm the merchant dashboard shows a connected Salla integration.
2. Open a storefront page and confirm the universal and store bundle scripts load.
3. Enable only the desired widgets in Nahla; the bundle includes every enabled sales widget.
4. Reload the storefront and confirm the expected widget appears once. Toggle it off and reload to confirm it disappears.

The dashboard's manual JavaScript loader is an alternative one-time theme installation for a merchant who has not installed the app. It uses the authenticated tenant's bundle URL. Do not paste HTML `<script>` tags into Salla's JavaScript editor.

Reference: [Salla App Snippet documentation](https://docs.salla.dev/partner-apis/app-snippet).
