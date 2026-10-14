package main

import (
	"bufio"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"sort"
	"strings"
)

// Edge is one resolver answer to judge: the site (file, line and 1-based
// byte column of the callee token, plus its name) and the target declaration.
type Edge struct {
	File   string `json:"file"`
	Line   int    `json:"line"`
	Col    int    `json:"col"`
	Name   string `json:"name"`
	Target struct {
		QName string `json:"qname"`
		File  string `json:"file"`
		Line  int    `json:"line"`
	} `json:"target"`
	Note string `json:"note,omitempty"`
}

func normQName(q string) string {
	// pkg.(*T[...]).M and pkg.(*T).M are the same declaration
	return strings.ReplaceAll(q, "[...]", "")
}

func (e *Edge) matches(c *Callee) bool {
	if e.Target.File != "" && e.Target.Line > 0 {
		return e.Target.File == c.File && e.Target.Line == c.Line
	}
	return normQName(e.Target.QName) == normQName(c.QName)
}

func cmdMatch(args []string) {
	fs := flag.NewFlagSet("match", flag.ExitOnError)
	obsPath := fs.String("obs", "", "observations from report")
	sitesPath := fs.String("sites", "", "site table from instrument")
	edgesPath := fs.String("edges", "", "resolver edges (JSON lines)")
	fs.Parse(args)
	if *obsPath == "" || *sitesPath == "" || *edgesPath == "" {
		fs.Usage()
		os.Exit(2)
	}
	t := loadTable(*sitesPath)
	bySite := map[int]*Obs{}
	byGroup := map[int]*Obs{}
	readJSONL(*obsPath, func(b []byte) {
		o := &Obs{}
		check(json.Unmarshal(b, o))
		if o.Kind == "site" {
			bySite[o.Sid] = o
		} else {
			byGroup[o.Fid] = o
		}
	})
	byPos := map[string]*FuncRec{}
	byQ := map[string]*FuncRec{}
	for _, fn := range t.funcs {
		byPos[fmt.Sprintf("%s:%d", fn.File, fn.Line)] = fn
		byQ[normQName(fn.QName)] = fn
	}
	index := map[string][]*SiteRec{}
	for _, s := range t.sites {
		k := fmt.Sprintf("%s:%d:%s", s.File, s.Line, s.Name)
		index[k] = append(index[k], s)
	}
	counts := map[string]int{}
	readJSONL(*edgesPath, func(b []byte) {
		e := &Edge{}
		check(json.Unmarshal(b, e))
		cands := index[fmt.Sprintf("%s:%d:%s", e.File, e.Line, e.Name)]
		var s *SiteRec
		for _, c := range cands {
			if len(cands) == 1 || c.Col == e.Col {
				s = c
			}
		}
		verdict, detail := "NO-SITE", ""
		if s != nil {
			o := bySite[s.Sid]
			group := false
			if o == nil && s.Stmt == "defer" && s.Fid >= 0 {
				if g := byGroup[s.Fid]; g != nil {
					o, group = g, len(g.Defers) > 1
				}
			}
			var target *FuncRec
			if e.Target.File != "" {
				target = byPos[fmt.Sprintf("%s:%d", e.Target.File, e.Target.Line)]
			} else {
				target = byQ[normQName(e.Target.QName)]
			}
			switch {
			case o == nil || (len(o.Callees) == 0 && o.Exec <= 0):
				verdict = "UNEXERCISED"
			case group:
				verdict = "DEFER-GROUP-OTHER"
				for _, c := range o.Callees {
					if e.matches(c) {
						verdict = "DEFER-GROUP-CONSISTENT"
					}
				}
			default:
				hit := false
				var names []string
				for _, c := range o.Callees {
					if e.matches(c) {
						hit = true
					}
					names = append(names, c.QName)
				}
				sort.Strings(names)
				// entries that reached no instrumented function: the statement
				// completed more often than instrumented code was entered
				unins := int64(0)
				if o.Exec >= 0 && o.Exec > int64(o.Hits) {
					unins = o.Exec - int64(o.Hits)
					names = append(names, fmt.Sprintf("<%d uninstrumented>", unins))
				}
				detail = strings.Join(names, ", ")
				others := len(o.Callees) > 1 || (len(o.Callees) == 1 && !hit)
				switch {
				case others && hit:
					verdict = "WRONG-MULTI"
				case len(o.Callees) > 0 && !hit:
					verdict = "WRONG-OTHER"
				case target == nil || !target.Hooked:
					verdict = "UNVERIFIABLE-TARGET" // the target records nothing; exec counts prove nothing
				case hit && unins > 0:
					verdict = "WRONG-MULTI"
				case !hit && unins > 0:
					verdict = "WRONG-OTHER"
				case hit && o.Exec >= 0:
					verdict = "CONFIRMED"
				case hit:
					verdict = "CONFIRMED-NOEXEC" // no completion counter: entries outside instrumented code cannot be ruled out
				default:
					verdict = "UNEXERCISED"
				}
			}
		}
		counts[verdict]++
		fmt.Printf("%-22s %s:%d:%d %s -> %s  observed: %s %s\n", verdict, e.File, e.Line, e.Col, e.Name,
			e.Target.QName, detail, e.Note)
	})
	var keys []string
	for k := range counts {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	fmt.Print("summary:")
	for _, k := range keys {
		fmt.Printf(" %s=%d", k, counts[k])
	}
	fmt.Println()
}

func readJSONL(path string, fn func([]byte)) {
	f, err := os.Open(path)
	check(err)
	defer f.Close()
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 1<<20), 1<<26)
	for sc.Scan() {
		if len(sc.Bytes()) > 0 {
			fn(sc.Bytes())
		}
	}
	check(sc.Err())
}
