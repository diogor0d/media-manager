# Performance Notes

Last updated: 2026-09-04

Performance work is constrained by output fidelity and security. The project
does not silently change codecs, encoder presets, quality, worker concurrency,
hardware acceleration, or stream-copy behavior to claim speed gains.

Implemented optimizations:

- FFprobe version, FFmpeg encoder, and FFmpeg filter startup checks run
  concurrently.
- Small incoming body fragments are coalesced into bounded 512 KiB writes.
- Browser uploads use resumable bounded chunks and retry from the accepted
  server offset after a network interruption.

On 2026-09-04, six local runs with the first discarded measured the startup
checks at a 1530.12 ms sequential median and 1014.81 ms concurrent median, a
33.7% reduction. This is machine-specific startup evidence, not an encoder
throughput claim. Representative production media and host resource benchmarks
remain required before changing runtime limits.
