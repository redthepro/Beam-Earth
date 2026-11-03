-- Production implementation: no capture dependency, state, allocation, or I/O.
local function noop() end
local function disabled() return false end
return { enabled = disabled, setActive = noop, frame = noop, event = disabled,
  drain = noop, reset = noop }
