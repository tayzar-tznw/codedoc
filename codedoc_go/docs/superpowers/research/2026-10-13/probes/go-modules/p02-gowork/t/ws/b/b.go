package b

import "example.com/dep"

func B() string { return dep.Version() }
