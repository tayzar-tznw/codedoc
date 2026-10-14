package a

import "example.com/dep"

func A() string { return dep.Version() }
