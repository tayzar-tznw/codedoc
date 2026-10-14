package text

import "fmt"

type ReceiptFormatter struct{}

func (ReceiptFormatter) Format(yen int) string {
	return fmt.Sprintf("[会計] %d円", yen)
}
