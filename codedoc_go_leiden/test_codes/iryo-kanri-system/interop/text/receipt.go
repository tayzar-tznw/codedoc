package text

import "fmt"

type ReceiptFormatter struct{}

func (ReceiptFormatter) Format(yen int) string {
	return fmt.Sprintf("[連携] %d円", yen)
}
