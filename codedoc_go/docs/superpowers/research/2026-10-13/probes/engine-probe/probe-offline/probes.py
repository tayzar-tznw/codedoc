SITES = [
    dict(id="OF01", token="Info", expect=["github.com/go-logr/logr.Logger.Info"], cat="offline", oracle="trace", note="hazard (a): depth-1 embedded logr.Logger vs depth-2 in-repo Deep.Info"),
    dict(id="OF02", token="Enabled", expect=["github.com/go-logr/logr.Logger.Enabled"], cat="offline", oracle="trace", note="hazard (a), second method"),
    dict(id="OF03", token="Info", expect=["github.com/go-logr/logr.(*Logger).Info", "github.com/go-logr/logr.Logger.Info"], cat="offline", oracle="trace", note="hazard (a) with *logr.Logger embedded"),
    dict(id="OF04", token="Name", occ=3, expect=["example.com/off.ByUUID.Name"], cat="offline", oracle="trace", note="hazard (b): interface method with missing-module param type"),
    dict(id="OF05", token="Flush", expect=["example.com/off.FullSink.Flush"], cat="offline", oracle="trace", note="hazard (b'): interface embeds missing-module interface"),
    dict(id="OF06", token="NewString", expect=["github.com/google/uuid.NewString"], cat="offline", oracle="spec", note="third-party call"),
    dict(id="OF07", token="Info", expect=["github.com/go-logr/logr.Logger.Info"], cat="offline", oracle="spec", note="method on missing-module type"),
    dict(id="OF08", token="ID", expect=["example.com/off.ID"], cat="offline", oracle="spec", note="plain in-repo call in a package with missing imports"),
]
