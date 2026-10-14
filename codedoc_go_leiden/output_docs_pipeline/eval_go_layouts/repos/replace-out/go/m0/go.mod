module example.com/layouts/replaceout

go 1.26

require example.com/outside/extlib v0.0.0

replace example.com/outside/extlib => ../_outside/extlib
