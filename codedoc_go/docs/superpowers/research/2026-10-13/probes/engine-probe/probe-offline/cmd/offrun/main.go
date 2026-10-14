package main

import (
	"fmt"

	"example.com/off"
	"github.com/go-logr/logr"
)

func main() {
	off.UseSvc(off.Svc{})
	off.UseSvc2(off.Svc2{Logger: &logr.Logger{}})
	off.UseSink(off.FullSink{})
	fmt.Println(off.UseNamer(off.ByUUID{}))
	fmt.Printf("DeepHits=%q\n", off.DeepHits)
}
