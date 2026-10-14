package y

func a() {}
func b() {}

func Serve(ch chan int, started chan bool) {
	defer a()
	if ch != nil {
		defer b()
		started <- true
		<-ch
	}
}
