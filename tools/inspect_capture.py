#!/usr/bin/env python3
"""Summarize a local telemetry JSONL without copying coordinates, paths, or player identities.

  python tools/inspect_capture.py path/to/capture-....jsonl
  BeamNG.Earth-<version>.exe --inspect-capture path/to/capture-....jsonl

Safe to run while recording. A missing end record means still recording or an
interrupted capture; it does not prove a game crash. No data is uploaded.
"""
import argparse
import json
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from telemetry import HISTOGRAM_MS, REASONS, clean_client, clean_server  # noqa: E402

MAX_LINE = 64 * 1024


def _finite(value):
    return type(value) in (int, float) and abs(value) <= 1e18 and math.isfinite(value)


def _range(stats, key, value):
    if not _finite(value):
        return
    item = stats.setdefault(key, {"min": value, "max": value, "first": value, "last": value, "n": 0, "sum": 0.0})
    item.update(min=min(item["min"], value), max=max(item["max"], value), last=value,
                n=item["n"] + 1, sum=item["sum"] + value)


def _pct(histogram, share, maximum):
    rank = math.ceil(sum(histogram) * share)
    if rank <= 0:
        return None
    n = 0
    for upper, count in zip(HISTOGRAM_MS, histogram):
        n += count
        if n >= rank:
            return upper if upper is not None else maximum
    return None


def inspect(path):
    out = {"schema": 1, "complete": False, "end_reason": None, "samples": 0, "server_samples": 0,
           "resource_samples": 0, "invalid_lines": 0, "truncated_tail": False, "sequence_gaps": 0,
           "elapsed_s": 0.0, "frame_wall_s": 0.0, "frames": 0, "max_frame_ms": 0.0,
           "over_33": 0, "over_50": 0, "over_100": 0, "paused_frames": 0,
           "histogram": [0] * len(HISTOGRAM_MS), "high_speed_wall_s": 0.0,
           "context": {}, "server": {}, "resources": {}, "markers": [], "worst_windows": [],
           "capture": {}, "coordinates_recorded": False, "resource_discovery_failures": 0,
           "resource_partial_groups": 0, "game_resource_missing": 0}
    prev_seq = None
    with Path(path).open("rb") as f:
        while True:
            raw = f.readline(MAX_LINE + 1)
            if not raw:
                break
            if len(raw) > MAX_LINE:
                while raw and not raw.endswith(b"\n"):
                    raw = f.readline(MAX_LINE + 1)
                out["invalid_lines"] += 1
                continue
            if not raw.endswith(b"\n"):
                out["truncated_tail"] = True
            try:
                row = json.loads(raw, parse_constant=lambda _: None)
            except (ValueError, UnicodeError):
                out["invalid_lines"] += 1
                continue
            if not isinstance(row, dict):
                out["invalid_lines"] += 1
                continue
            elapsed = row.get("elapsed_s")
            if not _finite(elapsed) or elapsed < 0:
                out["invalid_lines"] += 1
                continue
            out["elapsed_s"] = max(out["elapsed_s"], elapsed)
            kind = row.get("kind")
            if kind == "start":
                out["coordinates_recorded"] = row.get("include_location") is True
            elif kind == "end":
                out["complete"] = isinstance(row.get("reason"), str) and row["reason"] in REASONS
                out["end_reason"] = row.get("reason") if out["complete"] else None
                capture = row.get("capture")
                capture = capture if isinstance(capture, dict) else {}
                out["capture"] = {k: v for k, v in capture.items()
                                  if k in ("samples", "dropped", "bytes", "write_ms", "processing_ms") and _finite(v)}
            elif kind == "marker":
                if len(out["markers"]) < 100:
                    out["markers"].append(elapsed)
            elif kind in ("sample", "server"):
                if "server" in row:
                    out["server_samples"] += 1
                    for key, value in clean_server(row["server"]).items():
                        _range(out["server"], key, value)
                if kind == "server":
                    continue
                client = clean_client(row.get("client"))
                if client is None:
                    out["invalid_lines"] += 1
                    continue
                out["samples"] += 1
                seq = client.get("seq")
                if type(seq) is int and prev_seq is not None and seq > prev_seq + 1:
                    out["sequence_gaps"] += seq - prev_seq - 1
                prev_seq = seq
                frame = client["frame"]
                out["frames"] += frame["frames"]
                out["frame_wall_s"] += frame["wall_ms"] / 1000
                out["max_frame_ms"] = max(out["max_frame_ms"], frame.get("max_ms", 0))
                for key in ("over_33", "over_50", "over_100", "paused_frames"):
                    out[key] += frame.get(key, 0)
                out["histogram"] = [a + b for a, b in zip(out["histogram"], frame["histogram"])]
                context = client["context"]
                if context.get("speed_mps", 0) * 2.236936 >= 200:
                    out["high_speed_wall_s"] += frame["wall_ms"] / 1000
                for key, value in context.items():
                    _range(out["context"], key, value)
                _range(out["context"], "sim_wall_ratio", frame.get("sim_wall_ratio"))
                worst = client.get("worst") or {}
                hitch_context = worst.get("context") or context
                window = {"elapsed_s": elapsed, "max_ms": frame.get("max_ms", 0),
                          "speed_mph": hitch_context["speed_mps"] * 2.236936 if "speed_mps" in hitch_context else None,
                          "road_ahead_s": hitch_context.get("road_ahead_s"),
                          "server_backlog": hitch_context.get("server_backlog"),
                          "mod_ms": worst.get("mod_ms", {}), "engine_ms": worst.get("engine_ms", {})}
                out["worst_windows"].append(window)
                out["worst_windows"].sort(key=lambda x: x["max_ms"], reverse=True)
                del out["worst_windows"][15:]
            elif kind == "resources":
                resources = row.get("resources")
                if not isinstance(resources, dict):
                    out["invalid_lines"] += 1
                    continue
                out["resource_samples"] += 1
                if resources.get("discovery_ok") is False:
                    out["resource_discovery_failures"] += 1
                game = resources.get("game")
                if not isinstance(game, dict) or not game.get("sampled"):
                    out["game_resource_missing"] += 1
                _range(out["resources"], "sample_ms", resources.get("sample_ms"))
                for group in ("companion", "workers", "game"):
                    values = resources.get(group)
                    if isinstance(values, dict):
                        if values.get("partial") is True:
                            out["resource_partial_groups"] += 1
                        for key in ("process_count", "sampled", "rss_bytes", "private_bytes", "handles", "fds",
                                    "read_bytes", "write_bytes", "page_faults", "major_faults", "cpu_pct", "read_bytes_s", "write_bytes_s"):
                            _range(out["resources"], group + "." + key, values.get(key))
    out["fps"] = out["frames"] / out["frame_wall_s"] if out["frame_wall_s"] > 0 else None
    out["p50_ms"] = _pct(out["histogram"], .5, out["max_frame_ms"])
    out["p95_ms"] = _pct(out["histogram"], .95, out["max_frame_ms"])
    out["p99_ms"] = _pct(out["histogram"], .99, out["max_frame_ms"])
    for group in ("context", "server", "resources"):
        for item in out[group].values():
            item["mean"] = item.pop("sum") / item["n"]
    return out


def _fmt(value, digits=1):
    return f"{value:.{digits}f}" if _finite(value) else "unavailable"


def render(report):
    r = report
    lines = ["# BeamNG.Earth driving capture", "",
             "Local measurements only. Coordinates, player identities, paths and raw logs are excluded from this report.", "",
             f"Capture: {'ended (' + r['end_reason'] + ')' if r['complete'] else 'still recording or interrupted; no end record'}. "
             f"Observed span: {_fmt(r['elapsed_s'])} s; {r['samples']} gameplay, {r['server_samples']} companion and "
             f"{r['resource_samples']} resource samples.",
             f"Coverage: {_fmt(r['frame_wall_s'])} s of measured gameplay frame intervals; "
             f"{r['sequence_gaps']} missing sequence numbers; {r['invalid_lines']} invalid records; "
             f"truncated final line: {r['truncated_tail']}.", "",
             f"Frames: {r['frames']}; average {_fmt(r['fps'])} FPS, worst {_fmt(r['max_frame_ms'])} ms. "
             f"Histogram upper-bound p50/p95/p99: {_fmt(r['p50_ms'])}/{_fmt(r['p95_ms'])}/{_fmt(r['p99_ms'])} ms. "
             "Percentiles combine frame counts, not averages of per-second percentiles.",
             f"Frames over 33.4/50/100 ms: {r['over_33']}/{r['over_50']}/{r['over_100']}; "
             f"paused frames: {r['paused_frames']}. Samples at 200+ MPH cover {_fmt(r['high_speed_wall_s'])} s.", ""]
    lines += ["## Streaming and memory", ""]
    for key, label, units in (("speed_mps", "Speed", "m/s"), ("road_ahead_s", "Straight-ahead ready road horizon", "s"),
                              ("block_edge_s", "Time to current terrain edge", "s"),
                              ("server_backlog", "Server tile backlog", "tiles"),
                              ("trees_pending", "Pending trees", "items"), ("lua_mb", "Lua heap", "MiB"),
                              ("sim_wall_ratio", "Simulation/wall ratio", ""),
                              ("telemetry_ms", "Previous client telemetry work", "ms")):
        value = r["context"].get(key)
        if value:
            lines.append(f"- {label}: min {_fmt(value['min'])}, max {_fmt(value['max'])}, "
                         f"first/last {_fmt(value['first'])}/{_fmt(value['last'])} {units}.")
    for group in ("game", "companion", "workers"):
        rss = r["resources"].get(group + ".rss_bytes")
        cpu = r["resources"].get(group + ".cpu_pct")
        if rss:
            lines.append(f"- {group} resident memory: first {_fmt(rss['first'] / 1048576)} MiB, "
                         f"last {_fmt(rss['last'] / 1048576)} MiB, peak {_fmt(rss['max'] / 1048576)} MiB.")
        if cpu:
            lines.append(f"- {group} CPU: sampled mean {_fmt(cpu['mean'])}%, max {_fmt(cpu['max'])}% (100% = one logical core).")
    lag = r["server"].get("loop_lag_ms")
    if lag:
        lines.append(f"- Companion loop scheduling delay: mean {_fmt(lag['mean'])}, max {_fmt(lag['max'])} ms.")
    age = r["server"].get("client_age_s")
    if age:
        lines.append(f"- Longest time since a received gameplay sample: {_fmt(age['max'])} s.")
    lines += ["", "The road horizon is a conservative straight-line probe, not a guarantee of every collision surface. "
              "Resource groups sum resident pages, including shared pages more than once. Memory changes do not by themselves prove a leak. "
              "Rendering, driver and OS stalls can remain outside the available timers.", "", "## Worst sampled windows", ""]
    for item in r["worst_windows"]:
        def top(values):
            return ", ".join(f"{key} {_fmt(value)} ms" for key, value in sorted(values.items(), key=lambda p: -p[1])[:3]) or "unavailable"
        lines.append(f"- t={_fmt(item['elapsed_s'])} s: {_fmt(item['max_ms'])} ms frame, "
                     f"{_fmt(item['speed_mph'])} MPH, road ahead {_fmt(item['road_ahead_s'])} s; "
                     f"mod timers: {top(item['mod_ms'])}; engine timers: {top(item['engine_ms'])}.")
    lines += ["", "Timers are correlated measurements, not a diagnosis of what caused a hitch.", "", "## Capture overhead and gaps", ""]
    cap = r["capture"]
    lines.append(f"Dropped sink records: {_fmt(cap.get('dropped'), 0)}. "
                 f"Total enqueue/sanitizing work: {_fmt(cap.get('processing_ms'))} ms; "
                 f"writer serialization/flush work: {_fmt(cap.get('write_ms'))} ms (background thread).")
    lines.append(f"Resource discovery failures: {r['resource_discovery_failures']}; "
                 f"partially sampled process groups: {r['resource_partial_groups']}; "
                 f"resource ticks without a sampled game process: {r['game_resource_missing']}.")
    resource_ms = r["resources"].get("sample_ms")
    if resource_ms:
        lines.append(f"Resource sampling: mean {_fmt(resource_ms['mean'])}, max {_fmt(resource_ms['max'])} ms (background thread).")
    if r["markers"]:
        lines.append("Player markers at seconds: " + ", ".join(_fmt(n) for n in r["markers"]) + ".")
    lines.append("A missing end record may be an active capture, companion termination or a crash; inspect the separate game crash report before assigning a cause.")
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("capture", type=Path)
    parser.add_argument("--json", action="store_true", help="sanitized machine-readable summary")
    parser.add_argument("--out", "--outfile", type=Path, help="write the sanitized summary to this file")
    args = parser.parse_args(argv)
    if args.out and args.out.resolve() == args.capture.resolve():
        parser.error("The summary output must be a different file from the capture.")
    try:
        report = inspect(args.capture)
    except OSError as exc:
        parser.error(f"Could not read capture ({type(exc).__name__}).")
    text = json.dumps(report, indent=2, allow_nan=False) + "\n" if args.json else render(report)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
