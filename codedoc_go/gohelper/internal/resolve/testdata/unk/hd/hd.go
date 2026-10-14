package hd

import xctx "example.com/missing/ctx"

type T struct{}

func (T) Handle(ctx xctx.Context) error { return nil }

type U struct{}

func (U) Handle(ctx xctx.Context) error { return nil }

func (U) Name() int { return 0 }
