package main

import (
	"fmt"
	"strings"

	"example.com/thirdparty/yamlish"
)

func main() {
	doc := yamlish.NewDocument()
	doc.Set("layout", "modvendor")
	fmt.Println(strings.Join(doc.Keys(), ","))
	fmt.Print(render(doc))
}

func render(doc *yamlish.Document) string {
	return doc.Encode()
}
