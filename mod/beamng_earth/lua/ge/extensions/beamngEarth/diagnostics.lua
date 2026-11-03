-- Build-selected diagnostics boundary. Stable packages omit the capture provider.
-- No runtime setting can turn a stable package into a diagnostic build. Missing
-- build metadata also fails closed, so a partial installation cannot add probes.
local ok, build = pcall(require, 'beamngEarth/buildinfo')
if ok and type(build) == 'table' and build.devDiagnostics == true then
  return require('beamngEarth/diagnostics_capture')
end
return require('beamngEarth/diagnostics_noop')
