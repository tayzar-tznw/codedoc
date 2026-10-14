package q

type RW interface{ Read() int; Write() int }

type F struct{}

func (F) Read() int  { return 1 }
func (F) Write() int { return 2 }

func Use(rw RW) int {
	n := rw.Write()
	return n
}
