local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local blocked, bytes, encoded = false, '', 0
local sock = {}
function sock:settimeout() end
function sock:connect() return 1 end
function sock:setoption() end
function sock:close() end
function sock:send(data, start)
  if blocked then return nil, 'timeout', start - 1 end
  bytes = bytes .. data:sub(start)
  return #data
end
function sock:receive() return nil, 'timeout', '' end
package.loaded.socket = { tcp = function() return sock end }
function jsonEncode(msg) encoded = encoded + 1; return msg.payload or '{}' end
function jsonDecode() return {} end
local net = require('beamngEarth/net')
T.check(not net.sendTelemetry({}), 'disconnected samples are rejected')
net.update(3)
blocked = true
T.check(net.sendTelemetry({ payload = 'sample' }) and net.pendingFrames() == 1, 'one sample can wait on a blocked socket')
local n = encoded
local allDropped = true
for i = 1, 10000 do if net.sendTelemetry({}) then allDropped = false end end
T.check(allDropped, 'all 10000 attempts during backpressure are dropped')
T.check(net.pendingFrames() == 1 and encoded == n, 'blocked samples allocate no encoded payload and queue remains bounded')
T.check(net.send({ payload = 'pose' }) and net.pendingFrames() == 2, 'ordinary gameplay messages retain normal semantics')
blocked = false
net.update(0.016)
T.check(net.pendingFrames() == 0 and #bytes > 0, 'normal flushing drains the retained sample and gameplay messages')
T.check(not net.sendTelemetry({ payload = string.rep('x', 33000) }) and net.pendingFrames() == 0,
  'oversize samples rejected without enqueueing')
T.check(net.sendTelemetry({ payload = 'next' }), 'samples resume after backpressure clears')
net.close()
T.check(net.pendingFrames() == 0 and not net.sendTelemetry({}), 'closing discards queue and samples')
T.done('test_telemetry_net')
