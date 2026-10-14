// Command cg builds SSA for a workspace and compares CHA and VTA callee counts at in-repo
// interface-method (invoke) call sites. Research probe (codedoc_go, track real-repos).
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"go/token"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	"golang.org/x/tools/go/callgraph"
	"golang.org/x/tools/go/callgraph/cha"
	"golang.org/x/tools/go/callgraph/vta"
	"golang.org/x/tools/go/packages"
	"golang.org/x/tools/go/ssa"
	"golang.org/x/tools/go/ssa/ssautil"
)

func vmHWM() int {
	b, _ := os.ReadFile("/proc/self/status")
	for _, l := range strings.Split(string(b), "\n") {
		if strings.HasPrefix(l, "VmHWM:") {
			var kb int
			fmt.Sscanf(strings.TrimSpace(strings.TrimPrefix(l, "VmHWM:")), "%d", &kb)
			return kb / 1024
		}
	}
	return -1
}

type siteRec struct {
	File   string `json:"file"`
	Off    int    `json:"call_off"`
	Invoke bool   `json:"invoke"`
	CHA    int    `json:"cha"`
	VTA    int    `json:"vta"`
	CHARepo int   `json:"cha_repo"`
	VTARepo int   `json:"vta_repo"`
}

func main() {
	root := flag.String("root", "", "repo root")
	out := flag.String("out", "cg.jsonl", "output")
	tests := flag.Bool("tests", false, "include tests")
	flag.Parse()
	abs, _ := filepath.Abs(*root)
	stats := map[string]any{}
	t0 := time.Now()
	cfg := &packages.Config{Mode: packages.LoadAllSyntax | packages.NeedModule, Dir: abs, Tests: *tests}
	pkgs, err := packages.Load(cfg, "work")
	if err != nil {
		panic(err)
	}
	stats["load_s"] = time.Since(t0).Seconds()
	t1 := time.Now()
	prog, _ := ssautil.AllPackages(pkgs, ssa.InstantiateGenerics)
	prog.Build()
	stats["ssa_s"] = time.Since(t1).Seconds()
	stats["vmhwm_after_ssa_mb"] = vmHWM()
	t2 := time.Now()
	chaCG := cha.CallGraph(prog)
	stats["cha_s"] = time.Since(t2).Seconds()
	stats["vmhwm_after_cha_mb"] = vmHWM()
	t3 := time.Now()
	vtaCG := vta.CallGraph(ssautil.AllFunctions(prog), chaCG)
	stats["vta_s"] = time.Since(t3).Seconds()
	stats["vmhwm_after_vta_mb"] = vmHWM()
	inRepo := func(fn *ssa.Function) bool {
		if fn == nil || fn.Pkg == nil {
			return false
		}
		f := prog.Fset.Position(fn.Pos()).Filename
		return strings.HasPrefix(f, abs+"/")
	}
	inRepoID := func(id any) bool {
		switch x := id.(type) {
		case *ssa.Function:
			return inRepo(x)
		case interface{ Pos() token.Pos }:
			return strings.HasPrefix(prog.Fset.Position(x.Pos()).Filename, abs+"/")
		}
		return false
	}
	type key struct {
		file string
		off  int
	}
	collect := func(cg *callgraph.Graph) map[key]map[any]bool {
		m := map[key]map[any]bool{}
		for fn, node := range cg.Nodes {
			if fn == nil {
				continue
			}
			for _, e := range node.Out {
				site := e.Site
				if site == nil || !site.Common().IsInvoke() {
					continue
				}
				p := site.Pos()
				if !p.IsValid() {
					continue
				}
				pos := prog.Fset.Position(p)
				if !strings.HasPrefix(pos.Filename, abs+"/") {
					continue
				}
				rel, _ := filepath.Rel(abs, pos.Filename)
				k := key{rel, pos.Offset}
				if m[k] == nil {
					m[k] = map[any]bool{}
				}
				var id any = e.Callee.Func
				if o := e.Callee.Func.Object(); o != nil {
					id = o // fold synthetic wrappers into the method they wrap
				}
				m[k][id] = true
			}
		}
		return m
	}
	cm, vm := collect(chaCG), collect(vtaCG)
	// invoke sites with no VTA edges at all are absent from vm
	allSites := map[key]bool{}
	for fn := range ssautil.AllFunctions(prog) {
		for _, b := range fn.Blocks {
			for _, ins := range b.Instrs {
				if c, ok := ins.(ssa.CallInstruction); ok && c.Common().IsInvoke() && c.Pos() != token.NoPos {
					pos := prog.Fset.Position(c.Pos())
					if strings.HasPrefix(pos.Filename, abs+"/") {
						rel, _ := filepath.Rel(abs, pos.Filename)
						allSites[key{rel, pos.Offset}] = true
					}
				}
			}
		}
	}
	f, _ := os.Create(*out)
	enc := json.NewEncoder(f)
	for k := range allSites {
		r := siteRec{File: k.file, Off: k.off, Invoke: true, CHA: len(cm[k]), VTA: len(vm[k])}
		for id := range cm[k] {
			if inRepoID(id) {
				r.CHARepo++
			}
		}
		for id := range vm[k] {
			if inRepoID(id) {
				r.VTARepo++
			}
		}
		enc.Encode(r)
	}
	f.Close()
	stats["invoke_sites"] = len(allSites)
	stats["total_s"] = time.Since(t0).Seconds()
	stats["vmhwm_mb"] = vmHWM()
	stats["go"] = runtime.Version()
	b, _ := json.MarshalIndent(stats, "", "  ")
	fmt.Println(string(b))
}
