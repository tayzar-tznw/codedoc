package newgo

func A() int { return B() } //@NG01

func B() int { return 1 }
