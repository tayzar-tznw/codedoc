module example.com/v1

go 1.22

require (
	example.com/Upper v1.0.0
	example.com/dep v1.1.0
	example.com/dep/v2 v2.0.0
	example.com/loc v0.0.0
)

replace example.com/loc => ./local
