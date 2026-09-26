# Verification

## 0.2.9 — browser capture cadence

- Repeated ten-second 1080p30 captures of the same full-screen 1080p60 motion
  clip exposed startup-phase-dependent losses: the original gate measured
  30.01, 22.89 and 24.20 fps. With the source-paced browser gate it measured
  30.01, 30.00 and 29.99 fps. Original timestamps are preserved; no repeated
  frames are inserted to manufacture the output rate.
- Frame IDs burned into the source video were independently decoded from the
  recorded H.264: the revised runs contained 299, 298 and 299 distinct pictures
  over ten seconds, versus 300, 228 and 242 with the old gate. The middle run
  of each series enabled X11 shared memory. Shared memory alone did not fix
  the clock-grid frame losses; no isolated speedup is claimed for that change.
- These measurements used ordinary sandboxed Chrome and OpenH264 in Linux
  amd64 containers under macOS emulation. Occasional longer capture gaps remain
  under host load. This establishes the regression and its fix, not native HA
  GPU throughput, sustained YouTube playback quality, or physical TV smoothness.
- A deterministic regression covers jitter at eight clock phases for both
  30 and 60 fps, duplicate/regressing timestamps, and retained 60-to-30 fps
  tuner downsampling. The existing installed diagnostic now reports decoded
  video frame counts/rate/gaps alongside its timing results.

## 0.2.3 — idle event channel

- The updated Linux image passes all six native suites, 80 Python tests and
  the actual sandboxed-browser regressions.
- The longer installed live-TV trial exposed a 90-second read deadline on an
  otherwise idle AirPlay event channel. Regular feedback and media delivery
  were separate and remained active until the sender treated that idle read as
  failure. Events now wait through quiet periods without dropping the session.
- Native encrypted socket-pair tests cover repeated idle intervals, fragmented
  event arrival, pipelined events already in the decrypted buffer, actual peer
  closure and a stalled partial message. Message authentication and bounded
  parsing remain enabled.
- Receiver testing must extend beyond the previous 90-second cutoff, then
  explicitly verify Stop and tuner release. Transport success still does not
  establish physical picture, sound or lip sync.

## 0.2.2 — live TV startup and packet scheduling

- The Linux release image passes six native suites, all 80 Python tests, the
  actual sandboxed-browser regressions and browser-module syntax checks.
- The original engine fails a synthetic 1080i MPEG-2/AC-3 transport stream that
  starts between sequence headers, matching the failure on a local live tuner.
  The new engine recovers, deinterlaces and produces H.264 with non-silent PCM.
  Sustained damaged data fails within a bounded deadline; Stop joins all workers.
- Independently paced video/audio decoding uses a single HTTP tuner connection
  and bounded compressed-packet queues. A native regression measures steady-state
  audio age and keeps the source clock shared. The HTTP test verifies Stop closes
  the connection, and a delayed tuning failure gives a safe actionable error.
- A private five-second replay of the same real broadcast measured median audio
  age falling from 765 ms to -6 ms (packets emitted within their decoded audio
  frame), maximum 10 ms; video median remained 48 ms. This measures publication
  delay against source PTS, not TV speaker/display synchronization.
- A fresh live-tuner capture completed with 150 video frames at 1920x1080 and
  stereo PCM. Physical receiver picture, sound and lip sync remain separate
  acceptance checks. Private broadcast captures are not included in the repo.

## 0.1.6 — buffer negotiation and settings

- Three CTest suites pass, including an independent receiver interpretation of
  audio sync bytes and SETUP latency bounds at 500/750/1000/1500/2000 ms. Tests
  compare with video presentation time, including late joins, RTP wrap and long
  streams. The NTP/RTP mapping already included the lead; the fixed 250–2000 ms
  audio negotiation bounds did not require a receiver to use that lead. New
  bounds request the same configured delay and identify screen audio explicitly.
- All 52 Python tests pass. Isolated edits preserve unrelated values, reject
  conflicting writes and active-playback saves, skip unchanged/offline tuners,
  retain the browser and only reconnect affected services.
- Actual sandboxed Chrome verifies no scrollbar for filled video, exact style
  restoration for consent/navigation, and the existing native-control/profile
  regressions. This fixture requires no account or external video.
- The first-run wizard and subsequent Settings pages were exercised in the
  actual container. Changing pages retains drafts; saving only the buffer leaves
  an unsaved home-page edit untouched. Saving sound settings retains the preview.
  At 390 px the settings navigation fits, the expanded preview fills the viewport
  without overflow, and Play is below the TV selection area.
- Sender timing tests establish the requested schedule, not the TV's actual
  picture/speaker timing. Physical lip sync still needs user observation after
  restarting playback; no quality or synchronization measurement is inferred.

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

## 0.1.1 installation follow-up

The initial public commit passed GitHub's clean Linux build and all tests.
Home Assistant installed it from the public app repository with Protection mode
enabled. First start found a host port collision on UDP 57102, before any app
process or pairing started. Version 0.1.1 moves the dedicated mappings to
18200–18215; existing browser apps and their data are unchanged.

## 0.1.5 browser sign-in mode and wizard preview

- All three CTest suites and 45 Python tests pass in the Linux image.
- Two actual sandboxed Chrome instances pass native navigation, Unicode paste
  into a masked fixture, no form submission, clipboard clearing, restored VNC
  input, authenticated preview, and profile/extension identity after reopening.
  The test runs with umask 077 and occupied conventional browser ports. Its own
  page reports webdriver false; Chrome has no debugging or automation flags.
- The actual container wizard opens a browser before Setup is complete. Full
  screen preview, page zoom, the Paste dialog and return to Setup were checked.
  At 390 px the toolbar wraps and the page has no horizontal overflow. The
  public Big Buck Bunny YouTube clip plays and Fill video fills the canvas.
- Google account sign-in is deliberately completed by the user. Removing the
  debugging channel restores the previously accepted browser mode, but a local
  fixture cannot prove that Google will accept a particular account login.
