-- Waiting global reveals must survive rebases, without replaying invalid old local replacements.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local captureEvents, events = false, {}
package.loaded['beamngEarth/diagnostics'] = {
  enabled = function() return captureEvents end,
  event = function(name, fields)
    assert(captureEvents, 'disabled diagnostics must not emit')
    events[#events + 1] = {name = name, fields = fields}
  end,
}
local farfield = require('beamngEarth/farfield')
local geodesy = require('beamngEarth/geodesy')
local geotile = require('beamngEarth/geotile')
farfield.SPARE_MAX = 0
local free = { clock = function() return 0 end }
function free:try() return true end
function free:charge() end
function free:chargeMotion() end
local function step(n) for _ = 1, n or 60 do farfield.update(free, 1 / 60, nil) end end
local a = geodesy.newAnchor(38.1, -77.5)
local b = geodesy.newAnchor(38.1001, -77.4999)
local gx, gy = geotile.tileOf(38.3, -77.3, 12)
local function global(aid, child)
  local z, x, y = child and 13 or 12, child and gx * 2 or gx, child and gy * 2 or gy
  local lat, lon = geotile.centre(z, x, y)
  return { anchorId = aid, key = 'm' .. z .. '_' .. x .. '_' .. y, level = child and 4 or 5,
    shape = '/temp/beamngEarth/rebase/slot_' .. (child and '2' or '1') .. '.cdae',
    pos = { 0, 0, 100 }, err = 1, frame = { lat, lon }, state = 'group' }
end
local function localTile(aid, old)
  local x0, y0, x1, y1 = 0, 0, 256, 256
  if old then x0, y0, x1, y1 = old.x0, old.y0, old.x1, old.y1 end
  return { anchorId = aid, key = '0_80_80', level = 0,
    shape = '/temp/beamngEarth/rebase/local_' .. aid .. '.cdae', pos = { x0, y0, 100 }, err = 1,
    poly = { x0, y0, x1, y0, x1, y1, x0, y1 }, state = 'group' }
end
local function swap(aid, id, adds, removes)
  farfield.onSwap({ anchorId = aid, id = id, add = adds, remove = removes or {} })
end
local function reset()
  farfield.clear()
  farfield.setNearVisual('mesh')
  farfield.switchAnchor(1, false, { cx = 0, cy = 0 }, 'same', a)
end
local function rebase(sess)
  farfield.transform(a:transformTo(b), b)
  farfield.switchAnchor(2, true, { cx = 0, cy = 0 }, sess or 'same', b)
end
local function visible(inst)
  local o = inst and inst.id and S.objects[inst.id]
  return o ~= nil and not o.hidden
end

for _, imports in ipairs({0, 1}) do
  reset()
  local msg = global(1)
  farfield.onTile(msg)
  swap(1, 1, { msg.key })
  step(imports)
  local pending = farfield._pending(msg.key)
  local count = farfield.counters().imports
  T.check(pending and not pending.shown, 'global reveal is waiting at rebase (' .. imports .. ')')
  rebase()
  farfield.onDone({ anchorId = 2 })
  step()
  T.check(farfield._tile(msg.key) == pending and visible(pending),
    'already-sent swap reveals retained global after rebase (' .. imports .. ')')
  T.check(not farfield._pending(msg.key) and farfield.stats().queued == 0 and farfield.stats().groups == 0,
    'retained global drains without a duplicate swap (' .. imports .. ')')
  T.check(farfield.counters().imports == count + (imports == 0 and 1 or 0),
    'global geometry imports only once (' .. imports .. ')')
end

-- Mixed replacement: the server already considers the parent removed, but its old-frame local
-- replacement is discarded. Keep the parent visible until new-frame coverage actually exists.
reset()
local parent, child = global(1), global(1, true)
farfield.onTile(parent); swap(1, 1, { parent.key }); step()
local old = farfield._tile(parent.key)
local localOld = localTile(1)
farfield.onTile(child); farfield.onTile(localOld)
swap(1, 2, { child.key, localOld.key }, { parent.key })
step(1)
local retained = farfield._pending(child.key)
rebase()
step()
T.check(farfield._tile(child.key) == retained and visible(retained), 'mixed group retains its global reveal')
T.check(not farfield._pending(localOld.key) and not farfield._tile(localOld.key),
  'mixed group does not replay an invalid old local tile')
T.check(farfield._tile(parent.key) == nil and farfield._old(1, parent.key) == old and visible(old),
  'mixed replacement keeps the removed global as visible old coverage')
local replacement = localTile(2, old)
farfield.onTile(replacement); swap(2, 3, { replacement.key }); step()
T.check(not farfield._old(1, parent.key) and not visible(old), 'old coverage retires after replacement is shown')

-- A later waiting group withdraws the earlier global add: preserve final intent, not obsolete adds.
reset()
parent, child = global(1), global(1, true)
farfield.onTile(parent); swap(1, 1, { parent.key }); step()
old = farfield._tile(parent.key)
farfield.onTile(child); swap(1, 2, { child.key }, { parent.key })
localOld = localTile(1)
farfield.onTile(localOld); swap(1, 3, { localOld.key }, { child.key })
rebase(); step()
T.check(not farfield._pending(child.key) and not farfield._tile(child.key),
  'a global withdrawn by a later group is not resurrected')
T.check(farfield._old(1, parent.key) == old and visible(old), 'withdrawal does not erase old coverage prematurely')
replacement = localTile(2, old)
farfield.onTile(replacement); swap(2, 4, { replacement.key }); farfield.onDone({anchorId = 2}); step()
T.check(farfield.stats().old == 0 and farfield.stats().groups == 0, 'replacement completion drains retired geometry')

reset()
child = global(1, true)
farfield.onTile(child); swap(1, 1, { child.key })
rebase()
farfield.onRemove({anchorId = 2, key = child.key})
farfield.onDone({anchorId = 2}); step()
T.check(not farfield._tile(child.key) and not farfield._pending(child.key) and farfield.stats().groups == 0,
  'a current-anchor cancellation still cancels a retained reveal')

reset()
child = global(1, true)
farfield.onTile(child); swap(1, 1, { child.key })
rebase('different-session'); farfield.onDone({anchorId = 2}); step()
T.check(not farfield._tile(child.key) and not farfield._pending(child.key),
  'another server session never inherits the old session reveal')

-- The commit can run before the rest of the previous stream arrives. TCP preserves message
-- order, but a tile/swap already written by the server can be received on the following frame.
for _, receivedTile in ipairs({ false, true }) do
  reset()
  child = global(1, true)
  if receivedTile then farfield.onTile(child) end
  rebase()
  if not receivedTile then farfield.onTile(child) end
  swap(1, 1, {child.key})
  farfield.onDone({anchorId = 2}); step()
  T.check(visible(farfield._tile(child.key)) and not farfield._pending(child.key),
    'late previous-anchor global tile/swap reconciles (' .. tostring(receivedTile) .. ')')
end

reset()
parent, child = global(1), global(1, true)
farfield.onTile(parent); swap(1, 1, {parent.key}); step()
old = farfield._tile(parent.key)
rebase()
localOld = localTile(1)
farfield.onTile(child); farfield.onTile(localOld)
swap(1, 2, {child.key, localOld.key}, {parent.key}); step()
T.check(visible(farfield._tile(child.key)) and not farfield._pending(localOld.key),
  'late mixed group reveals only global data in its own frame')
T.check(farfield._old(1, parent.key) == old and visible(old), 'late mixed replacement preserves visible old coverage')
replacement = localTile(2, old)
farfield.onTile(replacement); swap(2, 3, {replacement.key}); farfield.onDone({anchorId = 2}); step()
T.check(not visible(old), 'late mixed replacement eventually retires old coverage')

reset()
child = global(1, true)
farfield.onTile(child)
rebase()
farfield.onRemove({anchorId = 1, key = child.key}); farfield.onDone({anchorId = 2}); step()
T.check(not farfield._pending(child.key) and not farfield._tile(child.key),
  'late previous-anchor withdrawal cancels its retained global')

reset()
child = global(1, true)
rebase('different-session')
farfield.onTile(child); swap(1, 1, {child.key}); farfield.onDone({anchorId = 2}); step()
T.check(not farfield._pending(child.key) and not farfield._tile(child.key),
  'late messages cannot cross a server-session change')

reset()
child = global(1, true)
child.state = 'prefetch'
farfield.onTile(child); rebase(); swap(1, 1, {child.key})
local promoted = farfield._pending(child.key)
T.check(promoted and not promoted.prefetch, 'late swap promotes retained global prefetch to required work')
local distant = localTile(2)
farfield.onTile(distant) -- other main work must not strand the required global behind the prefetch queue
local budget = 1
local limited = {clock = function() return 0 end}
function limited:try() if budget <= 0 then return false end; budget = budget - 1; return true end
function limited:charge() end
function limited:chargeMotion() end
farfield.update(limited, 1 / 60, {x=promoted.x, y=promoted.y, z=promoted.z, fx=1, fy=0, fz=0, fovDeg=60})
T.check(promoted.loaded and not farfield._pending(distant.key).loaded,
  'a required nearby global imports ahead of distant main work under a one-import budget')

-- Current selection is authoritative once its first far message arrives. A duplicate stale
-- tile/swap must neither replace that instance nor undo a current cancellation.
reset(); rebase()
child = global(2, true)
farfield.onTile(child)
local current = farfield._pending(child.key)
farfield.onTile(global(1, true)); swap(1, 9, {child.key})
T.check(farfield._pending(child.key) == current, 'old duplicate cannot supersede a current-anchor tile')
swap(2, 10, {child.key}); step()
T.check(visible(farfield._tile(child.key)), 'current-anchor reveal remains intact')
farfield.onRemove({anchorId = 2, key = child.key}); step()
farfield.onTile(global(1, true)); swap(1, 11, {child.key}); step()
T.check(not farfield._pending(child.key) and not farfield._tile(child.key),
  'stale duplicates cannot undo the current selection cancellation')

reset(); rebase(); farfield.closePreviousStream()
child = global(1, true)
farfield.onTile(child); swap(1, 1, {child.key}); step()
T.check(not farfield._pending(child.key) and not farfield._tile(child.key),
  'a transport reset closes previous-anchor message acceptance')

T.check(#events == 0, 'disabled diagnostics emitted no lifecycle events')
captureEvents = true
reset()
parent, child = global(1), global(1, true)
farfield.onTile(parent); swap(1, 1, {parent.key}); step()
rebase()
farfield.onTile(child); swap(1, 2, {child.key}, {parent.key}); step()
replacement = localTile(2, farfield._old(1, parent.key))
farfield.onTile(replacement); swap(2, 3, {replacement.key}); step()
farfield.onRemove({anchorId = 2, key = replacement.key}); step()
local seen = {}
local allowed = {adds=true, removes=true, waiting=true, retained=true, retired=true, reason=true,
  ready=true, anchor_id=true, previous_anchor_id=true}
for _, event in ipairs(events) do
  seen[event.name .. ':' .. tostring(event.fields.reason)] = true
  for key, value in pairs(event.fields) do
    T.check(allowed[key] and (type(value) == 'number' or type(value) == 'boolean'),
      'lifecycle event field is numeric/boolean and allowlisted')
  end
end
T.check(seen['far_rebase:1'] and seen['far_swap:1'] and seen['far_swap:2'] and seen['far_swap:4'],
  'diagnostics distinguish rebase, instant reveal, rise and late intent reconciliation')
T.check(seen['far_retire:1'] and seen['far_retire:2'], 'diagnostics distinguish covered and explicit retirements')
local count = #events
step()
T.check(#events == count, 'idle frames allocate no lifecycle event payloads')

T.done('farfield_rebase_groups')
