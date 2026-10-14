SITES = [
    dict(id="RP01", token="Do", expect=["example.com/rlib.Do"], cat="module", oracle="spec", note="replace => ../rlib, no go.work"),
    dict(id="RP02", token="Run", expect=["example.com/rlib.Svc.Run"], cat="module", oracle="spec", note="method across replace"),
    dict(id="RP03", token="helper", expect=["example.com/rother.helper"], cat="module", oracle="spec", note="third module, not referenced by rmain"),
]
