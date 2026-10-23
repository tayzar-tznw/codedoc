"""Focused tests for the Java declaration index and call-site extractor."""

import graph_generator.java_decls as java_decls
from graph_generator.java_decls import (
    build_decl_index,
    extract_call_sites,
    utf16_col,
)


def _by_symbol(index):
    return {decl.symbol: decl for decl in index.all_decls()}


def _span_text(source: str, span: tuple[int, int, int, int]) -> str:
    lines = source.splitlines(keepends=True)
    start_line, start_col, end_line, end_col = span
    start = sum(len(line.encode("utf-8")) for line in lines[:start_line]) + start_col
    end = sum(len(line.encode("utf-8")) for line in lines[:end_line]) + end_col
    return source.encode("utf-8")[start:end].decode("utf-8")


def test_nested_symbols_generics_constructors_and_parameter_spelling():
    source = """\
package com.acme.deep;
class Outer<T> {
    static class Nested<K, V> {
        Nested(java.lang.String label) {}
        <X> void transform(
            X value, int[] counts, String labels[], String... rest) {}
    }
    class Inner {}
}
class Plain {}
"""
    path = "/src/Outer.java"
    index = build_decl_index({path: source}, artifact="demo")
    symbols = _by_symbol(index)

    outer = "com.acme.deep.Outer<T>"
    nested = f"{outer}$Nested<K,V>"
    assert outer in symbols
    assert nested in symbols
    assert f"{outer}$Inner" in symbols
    assert f"{nested}.<init>(String)" in symbols
    assert (
        f"{nested}.transform<X>(X, int[], String[], String...)"
        in symbols
    )
    assert index.ctors_of("com.acme.deep.Plain") == []
    assert index.type_decl(nested) is symbols[nested]
    assert index.aliases == {}


def test_record_synthesizes_canonical_constructor_and_accessors_with_spans():
    source = (
        "package money; "
        "record Money(java.math.BigDecimal amount, String currency) {}"
    )
    path = "/src/Money.java"
    index = build_decl_index({path: source}, artifact="domain")
    symbols = _by_symbol(index)

    type_symbol = "money.Money"
    ctor = symbols[f"{type_symbol}.<init>(BigDecimal, String)"]
    amount = symbols[f"{type_symbol}.amount()"]
    currency = symbols[f"{type_symbol}.currency()"]

    assert symbols[type_symbol].type_kind == "record"
    assert _span_text(source, ctor.span) == (
        "record Money(java.math.BigDecimal amount, String currency)"
    )
    assert _span_text(source, amount.span) == "amount"
    assert _span_text(source, currency.span) == "currency"
    assert ctor.line == amount.line == currency.line == 1


def test_record_explicit_compact_canonical_and_accessor_override_synthesis():
    source = """\
package money;
record Compact(int value) {
    Compact { if (value < 0) throw new IllegalArgumentException(); }
    public int value() { return value; }
}
record Canonical(String text) {
    Canonical(String text) { this.text = text; }
}
"""
    path = "/src/Records.java"
    index = build_decl_index({path: source}, artifact="domain")

    compact_ctors = index.lookup("money.Compact.<init>(int)")
    compact_accessors = index.lookup("money.Compact.value()")
    canonical_ctors = index.lookup("money.Canonical.<init>(String)")
    canonical_accessors = index.lookup("money.Canonical.text()")

    assert len(compact_ctors) == 1
    assert _span_text(source, compact_ctors[0].span).startswith("Compact {")
    assert compact_ctors[0].has_body is True

    assert len(compact_accessors) == 1
    assert _span_text(source, compact_accessors[0].span).startswith(
        "public int value()"
    )
    assert compact_accessors[0].has_body is True

    assert len(canonical_ctors) == 1
    assert _span_text(source, canonical_ctors[0].span).startswith(
        "Canonical(String text)"
    )
    assert canonical_ctors[0].has_body is True

    assert len(canonical_accessors) == 1
    assert _span_text(source, canonical_accessors[0].span) == "text"


def test_interface_method_bodies_and_enum_members():
    source = """\
package api;
interface Operations {
    void abstractCall();
    default void defaultCall() {}
    static void staticCall() {}
    private void privateCall() {}
}
enum Mode {
    FAST, SAFE(1);
    Mode() {}
    Mode(int code) {}
    void apply() {}
}
"""
    index = build_decl_index({"/src/Api.java": source}, artifact="api")
    symbols = _by_symbol(index)

    assert symbols["api.Operations.abstractCall()"].has_body is False
    assert symbols["api.Operations.defaultCall()"].has_body is True
    assert symbols["api.Operations.staticCall()"].has_body is True
    assert symbols["api.Operations.privateCall()"].has_body is True

    assert "api.Mode.<init>()" in symbols
    assert "api.Mode.<init>(int)" in symbols
    assert "api.Mode.apply()" in symbols
    assert not any(decl.name in {"FAST", "SAFE"} for decl in index.all_decls())


def test_superclass_and_super_interfaces_are_kept_separate_and_raw():
    source = """\
package shapes;
class Child<T> extends base.Parent<T> implements api.Left, api.Right<T> {}
interface Combined extends api.Left, api.Right<String> {}
record Value(int n) implements api.Left {}
enum Choice implements api.Right<Integer> { ONE }
"""
    index = build_decl_index({"/src/Shapes.java": source}, artifact="shapes")
    symbols = _by_symbol(index)

    child = symbols["shapes.Child<T>"]
    assert child.superclass == ["base.Parent<T>"]
    assert child.super_interfaces == ["api.Left", "api.Right<T>"]
    assert child.base_types == [
        "base.Parent<T>",
        "api.Left",
        "api.Right<T>",
    ]

    combined = symbols["shapes.Combined"]
    assert combined.superclass == []
    assert combined.super_interfaces == ["api.Left", "api.Right<String>"]
    assert symbols["shapes.Value"].super_interfaces == ["api.Left"]
    assert symbols["shapes.Choice"].super_interfaces == [
        "api.Right<Integer>"
    ]


def test_qualified_generic_parameter_normalization():
    source = """\
package normal;
class Types {
    void accept(
        java.lang.String text,
        java.util.List<java.lang.String> names,
        java.util.Map<java.lang.String, java.util.List<pkg.Item>> lookup) {}
}
"""
    index = build_decl_index({"/src/Types.java": source}, artifact="normal")
    expected = (
        "normal.Types.accept("
        "String, List<String>, Map<String, List<Item>>)"
    )
    assert index.lookup(expected)


def test_utf16_round_trip_with_japanese_identifier_and_astral_character():
    source = (
        'package jp.test; class 診療 { void 処理() { '
        'String note = "\U00029E3D外来"; 処理(); } }'
    )
    path = "/src/診療.java"
    index = build_decl_index({path: source}, artifact="jp")
    assert index.lookup("jp.test.診療.処理()")

    site = next(site for site in extract_call_sites(path, source) if site.token == "処理")
    codepoint_col = source.rindex("処理")
    assert site.col == utf16_col(source, codepoint_col)
    assert site.col == codepoint_col + 1

    byte_col = index._char16_to_byte(path, 0, site.col)
    assert byte_col == len(source[:codepoint_col].encode("utf-8"))
    enclosing = index.enclosing_member(path, 0, site.col)
    assert enclosing is not None
    assert enclosing.symbol == "jp.test.診療.処理()"


def test_anonymous_and_local_members_are_not_indexed_but_calls_stay_enclosed():
    source = """\
package app;
class Host {
    void named() {
        Runnable task = new Runnable() {
            public void run() { helper(); }
        };
        class Local {
            void hidden() { localCall(); }
        }
    }
    void helper() {}
}
"""
    path = "/src/Host.java"
    index = build_decl_index({path: source}, artifact="app")
    symbols = {decl.symbol for decl in index.all_decls()}

    assert "app.Host.named()" in symbols
    assert "app.Host.helper()" in symbols
    assert not any(".run(" in symbol or ".hidden(" in symbol for symbol in symbols)
    assert not any("$Local" in symbol for symbol in symbols)

    sites = extract_call_sites(path, source)
    for token in ("helper", "localCall"):
        site = next(site for site in sites if site.token == token)
        enclosing = index.enclosing_member(path, site.line - 1, site.col)
        assert enclosing is not None
        assert enclosing.symbol == "app.Host.named()"


def test_invocations_receiver_text_and_constructor_chain_flag(monkeypatch):
    source = """\
class Calls extends Base {
    Calls() { this(1); }
    Calls(int value) { super(); }
    void run() {
        staticImported();
        service.work(1);
        Util.<String>make();
    }
}
"""
    path = "/src/Calls.java"
    sites = extract_call_sites(path, source)
    by_token = {site.token: site for site in sites if site.site_kind == "invocation"}

    assert by_token["this"].arg_count == 1
    assert by_token["super"].arg_count == 0
    assert by_token["staticImported"].receiver is None
    assert by_token["staticImported"].receiver_text is None
    assert by_token["work"].receiver is not None
    assert by_token["work"].receiver_text == "service"
    assert by_token["work"].arg_count == 1
    assert by_token["make"].receiver_text == "Util"
    assert by_token["make"].col == utf16_col(
        source.splitlines()[6], source.splitlines()[6].index("make")
    )

    monkeypatch.setattr(java_decls, "EMIT_CTOR_CHAIN_SITES", False)
    without_chains = extract_call_sites(path, source)
    assert not any(
        site.site_kind == "invocation" and site.token in {"this", "super"}
        for site in without_chains
    )
    assert any(site.token == "work" for site in without_chains)


def test_object_creation_anchors_type_including_qualified_and_anonymous_forms():
    source = """\
class Factory {
    void make(Outer outer) {
        new pkg.Box<String>(1);
        outer.new Inner();
        new Notifier() { public void notifyNow() { inside(); } };
    }
}
"""
    path = "/src/Factory.java"
    sites = [
        site
        for site in extract_call_sites(path, source)
        if site.site_kind == "object_creation"
    ]
    assert [(site.token, site.arg_count) for site in sites] == [
        ("Box", 1),
        ("Inner", 0),
        ("Notifier", 0),
    ]

    lines = source.splitlines()
    for site in sites:
        assert site.col == utf16_col(
            lines[site.line - 1], lines[site.line - 1].index(site.token)
        )


def test_di_class_literals_require_exactly_two_literal_arguments():
    source = """\
class Wiring {
    void configure(Class<?> service, Class<?> impl) {
        services.addSingleton(api.Service.class, impl.ServiceImpl.class);
        services.addScoped(ScopedService.class, ScopedImpl.class);
        services.addTransient(TransientService.class, TransientImpl.class);
        services.addSingleton(OnlyOne.class);
        services.addSingleton(FactoryService.class, () -> new FactoryImpl());
        services.addSingleton(A.class, B.class, C.class);
        services.addSingleton(service, impl);
    }
}
"""
    path = "/src/Wiring.java"
    literal_sites = [
        site
        for site in extract_call_sites(path, source)
        if site.site_kind == "class_literal"
    ]

    assert [site.token for site in literal_sites] == [
        "Service",
        "ServiceImpl",
        "ScopedService",
        "ScopedImpl",
        "TransientService",
        "TransientImpl",
    ]
    assert [site.ta_role for site in literal_sites] == [
        "service",
        "impl",
        "service",
        "impl",
        "service",
        "impl",
    ]
    assert [site.role for site in literal_sites] == [
        "service",
        "impl",
        "service",
        "impl",
        "service",
        "impl",
    ]
    assert [site.di_method for site in literal_sites] == [
        "addSingleton",
        "addSingleton",
        "addScoped",
        "addScoped",
        "addTransient",
        "addTransient",
    ]

    groups = {site.ta_group for site in literal_sites}
    assert len(groups) == 3
    assert all(group is not None and group.startswith(f"{path}:") for group in groups)
    for group in groups:
        pair = [site for site in literal_sites if site.ta_group == group]
        assert [site.ta_index for site in pair] == [0, 1]


def test_method_references_anchor_method_or_constructor_type():
    source = """\
class References {
    void use() {
        Object a = Type::method;
        Object b = expression::method;
        Object c = pkg.Logger::new;
        Object d = String[]::new;
        Object e = Type::<String>generic;
    }
}
"""
    path = "/src/References.java"
    sites = [
        site
        for site in extract_call_sites(path, source)
        if site.site_kind == "method_reference"
    ]
    assert [(site.token, source.splitlines()[site.line - 1][site.col:]) for site in sites] == [
        ("method", "method;"),
        ("method", "method;"),
        ("new", "Logger::new;"),
        ("new", "String[]::new;"),
        ("generic", "generic;"),
    ]


def test_decompiled_stub_signatures_and_compiled_from_header_are_tolerated():
    source = """\
// compiled from Stub.java -- decompiler header junk
package binary.api;
public class Stub<T> extends base.Parent implements api.Face {
    public Stub();
    public Stub(java.lang.String value);
    public java.util.List<java.lang.String> load(java.lang.String[] names);
    public void empty() {}
}
"""
    path = "/decompiled/Stub.java"
    index = build_decl_index({path: source}, artifact="binary-api")
    symbols = _by_symbol(index)

    assert "binary.api.Stub<T>.<init>()" in symbols
    assert "binary.api.Stub<T>.<init>(String)" in symbols
    assert "binary.api.Stub<T>.load(String[])" in symbols
    assert symbols["binary.api.Stub<T>.load(String[])"].has_body is False
    assert symbols["binary.api.Stub<T>.empty()"].has_body is True

    stub_type = symbols["binary.api.Stub<T>"]
    assert stub_type.superclass == ["base.Parent"]
    assert stub_type.super_interfaces == ["api.Face"]

    line0 = 3
    line = source.splitlines()[line0]
    position = utf16_col(line, line.index("Stub"))
    decl = index.decl_at(path, line0, position)
    assert decl is not None
    assert decl.symbol == "binary.api.Stub<T>.<init>()"
