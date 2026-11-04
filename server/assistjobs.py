"""Validated, finite world-generation jobs for the opt-in assistance service.

Only geometry and road grading plans cross this boundary. Imagery, game objects,
vehicle simulation and arbitrary file/URL jobs are deliberately outside it.
Known dataset versions are checked, but are not a complete immutable DEM snapshot.
"""
import hashlib
import io
import json
import math
import os
from collections import OrderedDict
from pathlib import Path

import numpy as np

import peerwork
import worldbundle

OPS = frozenset(("far_geometry", "far_global_geometry", "plan_cell"))
MAX_VERTICES = 20000
MAX_TRIANGLES = 40000
_context_sources = {}
_settings = None
_cache_dir = None
_anchors = OrderedDict()
_next_anchor = 1
_tainted = False


def _aid(lat, lon):
    global _next_anchor
    key = (float(lat), float(lon))
    if key not in _anchors:
        _anchors[key] = _next_anchor
        _next_anchor += 1
        while len(_anchors) > 2:
            _anchors.popitem(last=False)
    return _anchors[key]


def identity():
    return {"schema": 1, "generator": worldbundle.generator_identity()}


def settings_dict(settings):
    if (not isinstance(settings, dict) or set(settings) != set(peerwork.SETTINGS)
            or any(type(v) is not bool for v in settings.values())):
        raise ValueError("Assistance requires explicit boolean world settings")
    if settings["grading"] and not settings["roads"]:
        raise ValueError("Grading requires roads")
    return dict(settings)


def register_context(cache_dir, md, airport_db=None):
    """Use the running companion's source releases, not a later disk update."""
    _context_sources[os.path.abspath(cache_dir)] = (md, airport_db)


def source_context(cache_dir, settings, md=None, plan=False):
    import gradesep
    import mapdata
    import maprasters
    import overture
    import railtags
    root = Path(cache_dir)
    registered = _context_sources.get(os.path.abspath(cache_dir))
    if md is None and registered is not None:
        md = registered[0]
    if md is not None:
        sources = md.bundle_versions()
    else:
        sources = {"pipeline": mapdata.VERSION, "overture": overture.RELEASE,
                   "worldcover": maprasters.WORLDCOVER_RELEASE, "canopy": maprasters.CHM_RELEASE,
                   "rails": worldbundle.cached_source_build(root / "pagesdata/rail/manifest.json") if railtags.ON else None,
                   "lanes": worldbundle.cached_source_build(root / "pagesdata/lanes/manifest.json") if railtags.LANES_ON else None,
                   "gradesep": (gradesep.VERSION, gradesep.MODE) if gradesep.TUNNELS else None}
    if settings["roads"] and ((railtags.ON and not sources.get("rails"))
                              or (railtags.LANES_ON and not sources.get("lanes"))):
        raise ValueError("Assistance requires known enabled road source manifests")
    airport = None
    if settings["airports"] and not plan:
        if registered is not None:
            airport = getattr(registered[1], "snapshot", None)
        else:
            pointer = root / "airports/ourairports/current.json"
            try:
                with pointer.open("rb") as stream:
                    raw = stream.read(65537)
                if len(raw) > 65536:
                    raise ValueError("Airport metadata too large")
                airport = json.loads(raw).get("snapshot")
            except FileNotFoundError:
                pass
        if not isinstance(airport, str) or not airport:
            raise ValueError("Assistance needs the active airport snapshot")
    return json.loads(worldbundle.canonical({"sources": sources, "airport": airport}))


def _context(value):
    if not isinstance(value, dict) or set(value) != {"sources", "airport"}:
        raise ValueError("Bad source context")
    sources = value["sources"]
    if (not isinstance(sources, dict) or set(sources) !=
            {"pipeline", "overture", "worldcover", "canopy", "rails", "lanes", "gradesep"}):
        raise ValueError("Bad source versions")
    raw = worldbundle.canonical(value)
    if len(raw) > 4096:
        raise ValueError("Source context too large")
    return json.loads(raw)


def _layout(value):
    import farfield
    if not isinstance(value, dict) or set(value) != {"cells", "block", "mesh", "tex"}:
        raise ValueError("Bad layout")
    if type(value["mesh"]) is not bool:
        raise ValueError("Bad mesh setting")
    cells = peerwork._int(value["cells"], 32, 64, "cells")
    block = peerwork._int(value["block"], 512, 1024, "block")
    tex = value["tex"]
    if tex is not None and (not isinstance(tex, list) or not 1 <= len(tex) <= 16
                           or any(type(v) is not int or v not in (16, 32, 64, 128, 256, 512, 1024, 2048, 4096) for v in tex)):
        raise ValueError("Bad layout textures")
    return farfield.Layout(cells, block, value["mesh"], None if tex is None else tuple(tex))


def validate_request(header, configured_settings):
    if not isinstance(header, dict) or header.get("op") not in OPS:
        raise ValueError("Unsupported assistance operation")
    settings = settings_dict(header.get("settings"))
    if settings != settings_dict(configured_settings):
        raise ValueError("World settings differ")
    context = _context(header.get("context"))
    op = header["op"]
    required = {"op", "settings", "context"}
    if op == "plan_cell":
        required |= {"cell"}
        key = worldbundle._key(header.get("cell"))
        if key[0] != 14 or not settings["roads"] or not settings["grading"] or context["airport"] is not None:
            raise ValueError("Unsupported road-plan request")
    else:
        required |= {"layout", "zoom", "ref_zoom"}
        _layout(header.get("layout"))
        peerwork._int(header.get("zoom"), 0, 16, "zoom")
        if header.get("ref_zoom") is not None:
            peerwork._int(header["ref_zoom"], 0, 16, "reference zoom")
        if op == "far_global_geometry":
            required |= {"cell"}
            z, x, y = worldbundle._key(header.get("cell"))
            if not 0 <= z <= 15:
                raise ValueError("Unsupported global terrain level")
            import farfield
            cells = header["layout"]["cells"]
            if (header["zoom"] != farfield.global_dem_zoom(z, cells)
                    or header["ref_zoom"] != farfield.global_dem_zoom(z + 1, cells)):
                raise ValueError("Global terrain sampling must match its level")
        else:
            required |= {"lat", "lon", "tile", "q", "with_sink"}
            peerwork.check_job({k: v for k, v in header.items() if k != "context"})
            # Keep the physics-near tiles local: the helper does not pin every raw
            # elevation input, so distant visuals must not change road collision.
            if header["tile"][0] < 1 or type(header["with_sink"]) is not bool:
                raise ValueError("Near collision geometry stays local")
    if set(header) != required:
        raise ValueError("Unexpected request fields")
    return json.loads(worldbundle.canonical(header))


def make_request(name, args, settings, cache_dir):
    if name not in OPS or name == "plan_cell":
        raise ValueError("Unsupported geometry job")
    if name == "far_global_geometry":
        z, x, y, zoom, ref_zoom, lay = args
        header = {"op": name, "cell": [int(z), int(x), int(y)], "zoom": int(zoom), "ref_zoom": ref_zoom,
                  "layout": {"cells": lay.cells, "block": lay.block, "mesh": lay.mesh,
                             "tex": None if lay.tex is None else list(lay.tex)}}
    else:
        header = peerwork.job_header(name, args, settings)
    header["settings"] = settings_dict(settings)
    header["context"] = source_context(cache_dir, settings)
    return validate_request(header, settings)


def plan_request(key, settings, cache_dir, md=None):
    return validate_request({"op": "plan_cell", "cell": [int(v) for v in key],
                             "settings": settings_dict(settings),
                             "context": source_context(cache_dir, settings, md, plan=True)}, settings)


def request_id(request):
    return hashlib.sha256(worldbundle.canonical(request)).hexdigest()


def _array(blob, columns=None, integer=False):
    if not isinstance(blob, bytes) or len(blob) > 16 << 20:
        raise ValueError("Geometry array too large")
    stream = io.BytesIO(blob)
    version = np.lib.format.read_magic(stream)
    if version == (1, 0):
        shape, order, dtype = np.lib.format.read_array_header_1_0(stream, max_header_size=4096)
    elif version == (2, 0):
        shape, order, dtype = np.lib.format.read_array_header_2_0(stream, max_header_size=4096)
    else:
        raise ValueError("Unsupported geometry array")
    if (len(shape) != 2 or any(type(n) is not int or not 1 <= n <= MAX_TRIANGLES for n in shape)
            or dtype.hasobject or dtype.fields or dtype.subdtype
            or dtype.kind not in ("iu" if integer else "f") or dtype.itemsize not in (4, 8)
            or math.prod(shape) * dtype.itemsize != len(blob) - stream.tell()
            or (columns is not None and shape[1] != columns)):
        raise ValueError("Invalid geometry shape or dtype")
    result = np.lib.format.read_array(io.BytesIO(blob), allow_pickle=False, max_header_size=4096)
    if not np.isfinite(result).all():
        raise ValueError("Nonfinite geometry")
    return result


def validate_built(meta, blobs, request):
    import farfield
    if not isinstance(meta, dict) or set(meta) != {"pos", "err", "sink", "skirt", "size", "arrays"}:
        raise ValueError("Invalid geometry metadata")
    names = meta["arrays"]
    needed = {"positions", "normals", "uvs", "planar", "tris"}
    if (not isinstance(names, list) or len(names) != len(blobs) or len(set(names)) != len(names)
            or not needed <= set(names) <= needed | {"grid"}):
        raise ValueError("Invalid geometry arrays")
    arrays = {n: _array(b, 3 if n in ("positions", "normals", "tris") else 2 if n in ("uvs", "planar") else None,
                        n == "tris") for n, b in zip(names, blobs)}
    count = len(arrays["positions"])
    if count > MAX_VERTICES or any(len(arrays[n]) != count for n in ("normals", "uvs", "planar")):
        raise ValueError("Vertex counts differ")
    tris = arrays["tris"]
    if len(tris) > MAX_TRIANGLES or tris.min() < 0 or tris.max() >= count:
        raise ValueError("Invalid triangle indices")
    grid = arrays.get("grid")
    if grid is not None and grid.shape != (request["layout"]["cells"] + 1,) * 2:
        raise ValueError("Invalid terrain grid")
    if not isinstance(meta["pos"], list) or len(meta["pos"]) != 3:
        raise ValueError("Invalid tile origin")
    for v in meta["pos"]:
        peerwork._num(v, -1e9, 1e9, "origin")
    for key in ("err", "skirt"):
        peerwork._num(meta[key], 0, 1e8, key)
    peerwork._num(meta["size"], 0.01, 5e7, "size")
    peerwork._num(meta["sink"], -1e7, 1e7, "sink", allow_none=True)
    if np.max(np.abs(arrays["positions"])) > 1e8 or np.max(np.abs(arrays["normals"])) > 1.01:
        raise ValueError("Geometry outside bounds")
    return farfield.Built(**{k: meta[k] for k in ("pos", "err", "sink", "skirt", "size")}, **arrays,
                          **({"grid": None} if grid is None else {}))


def decode_result(name, head, blobs, request):
    if (not isinstance(head, dict) or head.get("ok") is not True or head.get("request") != request_id(request)
            or head.get("identity") != identity() or head.get("context") != request["context"]
            or head.get("complete") is not True):
        raise ValueError("Unverified assistance result")
    if name == "plan_cell":
        if len(blobs) != 1:
            raise ValueError("Invalid plan reply")
        artifact = head.get("artifact")
        if (not isinstance(artifact, dict) or set(artifact) != {"size", "sha256"}
                or type(artifact["size"]) is not int or not 0 < artifact["size"] <= worldbundle.MAX_ARTIFACT_BYTES
                or not isinstance(artifact["sha256"], str) or worldbundle.HEX.fullmatch(artifact["sha256"]) is None):
            raise ValueError("Missing or invalid plan integrity metadata")
        return worldbundle.validate_artifact(blobs[0], artifact)
    return validate_built(head.get("built"), blobs, request)


def init_worker(cache_dir, settings):
    global _cache_dir, _settings, _tainted
    import blockworker
    import plancache
    import revisit
    os.environ["BNGE_ASSIST_WORKER"] = "1"
    os.environ.pop("BNGE_ASSIST_CONFIG", None)
    os.environ.pop("BNGE_WORLD_BUNDLE", None)
    _cache_dir, _settings = cache_dir, settings_dict(settings)
    _tainted = False
    # Revisit entries do not carry complete remote-input provenance. This worker
    # uses fresh/current-run plans and the service's bounded verified reply cache.
    revisit.ON = False
    if not os.environ.get(plancache.RUN_ENV):
        plancache.start_run(cache_dir)
    s = _settings
    blockworker.init(cache_dir, False, s["roads"], s["grading"], s["road_widths"], False, None,
                     airports=s["airports"], sea=s["sea"], roads_in_ground=s["roads_in_ground"])
    state = blockworker._s
    _prime_sources(state)
    register_context(cache_dir, state.md, getattr(state.airports, "db", None))


def _prime_sources(state):
    # Bootstrap identities through the existing provider-aware fetcher. Otherwise
    # a fresh helper would reject every warmed client before it could learn them.
    if state.md is not None:
        for name in ("rail_tags", "lane_tags"):
            source = getattr(state.md, name, None)
            if source is not None:
                source.version()


def _reset_state():
    """Discard in-memory dependencies touched by a failed/incomplete job."""
    import blockworker
    state = blockworker._s
    if state.md is not None:
        state.md.close()
    state.dem.fetcher.close()
    s = _settings
    state = blockworker._s = blockworker._State(_cache_dir, False, s["roads"], s["grading"],
            s["road_widths"], False, airports=s["airports"], sea=s["sea"])
    _prime_sources(state)
    register_context(_cache_dir, state.md, getattr(state.airports, "db", None))


def run(request):
    global _tainted
    if _tainted:
        _reset_state()
        _tainted = False
    try:
        reply = _run(request)
        if not reply[0].get("ok"):
            _tainted = True
        return reply
    except BaseException:
        _tainted = True
        raise


def _run(request):
    import blockworker
    import mapcells
    import revisit
    request = validate_request(request, _settings)
    op = request["op"]
    def context():
        return source_context(_cache_dir, _settings, plan=op == "plan_cell")
    if context() != request["context"]:
        return {"ok": False, "error": "source_mismatch"}, []
    faults = revisit.faults()
    if op == "plan_cell":
        key = mapcells.CellKey(*request["cell"])
        lat, lon = key.center()
        grader, _ = blockworker._s.anchor(_aid(lat, lon), lat, lon)
        planner = grader.store.planner_of_cell(key)
        planner._cell_plans(key)
        path = Path(grader.shared._path(key))
        try:
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            worldbundle._read_completion(str(path) + ".complete", key, grader.shared.signature,
                                         digest, context()["sources"], identity()["generator"])
            worldbundle.validate_artifact(data)
        except (OSError, ValueError, KeyError):
            # A transient source failure must remain retryable, not become a
            # permanently cached incomplete plan in this long-lived helper.
            with planner.lock:
                planner.cells.pop(key, None)
            return {"ok": False, "error": "incomplete_inputs"}, []
        head = {"artifact": {"size": len(data), "sha256": digest}}
        blobs = [data]
    else:
        layout = _layout(request["layout"])
        if op == "far_global_geometry":
            built = blockworker.far_global_geometry(*request["cell"], request["zoom"], request["ref_zoom"], layout)
        else:
            _, _, kw = peerwork.check_job({k: v for k, v in request.items() if k != "context"})
            built = blockworker.far_geometry(_aid(kw["lat"], kw["lon"]), kw["lat"], kw["lon"], kw["tile"], kw["zoom"], kw["q"],
                                             kw["ref_zoom"], kw["with_sink"], layout)
        meta, blobs = peerwork.built_msg(built)
        validate_built(meta, blobs, request)
        head = {"built": meta}
    if revisit.faults() != faults or context() != request["context"]:
        return {"ok": False, "error": "incomplete_inputs"}, []
    head.update(ok=True, complete=True, cacheable=True, identity=identity(), context=request["context"],
                request=request_id(request))
    return head, blobs
