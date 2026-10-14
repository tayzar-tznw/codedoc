package rezept

import "fmt"

type ReceiptFormatter struct{}

func (ReceiptFormatter) Format(yen int) string {
	return fmt.Sprintf("[旧レセ] %d円", yen)
}
