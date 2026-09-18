# Verification

## 0.1.0 — 2026-09-17

This is an experimental first release. Verified results will be updated as the
installed app is exercised; uncompleted acceptance checks remain explicit.

Completed locally in Linux amd64 containers:

- C++ engine and complete application image build.
- Normal HAP pairing/verification, pinned-identity rejection, private storage,
  encrypted control/data framing and tamper checks (57 original assertions).
- H.264 media and authenticated stereo PCM packet checks, monotonic shared A/V
  presentation times, and correct NTP arithmetic at two hours.
- Two subscribers share identical encoded packets; one can leave while the
  other continues. This verifies source sharing, not two physical TVs.
- Generated H.264/AAC MPEG-TS fixture traverses the same direct-libav input,
  decode, resample and encode pipeline used by the tuner mode, including a
  deliberately introduced 300 ms audio timestamp gap.
- 31 Python checks for persistence, input validation, setup/usage separation,
  ingress/request guards, lifecycle cancellation, failed source replacement,
  independent Stop, stable discovery IDs and retained-command rejection.
- Actual five-step wizard, saved configuration and daily browser controls.
- Sandboxed container browser, authenticated interactive preview, 1080p X11
  video plus PulseAudio through the shared C++ pipeline, and responsive daily
  controls without horizontal overflow at a 390-pixel viewport.

Remaining physical acceptance: this release's combined video/audio on an Apple
TV, audio/video synchronization, a sustained HDHomeRun session, multiple TVs,
60 fps/hardware encoding performance, and additional receiver/tvOS versions.
Earlier independent experiments confirmed 1080p30 generated video on one Apple
TV and container browser capture. Those results are not a substitute for the
combined app's acceptance tests.

No comparative quality winner over the previous stack is claimed.
