package billing

import "fmt"

type Session struct {
	Desk    string
	Total   int
	entries []string
}

func Open(desk string) *Session {
	return &Session{Desk: desk}
}

func (s *Session) Charge(yen int) {
	s.Total += yen
	s.entries = append(s.entries, fmt.Sprintf("課金 %d", yen))
}

func (s *Session) Close() string {
	return fmt.Sprintf("%s 合計=%d円 明細=%d件", s.Desk, s.Total, len(s.entries))
}
