package main

import "example.com/lib/trace"

func héllo() string {
	trace.Hit()
	return "é"
}

func 𝔊() string {
	trace.Hit()
	return "astral"
}

func encoding() {
	_ = "日本語"; _ = héllo() //@EN01
	_ = "😀"; _ = 𝔊()        //@EN02
}
