package main

func platformName() string {
	return winOnly() //@BC02
}

func winOnly() string { return "windows" + platform() } //@BC11
