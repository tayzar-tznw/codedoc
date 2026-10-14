SITES = [
    dict(id="VN01", token="New", expect=["github.com/google/uuid.New"], cat="vendor", oracle="spec", note="vendored third-party call"),
    dict(id="VN02", marker="VN01", token="Version", expect=["github.com/google/uuid.UUID.Version"], cat="vendor", oracle="spec", note="vendored method"),
]
