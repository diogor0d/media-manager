# iPhone PWA

Last updated: 2026-09-04

The web client is a zero-build, same-origin PWA designed for iPhone Safari and
standalone use. Install it from Safari with Share, then Add to Home Screen.
Cloudflare Access must cover the root, assets, service worker, manifest, and API.

## Mobile Flow

1. Choose one local media file. Detection and preview happen on-device.
2. Choose a compatible format and adjust only relevant controls.
3. Start conversion from the safe-area-aware bottom action.
4. Follow accessible Upload, Inspect, Convert, and Verify stages.
5. Compare exact sizes, then use Share or save through the native share sheet.
   Open remains available as a download fallback.

The interface uses 44-pixel minimum touch targets, narrow-screen header
fallbacks, explicit range labeling, `aria-current` processing stages, focused
view transitions, reduced-motion support, dynamic viewport units, and all four
iOS safe-area insets.

## Recovery and Updates

After upload completion, only the active job ID is stored in `sessionStorage`.
If iOS evicts or reloads the PWA, the client can recover polling or a retained
result until server expiry. Media bytes, filenames, credentials, and previews
are not persisted. An upload interrupted by process eviction cannot be resumed
because Safari does not grant durable access to the selected file; reconnects
without process loss still resume from the server's accepted chunk offset.

The service worker caches only the authenticated application shell, never API
responses. Shell revisions use a versioned cache. A waiting update is announced
and is not activated during conversion; activation reloads only after the user
requests it and processing is safe. Offline shell display does not imply that
conversion works offline.

Native file sharing fetches the retained result only after the user taps Share
or save. If the Web Share files API rejects that media type or size, the Open
fallback remains available.

## Verification Limits

Static tests verify the shell contract, but Windows cannot validate standalone
iOS process eviction, native share destinations, Safari download behavior, or
Add to Home Screen. Those behaviors remain gated by on-device testing.
