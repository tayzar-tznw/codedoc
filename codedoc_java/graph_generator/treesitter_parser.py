"""Extract code entities from source files using tree-sitter AST parsing.

Supports Java only, with exact same output schema as the LLM entity
extraction prompt. The other JVM extensions in ``config.SOURCE_EXTENSIONS``
(.jsp/.kt/.groovy/.scala/.gradle) are scanned for documentation but never
parsed here — ``parse_entities``/``chunk_by_structure`` return ``None`` for
them so the caller falls back to the LLM path.
"""

from __future__ import annotations

import os
from typing import Any

import tree_sitter as ts

# Lazy-loaded language objects
_languages: dict[str, ts.Language] = {}
_parsers: dict[str, ts.Parser] = {}

# Extension → language name mapping
_EXT_TO_LANG: dict[str, str] = {
    ".java": "java",
}


_missing_langs: set[str] = set()


def _get_parser(lang: str) -> ts.Parser | None:
    """Get or create a parser for the given language."""
    if lang in _parsers:
        return _parsers[lang]
    if lang in _missing_langs:
        return None

    try:
        if lang == "java":
            import tree_sitter_java as mod
        else:
            return None

        language = ts.Language(mod.language())
        parser = ts.Parser(language)
        _languages[lang] = language
        _parsers[lang] = parser
        return parser
    except ImportError:
        _missing_langs.add(lang)
        return None
    except Exception:
        return None


def _report_missing_langs():
    """Print install instructions for missing tree-sitter packages."""
    if not _missing_langs:
        return
    pkg_map = {
        "java": "tree-sitter-java",
    }
    pkgs = sorted({pkg_map.get(lang, f"tree-sitter-{lang}") for lang in _missing_langs})
    print(f"\n  WARNING: Tree-sitter packages not installed for: {', '.join(sorted(_missing_langs))}")
    print(f"  Install with: pip install {' '.join(pkgs)}\n")


def _node_text(node: ts.Node) -> str:
    return node.text.decode("utf-8", errors="replace")


def _get_modifiers(node: ts.Node) -> str:
    """Extract modifier keywords (and annotations) from a declaration node."""
    mods = []
    for c in node.children:
        if c.type == "modifiers":
            for m in c.children:
                text = _node_text(m).strip()
                if text:
                    mods.append(text)
    return " ".join(mods)


# ===================================================================
# Java extraction
# ===================================================================


# Java type declarations (kind name reported to the doc pipeline)
_JAVA_TYPE_DECLS = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "record_declaration": "record",
    "annotation_type_declaration": "annotation",
}

# Bodies that hold member declarations
_JAVA_BODIES = {
    "class_body", "interface_body", "enum_body", "annotation_type_body",
    "enum_body_declarations",
}


def _extract_java_invocations(body_node: ts.Node) -> list[str]:
    """Extract called method names from a method body."""
    calls: list[str] = []

    def _walk(node: ts.Node):
        if node.type == "method_invocation":
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                calls.append(_node_text(name_node))
        elif node.type == "object_creation_expression":
            type_node = node.child_by_field_name("type")
            if type_node is not None:
                calls.append(f"new {_node_text(type_node)}")
        elif node.type == "explicit_constructor_invocation":
            ctor = node.child_by_field_name("constructor")
            if ctor is not None:
                calls.append(_node_text(ctor))
        elif node.type == "method_reference":
            calls.append(_node_text(node))
        for child in node.children:
            _walk(child)

    _walk(body_node)
    return list(dict.fromkeys(calls))  # dedupe preserving order


def _java_body_members(node: ts.Node) -> list[ts.Node]:
    """Direct member declarations of a type body (enum bodies nest one level)."""
    out: list[ts.Node] = []
    for child in node.children:
        if child.type == "enum_body_declarations":
            out.extend(child.children)
        else:
            out.append(child)
    return out


def _extract_java_methods(body: ts.Node, type_name: str) -> list[dict]:
    """Extract methods/constructors from a Java type body node."""
    methods = []
    for node in _java_body_members(body):
        if node.type not in ("method_declaration", "constructor_declaration",
                             "compact_constructor_declaration"):
            continue

        mods = _get_modifiers(node)
        name_node = node.child_by_field_name("name")
        name = _node_text(name_node) if name_node is not None else ""
        params_node = node.child_by_field_name("parameters")
        params_str = _node_text(params_node)[1:-1].strip() if params_node is not None else ""

        if node.type == "method_declaration":
            type_node = node.child_by_field_name("type")
            ret_type = _node_text(type_node) if type_node is not None else "void"
        else:
            ret_type = ""          # constructors have no return type
            name = name or type_name

        body_node = next((c for c in node.children
                          if c.type in ("block", "constructor_body")), None)
        calls = _extract_java_invocations(body_node) if body_node is not None else []

        if name:
            methods.append({
                "name": name,
                "modifiers": mods,
                "return_type": ret_type or "void",
                "parameters": params_str,
                "calls": calls,
            })

    return methods


def _java_clause_types(clause: ts.Node | None) -> list[str]:
    """Type names listed in an extends/implements/permits clause."""
    if clause is None:
        return []
    type_list = next((c for c in clause.named_children if c.type == "type_list"), None)
    nodes = type_list.named_children if type_list is not None else clause.named_children
    return [_node_text(c) for c in nodes]


def _extract_java_type(node: ts.Node) -> dict | None:
    """Extract a class/interface/enum/record/annotation declaration."""
    kind = _JAVA_TYPE_DECLS.get(node.type)
    if not kind:
        return None

    name_node = node.child_by_field_name("name")
    name = _node_text(name_node) if name_node is not None else ""
    if not name:
        return None

    # syntax-exact split: `extends` (superclass) vs `implements` (interfaces).
    # An interface's `extends` list IS its super-interface list.
    superclass_clause = node.child_by_field_name("superclass") or next(
        (c for c in node.named_children if c.type == "superclass"), None)
    interfaces_clause = node.child_by_field_name("interfaces") or next(
        (c for c in node.named_children
         if c.type in ("super_interfaces", "extends_interfaces")), None)

    base_classes = _java_clause_types(superclass_clause)
    interfaces = _java_clause_types(interfaces_clause)
    if node.type == "interface_declaration":
        interfaces = interfaces + base_classes
        base_classes = []

    body = node.child_by_field_name("body")
    methods = _extract_java_methods(body, name) if body is not None else []

    return {
        "name": name,
        "kind": kind,
        "modifiers": _get_modifiers(node),
        "base_classes": base_classes,
        "interfaces": interfaces,
        "methods": methods,
    }


def _parse_java(tree: ts.Tree, file_path: str) -> dict[str, Any]:
    """Parse a Java file AST into the entity schema."""
    root = tree.root_node
    namespace = ""     # Java package
    imports: list[str] = []
    classes: list[dict] = []

    def _walk(node: ts.Node):
        nonlocal namespace
        for c in node.children:
            if c.type == "package_declaration":
                for pc in c.named_children:
                    if pc.type in ("identifier", "scoped_identifier"):
                        namespace = _node_text(pc)
                        break

            elif c.type == "import_declaration":
                is_static = any(_node_text(ic) == "static" for ic in c.children)
                target = ""
                for ic in c.named_children:
                    if ic.type in ("identifier", "scoped_identifier"):
                        target = _node_text(ic)
                        break
                if target:
                    if any(_node_text(ic) == "*" for ic in c.children):
                        target += ".*"
                    imports.append(f"static {target}" if is_static else target)

            elif c.type in _JAVA_TYPE_DECLS:
                type_info = _extract_java_type(c)
                if type_info:
                    classes.append(type_info)
                # nested types are reported as their own entities too
                body = c.child_by_field_name("body")
                if body is not None:
                    _walk(body)

            elif c.type in _JAVA_BODIES:
                _walk(c)

    _walk(root)

    return {
        "file_path": file_path,
        "namespace": namespace,
        "classes": classes,
        "imports": imports,
    }


# ===================================================================
# Public API
# ===================================================================


_LANG_PARSERS = {
    "java": _parse_java,
}


def _node_source(node: ts.Node, source: bytes) -> str:
    """Extract the source text for an AST node."""
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


# Type declarations we split into member-level units
_TYPE_DECLS = set(_JAVA_TYPE_DECLS)

# Containers that hold type declarations
_CONTAINERS = set(_JAVA_BODIES)


def _collect_units(node: ts.Node, source: bytes, max_chars: int) -> list[str]:
    """Walk an AST node and collect semantic units that fit within max_chars.

    A "unit" is a complete, self-contained piece of code: an import block,
    a class, a method, a field, etc. Large classes are broken into
    member-level units. Oversized single members get character-split
    with a context header.
    """
    units: list[str] = []

    # Group consecutive package/import declarations
    imports: list[str] = []
    i = 0
    children = node.children
    while i < len(children):
        child = children[i]
        if child.type in ("import_declaration", "package_declaration"):
            imports.append(_node_source(child, source))
            i += 1
            continue
        if imports:
            units.append("\n".join(imports))
            imports = []

        size = child.end_byte - child.start_byte

        if child.type in _TYPE_DECLS:
            if size <= max_chars:
                # Whole class fits — keep it as one unit
                units.append(_node_source(child, source))
            else:
                # Class too large — break into members
                name_node = child.child_by_field_name("name")
                class_name = _node_source(name_node, source) if name_node is not None else ""

                # Collect the class header (modifiers, name, base lists)
                header_parts = []
                for cc in child.children:
                    if cc.type in _CONTAINERS:
                        break
                    header_parts.append(_node_source(cc, source))
                header = " ".join(header_parts)

                for cc in child.children:
                    if cc.type not in _CONTAINERS:
                        continue
                    for member in cc.children:
                        if not member.is_named:
                            continue
                        member_src = _node_source(member, source)
                        member_size = len(member_src)

                        if member_size <= max_chars:
                            # Add class context so LLM knows where this belongs
                            units.append(f"// class {class_name}\n{header} {{\n{member_src}\n}}")
                        else:
                            # Single member too large (e.g., a huge literal table)
                            # Character-split with context
                            mn = member.child_by_field_name("name")
                            member_name = _node_source(mn, source) if mn is not None else ""
                            context = f"// class {class_name}, member {member_name} ({member_size:,} chars, split)"
                            pos = 0
                            part_idx = 0
                            while pos < member_size:
                                end = pos + max_chars - 200  # room for context header
                                chunk_text = member_src[pos:end]
                                part_idx += 1
                                units.append(f"{context} part {part_idx}\n{chunk_text}")
                                pos = end

        elif child.type in _CONTAINERS:
            # Recurse into a type body
            units.extend(_collect_units(child, source, max_chars))

        else:
            # Other top-level nodes (comments, blocks, expressions, etc.)
            src = _node_source(child, source)
            if not src.strip():
                i += 1
                continue
            if len(src) <= max_chars:
                units.append(src)
            else:
                # Oversized node — character-split with context
                context = f"// {child.type} (line {child.start_point[0]+1}, {len(src):,} chars, split)"
                pos = 0
                part_idx = 0
                while pos < len(src):
                    end = pos + max_chars - 200
                    chunk_text = src[pos:end]
                    part_idx += 1
                    units.append(f"{context} part {part_idx}\n{chunk_text}")
                    pos = end

        i += 1

    if imports:
        units.append("\n".join(imports))

    return units


def chunk_by_structure(file_path: str, content: str, max_chars: int = 800_000) -> list[str] | None:
    """Split a source file into semantically meaningful chunks using tree-sitter.

    Each chunk contains complete code structures (classes, methods, fields).
    Returns None if the language is unsupported (caller should fall back).

    Args:
        file_path: Path to determine language
        content: Full source code
        max_chars: Max characters per chunk

    Returns:
        List of chunk strings, or None if language unsupported.
    """
    ext = os.path.splitext(file_path)[1].lower()
    lang = _EXT_TO_LANG.get(ext)
    if not lang:
        return None

    parser = _get_parser(lang)
    if not parser:
        return None

    try:
        source = content.encode("utf-8", errors="replace")
        tree = parser.parse(source)
    except Exception:
        return None

    # Use 95% of budget for units to leave room for join separators and headers
    unit_budget = int(max_chars * 0.95)
    units = _collect_units(tree.root_node, source, unit_budget)
    if not units:
        return [content]  # fallback: whole file as single chunk

    # Pack units into chunks greedily
    chunks: list[str] = []
    current: list[str] = []
    current_size = 0

    for unit in units:
        unit_size = len(unit) + 2  # account for "\n\n" separator
        if current and current_size + unit_size > max_chars:
            chunks.append("\n\n".join(current))
            current = []
            current_size = 0
        current.append(unit)
        current_size += unit_size

    if current:
        chunks.append("\n\n".join(current))

    return chunks


def parse_entities(file_path: str, content: str) -> dict[str, Any] | None:
    """Parse a source file and extract entities using tree-sitter.

    Args:
        file_path: Path to the source file (used to determine language and for output)
        content: Source code content as string

    Returns:
        Entity dict matching the LLM extraction schema, or None if language unsupported.
    """
    ext = os.path.splitext(file_path)[1].lower()
    lang = _EXT_TO_LANG.get(ext)
    if not lang:
        return None

    parser = _get_parser(lang)
    if not parser:
        return None

    parse_fn = _LANG_PARSERS.get(lang)
    if not parse_fn:
        return None

    try:
        tree = parser.parse(content.encode("utf-8", errors="replace"))
        return parse_fn(tree, file_path)
    except Exception:
        return None
