BeamNG Earth 1.8.3 contains targeted streaming and launcher reliability fixes. Canceled tile workers keep ownership of their files until they finish, preventing a late worker from overwriting a shape assigned to another tile. Pooled-slot allocation also checks holds restored during reconnect.

**Known limitation:** the reported long-session native BeamNG crash is still under investigation. These fixes have reproducible regression coverage, but no controlled driving comparison establishes that the crash or stuttering is resolved. A slot-ownership gap between disconnect and the next hello remains open. The experimental queue-timeout change was excluded because it could increase exposure to that gap.

### Downloads

- **Play on Windows:** `BeamNG.Earth-1.8.3.exe`, compiled and packaged locally using the project's source-built PyInstaller bootloader.
- **Play on Linux:** `BeamNG.Earth-1.8.3-x86_64.AppImage`, built by the Linux release job.
- **Host:** `BeamNG.Earth-Host-1.8.3-Windows.zip` or `BeamNG.Earth-Host-1.8.3-Linux-x86_64.zip`. These include unmodified official BeamMP 3.9.3 server executables, Earth resources, configuration, licenses and corresponding source. Supply your own BeamMP AuthKey; this project did not compile the BeamMP executable.
- **Existing BeamMP installation:** `BeamNG.Earth-BeamMP-Server-1.8.3.zip` contains the Earth plugin and mod.
- **Source/project package:** `BeamNG.Earth-1.8.3.zip`. `SHA256SUMS.txt` covers the downloadable player binaries and ZIPs.

BeamNG.drive is required and is not included. Steam should be running for normal player launch.

### Included fixes

- Retain canceled tile producers until actual completion; defer anchor pruning while they still own files.
- Avoid reassigning free slots whose game holds were restored by a reconnect hello. The earlier disconnect interval still needs persistent client ownership.
- Limit installed BeamMP readiness waiting to 30 seconds, then stop the owned attempt and continue single-player. Use `--beammp-wait 120` for slower updates or `--no-beammp` to bypass it.
- Clean up interrupted BeamMP starts, failed game launches and owned companion workers; improve Windows game-exit detection and process-handle cleanup.
- Detect companion readiness from its fresh startup log without creating a fake game connection. Cancel and await the stream's event waiter on disconnect.
- Normalize effective bridge-profile defaults in shared-plan provenance and reject pinned bundles in legacy compute helpers that cannot negotiate their identity.
- Fix the Linux Steam-process test fixture and add real-worker cancellation and restored-slot allocation regressions.

Version 1.8.2 was a local candidate; its release workflow was canceled and these changes are included here.

### Validation boundaries

Publication is gated by Windows/Linux regression jobs and packaged command smoke checks. Local validation includes Python/Lua/JavaScript/Rust checks, real worker cancellation, isolated Windows process-tree cleanup, and packaged companion startup/stop/restart. These tests are not a long-session gameplay or two-client BeamMP acceptance test.

Shared road-plan bundles remain experimental and disabled by default; they do not pin the entire world. Steam parties, embedded voice, radio and downloaded-model placement are not newly implemented in this release.

See [stability evidence and remaining acceptance](https://github.com/redthepro/Beam-Earth/blob/v1.8.3/docs/STABILITY_VALIDATION.md), [startup troubleshooting](https://github.com/redthepro/Beam-Earth/blob/v1.8.3/docs/RELAUNCH_TROUBLESHOOTING.md), and [feature status](https://github.com/redthepro/Beam-Earth/blob/v1.8.3/docs/PLATFORM_STATUS.md).
