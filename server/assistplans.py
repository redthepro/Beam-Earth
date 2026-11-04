"""Opt-in shared grading plans with short foreground waits and bounded lookahead.

Uses the existing per-run PlanCache and its cross-process lock. No downloaded
plan becomes active until schema, content hash, generator and source checks pass.
Failure falls back to the existing local planner; pinned worlds never use this.
"""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
import threading
import time

import assistjobs
import xproc

log = logging.getLogger("earthd.assistplans")
FOREGROUND_S = 0.75
PREFETCH_S = 4.0
MAX_PENDING = 4
RETRY_S = 10.0
_prefetch = None
_prefetch_lock = threading.Lock()


def attach(cache, grader, cache_dir, widths, wet):
    import assistclient
    client = assistclient.get_client()
    if client is None:
        return
    try:
        settings = assistjobs.settings_dict(json.loads(os.environ.get("BNGE_ASSIST_SETTINGS", "{}")))
        if settings["road_widths"] != (widths is not None) or settings["sea"] != (wet is not None):
            return
        cache.remote = RemotePlans(cache, client, settings, cache_dir, getattr(grader, "md", None))
    except (ValueError, TypeError):
        log.warning("Road assistance unavailable: world settings are not initialized")


class RemotePlans:
    def __init__(self, cache, client, settings, cache_dir, md):
        self.cache, self.client, self.settings, self.cache_dir, self.md = cache, client, settings, cache_dir, md
        self.stats = {"hit": 0, "miss": 0, "seconds": 0.0, "prefetched": 0}

    def fetch(self, key, timeout_s=FOREGROUND_S):
        start = time.monotonic()
        try:
            request = assistjobs.plan_request(key, self.settings, self.cache_dir, self.md)
            head, blobs = self.client.call(request, timeout_s=timeout_s)
            result = assistjobs.decode_result("plan_cell", head, blobs, request)
            if request["context"] != assistjobs.source_context(self.cache_dir, self.settings, self.md, plan=True):
                raise ValueError("Road source metadata changed while receiving plans")
            self.stats["hit"] += 1
            if self.stats["hit"] == 1 or self.stats["hit"] % 100 == 0:
                log.info("server road assistance: %d verified plans received, %d local fallbacks",
                         self.stats["hit"], self.stats["miss"])
            return result
        except Exception as exc:
            # A remote error never removes safe ground or bypasses local grading.
            self.stats["miss"] += 1
            if self.stats["miss"] <= 3:
                log.debug("Road assistance fallback (%s)", type(exc).__name__)
            return None
        finally:
            self.stats["seconds"] += time.monotonic() - start

    def prefetch(self, keys):
        global _prefetch
        with _prefetch_lock:
            if _prefetch is None:
                _prefetch = Prefetcher()
            prefetch = _prefetch
        prefetch.submit(self, keys)


class Prefetcher:
    def __init__(self):
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="assist-roads")
        self.lock = threading.Lock()
        self.pending = set()
        self.recent = OrderedDict()
        self.closed = False

    def submit(self, remote, keys):
        now = time.monotonic()
        for key in keys:
            key = tuple(int(v) for v in key)
            if os.path.isfile(remote.cache._path(key)):
                continue
            tag = (remote.cache.dir, key)
            with self.lock:
                if self.closed or len(self.pending) >= MAX_PENDING:
                    return
                if tag in self.pending or now - self.recent.get(tag, -1e9) < RETRY_S:
                    continue
                self.pending.add(tag)
                self.recent[tag] = now
                self.recent.move_to_end(tag)
                while len(self.recent) > 128:
                    self.recent.popitem(last=False)
                try:
                    self.pool.submit(self._run, remote, key, tag)
                except RuntimeError:
                    self.pending.discard(tag)

    def _run(self, remote, key, tag):
        cache = remote.cache
        lock = cache._path(key) + ".lock"
        try:
            if cache.load(key) is not None:
                return
            result = remote.fetch(key, PREFETCH_S)
            if result is None or not xproc.take(lock, kind="prefetch"):
                return
            try:
                if cache.load(key) is None and cache.store(key, *result, persist=False):
                    remote.stats["prefetched"] += 1
            finally:
                xproc.release(lock)
        finally:
            with self.lock:
                self.pending.discard(tag)

    def close(self):
        with self.lock:
            self.closed = True
        self.pool.shutdown(wait=False, cancel_futures=True)


def close():
    global _prefetch
    with _prefetch_lock:
        if _prefetch is not None:
            _prefetch.close()
            _prefetch = None
