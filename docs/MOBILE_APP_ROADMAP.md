# CountDepot Mobile App Roadmap

## Recommended path

Start with a production-ready PWA, then ship store apps using a thin native wrapper.

This keeps CountDepot as one product: the existing Flask app remains the source of truth, while iOS and Android get installable launchers, camera access, push-notification support later, and store presence without rebuilding every screen twice.

## Phase 1: Installable mobile web app

- Add a web app manifest, app icons, service worker, offline page, and mobile app metadata.
- Keep inventory data network-first so stale cached inventory never overwrites live operations.
- Make camera scanning work from installed standalone mode.
- Add mobile smoke tests for login, add item, scan/search, reservations, checkout, and bulk flows.

## Phase 2: App store wrapper

- Use Capacitor for iOS and Android if we need shared native plugins and a standard Apple/Google build pipeline.
- Use Android Trusted Web Activity only if Android speed matters more than native flexibility.
- Keep authentication inside the existing web session first; add native push/deep links after store release.

## Phase 3: Native capabilities

- Push notifications for low stock, reservation conflicts, overdue check-ins, and failed invite/setup events.
- Barcode scanning fallback using native camera plugins if browser scanning is unreliable on older devices.
- Deep links for `/add-item`, reservations, item details, and checkout flows.
- Device-aware audit logging for security and customer support.

## Release requirements

- Apple Developer Program account.
- Google Play Console account.
- Production domain with HTTPS.
- App name, subtitle, descriptions, screenshots, privacy policy URL, support URL, and app icon PNG export set.
- Clear privacy disclosures for camera access, account data, inventory data, and any analytics/error reporting.

## Current status

- PWA foundation is implemented in this repo.
- Native wrapper projects are not created yet.
- PNG icon exports, store screenshots, privacy copy, and push notification backend are still needed before store submission.
