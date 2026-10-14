// Deprecated: use example.com/all/v2 instead.
module example.com/all // trailing comment

go 1.25.0

toolchain go1.25.3

godebug default=go1.21
godebug (
	panicnil=1
	asynctimerchan=0
)

require (
	golang.org/x/text v0.38.0 // indirect
	"example.com/quoted" v1.0.0
	gopkg.in/yaml.v3 v3.0.1
)

require example.com/dep/v2 v2.3.4

exclude example.com/dep/v2 v2.0.0

replace example.com/dep/v2 v2.3.4 => example.com/fork/dep/v2 v2.3.5
replace (
	example.com/local => ./local
	example.com/abs => /abs/path/outside
	example.com/up => ../outside
)

retract v1.0.1
retract [v1.1.0, v1.2.0] // range

tool golang.org/x/tools/cmd/stringer

ignore (
	./node_modules
	static
)
