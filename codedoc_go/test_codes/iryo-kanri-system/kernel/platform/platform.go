package platform

func Describe() string {
	return Name() + ":" + homeDir() + ":" + Accel()
}
