package main

import (
	"strings"

	"github.com/google/uuid"
)

func thirdParty() {
	_ = uuid.New()                   //@TP01
	u := uuid.Must(uuid.NewRandom()) //@TP02
	_ = u.String()                   //@TP03
	_ = strings.ToUpper("x")         //@SL01
	var sb strings.Builder
	sb.WriteString("x") //@SL02
}
