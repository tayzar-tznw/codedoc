package resolve

// JSON records of `gohelper resolve` (C2). Field order follows the contract;
// every field is always present (no omitempty), so absent values are null.

// Target is a declaration: the position of its name plus its identity.
type Target struct {
	Path     string `json:"path"`
	Line     int    `json:"line"`
	Col      int    `json:"col"`
	Name     string `json:"name"`
	Qualname string `json:"qualname"` // F, T.M, I.M (R37); "" for closures
	Pkg      string `json:"pkg"`
	Module   string `json:"module"`
	Version  string `json:"version"`
	Obj      string `json:"obj"` // func|method|iface_method|var|type|builtin|closure
	Recv     string `json:"recv"`
}

// TypeRef names a type declaration in embed and implements records.
type TypeRef struct {
	Path string `json:"path"`
	Line int    `json:"line"`
	Col  int    `json:"col"`
	Name string `json:"name"`
	Pkg  string `json:"pkg"`
}

// Candidate is an in-repo implementer's method for an interface call.
type Candidate struct {
	Path             string `json:"path"`
	Line             int    `json:"line"`
	Col              int    `json:"col"`
	Name             string `json:"name"`
	Qualname         string `json:"qualname"` // T.M, or I.M for a method of an embedded interface (R37)
	Pkg              string `json:"pkg"`      // package of the method declaration
	Type             string `json:"type"`     // the implementing type's name
	Pointer          bool   `json:"pointer"`
	TestOnly         bool   `json:"test_only"`
	ViaEmbeddedIface bool   `json:"via_embedded_iface"`
}

// IfaceInfo describes the dispatch of an iface or typeparam site.
type IfaceInfo struct {
	RecvType   string       `json:"recv_type"`
	Candidates []*Candidate `json:"candidates"`
	OpenWorld  bool         `json:"open_world"`
	Suppressed bool         `json:"suppressed"` // G4: no candidate search at all
}

type fileRecord struct {
	Kind     string   `json:"kind"`
	Path     string   `json:"path"`
	SHA256   string   `json:"sha256"`
	Packages []string `json:"packages"`
}

type siteRecord struct {
	Kind          string     `json:"kind"`
	Path          string     `json:"path"`
	Offset        int        `json:"offset"`
	Line          int        `json:"line"`
	Col           int        `json:"col"`
	EndCol        int        `json:"end_col"`
	Name          string     `json:"name"`
	SiteKind      string     `json:"site_kind"`
	Shape         string     `json:"shape"`
	Class         string     `json:"class"`
	Target        *Target    `json:"target"`
	PromotedDepth int        `json:"promoted_depth"`
	EmbedInvalid  bool       `json:"embed_invalid"`
	Stub          bool       `json:"stub"`
	PkgErrors     bool       `json:"pkg_errors"`
	ValueKind     *string    `json:"value_kind"`
	Alias         *Target    `json:"alias"`
	Linkname      *string    `json:"linkname"`
	Asm           bool       `json:"asm"`
	Iface         *IfaceInfo `json:"iface"`
	Variants      []*Target  `json:"variants"`
	Build         string     `json:"build"`
}

type conversionRecord struct {
	Kind   string `json:"kind"`
	Path   string `json:"path"`
	Offset int    `json:"offset"`
}

type notcallRecord struct {
	Kind     string `json:"kind"`
	Path     string `json:"path"`
	Offset   int    `json:"offset"`
	SiteKind string `json:"site_kind"`
}

type embedRecord struct {
	Kind     string   `json:"kind"`
	Type     *TypeRef `json:"type"`
	Embedded *Target  `json:"embedded"`
	Pointer  bool     `json:"pointer"`
}

type implementsRecord struct {
	Kind            string   `json:"kind"`
	Type            *TypeRef `json:"type"`
	Iface           *TypeRef `json:"iface"`
	PointerReceiver bool     `json:"pointer_receiver"`
	Incomplete      bool     `json:"incomplete"` // the pair's method facts are not sound: not edged
}

type importRecord struct {
	Kind       string  `json:"kind"`
	Path       string  `json:"path"`
	Offset     int     `json:"offset"`
	Line       int     `json:"line"`
	Col        int     `json:"col"`
	ImportPath string  `json:"import_path"`
	TargetDir  string  `json:"target_dir"`
	Module     string  `json:"module"`
	Version    string  `json:"version"`
	Std        bool    `json:"std"`
	Error      *string `json:"error"`
}

type errorRecord struct {
	Kind string `json:"kind"`
	Pkg  string `json:"pkg"`
	Path string `json:"path"`
	Line int    `json:"line"`
	Msg  string `json:"msg"`
}

type summaryRecord struct {
	Kind         string  `json:"kind"`
	Packages     int     `json:"packages"`
	Errors       int     `json:"errors"`
	LoadSeconds  float64 `json:"load_seconds"`
	Sites        int     `json:"sites"`
	Conversions  int     `json:"conversions"`
	Notcalls     int     `json:"notcalls"`
	PeakRSSBytes int64   `json:"peak_rss_bytes"`
	Seconds      float64 `json:"seconds"`
}

func strp(s string) *string { return &s }
