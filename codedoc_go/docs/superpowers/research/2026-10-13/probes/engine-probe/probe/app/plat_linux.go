package main

import "example.com/lib/trace"

func platformName() string {
	trace.Hit()
	return linuxOnly() //@BC10
}

func linuxOnly() string {
	trace.Hit()
	return "linux"
}
