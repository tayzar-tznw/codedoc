package extract

// The records below are C3 (entities.json FileRecord, version 1). Positions
// are taken with PositionFor(p, false), so //line directives are ignored:
// lines are 1-based, columns are 1-based byte columns, offsets are 0-based
// bytes. A name range is [line, col, end_col) on one line.

// FileRecord is one file's entities.
type FileRecord struct {
	SHA256          string         `json:"sha256"`
	Module          string         `json:"module"`  // module path; "" under no go.mod
	Package         string         `json:"package"` // import path; xtest files get "_test"
	PackageName     string         `json:"package_name"`
	Origin          string         `json:"origin"` // package, command, test, xtest
	Generated       bool           `json:"generated"`
	BuildConstraint string         `json:"build_constraint"`
	BuildIncluded   bool           `json:"build_included"`
	SyntaxErrors    []int          `json:"syntax_errors"`
	Imports         []ImportRecord `json:"imports"`
	Types           []TypeRecord   `json:"types"`
	Funcs           []*FuncRecord  `json:"funcs"`
	// Conversions are the calls of a type literal in Funcs' bodies and
	// initializers: conversions by syntax, never sites (spec R53; additive to
	// C3). Each is the byte offset of the call's Lparen, the key resolve's
	// conversion record has, in increasing order.
	Conversions []int `json:"conversions"`
}

// ImportRecord is one import spec; the position is its path literal's.
type ImportRecord struct {
	Path   string `json:"path"`
	Alias  string `json:"alias"`
	Kind   string `json:"kind"` // normal, dot, blank
	Line   int    `json:"line"`
	Col    int    `json:"col"`
	Offset int    `json:"offset"`
}

// TypeRecord is a package-level type declaration, or the file's <package>
// pseudo-type.
type TypeRecord struct {
	Name       string         `json:"name"`
	Qualname   string         `json:"qualname"`
	Kind       string         `json:"kind"` // struct, interface, named, alias, package
	TypeParams string         `json:"type_params"`
	Embeds     []EmbedRecord  `json:"embeds"`
	Methods    []MethodRecord `json:"methods"` // interface methods
	NameRange  [3]int         `json:"name_range"`
	StartLine  int            `json:"start_line"`
	EndLine    int            `json:"end_line"`
	Doc        string         `json:"doc"`
}

// EmbedRecord is an embedded struct field or interface element; the
// position is the type name's.
type EmbedRecord struct {
	Text    string `json:"text"`
	Line    int    `json:"line"`
	Col     int    `json:"col"`
	Pointer bool   `json:"pointer"`
}

// MethodRecord is an interface method. Signature, doc and lines are
// additive to C3 (they fill the Methods columns of interface methods).
type MethodRecord struct {
	Name      string `json:"name"`
	Qualname  string `json:"qualname"`
	NameRange [3]int `json:"name_range"`
	Signature string `json:"signature"`
	Doc       string `json:"doc"`
	StartLine int    `json:"start_line"`
	EndLine   int    `json:"end_line"`
}

// FuncRecord is a function, method, init function, closure or the file's
// <init> pseudo-function.
type FuncRecord struct {
	Name       string          `json:"name"`
	Qualname   string          `json:"qualname"`
	MemberKind string          `json:"member_kind"` // func, method, init, closure, var_init
	Receiver   *ReceiverRecord `json:"receiver"`
	TypeParams string          `json:"type_params"`
	Signature  string          `json:"signature"`
	HasBody    bool            `json:"has_body"`
	Linkname   *string         `json:"linkname"`
	NameRange  [3]int          `json:"name_range"`
	StartLine  int             `json:"start_line"`
	EndLine    int             `json:"end_line"`
	Doc        string          `json:"doc"`
	Parent     *string         `json:"parent"`
	Sites      []SiteRecord    `json:"sites"`
}

// ReceiverRecord is a method's receiver. OwnerRelpath is the file declaring
// the base type in the same directory and package clause (spec R18). When no
// such file is listed, it is the method's own file, whose <package> owns the
// method, and Unresolved is true.
type ReceiverRecord struct {
	Text         string  `json:"text"`
	Base         string  `json:"base"`
	Pointer      bool    `json:"pointer"`
	OwnerRelpath *string `json:"owner_relpath"`
	Unresolved   bool    `json:"receiver_unresolved"`
}

// SiteRecord is a Site of the function that runs it.
type SiteRecord struct {
	Kind     string  `json:"kind"`
	Name     string  `json:"name"`
	Line     int     `json:"line"`
	Col      int     `json:"col"`
	EndCol   int     `json:"end_col"`
	Offset   int     `json:"offset"`
	Shape    string  `json:"shape"`
	Receiver *string `json:"receiver"`
	Caller   string  `json:"caller"`
}
