-- Development-only control must not disable sound in a later production package.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
dofile(dir .. '/stubs.lua')
local buildinfo = require('beamngEarth/buildinfo')
local saved = { version = 1, devStreamedAudio = false }
function jsonReadFile() return saved end
function jsonWriteFile(_, data) saved = data; return true end
FS = { fileExists = function() return true end }
local settings = require('beamngEarth/settings')
local ambience = require('beamngEarth/ambience')

buildinfo.devDiagnostics = false
settings.load()
settings.apply('devStreamedAudio')
T.check(ambience.streamedAudioState().requested, 'production ignores persisted dev isolation')
T.check(not settings.set('devStreamedAudio', false).ok, 'production refuses isolation requests')
T.check(settings.uiState().status.streamedAudio == nil, 'production does not expose isolation UI')

buildinfo.devDiagnostics = true
settings.apply('devStreamedAudio')
T.check(not ambience.streamedAudioState().requested, 'dev restores its saved isolation choice')
T.check(settings.uiState().status.streamedAudio.requested == false, 'dev UI reports requested mode')
ambience.onSpaces('empty', {}) -- latch this level without needing native sound objects
T.check(settings.set('devStreamedAudio', true).ok, 'dev accepts next-level choice')
local state = settings.uiState().status.streamedAudio
T.check(state.requested and not state.active and state.pending, 'UI distinguishes active and pending modes')
ambience.clear()
T.check(ambience.streamedAudioState().pending, 'disconnect cannot apply a pending isolation change')
ambience.reset()
ambience.onSpaces('next', {})
T.check(ambience.streamedAudioState().active and not ambience.streamedAudioState().pending,
  'next level applies the requested mode')
settings.set('devStreamedAudio', false)
buildinfo.devDiagnostics = false
ambience.reset()
settings.applyAll()
ambience.onSpaces('stable', {})
T.check(ambience.streamedAudioState().active, 'dev to stable transition always restores sound')
T.check(settings.defaults().devStreamedAudio, 'default is enabled')
T.done('test_audio_isolation_settings')
