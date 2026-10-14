package platform

func Name() string {
	return windowsName()
}

func homeDir() string {
	return `C:\iryo`
}

func windowsName() string {
	return "windows"
}
