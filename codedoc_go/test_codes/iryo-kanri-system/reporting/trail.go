package reporting

type Sink interface {
	Write(line string) int
}

type MemorySink struct {
	Lines []string
}

func (m *MemorySink) Write(line string) int {
	m.Lines = append(m.Lines, line)
	return len(m.Lines)
}

type AuditTrail struct {
	Sink
	Name string
}

func (t AuditTrail) Note(msg string) int {
	return t.Write(t.Name + ": " + msg)
}

type Journal interface {
	Write(line string) int
}

func Publish(j Journal, msg string) int {
	return j.Write("[公開] " + msg)
}
