-- Dev-only, opt-in event chronology. It never reads the scene or does I/O.
-- Call frame(wall_ms) once at the beginning of onUpdate, before instrumented
-- work. This is elapsed frame duration, not an absolute clock. Events before
-- that first update have frame 0. seq orders events within/across frames and
-- windows. Every counter resets on a new capture; drain only resets the queue.
-- phase/reason/mode are numeric enums defined by their instrumented call sites.
-- No names, coordinates, tile keys, paths, or arbitrary strings are accepted.
local M = { MAX_EVENTS = 32 }
local LIMIT = 1e15
local names = { frame_shift = true, player_jump = true, far_rebase = true,
  far_swap = true, far_retire = true, drop_hold = true, edge_guard = true }
local fields = { 'anchor_id', 'previous_anchor_id', 'phase', 'distance_m',
  'speed_mps', 'vertical_speed_mps', 'adds', 'removes', 'waiting', 'retained',
  'retired', 'reason', 'ready', 'mode', 'holds', 'rescues' }
local active, frame, sequence, elapsed = false, 0, 0, 0
local events, dropped = {}, 0
local function finite(n)
  return type(n) == 'number' and n == n and n >= -LIMIT and n <= LIMIT
end
function M.enabled() return active end
function M.reset()
  frame, sequence, elapsed, events, dropped = 0, 0, 0, {}, 0
end
function M.setActive(value)
  local nextActive = value == true
  if active ~= nextActive then M.reset() end
  active = nextActive
end
function M.frame(wall_ms)
  if not active then return end
  frame = math.min(LIMIT, frame + 1)
  if finite(wall_ms) and wall_ms >= 0 then elapsed = math.min(LIMIT, elapsed + wall_ms) end
end
function M.event(name, values)
  if not active or type(name) ~= 'string' or not names[name] then return false end
  sequence = math.min(LIMIT, sequence + 1)
  if #events >= M.MAX_EVENTS then
    dropped = math.min(LIMIT, dropped + 1)
    return false
  end
  local event = { name = name, frame = frame, seq = sequence, elapsed_ms = elapsed }
  if type(values) == 'table' then
    -- Iterate our constant allowlist, never an unbounded caller-owned table.
    for _, key in ipairs(fields) do
      local value = values[key]
      if type(value) == 'boolean' or finite(value) then event[key] = value end
    end
  end
  events[#events + 1] = event
  return true
end
function M.drain()
  if not active then return nil end
  local result = { events = events, dropped = dropped }
  events, dropped = {}, 0
  return result
end
return M
