package c

import "example.com/ghost/nothere"

func C() string { return nothere.X() + local() }

func local() string { return "c" }
