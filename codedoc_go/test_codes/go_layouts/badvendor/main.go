package main

import (
	"fmt"

	"example.com/thirdparty/yamlish"
)

func main() {
	fmt.Print(yamlish.Marshal(map[string]string{"layout": "badvendor"}), local())
}

func local() string {
	return "local\n"
}
