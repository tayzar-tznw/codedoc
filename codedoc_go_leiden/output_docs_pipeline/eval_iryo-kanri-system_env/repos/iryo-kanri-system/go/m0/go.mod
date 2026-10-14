module example.com/iryo/admin-console

go 1.26

require (
	example.com/iryo/billing v0.0.0
	example.com/iryo/domain v0.0.0
	example.com/iryo/kernel v0.0.0
	example.com/iryo/pharmacy v0.0.0
	example.com/iryo/reporting v0.0.0
	example.com/thirdparty/textfmt v1.1.0
)

replace (
	example.com/iryo/billing => ../billing
	example.com/iryo/domain => ../domain
	example.com/iryo/kernel => ../kernel
	example.com/iryo/pharmacy => ../pharmacy
	example.com/iryo/reporting => ../reporting
)
