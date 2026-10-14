package common

import "strings"

func F() string { return strings.ToUpper(local()) }

func G() int { return int(conv(1)) }
