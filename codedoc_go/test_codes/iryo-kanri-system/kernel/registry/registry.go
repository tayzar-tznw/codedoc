package registry

import (
	"sort"
	"strings"
)

var handlers = map[string]func(string) string{}

var defaultName = strings.ToUpper("iryo")

var banner = makeBanner(defaultName)

var normalizer = func(s string) string {
	return strings.TrimSpace(s)
}

func makeBanner(name string) string {
	return "== " + name + " =="
}

func Register(name string, h func(string) string) {
	handlers[name] = h
}

func init() {
	Register("echo", echo)
}

func init() {
	Register("upper", strings.ToUpper)
}

func echo(s string) string {
	return s
}

func Call(name, arg string) string {
	h, ok := handlers[name]
	if !ok {
		return ""
	}
	return h(arg)
}

func Banner() string {
	return banner
}

func Normalize(s string) string {
	return normalizer(s)
}

func Names() []string {
	names := make([]string, 0, len(handlers))
	for name := range handlers {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}
