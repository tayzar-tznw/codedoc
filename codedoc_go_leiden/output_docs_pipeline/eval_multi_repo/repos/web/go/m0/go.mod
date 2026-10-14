module example.com/iryo/web

go 1.26

require (
	example.com/iryo/shared v1.0.0
	example.com/iryo/textkit v1.0.0
	example.com/thirdparty/uuidlite v1.0.0
)

// Peer substitutions added by codedoc_go (spec section 8.2).
replace example.com/iryo/shared => /home/admin/dev/code/codedoc_go_leiden/test_codes/multi_repo/shared
