"""Read-only process counters for opt-in captures; no commands, paths or PIDs are exported.

Call ResourceSampler.sample() from the capture writer thread, not the event loop.
Discovery is at most once per five seconds. CPU percentages use actual elapsed
monotonic time; 100% is one logical core. RSS is a sum (shared pages can be counted
more than once), not unique physical memory. Windows private_bytes is committed
private memory; it is unavailable on Linux because smaps scans are too expensive.
No GPU utilization is inferred from these counters.
"""
import os
from pathlib import Path
import sys
import time

DISCOVERY_S = 5.0
MAX_PROCESSES = 8192
MAX_TRACKED = 256
MAX_FDS = 65536
GAME_NAMES = frozenset(("beamng.drive.x64", "beamng.drive.x64.exe"))
COUNTERS = ("rss_bytes", "private_bytes", "handles", "fds", "read_bytes", "write_bytes",
            "page_faults", "major_faults")


def _proc_stat(text):
    """Parse /proc/PID/stat despite spaces or parentheses in comm (Linux field numbers)."""
    end = text.rfind(")")
    start = text.find("(")
    if start < 0 or end <= start:
        raise ValueError("invalid proc stat")
    tail = text[end + 2:].split()  # starts at field 3, state
    return {"name": text[start + 1:end], "state": tail[0], "ppid": int(tail[1]),
            "cpu_ticks": int(tail[11]) + int(tail[12]), "birth": int(tail[19]),
            "rss_pages": int(tail[21]), "page_faults": int(tail[7]) + int(tail[9]),
            "major_faults": int(tail[9])}


class _Linux:
    def __init__(self, root="/proc"):
        self.root = Path(root)
        self.ticks = os.sysconf("SC_CLK_TCK")
        self.page = os.sysconf("SC_PAGE_SIZE")
        self.limited = False

    @staticmethod
    def _text(path, limit=16384):
        with path.open(encoding="utf-8", errors="replace") as f:
            return f.read(limit)

    def discover(self):
        out = {}
        self.limited = False
        inspected = 0
        with os.scandir(self.root) as entries:
            for ent in entries:
                if not ent.name.isdigit():
                    continue
                if inspected >= MAX_PROCESSES:
                    self.limited = True
                    break
                inspected += 1
                try:
                    d = self.root / ent.name
                    s = _proc_stat(self._text(d / "stat"))
                    if s["state"] in ("Z", "X", "x"):
                        continue
                    # comm is truncated to 15 bytes. Resolve only candidate game names;
                    # no command line is read or stored.
                    name = s["name"]
                    if name.casefold().startswith("beamng.drive"):
                        name = os.path.basename(os.readlink(d / "exe"))
                    out[int(ent.name)] = (s["ppid"], name)
                except (OSError, ValueError, IndexError):
                    pass
        return out

    def read(self, pid):
        try:
            d = self.root / str(pid)
            s = _proc_stat(self._text(d / "stat"))
            if s["state"] in ("Z", "X", "x"):
                return None
            name = s["name"]
            if name.casefold().startswith("beamng.drive"):
                name = os.path.basename(os.readlink(d / "exe"))
            out = {"birth": s["birth"], "name": name, "cpu_s": s["cpu_ticks"] / self.ticks,
                   "rss_bytes": max(0, s["rss_pages"]) * self.page,
                   "page_faults": s["page_faults"], "major_faults": s["major_faults"]}
            try:
                with os.scandir(d / "fd") as fds:
                    count = 0
                    for _ in fds:
                        count += 1
                        if count > MAX_FDS:
                            break
                    if count <= MAX_FDS:
                        out["fds"] = count
            except OSError:
                pass
            try:
                io = dict(line.split(":", 1) for line in self._text(d / "io").splitlines() if ":" in line)
                out.update({k: int(io[k]) for k in ("read_bytes", "write_bytes") if k in io})
            except (OSError, ValueError):
                pass
            # Recheck identity: /proc paths can be recycled between individual reads.
            if _proc_stat(self._text(d / "stat"))["birth"] != out["birth"]:
                return None
            return out
        except (OSError, ValueError, IndexError):
            return None


class _Windows:
    def __init__(self):
        import ctypes as c
        from ctypes import wintypes as w
        self.c, self.w = c, w

        class PE(c.Structure):
            _fields_ = [("size", w.DWORD), ("usage", w.DWORD), ("pid", w.DWORD),
                        ("heap", c.c_size_t), ("module", w.DWORD), ("threads", w.DWORD),
                        ("ppid", w.DWORD), ("priority", w.LONG), ("flags", w.DWORD),
                        ("name", w.WCHAR * 260)]

        class Memory(c.Structure):
            _fields_ = [("cb", w.DWORD), ("PageFaultCount", w.DWORD)] + [
                (n, c.c_size_t) for n in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                "PagefileUsage", "PeakPagefileUsage", "PrivateUsage")]

        class IO(c.Structure):
            _fields_ = [(n, c.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount",
                "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        self.PE, self.Memory, self.IO = PE, Memory, IO
        self.k = k = c.WinDLL("kernel32", use_last_error=True)
        self.ps = ps = c.WinDLL("psapi", use_last_error=True)
        signatures = (
            (k.OpenProcess, [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            (k.CloseHandle, [w.HANDLE], w.BOOL),
            (k.CreateToolhelp32Snapshot, [w.DWORD, w.DWORD], w.HANDLE),
            (k.Process32FirstW, [w.HANDLE, c.POINTER(PE)], w.BOOL),
            (k.Process32NextW, [w.HANDLE, c.POINTER(PE)], w.BOOL),
            (k.GetProcessTimes, [w.HANDLE] + [c.POINTER(w.FILETIME)] * 4, w.BOOL),
            (k.QueryFullProcessImageNameW, [w.HANDLE, w.DWORD, w.LPWSTR, c.POINTER(w.DWORD)], w.BOOL),
            (ps.GetProcessMemoryInfo, [w.HANDLE, c.POINTER(Memory), w.DWORD], w.BOOL),
            (k.GetProcessIoCounters, [w.HANDLE, c.POINTER(IO)], w.BOOL),
            (k.GetProcessHandleCount, [w.HANDLE, c.POINTER(w.DWORD)], w.BOOL),
            (k.GetExitCodeProcess, [w.HANDLE, c.POINTER(w.DWORD)], w.BOOL),
        )
        for fn, args, result in signatures:
            fn.argtypes, fn.restype = args, result
        self.limited = False

    def discover(self):
        c, k = self.c, self.k
        snap = k.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
        if not snap or snap == self.w.HANDLE(-1).value:
            raise OSError("process snapshot unavailable")
        out = {}
        self.limited = False
        try:
            e = self.PE()
            e.size = c.sizeof(e)
            ok = k.Process32FirstW(snap, c.byref(e))
            while ok:
                if len(out) >= MAX_PROCESSES:
                    self.limited = True
                    break
                out[int(e.pid)] = (int(e.ppid), e.name)
                ok = k.Process32NextW(snap, c.byref(e))
        finally:
            k.CloseHandle(snap)
        return out

    def read(self, pid):
        c, w, k = self.c, self.w, self.k
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION only
        if not h:
            return None
        try:
            creation, ended, kernel, user = (w.FILETIME() for _ in range(4))
            if not k.GetProcessTimes(h, c.byref(creation), c.byref(ended), c.byref(kernel), c.byref(user)):
                return None
            ticks = lambda f: (int(f.dwHighDateTime) << 32) | int(f.dwLowDateTime)
            exit_code = w.DWORD()
            if not k.GetExitCodeProcess(h, c.byref(exit_code)) or exit_code.value != 259:  # STILL_ACTIVE
                return None
            image = c.create_unicode_buffer(32768)
            size = w.DWORD(len(image))
            if not k.QueryFullProcessImageNameW(h, 0, image, c.byref(size)):
                return None
            out = {"birth": ticks(creation), "name": os.path.basename(image.value),
                   "cpu_s": (ticks(kernel) + ticks(user)) / 1e7}
            mem = self.Memory()
            mem.cb = c.sizeof(mem)
            if self.ps.GetProcessMemoryInfo(h, c.byref(mem), mem.cb):
                out.update(rss_bytes=int(mem.WorkingSetSize), private_bytes=int(mem.PrivateUsage),
                           page_faults=int(mem.PageFaultCount))
            io = self.IO()
            if k.GetProcessIoCounters(h, c.byref(io)):
                out.update(read_bytes=int(io.ReadTransferCount), write_bytes=int(io.WriteTransferCount))
            handles = w.DWORD()
            if k.GetProcessHandleCount(h, c.byref(handles)):
                out["handles"] = int(handles.value)
            return out
        finally:
            k.CloseHandle(h)


class ResourceSampler:
    """Bounded, best-effort resources, with process identity checked on every sample.

    The game's top-level x64 process is selected by its executable name, excluding
    game-named descendants (CEF helpers). Multiple independent games are summed.
    Discovery can lag by five seconds; newly discovered processes need a second
    sample for CPU/rate deltas. Permissions or unsupported platforms yield nulls.
    Linux detects native executables; a Proton process whose /proc image is Wine
    rather than BeamNG may not be discoverable without reading command lines.
    """
    def __init__(self, *, backend=None, clock=time.monotonic, pid=None):
        self.clock = clock
        self.pid = os.getpid() if pid is None else pid
        try:
            self.backend = backend or (_Windows() if os.name == "nt" else _Linux() if sys.platform == "linux" else None)
        except (OSError, AttributeError):
            self.backend = None
        self._groups = {"companion": {}, "workers": {}, "game": {}}
        self._last = {}
        self._refresh_at = -float("inf")
        self._discovery_ok = False
        self._limited = False
        self._closed = False

    def _discover(self, now):
        self._refresh_at = now
        try:
            procs = self.backend.discover()
            self._discovery_ok = True
        except (OSError, ValueError):
            self._discovery_ok = False
            return
        self._limited = getattr(self.backend, "limited", False)
        groups = {"companion": [self.pid], "workers": [], "game": []}
        descendants = {self.pid}
        while len(descendants) < MAX_TRACKED:
            new = [p for p, (pp, _) in procs.items() if pp in descendants and p not in descendants]
            if not new:
                break
            if len(descendants) + len(new) >= MAX_TRACKED:
                new = new[:MAX_TRACKED - len(descendants)]
                self._limited = True
            descendants.update(new)
            groups["workers"].extend(new)
        candidates = {p for p, (_, name) in procs.items() if name.casefold() in GAME_NAMES}
        for p in sorted(candidates):
            ancestor, seen = procs[p][0], {p}
            while ancestor in procs and ancestor not in seen and ancestor not in candidates:
                seen.add(ancestor)
                ancestor = procs[ancestor][0]
            if ancestor not in candidates:
                groups["game"].append(p)
        if len(groups["game"]) > 8:
            self._limited = True
            groups["game"] = groups["game"][:8]
        accepted = {}
        for group, pids in groups.items():
            selected = {}
            for p in pids:
                row = self.backend.read(p)
                expected = procs.get(p)
                if not row:
                    selected[p] = None  # process exists but cannot be sampled; not reported as zero resources
                    continue
                if expected is not None and row["name"].casefold() != expected[1].casefold():
                    continue
                if group == "workers":
                    parent_birth = accepted.get(expected[0]) if expected else None
                    if parent_birth is None or row["birth"] < parent_birth:
                        continue  # parent PID was recycled, or cannot establish ancestry
                selected[p] = row["birth"]
                accepted[p] = row["birth"]
            self._groups[group] = selected

    def sample(self):
        begin = self.clock()
        if self._closed or self.backend is None:
            return {"available": False, "sample_ms": 0.0}
        if begin - self._refresh_at >= DISCOVERY_S:
            self._discover(begin)
        out = {"available": True, "discovery_ok": self._discovery_ok,
               "discovery_limited": self._limited, "io_is_storage": os.name != "nt"}
        next_last = {}
        for group, pids in self._groups.items():
            rows, rates = [], []
            for pid, birth in pids.items():
                row = self.backend.read(pid) if birth is not None else None
                if row is None or row["birth"] != birth:
                    continue  # exited or recycled: never attribute an unrelated process
                rows.append(row)
                identity = (pid, birth)
                prev = self._last.get(identity)
                delta = begin - prev[0] if prev else 0
                rate = {}
                if prev and delta > 0:
                    for key, dest, scale in (("cpu_s", "cpu_pct", 100.0),
                                             ("read_bytes", "read_bytes_s", 1.0),
                                             ("write_bytes", "write_bytes_s", 1.0)):
                        a, b = row.get(key), prev[1].get(key)
                        if a is not None and b is not None and a >= b:
                            rate[dest] = (a - b) * scale / delta
                rates.append(rate)
                next_last[identity] = (begin, row)
            summary = {"process_count": len(pids), "sampled": len(rows), "partial": len(rows) != len(pids)}
            for key in COUNTERS:
                values = [r.get(key) for r in rows]
                summary[key] = sum(values) if values and all(v is not None for v in values) else None
            for key in ("cpu_pct", "read_bytes_s", "write_bytes_s"):
                values = [r.get(key) for r in rates]
                summary[key] = round(sum(values), 3) if values and all(v is not None for v in values) else None
            out[group] = summary
        self._last = next_last
        out["sample_ms"] = round(max(0.0, self.clock() - begin) * 1000, 3)
        return out

    def close(self):
        self._closed = True
        self._last.clear()
        for group in self._groups:
            self._groups[group].clear()
