package main

import (
	"fmt"

	"example.com/rlib"
)

func main() {
	fmt.Println(rlib.Do())        //@RP01
	fmt.Println(rlib.Svc{}.Run()) //@RP02
}
