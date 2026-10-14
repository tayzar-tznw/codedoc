package q

import (
	"context"

	"example.com/unk/api"
)

func Serve(h api.Handler) error { return h.Handle(context.Background()) }

func ServeNamed(n api.Named) error { return n.Handle(context.Background()) }

func UseReady(x api.Ready) (*api.Manager, error) { return x.Get() }
