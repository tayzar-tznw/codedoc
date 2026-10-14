package iface

type fakeClock struct{}

func (fakeClock) Now() int64 { return 0 }

type syncer interface{ synced() bool }

func (OnlyImpl) synced() bool { return true }

func useSyncer(s syncer) bool { return s.synced() }

func (A) Flush() {}
