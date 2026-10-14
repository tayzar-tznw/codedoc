package main

import (
	"fmt"

	"example.com/layouts/wsvendor/util/pad"
	"example.com/thirdparty/uuidlite"
)

func main() {
	id := uuidlite.New()
	fmt.Println(pad.Left(id.String(), 40))
}
