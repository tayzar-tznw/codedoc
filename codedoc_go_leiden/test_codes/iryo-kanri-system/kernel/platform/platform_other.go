//go:build !linux && !windows

package platform

func Name() string {
	return "other"
}

func homeDir() string {
	return "/var/iryo"
}
