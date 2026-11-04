local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local li = require('beamngEarth/levelinfo')
local saved = { {id=1, name='First place', lat=1, lon=2}, {id=2, name='Second place', lat=3, lon=4} }
local earth = li.build(saved)
earth.levelName = li.LEVEL
earth.spawnPoints[#earth.spawnPoints + 1] = {objectname='stock_pit'}
local originalPoints = earth.spawnPoints
local other = {spawnPoints={{objectname='other_spawn'}}}
local objects = {spawn_default=true, stock_pit=true, other_spawn=true}
local errors, refreshes, visibleDuringCall = {}, 0, nil
local throw, callback = false, nil
scenetree = {findObject=function(name) return objects[name] end}
-- Mirrors the installed core_levels POI contract: all objectname rows are resolved,
-- without an opt-out flag; absent objects log an error. No engine source is copied.
local function scan(level, elements)
  local info = level == li.LEVEL and earth or other
  visibleDuringCall = info.spawnPoints
  for _, point in ipairs(info.spawnPoints) do
    if point.objectname then
      if scenetree.findObject(point.objectname) then elements[#elements+1]=point.objectname
      else errors[#errors+1]=point.objectname end
    end
  end
  if throw then error('genuine POI failure') end
  return 'ok', nil, 'tail'
end
core_levels = {getLevelByName=function(name) return name == li.LEVEL and earth or other end,
  onGetRawPoiListForLevel=scan}
callback = scan -- extensions caches the function, not a table lookup on every hook.
extensions = {refresh=function(name)
  T.check(name == 'core_levels', 'only the edited extension hook cache is refreshed')
  refreshes=refreshes+1; callback=core_levels.onGetRawPoiListForLevel; return core_levels
end}
local function hook() return li.hookPoiFilter and li.hookPoiFilter() end
local function unhook() if li.unhookPoiFilter then li.unhookPoiFilter() end end
T.check(hook(), 'virtual POI adapter installed')
local elements = {}
local a,b,c = callback(li.LEVEL,elements)
T.check(#errors == 0, 'saved-place selectors do not log missing physical spawn objects')
T.check(#elements == 2 and elements[1]=='spawn_default' and elements[2]=='stock_pit',
  'default and real physical spawns remain in the map; no fake worldwide fast-travel markers')
T.check(earth.spawnPoints == originalPoints and #earth.spawnPoints == 5,
  'full saved-place menu metadata is restored by identity after each scan')
T.check(a=='ok' and b==nil and c=='tail', 'original callback return values are preserved')
T.check(visibleDuringCall ~= originalPoints and #visibleDuringCall==2,
  'only the synchronous physical POI scan sees the filtered list')
local n = refreshes
hook(); hook()
T.check(refreshes==n, 'repeated place sync does not refresh engine hooks repeatedly')
elements={}; callback('other_map',elements)
T.check(#elements==1 and elements[1]=='other_spawn' and visibleDuringCall==other.spawnPoints,
  'other maps use their unchanged stock metadata and callback')
-- Preserve genuine missing spawns, including names near but outside our selector namespace.
earth.spawnPoints[#earth.spawnPoints+1]={objectname='bne_place_typo'}
errors={}; callback(li.LEVEL,{})
T.check(#errors==1 and errors[1]=='bne_place_typo', 'unrelated missing-spawn errors are not hidden')
earth.spawnPoints[#earth.spawnPoints]=nil
objects.bne_place_1=true
elements={}; callback(li.LEVEL,elements)
T.check(#elements==3, 'an actual scene object with a selector name remains a real POI')
objects.bne_place_1=nil
throw=true
local ok,err=pcall(callback,li.LEVEL,{})
T.check(not ok and tostring(err):find('genuine POI failure',1,true) and earth.spawnPoints==originalPoints,
  'original exceptions propagate after restoring the full metadata')
throw=false
unhook()
T.check(core_levels.onGetRawPoiListForLevel==scan and callback==scan,
  'unload restores the exact original function and refreshes cached hooks')
hook()
local ours=core_levels.onGetRawPoiListForLevel
local function another(level,out) return ours(level,out) end
core_levels.onGetRawPoiListForLevel=another
unhook()
T.check(core_levels.onGetRawPoiListForLevel==another, 'unload never overwrites another extension wrapper')
errors={}; another(li.LEVEL,{})
T.check(#errors==3, 'a retained wrapper is inert after unload')
core_levels.onGetRawPoiListForLevel=scan
hook()
T.check(core_levels.onGetRawPoiListForLevel~=scan, 'adapter reinstalls after an extension reload')
unhook()
-- A missing hook-refresh API must not leave a partially installed patch.
extensions=nil
T.check(not hook() and core_levels.onGetRawPoiListForLevel==scan,
  'unsupported engine hook-refresh interface leaves stock behavior intact')
extensions={refresh=function() error('refresh unavailable') end}
T.check(not hook() and core_levels.onGetRawPoiListForLevel==scan,
  'failed engine hook-cache refresh rolls back installation')
-- Saved choice and default spawn configuration are unaffected by a POI query.
jsonReadFile=function() return {levels={beamng_earth={defaultSpawnPointName='bne_place_2'}}} end
T.check(li.chosen()=='bne_place_2' and li.placeId(li.chosen())==2 and earth.defaultSpawnPointName=='spawn_default',
  'saved-choice identity and stock fallback spawn are preserved')
T.done('test_levelinfo')
