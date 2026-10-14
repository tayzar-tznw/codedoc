package idkit

import "example.com/thirdparty/uuidlite"

type PatientIDs struct {
	seq *uuidlite.Sequence
}

func NewPatientIDs(seed uint64) *PatientIDs {
	return &PatientIDs{seq: uuidlite.NewSequence(seed)}
}

func (p *PatientIDs) Issue() string {
	id := uuidlite.NewFrom(p.seq)
	return "P-" + id.String()[28:]
}

func Canonical(raw string) (string, error) {
	u, err := uuidlite.Parse(raw)
	if err != nil {
		return "", err
	}
	return u.String() + "/" + u.Short(), nil
}
