# Estimate validation — 2026-10-09

Occurrence and verification date: 2026-10-09. Local disposable media only;
no production services, accounts, or user media were used.

Six generated fixtures/settings were measured with the real FFmpeg processor
and its normal command/resource controls. Timed sources lasted eight seconds;
video was 640×360 at 12 FPS, audio was a 440 Hz tone, and the image was 640×360.
Measurements ran alongside other disposable validation workloads, so they are
acceptance evidence rather than a performance benchmark.

| Case | Estimated bytes | Actual bytes | Estimated seconds | Actual seconds | Target met |
| --- | ---: | ---: | ---: | ---: | --- |
| video MP4 q50 | 262,360–524,722 | 356,507 | 0–3 | 0.668 | n/a |
| video WebM q76 480p drop | 150,000–666,425 | 236,671 | 4–36 | 12.235 | no |
| audio MP3 q75 | 138,798–277,598 | 193,767 | 0–1 | 0.230 | n/a |
| audio auto compress 100KB | 75,000–196,099 | 98,856 | 0–3 | 1.092 | yes |
| image WebP q50 | 6,200–12,402 | 8,858 | 0–1 | 0.114 | n/a |
| image WebP 480p 20KB | 6,200–25,000 | 8,858 | 0–3 | 0.149 | yes |

All six actual output sizes and processing times fell within the sampled
ranges in this run. The deliberately demanding 200,000-byte WebM target was
missed; the smallest validated candidate was returned with `met_target=false`.
Ranges are advisory and are not calibrated guarantees for arbitrary media.
The timing check found and corrected a one-second minimum that excluded
subsecond processing from the displayed lower bound.

Automated tests additionally cover three samples of at most two seconds each,
shared sampling capacity, cancellation cleanup, deadline fallback, unknown
media duration, unchanged-estimate caching, processing priority over a blocked
sample, advanced compression controls, and impossible size targets. The source
fixtures and real-encoder assertions are in `tests/test_estimates.py` and
`tests/test_processor_advanced.py`.

Browser evidence: Chromium and WebKit completed a real PNG→WebP workflow with
advanced options, separate estimate/start actions, page closure, history
restoration, and result retrieval. The additional READY URL processing test
uses an explicitly mocked download response; it is not MeTube acceptance.
Responsive captures cover phone, tablet, and desktop. WebKit emitted warnings
only from Playwright's screenshot helper being rejected by the application's
strict CSP; functional assertions passed. Physical iPhone acceptance is pending.
