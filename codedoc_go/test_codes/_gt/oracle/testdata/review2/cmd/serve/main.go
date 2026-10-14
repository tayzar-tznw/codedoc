package main

import "example.com/review2/y"

func main() {
	y.Serve(nil, nil)
	started := make(chan bool)
	go y.Serve(make(chan int), started)
	<-started
}
