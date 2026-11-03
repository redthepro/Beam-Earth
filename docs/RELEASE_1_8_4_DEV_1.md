# BeamNG.Earth 1.8.4-dev.1 — high-speed driving telemetry

This development prerelease adds a local recording mode for finding out why a
drive starts stuttering or falls behind on world loading. It is based on 1.8.3.
**1.8.3 remains the stable release. This is a diagnostic build, not a claimed FPS
or native-crash fix.**

Download **BeamNG.Earth-1.8.4-dev.1.exe** for Windows or the **x86_64.AppImage**
for Linux. The launcher installs the matching mod and starts the companion.

Open **Esc → BeamNG.Earth → Telemetry → Start capture**, then drive normally.
Use **Ctrl+Shift+T** to mark a stutter, and **Stop capture** when finished.
Recordings are local; coordinates are optional and off by default.

- Frame-time distributions, worst-frame timers, and simulation slowdown.
- Speed, road readiness ahead in seconds, loading holds, and streaming queues.
- Game/companion/worker CPU, memory, I/O and resource counts where available.
- Companion loop lag, job queues, network counters and cache occupancy.
- Bounded recording files, visible dropped-data counts, and partial-file recovery.
- A built-in report command: `--inspect-capture <file> --out drive-report.md`.

Windows captures are in `%LOCALAPPDATA%\beamng-earth\telemetry`; Linux captures
are in `~/.cache/beamng-earth/telemetry`. See the [recording guide](https://github.com/redthepro/Beam-Earth/blob/v1.8.4-dev.1/docs/TELEMETRY.md)
for the I-95 test procedure, privacy details, limits and report commands.

The telemetry is tested with automated regression/stub tests and package smoke
checks. Sustained gameplay and the usefulness of its crash tail still need a real
drive. GPU utilization/VRAM and definitive native-crash attribution are not
provided. Existing experimental shared road plans remain off by default; this
release does not add voice chat, Steam parties or new world-streaming algorithms.
