package extract

import "testing"

func TestBuildIncludedAndConstraintText(t *testing.T) {
	linux := newBuildConfig("linux", "amd64", nil, false)
	linuxCgo := newBuildConfig("linux", "amd64", nil, true)
	tagged := newBuildConfig("linux", "amd64", []string{"integration", "extra"}, false)
	windows := newBuildConfig("windows", "amd64", nil, false)
	for _, c := range []struct {
		name, relpath, src string
		bc                 *buildConfig
		included           bool
		constraint         string
	}{
		{"plain", "a.go", "package p\n", linux, true, ""},
		{"GOOS suffix excluded", "a_windows.go", "package p\n", linux, false, ""},
		{"GOOS suffix included", "a_linux.go", "package p\n", linux, true, ""},
		{"GOOS_GOARCH suffix", "a_linux_arm64.go", "package p\n", linux, false, ""},
		{"GOARCH test suffix", "a_amd64_test.go", "package p\n", linux, true, ""},
		{"suffix in a nested dir", "x/y/a_windows.go", "package p\n", windows, true, ""},
		{"go:build true", "a.go", "//go:build linux && !cgo\n\npackage p\n", linux, true, "linux && !cgo"},
		{"go:build normalized", "a.go", "//go:build linux&&(arm64||amd64)\n\npackage p\n", linux, true, "linux && (arm64 || amd64)"},
		{"go:build false", "a.go", "//go:build windows\n\npackage p\n", linux, false, "windows"},
		{"ignore", "a.go", "//go:build ignore\n\npackage main\n", linux, false, "ignore"},
		{"build tag set", "a.go", "//go:build integration\n\npackage p\n", tagged, true, "integration"},
		{"build tag unset", "a.go", "//go:build integration\n\npackage p\n", linux, false, "integration"},
		{"cgo tag with cgo off", "a.go", "//go:build cgo\n\npackage p\n", linux, false, "cgo"},
		{"cgo tag with cgo on", "a.go", "//go:build cgo\n\npackage p\n", linuxCgo, true, "cgo"},
		{"release tag", "a.go", "//go:build go1.21\n\npackage p\n", linux, true, "go1.21"},
		{"future release tag", "a.go", "//go:build go1.99\n\npackage p\n", linux, false, "go1.99"},
		{"unix", "a.go", "//go:build unix\n\npackage p\n", linux, true, "unix"},
		{"plus build", "a.go", "// +build !linux\n\npackage p\n", linux, false, "!linux"},
		{"plus build lines are ANDed", "a.go", "// +build linux darwin\n// +build amd64,!cgo\n\npackage p\n", linux, true, "(linux || darwin) && amd64 && !cgo"},
		{"plus build needs a blank line", "a.go", "// +build windows\npackage p\n", linux, true, ""},
		{"go:build wins over plus build", "a.go", "//go:build linux\n// +build windows\n\npackage p\n", linux, true, "linux"},
		{"after the package clause", "a.go", "package p\n\n//go:build windows\n", linux, true, ""},
		{"inside a block comment", "a.go", "/*\n//go:build windows\n*/\n\npackage p\n", linux, true, ""},
		{"multiple go:build lines", "a.go", "//go:build linux\n//go:build amd64\n\npackage p\n", linux, false, "linux"},
		{"malformed go:build", "a.go", "//go:build linux &&\n\npackage p\n", linux, false, "linux &&"},
		{"cgo import with cgo off", "a.go", "package p\n\nimport \"C\"\n", linux, false, ""},
		{"cgo import with cgo on", "a.go", "package p\n\nimport \"C\"\n", linuxCgo, true, ""},
		{"cgo import in a test file", "a_test.go", "package p\n\nimport \"C\"\n", linuxCgo, false, ""},
		{"documentation package", "a.go", "package documentation\n", linux, false, ""},
		{"underscore file", "_a.go", "package p\n", linux, false, ""},
		{"dot file", ".a.go", "package p\n", linux, false, ""},
	} {
		res := extractSource(c.relpath, []byte(c.src), c.bc, testModule)
		if res.err != "" {
			t.Fatalf("%s: %s", c.name, res.err)
		}
		if r := res.record; r.BuildIncluded != c.included || r.BuildConstraint != c.constraint {
			t.Errorf("%s: included %v constraint %q, want %v %q", c.name, r.BuildIncluded, r.BuildConstraint, c.included, c.constraint)
		}
	}
}

func TestBuildConfigFromFlags(t *testing.T) {
	bc := newBuildConfig("darwin", "arm64", []string{"a", "b"}, true)
	if bc.ctxt.GOOS != "darwin" || bc.ctxt.GOARCH != "arm64" || !bc.ctxt.CgoEnabled || len(bc.ctxt.BuildTags) != 2 {
		t.Errorf("context %+v", bc.ctxt)
	}
	if len(bc.ctxt.ReleaseTags) == 0 {
		t.Error("release tags must come from the toolchain (go1.x)")
	}
}
