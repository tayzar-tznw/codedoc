package money

import "strconv"

func Legacy(yen int64) string {
	return strconv.FormatInt(yen, 10) + " yen"
}
