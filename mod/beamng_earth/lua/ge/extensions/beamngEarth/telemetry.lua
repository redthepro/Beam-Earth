-- Opt-in gameplay capture. No files, sockets, or engine calls here: core supplies those.
-- One fixed-size histogram per window; never retains individual frames. Quantiles are
-- bucket upper bounds, not exact percentiles. A sample covers measured wall time, not an
-- assumed one second. The last (overflow) bucket's estimate is the observed maximum.
local M = {}
local diagnostics = require('beamngEarth/diagnostics')
M.INTERVAL_MS = 1000
M.EDGES = { 1, 2, 4, 8, 12, 16.7, 20, 25, 33.4, 50, 66.7, 100, 150, 250, 500, 1000, 2000, 5000 }
local active, sequence, elapsed, dropped, lastSim = false, 0, 0, 0, nil
local stoppedByUser = false
local state = { available = false, active = false }
local w

local function finite(n) return type(n) == 'number' and n == n and n > -math.huge and n < math.huge end
local function numeric(t)
  local out, n = {}, 0
  for k, v in pairs(t or {}) do
    if n < 32 and type(k) == 'string' and #k <= 48 and k:match('^[%a][%w_]*$') and finite(v) and v >= 0 then
      out[k], n = v, n + 1
    end
  end
  return out
end
local function resetWindow()
  w = { frames = 0, wall_ms = 0, max_ms = 0, over_33 = 0, over_50 = 0, over_100 = 0,
    sim_s = 0, sim_wall_ms = 0, paused_frames = 0, histogram = {}, mod = {}, modN = 0, worst = nil }
  for i = 1, #M.EDGES + 1 do w.histogram[i] = 0 end
end
resetWindow()

function M.status() return state end
function M.isActive() return active end
function M.setStatus(s)
  if type(s) ~= 'table' then return end
  local running = s.active == true and not stoppedByUser
  if running and (not active or s.session ~= state.session) then
    sequence, elapsed, dropped, lastSim = 0, 0, 0, nil
    resetWindow()
    diagnostics.reset()
  end
  state = s
  state.active = running
  active = running
  diagnostics.setActive(active)
  if not active then lastSim = nil; resetWindow() end
end
function M.stopLocal(reason)
  stoppedByUser = true
  active, lastSim = false, nil
  diagnostics.setActive(false)
  resetWindow()
  state = { available = state.available, active = false, session = state.session,
    samples = state.samples, dropped = state.dropped, bytes = state.bytes,
    status = reason or 'Stopped', include_location = state.include_location }
end
function M.startRequested() stoppedByUser = false end
function M.disconnected()
  M.stopLocal('Disconnected; start a new capture after reconnecting')
  state.available = false
end
-- onPreRender's sim time belongs to the interval measured at the next onUpdate.
function M.simulation(dtSim) if active then lastSim = finite(dtSim) and math.max(0, dtSim) or nil end end

local function percentile(p)
  local want, count = math.ceil(w.frames * p), 0
  for i, n in ipairs(w.histogram) do
    count = count + n
    if count >= want then return M.EDGES[i] or w.max_ms end
  end
  return 0
end

-- context() is expensive: once a window and for a new worst hitch only. engine() is
-- sampled at the window end; it is explicitly a last-frame observation, not a mean.
function M.frame(ms, marks, hitch, context, engine, send)
  if not active or not finite(ms) or ms <= 0 then return end
  w.frames, w.wall_ms, elapsed = w.frames + 1, w.wall_ms + ms, elapsed + ms
  local bin = #M.EDGES + 1
  for i, edge in ipairs(M.EDGES) do if ms <= edge then bin = i; break end end
  w.histogram[bin] = w.histogram[bin] + 1
  if ms > 33.4 then w.over_33 = w.over_33 + 1 end
  if ms > 50 then w.over_50 = w.over_50 + 1 end
  if ms > 100 then w.over_100 = w.over_100 + 1 end
  if lastSim then
    w.sim_s = w.sim_s + lastSim
    w.sim_wall_ms = w.sim_wall_ms + ms
    if lastSim == 0 then w.paused_frames = w.paused_frames + 1 end
  end
  lastSim = nil
  for k, v in pairs(marks or {}) do
    if type(k) == 'string' and #k <= 48 and finite(v) and v >= 0 then
      if w.mod[k] then w.mod[k] = w.mod[k] + v
      elseif w.modN < 32 and k:match('^[%a][%w_]*$') then w.mod[k], w.modN = v, w.modN + 1 end
    end
  end
  if ms > w.max_ms then
    w.max_ms = ms
    if ms > 50 then
      w.worst = { ms = ms, mod_ms = numeric(marks), engine_ms = numeric(hitch and hitch.engine),
        context = context and context(false) or {} }
    end
  end
  if w.wall_ms < M.INTERVAL_MS then return end
  sequence = sequence + 1
  local c = context and context(true) or {}
  c.telemetry_dropped = dropped
  local row = { type = 'telemetry_sample', schema = 1, seq = sequence, elapsed_s = elapsed / 1000,
    frame = { frames = w.frames, wall_ms = w.wall_ms, avg_ms = w.wall_ms / w.frames,
      p50_ms = percentile(0.5), p95_ms = percentile(0.95), p99_ms = percentile(0.99), max_ms = w.max_ms,
      over_33 = w.over_33, over_50 = w.over_50, over_100 = w.over_100,
      sim_s = w.sim_s, sim_wall_ms = w.sim_wall_ms,
      sim_wall_ratio = w.sim_wall_ms > 0 and w.sim_s / (w.sim_wall_ms / 1000) or nil, paused_frames = w.paused_frames,
      histogram = w.histogram }, mod_ms = w.mod, engine_ms = numeric(engine and engine()),
    context = c, worst = w.worst, diagnostics = diagnostics.drain() }
  resetWindow()
  if not send or not send(row) then dropped = dropped + 1 end
  return row
end

return M
