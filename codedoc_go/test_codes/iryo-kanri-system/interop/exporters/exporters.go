package exporters

import "strings"

type Exporter interface {
	Export(rows []string) string
}

type CsvExporter struct{}

func (CsvExporter) Export(rows []string) string {
	return strings.Join(rows, ",")
}

type XmlExporter struct {
	Root string
}

func (x XmlExporter) Export(rows []string) string {
	var sb strings.Builder
	sb.WriteString("<" + x.Root + ">")
	for _, r := range rows {
		sb.WriteString("<row>" + r + "</row>")
	}
	sb.WriteString("</" + x.Root + ">")
	return sb.String()
}

func ExportAll(es []Exporter, rows []string) []string {
	var out []string
	for _, e := range es {
		out = append(out, e.Export(rows))
	}
	return out
}

type Archiver interface {
	ArchiveAll(year int) error
}

func ArchiveIfAny(a Archiver, year int) error {
	if a == nil {
		return nil
	}
	return a.ArchiveAll(year)
}
