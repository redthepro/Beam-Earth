-- luajit mod/tests/test_ambience.lua  (forest wind and room tone, ambience.lua): a vegetation tile's sound nodes go to
-- the stock core_audioForest as one cloud round the listener (its main event the tiles' set), the room tone plays
-- and follows the listener, the set chosen by the tiles' sound items (kept until another has M.SWITCH times them),
-- a rebase moves the nodes, a teleport's old tiles go.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')

-- the engine pieces ambience.lua uses
package.preload['kdtreepoint3d'] = function()
  return { new = function() return { n = 0, preLoad = function(self) self.n = self.n + 1 end, build = function() end } end }
end
local clock = 0
os.clockhp = function() return clock end
local lx, ly = 0, 0
SFXSystem = { getPositionXYZ = function() return lx, ly, 0 end }
MatrixF = function()
  local m = { yaw = 0, pos = nil }
  function m:setFromEuler(v) self.yaw = v.z end
  function m:setPosition(p) self.pos = p end
  return m
end
Sim = { upcast = function(o) return o end }
Engine = { generateUUID = function() return 'u' end }
function simObjectExists(o) return S.objects[o.id] ~= nil end
local af = { calls = {} }
function af.setNodeCloud(c) af.cloud = c end
function af.setMainEvent(e) af.main = e end
function af.setEnabled(v) af.enabled = v end
local rb = { set = nil, cleared = 0 }
function rb.createEmitterHost(ev) return { eventNameStr = ev } end
function rb.updateRibbonData(r) r.numSegs = (#r.nodes - 4) / 2 + 1 end
function rb.setRibbons(l) rb.set = l end
function rb.recomputeMap() end
function rb.clearNearFarLists() end
function rb.clearAllSFXEmitters() rb.cleared = rb.cleared + 1 end
extensions = { core_audioForest = af, core_audioRibbon = rb }
local function patch(o)
  function o:setTransform(m) self.xf = m end
  function o:getId() return self.id end
  function o:play() self.playing = true end
  function o:setVolumePitchCT(v) self.vol = v end
  return o
end
local newObject = S.newObject
S.newObject = function(class) return patch(newObject(class)) end
createObject = function(class) return S.newObject(class) end

local amb = require('beamngEarth/ambience')

-- pure: events and vote
T.check(amb.events({ 0, 0 })[1]:match('ConiferousDeciduous_01$') ~= nil, 'temperate _01')
T.check(amb.events({ 2, 1 })[2]:match('Roomtone_Desert_02$') ~= nil, 'desert _02 room tone')
T.check(amb.events({ 3, 0 })[1]:match('Wind_Tropical$') ~= nil, 'tropical')
T.check(amb.events({ 9, 0 }) == nil, 'unknown set: nothing')
local function tile(cx, cy, b, w) return { cx = cx, cy = cy, biome = b, weight = w } end
local v = amb.vote({ tile(0, 0, { 0, 0 }, 10), tile(256, 0, { 2, 0 }, 30) }, 0, 0, nil)
T.check(v[1] == 2, 'the most sound items win')
v = amb.vote({ tile(0, 0, { 0, 0 }, 25), tile(256, 0, { 2, 0 }, 30) }, 0, 0, { 0, 0 })
T.check(v[1] == 0, 'the current set stays while the other has less than SWITCH times its items')
v = amb.vote({ tile(0, 0, { 1, 0 }, 0), tile(5000, 0, { 2, 0 }, 99) }, 0, 0, nil)
T.check(v[1] == 1, 'a tile without trees still gives its set; far tiles do not vote')

-- a tile's nodes as the cloud round the listener
amb.onTile(1, '0_0', { biome = { 0, 1 }, audio = { 10, 20, 5, 6, 0.5, 30, 200, 200, 5, 3, 0, 4, 2000, 0, 0, 1, 1, 1 } })
amb.update(1)
T.check(af.cloud and af.cloud.nodeCount == 2, 'the two nodes within the radius: ' .. tostring(af.cloud and af.cloud.nodeCount))
T.check(af.cloud.nodeDepth[1] == 6 and af.cloud.nodeSourceCount[1] == 30, 'depth and count kept')
T.check(af.main:match('ConiferousDeciduous_02$') ~= nil and af.enabled == true, 'main event set, enabled')
local rt = S.liveOf('SFXEmitter')
T.check(#rt == 1 and rt[1].fields.track:match('Roomtone_ConiferousDeciduous_02$') ~= nil and rt[1].playing, 'the room tone plays')

-- not built again until moved or changed (and not more often than GAP_S)
local builds = amb.builds
clock = 5
amb.update(1)
T.check(amb.builds == builds, 'nothing changed: not built again')
lx = 400
amb.update(1)
T.check(amb.builds == builds + 1, 'moved past MOVE_M: built again')
T.check(rt[1].pos.x == 400, 'the room tone follows the listener')

-- rebase: the nodes move (a ghost), and a teleport's tiles go
local xf = { apply = function(_, x, y, z) return x + 1000, y, z end }
amb.transform(xf)
lx = 1400
clock = 10
amb.update(2)
T.check(af.cloud.nodeCount == 2 and af.cloud.nodeX[1] == 1010, 'the ghost tile\'s nodes moved with the rebase')
amb.onTile(3, '0_0', { biome = { 0, 0 }, audio = {} })
clock = 15
amb.update(3)
T.check(af.cloud.nodeCount == 2, 'the ghost stays under another anchor\'s tile elsewhere')
clock = 200
amb.update(3)
T.check(af.cloud.nodeCount == 0 and af.enabled == false, 'the ghost goes after GHOST_MAX_S: no nodes, disabled')
lx = 0
clock = 202
amb.update(3)
T.check(af.main:match('_01$') ~= nil and S.liveOf('SFXEmitter')[1].fields.track:match('_01$') ~= nil,
  'back by the new tile: the wind and room tone of its set')
-- the room tone deleted by someone else: made again (no error every frame)
local old = S.liveOf('SFXEmitter')[1]
old:delete()
clock = 210
amb.update(3)
local now = S.liveOf('SFXEmitter')
T.check(#now == 1 and now[1].id ~= old.id, 'a deleted room tone: made again')

-- rivers: bank pairs across the flow at the surface, as wide as the water; made again on a change only
local bk = amb.banks({ { 0, 0, 5, 10 }, { 10, 0, 5, 10 }, { 20, 0, 4, 6 } })
T.check(#bk == 6, 'two bank points a node')
T.check(bk[1][1] == 0 and bk[1][2] == 5 and bk[2][2] == -5 and bk[1][3] == 5, 'left then right of an eastward flow, half the width each')
T.check(bk[5][2] == 3 and bk[6][2] == -3 and bk[5][3] == 4, 'the last node: its own width and z')
local seaV = { riversVersion = 1 }
local riverSet = { Ra = { kind = 'river', flow = 2, nodes = { { 0, 0, 5, 10 }, { 10, 0, 5, 10 } } },
                   Rb = { kind = 'stream', flow = 1, nodes = { { 0, 50, 6, 4 }, { 0, 60, 6, 4 }, { 0, 70, 6, 4 } } } }
seaV.rivers = function() return riverSet, 3 end
amb.update(3, seaV)
T.check(rb.set and #rb.set == 2 and rb.set[1].persistantId == 'Ra' and #rb.set[2].nodes == 6, 'a ribbon a river, in key order')
T.check(rb.set[1].emitters[2].eventNameStr == amb.RIVER_EVENT and rb.set[1].depths[1] == 10 and rb.set[1].speed == 1,
  'the stock river event, depth and speed')
local first = rb.set
seaV.riversVersion = 2
amb.update(3, seaV)
T.check(rb.set == first, 'the same rivers: not made again')
amb.update(4, seaV)
T.check(rb.set ~= first and #rb.set == 0 and rb.cleared == 1, 'another anchor\'s rivers: none, the emitters cleared')
-- only the rivers near the listener; a river's ribbon made once
riverSet.Rc = { kind = 'river', flow = 2, nodes = { { 5000, 0, 5, 10 }, { 5010, 0, 5, 10 } } }
riverSet.Rd = { kind = 'canal', flow = 0.5, nodes = { { 0, 20, 5, 10 }, { 10, 20, 5, 10 } } }   -- (still water: none)
seaV.riversVersion = 3
amb.update(3, seaV)
T.check(#rb.set == 2 and rb.set[1].persistantId == 'Ra', 'the far river left out')
local ra, made = rb.set[1], amb.ribbonsMade
lx = 4800
clock = 300
amb.update(3, seaV)
T.check(#rb.set == 1 and rb.set[1].persistantId == 'Rc', 'moved: the river there instead')
lx = 0
amb.update(3, seaV)
T.check(#rb.set == 2 and rb.set[1] == ra and amb.ribbonsMade == made + 1, 'back: the first ribbons reused, only Rc was made')

-- tunnels' and bridges' sound spaces: a box a row, its ambience by kind, gone with the tile, moved by a rebase
amb.onSpaces('1:3_4', { 0, 10, 20, 5, 42, 9.4, 8.2, 0.5, 1, 100, 0, 3, 30, 9, 24, 0, 7, 0, 0, 0, 1, 1, 1, 0 })
local sp = S.liveOf('SFXSpace')
T.check(#sp == 2, 'a space a known kind (kind 7: none): ' .. #sp)
table.sort(sp, function(p, q) return p.id < q.id end)
T.check(sp[1].fields.soundAmbience == 'Level_Tunnel_Closed' and sp[2].fields.soundAmbience == 'Level_Under_Bridge_A',
  'the tunnel\'s and the bridge\'s ambience')
T.check(sp[1].scale.x == 42 and sp[1].scale.y == 9.4 and sp[1].scale.z == 8.2 and sp[1].xf.yaw == -0.5
  and sp[1].xf.pos.x == 10 and sp[1].xf.pos.z == 5, 'placed: full size, the yaw clockwise')
amb.transform({ apply = function(_, x, y, z) return -y, x, z end })     -- (a quarter turn)
T.check(math.abs(sp[1].xf.pos.x + 20) < 1e-9 and math.abs(sp[1].xf.pos.y - 10) < 1e-9
  and math.abs(sp[1].xf.yaw + 0.5 + math.pi / 2) < 1e-9, 'a rebase turns and moves them')
amb.dropSpaces('1:3_4')
T.check(#S.liveOf('SFXSpace') == 0, 'the tile goes: its spaces with it')
amb.onSpaces('1:5_5', { 4, 3, 4, 5, 0, 0, 0, 0 })
local pe = nil
for _, o in ipairs(S.liveOf('SFXEmitter')) do if o.fields.track == amb.PORTAL_EVENT then pe = o end end
T.check(pe and pe.playing and pe.pos.x == 3 and pe.pos.z == 5, 'a portal: the stock portal event playing there')
amb.dropSpaces('1:5_5')
T.check(not scenetree.findObjectById(pe.id), 'and gone with its tile')

do -- Same-count remaps must preserve the sound kind, not only the object's position.
  local function row(kind, x) return { kind, x, 10, 5, 20, 10, 8, 0 } end
  local function tracked(class)
    local out = {}
    for _, o in ipairs(S.liveOf(class)) do
      if o.name and o.name:match('^bngEarthSpace_') then out[#out + 1] = o end
    end
    table.sort(out, function(a, b) return a.id < b.id end)
    return out
  end
  T.check(amb.spaceStats().live == 0, 'streamed sound live count excludes the room tone and other core audio')
  amb.onSpaces('remap', row(0, 0))
  local tunnel, before = tracked('SFXSpace')[1], amb.spaceStats()
  amb.placeSpaces('remap', row(0, 25))
  local after = amb.spaceStats()
  T.check(tracked('SFXSpace')[1] == tunnel and tunnel.xf.pos.x == 25 and amb.inTunnel(25, 10, 5),
    'same-kind remap keeps the object and updates geometry consumers')
  T.check(after.created == before.created and after.deleted == before.deleted and after.replaced == before.replaced
    and after.moved == before.moved + 1 and after.live == 1, 'same-kind remap adds no creation/deletion churn')

  amb.placeSpaces('remap', row(1, 40))
  local bridge = tracked('SFXSpace')[1]
  after = amb.spaceStats()
  T.check(bridge.id ~= tunnel.id and not S.objects[tunnel.id] and bridge.fields.soundAmbience == amb.SPACES[1]
    and not amb.inTunnel(40, 10, 5), 'same-count tunnel to bridge replaces stale ambience and roof semantics')
  T.check(after.created == before.created + 1 and after.deleted == before.deleted + 1
    and after.replaced == before.replaced + 1 and after.live == 1, 'replacement records actual create/delete operations')

  amb.placeSpaces('remap', row(4, 50))
  local portal = tracked('SFXEmitter')[1]
  T.check(#tracked('SFXSpace') == 0 and not S.objects[bridge.id] and portal.playing
    and portal.fields.track == amb.PORTAL_EVENT and portal.pos.x == 50, 'space to point remap creates a playing emitter')
  amb.placeSpaces('remap', row(8, 60))
  local plant = tracked('SFXEmitter')[1]
  T.check(plant.id ~= portal.id and not S.objects[portal.id] and plant.fields.track == amb.POINTS[8]
    and plant.vol == amb.POINT_VOLUME[8], 'point kind change updates event and volume, despite unchanged row count')
  amb.placeSpaces('remap', row(2, 70))
  local arch = tracked('SFXSpace')[1]
  T.check(#tracked('SFXEmitter') == 0 and arch.fields.soundAmbience == amb.SPACES[2]
    and amb.inTunnel(70, 10, 5) and #amb.tunnelLights() > 0, 'point to space restores the correct class and roof rows')

  before = amb.spaceStats()
  arch:delete() -- engine/external disappearance is not a deletion performed by this module
  amb.placeSpaces('remap', row(2, 80))
  local recovered = tracked('SFXSpace')[1]
  after = amb.spaceStats()
  T.check(recovered and recovered.id ~= arch.id and recovered.xf.pos.x == 80 and after.live == 1,
    'same-kind remap recreates a missing engine object instead of silently losing sound')
  T.check(after.created == before.created + 1 and after.replaced == before.replaced + 1
    and after.deleted == before.deleted, 'missing-object recovery does not invent a native delete')

  before = amb.spaceStats()
  local flat = row(99, 900) -- ignored/unsupported row must not misalign the supported rows
  for _, v0 in ipairs(row(2, 90)) do flat[#flat + 1] = v0 end
  amb.placeSpaces('remap', flat)
  after = amb.spaceStats()
  T.check(tracked('SFXSpace')[1] == recovered and recovered.xf.pos.x == 90 and amb.inTunnel(90, 10, 5)
    and after.created == before.created and after.deleted == before.deleted,
    'unknown rows neither shift a supported sound row nor cause needless replacement')
  before = after
  amb.transform({ apply = function(_, x, y, z) return x + 10, y, z or 0 end })
  after = amb.spaceStats()
  T.check(recovered.xf.pos.x == 100 and amb.inTunnel(100, 10, 5) and after.moved == before.moved + 1,
    'rebase updates current rows and counts actual managed placements')

  flat = row(2, 110)
  for _, v0 in ipairs(row(1, 150)) do flat[#flat + 1] = v0 end
  amb.placeSpaces('remap', flat)
  local pair = tracked('SFXSpace')
  T.check(#pair == 2 and amb.spaceStats().live == 2, 'count-changing replacement tracks both fresh objects')
  pair[2]:delete()
  before = amb.spaceStats()
  flat[2], flat[10] = 120, 160
  amb.placeSpaces('remap', flat)
  after = amb.spaceStats()
  T.check(after.moved == before.moved and after.created == before.created + 2
    and after.deleted == before.deleted + 1 and after.live == 2,
    'whole-set validation avoids a partial remap when a later object is missing')
  T.check(amb.inTunnel(120, 10, 5) and not amb.inTunnel(160, 10, 5), 'replacement rows remain correctly typed and located')
  amb.dropSpaces('remap')
  before = amb.spaceStats()
  amb.dropSpaces('remap')
  after = amb.spaceStats()
  T.check(after.live == 0 and after.deleted == before.deleted, 'repeated drops do not double-count deletes')
  after.live = 99
  T.check(amb.spaceStats().live == 0, 'telemetry snapshot cannot mutate lifecycle counters')
end

-- shores: a "Waves" ribbon along a shoreline near the listener, SHORE_W wide, with the rivers
seaV.shores = function() return { S1 = { kind = 'sea', nodes = { { 0, -30, 0 }, { 40, -30, 0 } } } }, 3 end
seaV.riversVersion = 9
amb.update(3, seaV)
local sh
for _, rbn in ipairs(rb.set) do if rbn.persistantId == 'S1' then sh = rbn end end
T.check(sh and sh.emitters[2].eventNameStr == amb.SHORE_EVENT and #rb.set == 3, 'the shore ribbon, with the rivers')
T.check(sh and math.abs(sh.nodes[1].y - sh.nodes[2].y) == amb.SHORE_W and amb.shoreRibbons == 1, 'SHORE_W across')
seaV.shores = nil

-- rain: the loop fades in with the weather's rain, out inside a tunnel's space, and goes when the rain stops
local function rainEm()
  for _, o in ipairs(S.liveOf('SFXEmitter')) do if o.name == 'bngEarthRain' then return o end end
end
beamngEarth_weather = { rainNow = 1 }
clock = 400; amb.update(3, seaV)
clock = 401; amb.update(3, seaV)
local re = rainEm()
T.check(re and re.fields.track == amb.RAIN_TRACK and math.abs(re.vol - amb.RAIN_GAIN / amb.RAIN_FADE_S) < 1e-9,
  'rain: the loop, fading in')
for i = 1, 8 do clock = 401 + i * 0.5; amb.update(3, seaV) end
T.check(math.abs(re.vol - amb.RAIN_GAIN) < 1e-9, 'full rain: RAIN_GAIN')
amb.onSpaces('3:0_0', { 0, lx, 0, 0, 40, 10, 10, 0 })
clock = 405.5; amb.update(3, seaV)
T.check(amb.inTunnel(lx, 0, 0) and re.vol < amb.RAIN_GAIN, 'in a tunnel: fading out')
amb.dropSpaces('3:0_0')
beamngEarth_weather.rainNow = 0
for i = 1, 12 do clock = 405.5 + i * 0.5; amb.update(3, seaV) end
T.check(rainEm() == nil, 'the rain stopped: the loop gone')
beamngEarth_weather = nil

amb.clear()
T.check(#S.liveOf('SFXEmitter') == 0 and af.enabled == false, 'clear: the room tone goes')
T.done('test_ambience')
