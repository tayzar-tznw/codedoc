package ifc

import "example.com/brk/bi"

type I interface{ M() string }

type K interface{ bi.J }
