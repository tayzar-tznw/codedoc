package resolve

import (
	"bufio"
	"bytes"
	"encoding/json"
	"io/fs"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	"codedoc.local/gohelper/internal/extract"
)

// extractSites runs `gohelper extract` on every .go file under root and
// returns relpath -> offset -> site kind.
func extractSites(t *testing.T, root string) map[string]map[int]string {
	t.Helper()
	var rels []string
	filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err == nil && !d.IsDir() && strings.HasSuffix(p, ".go") {
			rel, _ := filepath.Rel(root, p)
			rels = append(rels, filepath.ToSlash(rel))
		}
		return nil
	})
	sort.Strings(rels)
	list := filepath.Join(mkdir(t, "list"), "files.txt")
	writeFile(t, list, strings.Join(rels, "\n")+"\n")
	var out bytes.Buffer
	if err := extract.Run([]string{"--root", root, "--files-from", list}, &out); err != nil {
		t.Fatal(err)
	}
	sites := map[string]map[int]string{}
	sc := bufio.NewScanner(&out)
	sc.Buffer(make([]byte, 1<<20), 1<<26)
	for sc.Scan() {
		var line struct {
			Kind    string
			Relpath string
			Error   string
			Record  *struct {
				SyntaxErrors []int `json:"syntax_errors"`
				Funcs        []struct {
					Sites []struct {
						Kind   string `json:"kind"`
						Offset int    `json:"offset"`
					} `json:"sites"`
				} `json:"funcs"`
				Conversions []int `json:"conversions"`
			}
		}
		if err := json.Unmarshal(sc.Bytes(), &line); err != nil {
			t.Fatal(err)
		}
		if line.Kind != "file" {
			continue
		}
		if line.Error != "" || line.Record == nil || len(line.Record.SyntaxErrors) > 0 {
			t.Fatalf("extract: %s: %s", line.Relpath, line.Error)
		}
		m := map[int]string{}
		for _, f := range line.Record.Funcs {
			for _, s := range f.Sites {
				m[s.Offset] = s.Kind
			}
		}
		for _, off := range line.Record.Conversions { // calls of a type literal (R53)
			m[off] = "conversion"
		}
		sites[line.Relpath] = m
	}
	return sites
}

// TestJoinWithExtract checks the C2/C3 join: every extract site has exactly
// one resolve record (site, conversion or notcall) at the same offset, every
// extract conversion by syntax (R53) a conversion record, and resolve reports
// nothing that extract does not enumerate.
func TestJoinWithExtract(t *testing.T) {
	r := cls(t)
	ex := extractSites(t, clsDir)
	checked := 0
	for rel, m := range ex {
		for off, kind := range m {
			checked++
			s := r.sites[rel][off]
			n := 0
			if s != nil {
				n++
				if s.SiteKind != kind {
					t.Errorf("%s:%d: site kind %s, extract says %s", rel, off, s.SiteKind, kind)
				}
			}
			if r.convs[rel][off] {
				n++
			}
			if r.notcalls[rel][off] {
				n++
			}
			if n != 1 {
				t.Errorf("%s:%d (%s): %d resolve records, want 1", rel, off, kind, n)
			}
			if kind == "conversion" && !r.convs[rel][off] {
				t.Errorf("%s:%d: a conversion by syntax that resolve does not call a conversion", rel, off)
			}
		}
	}
	for rel, m := range r.sites {
		for off := range m {
			if _, ok := ex[rel][off]; !ok {
				t.Errorf("%s:%d: resolve site unknown to extract", rel, off)
			}
		}
	}
	for _, recs := range []map[string]map[int]bool{r.convs, r.notcalls} {
		for rel, m := range recs {
			for off := range m {
				if _, ok := ex[rel][off]; !ok {
					t.Errorf("%s:%d: resolve record unknown to extract", rel, off)
				}
			}
		}
	}
	if checked < 60 {
		t.Errorf("only %d extract sites checked", checked)
	}
}
