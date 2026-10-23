"""Java declaration index and call-site extraction for semantic resolution.

The public data model intentionally mirrors the declaration index this
pipeline grew for .NET so the resolution layer can switch parsers without
growing a second indexing API. Java has no import-alias equivalent:
``DeclIndex.aliases`` is retained only as an always-empty compatibility
mapping.

Symbols follow the Java ``SYMBOL_GRAMMAR``:

* nested types use ``$`` and retain their declared type parameters;
* constructors use ``<init>``;
* member parameter types are simple-name normalized while arrays, varargs,
  wildcards, and generic arguments retain their source spelling.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import tree_sitter as ts
import tree_sitter_java

_LANGUAGE = ts.Language(tree_sitter_java.language())

_TYPE_DECL_KINDS = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "record_declaration": "record",
    "annotation_type_declaration": "annotation",
}

_DI_METHODS = {"addSingleton", "addScoped", "addTransient"}

# Kept as a spike-controlled switch.  Explicit constructor invocations are
# useful call sites, but resolution may disable them if a future jdtls version
# cannot bind them reliably.
EMIT_CTOR_CHAIN_SITES = True


def utf16_col(line: str, cp_index: int) -> int:
    """Code-point index in a line -> UTF-16 code-unit column (LSP)."""
    return len(line[:cp_index].encode("utf-16-le")) // 2


def _parse(content_bytes: bytes) -> ts.Tree:
    parser = ts.Parser(_LANGUAGE)
    return parser.parse(content_bytes)


def _text(node: ts.Node) -> str:
    return node.text.decode("utf-8", "replace") if node.text is not None else ""


def _child_by_field(node: ts.Node, field_name: str) -> ts.Node | None:
    """Field lookup that also works for recovery ``ERROR`` nodes."""
    child = node.child_by_field_name(field_name)
    if child is not None:
        return child
    for index, candidate in enumerate(node.children):
        if node.field_name_for_child(index) == field_name:
            return candidate
    return None


def _byte_to_utf16(line_bytes: bytes, byte_col: int) -> int:
    prefix = line_bytes[:byte_col].decode("utf-8", "replace")
    return len(prefix.encode("utf-16-le")) // 2


@dataclass
class Decl:
    symbol: str
    kind: str            # type|method|constructor
    file: str
    line: int            # 1-based declaration (signature) line
    name: str
    container: str       # containing type symbol (types use their package)
    namespace: str       # Java package; name kept for resolution compatibility
    artifact: str        # project identity (Maven artifactId / jar label)
    params: list[str] = field(default_factory=list)
    span: tuple[int, int, int, int] = (0, 0, 0, 0)  # byte-based (sl, sc, el, ec)
    type_kind: str = ""  # for kind=="type": class|interface|enum|record|annotation
    base_types: list[str] = field(default_factory=list)
    has_body: bool = False
    superclass: list[str] = field(default_factory=list)
    super_interfaces: list[str] = field(default_factory=list)


@dataclass
class CallSite:
    file: str
    line: int             # 1-based, matches the ground-truth manifest
    col: int              # 0-based UTF-16 column of the anchor token
    token: str
    site_kind: str
    arg_count: int | None = None
    receiver: tuple[int, int] | None = None  # (line0, col16) of receiver name
    ta_role: str | None = None               # class_literal: service|impl
    ta_group: str | None = None              # groups one add*() registration
    ta_index: int | None = None
    di_method: str | None = None
    receiver_text: str | None = None

    @property
    def role(self) -> str | None:
        """Readable alias for the C#-compatible ``ta_role`` field."""
        return self.ta_role


class DeclIndex:
    """Declarations of one project, queryable by symbol and source position."""

    def __init__(self, artifact: str):
        self.artifact = artifact
        self._decls: list[Decl] = []
        self._by_file: dict[str, list[Decl]] = {}
        self._by_symbol: dict[str, list[Decl]] = {}
        self._lines: dict[str, list[str]] = {}
        # Java imports cannot alias types.  Keep the empty mapping so the
        # existing resolution bridge can remain a near drop-in during S9.
        self.aliases: dict[tuple[str, int], object] = {}

    # -- build ---------------------------------------------------------

    def add(self, decl: Decl):
        self._decls.append(decl)
        self._by_file.setdefault(decl.file, []).append(decl)
        self._by_symbol.setdefault(decl.symbol, []).append(decl)

    def set_lines(self, path: str, lines: list[str]):
        self._lines[path] = lines

    # -- query ---------------------------------------------------------

    def all_decls(self) -> list[Decl]:
        return list(self._decls)

    def lookup(self, symbol: str) -> list[Decl]:
        return self._by_symbol.get(symbol, [])

    def type_decl(self, type_symbol: str) -> Decl | None:
        for d in self._by_symbol.get(type_symbol, []):
            if d.kind == "type":
                return d
        return None

    def ctors_of(self, type_symbol: str) -> list[Decl]:
        return [
            d for d in self._decls
            if d.kind == "constructor" and d.container == type_symbol
        ]

    def members_of(self, type_symbol: str) -> list[Decl]:
        return [d for d in self._decls if d.container == type_symbol]

    def _char16_to_byte(self, path: str, line0: int, char16: int) -> int:
        lines = self._lines.get(path)
        if not lines or line0 >= len(lines):
            return char16
        line = lines[line0]
        units = 0
        for cp_i, ch in enumerate(line):
            if units >= char16:
                return len(line[:cp_i].encode("utf-8"))
            units += len(ch.encode("utf-16-le")) // 2
        return len(line.encode("utf-8"))

    def _decl_containing(
        self,
        path: str,
        line0: int,
        char16: int,
        want_member: bool,
    ) -> Decl | None:
        byte_col = self._char16_to_byte(path, line0, char16)
        best: Decl | None = None
        best_size: tuple[int, int] | None = None
        for d in self._by_file.get(path, []):
            if want_member and d.kind == "type":
                continue
            sl, sc, el, ec = d.span
            if (line0, byte_col) < (sl, sc) or (line0, byte_col) > (el, ec):
                continue
            size = (el - sl, ec if el != sl else ec - sc)
            if best_size is None or size < best_size:
                best, best_size = d, size
        return best

    def decl_at(self, path: str, line0: int, char16: int) -> Decl | None:
        """Innermost declaration (any kind) containing an LSP position."""
        return self._decl_containing(path, line0, char16, want_member=False)

    def enclosing_member(self, path: str, line0: int, char16: int) -> Decl | None:
        """Innermost non-type member containing a position (call attribution)."""
        return self._decl_containing(path, line0, char16, want_member=True)


# ---------------------------------------------------------------------
# type / parameter normalization (SYMBOL_GRAMMAR spellings)
# ---------------------------------------------------------------------


_SIMPLE_TYPE_NODES = {
    "type_identifier",
    "identifier",
    "integral_type",
    "floating_point_type",
    "boolean_type",
    "void_type",
}


def _rightmost_identifier(node: ts.Node | None) -> ts.Node | None:
    """Rightmost name token, excluding generic arguments."""
    if node is None:
        return None
    if node.type in ("identifier", "type_identifier"):
        return node
    if node.type == "generic_type":
        base = next(
            (c for c in node.named_children if c.type != "type_arguments"),
            None,
        )
        return _rightmost_identifier(base)
    if node.type == "array_type":
        elem = node.child_by_field_name("element")
        return _rightmost_identifier(elem)
    if node.type == "annotated_type":
        candidates = [
            c for c in node.named_children
            if c.type not in ("annotation", "marker_annotation")
        ]
        return _rightmost_identifier(candidates[-1] if candidates else None)

    named = [
        c for c in node.named_children
        if c.type not in ("type_arguments", "type_parameters")
    ]
    best = None
    for child in named:
        found = _rightmost_identifier(child)
        if found is not None:
            best = found
    return best


def _norm_type(node: ts.Node | None) -> str:
    if node is None:
        return ""
    t = node.type
    if t in _SIMPLE_TYPE_NODES:
        return _text(node)
    if t in ("scoped_type_identifier", "scoped_identifier"):
        name = _rightmost_identifier(node)
        return _text(name) if name is not None else _text(node).rsplit(".", 1)[-1]
    if t == "generic_type":
        base = next(
            (c for c in node.named_children if c.type != "type_arguments"),
            None,
        )
        args_node = next(
            (c for c in node.named_children if c.type == "type_arguments"),
            None,
        )
        args = (
            [_norm_type(c) for c in args_node.named_children]
            if args_node is not None
            else []
        )
        return f"{_norm_type(base)}<{', '.join(args)}>"
    if t == "array_type":
        elem = node.child_by_field_name("element")
        dims = node.child_by_field_name("dimensions")
        return _norm_type(elem) + (_text(dims) if dims is not None else "[]")
    if t == "annotated_type":
        candidates = [
            c for c in node.named_children
            if c.type not in ("annotation", "marker_annotation")
        ]
        return _norm_type(candidates[-1] if candidates else None)
    if t == "wildcard":
        bound = next(
            (
                c for c in node.named_children
                if c.type not in ("annotation", "marker_annotation")
            ),
            None,
        )
        if bound is None:
            return "?"
        keyword = next(
            (_text(c) for c in node.children if _text(c) in ("extends", "super")),
            "",
        )
        return f"? {keyword} {_norm_type(bound)}".strip()
    if t in ("union_type", "intersection_type"):
        sep = " | " if t == "union_type" else " & "
        return sep.join(_norm_type(c) for c in node.named_children)

    # The known Java type nodes above cover declaration signatures.  Keeping
    # this raw fallback makes the parser tolerant of newer grammar nodes.
    return _text(node)


def _param_name(param: ts.Node) -> ts.Node | None:
    name = param.child_by_field_name("name")
    if name is not None:
        return name
    declarator = next(
        (c for c in param.named_children if c.type == "variable_declarator"),
        None,
    )
    if declarator is not None:
        name = declarator.child_by_field_name("name")
        if name is not None:
            return name
        return _rightmost_identifier(declarator)
    return None


def _param_type(param: ts.Node) -> str:
    if param.type == "spread_parameter":
        type_node = next(
            (
                c for c in param.named_children
                if c.type
                not in (
                    "modifiers",
                    "annotation",
                    "marker_annotation",
                    "variable_declarator",
                    "dimensions",
                )
            ),
            None,
        )
        return _norm_type(type_node) + "..."

    type_node = param.child_by_field_name("type")
    value = _norm_type(type_node)
    # Java permits the array brackets after the parameter name: String s[].
    trailing_dims = [
        _text(c)
        for c in param.children
        if c.type == "dimensions"
        and (type_node is None or c.start_byte >= type_node.end_byte)
    ]
    return value + "".join(trailing_dims)


def _norm_params(param_list: ts.Node | None) -> list[str]:
    if param_list is None:
        return []
    return [
        _param_type(p)
        for p in param_list.named_children
        if p.type in ("formal_parameter", "spread_parameter")
    ]


def _type_params_suffix(node: ts.Node) -> str:
    params = node.child_by_field_name("type_parameters")
    if params is None:
        params = next(
            (c for c in node.named_children if c.type == "type_parameters"),
            None,
        )
    if params is None:
        return ""
    names: list[str] = []
    for p in params.named_children:
        if p.type != "type_parameter":
            continue
        name = next(
            (
                c for c in p.named_children
                if c.type in ("type_identifier", "identifier")
            ),
            None,
        )
        if name is not None:
            names.append(_text(name))
    return f"<{','.join(names)}>" if names else ""


# ---------------------------------------------------------------------
# declaration walking
# ---------------------------------------------------------------------


def build_decl_index(files: dict[str, str], artifact: str) -> DeclIndex:
    """Build declarations for one project from ``path -> Java source``."""
    index = DeclIndex(artifact)
    for path in sorted(files):
        content = files[path]
        index.set_lines(path, content.splitlines())
        tree = _parse(content.encode("utf-8"))
        package = _package_name(tree.root_node)
        for child in tree.root_node.named_children:
            if child.type in _TYPE_DECL_KINDS:
                _index_type(child, path, index, package, [])
    return index


def _package_name(root: ts.Node) -> str:
    package = next(
        (c for c in root.named_children if c.type == "package_declaration"),
        None,
    )
    if package is None:
        return ""
    name = next(
        (
            c for c in package.named_children
            if c.type in ("identifier", "scoped_identifier")
        ),
        None,
    )
    return _text(name) if name is not None else ""


def _type_symbol(namespace: str, chain: list[str]) -> str:
    joined = "$".join(chain)
    return f"{namespace}.{joined}" if namespace else joined


def _raw_clause_types(clause: ts.Node | None) -> list[str]:
    if clause is None:
        return []
    type_list = next(
        (c for c in clause.named_children if c.type == "type_list"),
        None,
    )
    if type_list is not None:
        return [_text(c) for c in type_list.named_children]
    return [_text(c) for c in clause.named_children]


def _body_members(body: ts.Node) -> list[ts.Node]:
    out: list[ts.Node] = []
    for child in body.named_children:
        if child.type == "enum_body_declarations":
            out.extend(child.named_children)
        else:
            out.append(child)
    return out


def _record_components(
    parameters: ts.Node | None,
) -> list[tuple[str, ts.Node, ts.Node]]:
    if parameters is None:
        return []
    result: list[tuple[str, ts.Node, ts.Node]] = []
    for param in parameters.named_children:
        if param.type not in ("formal_parameter", "spread_parameter"):
            continue
        name = _param_name(param)
        if name is not None:
            result.append((_param_type(param), name, param))
    return result


def _index_type(
    node: ts.Node,
    path: str,
    index: DeclIndex,
    namespace: str,
    outer_chain: list[str],
):
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    raw_name = _text(name_node)
    type_name = raw_name + _type_params_suffix(node)
    type_chain = outer_chain + [type_name]
    symbol = _type_symbol(namespace, type_chain)

    superclass_clause = node.child_by_field_name("superclass")
    if superclass_clause is None:
        superclass_clause = next(
            (c for c in node.named_children if c.type == "superclass"),
            None,
        )
    superclass = _raw_clause_types(superclass_clause)

    interfaces_clause = node.child_by_field_name("interfaces")
    if interfaces_clause is None:
        interfaces_clause = next(
            (
                c for c in node.named_children
                if c.type in ("super_interfaces", "extends_interfaces")
            ),
            None,
        )
    super_interfaces = _raw_clause_types(interfaces_clause)

    index.add(
        Decl(
            symbol=symbol,
            kind="type",
            file=path,
            line=name_node.start_point[0] + 1,
            name=type_name,
            container=namespace,
            namespace=namespace,
            artifact=index.artifact,
            span=(
                node.start_point[0],
                node.start_point[1],
                node.end_point[0],
                node.end_point[1],
            ),
            type_kind=_TYPE_DECL_KINDS[node.type],
            base_types=superclass + super_interfaces,
            superclass=superclass,
            super_interfaces=super_interfaces,
        )
    )

    parameters = (
        node.child_by_field_name("parameters")
        if node.type == "record_declaration"
        else None
    )
    components = _record_components(parameters)
    record_params = [component[0] for component in components]

    body = node.child_by_field_name("body")
    if body is None:
        return
    for member in _body_members(body):
        if member.type in _TYPE_DECL_KINDS:
            # Only direct member types receive stable binary names.  Local and
            # anonymous classes are deliberately never reached here.
            _index_type(member, path, index, namespace, type_chain)
        else:
            _index_member(
                member,
                path,
                index,
                namespace,
                symbol,
                raw_name,
                record_params,
            )

    if node.type == "record_declaration" and parameters is not None:
        canonical = [
            d
            for d in index.ctors_of(symbol)
            if d.params == record_params
        ]
        if not canonical:
            index.add(
                Decl(
                    symbol=f"{symbol}.<init>({', '.join(record_params)})",
                    kind="constructor",
                    file=path,
                    line=name_node.start_point[0] + 1,
                    name="<init>",
                    container=symbol,
                    namespace=namespace,
                    artifact=index.artifact,
                    params=record_params,
                    span=(
                        node.start_point[0],
                        node.start_point[1],
                        parameters.end_point[0],
                        parameters.end_point[1],
                    ),
                )
            )

        for component_type, component_name, _ in components:
            del component_type  # component type does not appear in accessor symbol
            accessor_name = _text(component_name)
            explicit = any(
                d.kind == "method"
                and d.name == accessor_name
                and not d.params
                for d in index.members_of(symbol)
            )
            if explicit:
                continue
            index.add(
                Decl(
                    symbol=f"{symbol}.{accessor_name}()",
                    kind="method",
                    file=path,
                    line=component_name.start_point[0] + 1,
                    name=accessor_name,
                    container=symbol,
                    namespace=namespace,
                    artifact=index.artifact,
                    span=(
                        component_name.start_point[0],
                        component_name.start_point[1],
                        component_name.end_point[0],
                        component_name.end_point[1],
                    ),
                )
            )


def _index_member(
    member: ts.Node,
    path: str,
    index: DeclIndex,
    namespace: str,
    type_symbol: str,
    declared_type_name: str,
    record_params: list[str],
):
    span = (
        member.start_point[0],
        member.start_point[1],
        member.end_point[0],
        member.end_point[1],
    )
    has_body = any(
        c.type in ("block", "constructor_body") for c in member.named_children
    )

    def add(
        symbol: str,
        kind: str,
        line: int,
        name: str,
        params: list[str],
    ):
        index.add(
            Decl(
                symbol=symbol,
                kind=kind,
                file=path,
                line=line,
                name=name,
                container=type_symbol,
                namespace=namespace,
                artifact=index.artifact,
                params=params,
                span=span,
                has_body=has_body,
            )
        )

    if member.type == "method_declaration":
        name_node = member.child_by_field_name("name")
        if name_node is None:
            return
        params = _norm_params(member.child_by_field_name("parameters"))
        method_name = _text(name_node) + _type_params_suffix(member)
        add(
            f"{type_symbol}.{method_name}({', '.join(params)})",
            "method",
            name_node.start_point[0] + 1,
            method_name,
            params,
        )
    elif member.type == "constructor_declaration":
        name_node = member.child_by_field_name("name")
        params = _norm_params(member.child_by_field_name("parameters"))
        line = (name_node if name_node is not None else member).start_point[0] + 1
        add(
            f"{type_symbol}.<init>({', '.join(params)})",
            "constructor",
            line,
            "<init>",
            params,
        )
    elif member.type == "compact_constructor_declaration":
        name_node = member.child_by_field_name("name")
        line = (name_node if name_node is not None else member).start_point[0] + 1
        add(
            f"{type_symbol}.<init>({', '.join(record_params)})",
            "constructor",
            line,
            "<init>",
            list(record_params),
        )
    elif member.type == "annotation_type_element_declaration":
        name_node = member.child_by_field_name("name")
        if name_node is not None:
            name = _text(name_node)
            add(
                f"{type_symbol}.{name}()",
                "method",
                name_node.start_point[0] + 1,
                name,
                [],
            )
    elif member.type == "ERROR":
        # jdtls disassembler stubs use source-like ``public Foo();``
        # constructor signatures.  They are intentionally invalid Java and
        # tree-sitter wraps the signature (without ';') in an ERROR node.
        name_node = _child_by_field(member, "name")
        parameters = _child_by_field(member, "parameters")
        if (
            name_node is not None
            and parameters is not None
            and _text(name_node) == declared_type_name
        ):
            params = _norm_params(parameters)
            add(
                f"{type_symbol}.<init>({', '.join(params)})",
                "constructor",
                name_node.start_point[0] + 1,
                "<init>",
                params,
            )


# ---------------------------------------------------------------------
# call-site extraction
# ---------------------------------------------------------------------


def extract_call_sites(path: str, content: str) -> list[CallSite]:
    lines_b = [line.encode("utf-8") for line in content.splitlines()]
    tree = _parse(content.encode("utf-8"))
    sites: list[CallSite] = []

    def pos16(node: ts.Node) -> tuple[int, int]:
        row, byte_col = node.start_point
        line = lines_b[row] if row < len(lines_b) else b""
        return row, _byte_to_utf16(line, byte_col)

    def add(node: ts.Node, token: str, kind: str, **kwargs):
        row, col = pos16(node)
        sites.append(
            CallSite(
                file=path,
                line=row + 1,
                col=col,
                token=token,
                site_kind=kind,
                **kwargs,
            )
        )

    def arguments(node: ts.Node) -> tuple[ts.Node | None, list[ts.Node]]:
        arg_list = node.child_by_field_name("arguments")
        if arg_list is None:
            arg_list = next(
                (c for c in node.named_children if c.type == "argument_list"),
                None,
            )
        return (
            arg_list,
            list(arg_list.named_children) if arg_list is not None else [],
        )

    def handle_invocation(node: ts.Node):
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        anchor = _rightmost_identifier(name_node)
        if anchor is None:
            return
        method_name = _text(anchor)
        _, args = arguments(node)

        receiver_node = node.child_by_field_name("object")
        receiver = None
        receiver_text = None
        if receiver_node is not None:
            receiver_text = _text(receiver_node)
            receiver_anchor = _rightmost_identifier(receiver_node)
            receiver = pos16(
                receiver_anchor if receiver_anchor is not None else receiver_node
            )

        add(
            anchor,
            method_name,
            "invocation",
            arg_count=len(args),
            receiver=receiver,
            receiver_text=receiver_text,
        )

        if (
            method_name in _DI_METHODS
            and len(args) == 2
            and all(arg.type == "class_literal" for arg in args)
        ):
            group = f"{path}:{node.start_point[0]}:{node.start_point[1]}"
            for i, literal in enumerate(args):
                type_node = next(iter(literal.named_children), None)
                literal_anchor = _rightmost_identifier(type_node)
                if literal_anchor is None:
                    continue
                role = "service" if i == 0 else "impl"
                add(
                    literal_anchor,
                    _text(literal_anchor),
                    "class_literal",
                    ta_role=role,
                    ta_group=group,
                    ta_index=i,
                    di_method=method_name,
                )

    def handle_constructor_chain(node: ts.Node):
        if not EMIT_CTOR_CHAIN_SITES:
            return
        constructor = node.child_by_field_name("constructor")
        if constructor is None:
            constructor = next(
                (
                    c for c in node.children
                    if c.type in ("this", "super")
                ),
                None,
            )
        if constructor is None:
            return
        _, args = arguments(node)
        add(
            constructor,
            _text(constructor),
            "invocation",
            arg_count=len(args),
        )

    def handle_object_creation(node: ts.Node):
        type_node = node.child_by_field_name("type")
        anchor = _rightmost_identifier(type_node)
        if anchor is None:
            return
        _, args = arguments(node)
        add(
            anchor,
            _text(anchor),
            "object_creation",
            arg_count=len(args),
        )

    def handle_method_reference(node: ts.Node):
        separator_index = next(
            (i for i, c in enumerate(node.children) if c.type == "::"),
            None,
        )
        if separator_index is None:
            return
        after = node.children[separator_index + 1 :]
        is_constructor = any(c.type == "new" for c in after)
        if is_constructor:
            before = [
                c for c in node.children[:separator_index] if c.is_named
            ]
            anchor = _rightmost_identifier(before[-1] if before else None)
            if anchor is not None:
                add(anchor, "new", "method_reference")
            return

        name_node = next(
            (
                c for c in reversed(after)
                if c.type in ("identifier", "type_identifier")
            ),
            None,
        )
        if name_node is not None:
            add(name_node, _text(name_node), "method_reference")

    def walk(node: ts.Node):
        for child in node.children:
            kind = child.type
            if kind == "method_invocation":
                handle_invocation(child)
            elif kind == "explicit_constructor_invocation":
                handle_constructor_chain(child)
            elif kind == "object_creation_expression":
                handle_object_creation(child)
            elif kind == "method_reference":
                handle_method_reference(child)
            walk(child)

    walk(tree.root_node)
    return sites
