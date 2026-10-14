"""Extract code entities from source files using tree-sitter AST parsing.

Supports C#, Java, C, C++, Python, TypeScript, JavaScript, Go, Rust, Ruby, and
Kotlin with exact same output schema as the LLM entity extraction prompt.
Returns None for unsupported languages so the caller can fall back to LLM.
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
    ".cs": "c_sharp",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".rake": "ruby",
    ".gemspec": "ruby",
    ".kt": "kotlin",
    ".kts": "kotlin",
}


_missing_langs: set[str] = set()


def _get_parser(lang: str) -> ts.Parser | None:
    """Get or create a parser for the given language."""
    if lang in _parsers:
        return _parsers[lang]
    if lang in _missing_langs:
        return None

    try:
        if lang == "c_sharp":
            import tree_sitter_c_sharp as mod
        elif lang == "java":
            import tree_sitter_java as mod
        elif lang == "c":
            import tree_sitter_c as mod
        elif lang == "cpp":
            import tree_sitter_cpp as mod
        elif lang == "python":
            import tree_sitter_python as mod
        elif lang == "typescript":
            import tree_sitter_typescript
            mod = None  # handled below
        elif lang == "tsx":
            import tree_sitter_typescript
            mod = None  # handled below
        elif lang == "javascript":
            import tree_sitter_javascript as mod
        elif lang == "go":
            import tree_sitter_go as mod
        elif lang == "rust":
            import tree_sitter_rust as mod
        elif lang == "ruby":
            import tree_sitter_ruby as mod
        elif lang == "kotlin":
            import tree_sitter_kotlin as mod
        else:
            return None

        # tree-sitter-typescript exposes separate functions for ts vs tsx
        if lang == "typescript":
            language = ts.Language(tree_sitter_typescript.language_typescript())
        elif lang == "tsx":
            language = ts.Language(tree_sitter_typescript.language_tsx())
        else:
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
        "c_sharp": "tree-sitter-c-sharp",
        "java": "tree-sitter-java",
        "c": "tree-sitter-c",
        "cpp": "tree-sitter-cpp",
        "python": "tree-sitter-python",
        "typescript": "tree-sitter-typescript",
        "tsx": "tree-sitter-typescript",
        "javascript": "tree-sitter-javascript",
        "go": "tree-sitter-go",
        "rust": "tree-sitter-rust",
        "ruby": "tree-sitter-ruby",
        "kotlin": "tree-sitter-kotlin",
    }
    pkgs = sorted({pkg_map.get(lang, f"tree-sitter-{lang}") for lang in _missing_langs})
    print(f"\n  WARNING: Tree-sitter packages not installed for: {', '.join(sorted(_missing_langs))}")
    print(f"  Install with: pip install {' '.join(pkgs)}\n")


def _node_text(node: ts.Node) -> str:
    return node.text.decode("utf-8", errors="replace")


def _get_modifiers(node: ts.Node) -> str:
    """Extract modifier keywords from a declaration node."""
    mods = []
    for c in node.children:
        if c.type == "modifier":
            mods.append(_node_text(c))
        elif c.type == "modifiers":
            for m in c.children:
                if m.is_named:
                    mods.append(_node_text(m))
    return " ".join(mods)


# ===================================================================
# C# extraction
# ===================================================================


def _extract_csharp_invocations(body_node: ts.Node) -> list[str]:
    """Extract method call names from a method body."""
    calls: list[str] = []

    def _walk(node: ts.Node):
        if node.type == "invocation_expression":
            func = node.children[0] if node.children else None
            if func:
                if func.type == "member_access_expression":
                    # obj.Method(...) → extract "Method"
                    name_node = func.child_by_field_name("name")
                    if name_node:
                        calls.append(_node_text(name_node))
                    else:
                        # fallback: last identifier child
                        for c in reversed(func.children):
                            if c.type == "identifier":
                                calls.append(_node_text(c))
                                break
                elif func.type == "identifier":
                    calls.append(_node_text(func))
        for child in node.children:
            _walk(child)

    _walk(body_node)
    return list(dict.fromkeys(calls))  # dedupe preserving order


def _extract_csharp_methods(decl_list: ts.Node) -> list[dict]:
    """Extract methods from a C# declaration_list node."""
    methods = []
    for node in decl_list.children:
        if node.type not in ("method_declaration", "constructor_declaration"):
            continue

        mods = _get_modifiers(node)
        name = ""
        ret_type = ""
        params_str = ""
        body = None

        for c in node.children:
            if c.type == "identifier" and not name:
                name = _node_text(c)
            elif c.type in ("predefined_type", "generic_name", "qualified_name",
                            "nullable_type", "array_type", "identifier") and not ret_type and c != node.children[0]:
                # For methods, return type comes before the method name identifier
                if not name:
                    ret_type = _node_text(c)
                elif c.type != "identifier":
                    ret_type = _node_text(c)
            elif c.type == "parameter_list":
                params_str = _node_text(c)[1:-1].strip()  # strip ()
            elif c.type == "block":
                body = c

        # For constructor_declaration, name is the class name
        if node.type == "constructor_declaration":
            ret_type = ""

        # Better return type extraction: find the type node before identifier
        if not ret_type and node.type == "method_declaration":
            found_type = False
            for c in node.children:
                if c.type == "modifier":
                    continue
                if c.type != "identifier" and not found_type:
                    ret_type = _node_text(c)
                    found_type = True
                    continue
                if c.type == "identifier":
                    name = _node_text(c)
                    break

        calls = _extract_csharp_invocations(body) if body else []

        if name:
            methods.append({
                "name": name,
                "modifiers": mods,
                "return_type": ret_type or "void",
                "parameters": params_str,
                "calls": calls,
            })

    return methods


def _extract_csharp_type(node: ts.Node) -> dict | None:
    """Extract a class/struct/interface/enum from a C# type declaration node."""
    kind_map = {
        "class_declaration": "class",
        "struct_declaration": "struct",
        "interface_declaration": "interface",
        "enum_declaration": "enum",
        "record_declaration": "record",
    }
    kind = kind_map.get(node.type)
    if not kind:
        return None

    mods = _get_modifiers(node)
    name = ""
    base_classes: list[str] = []
    interfaces: list[str] = []
    methods: list[dict] = []

    for c in node.children:
        if c.type == "identifier" and not name:
            name = _node_text(c)
        elif c.type == "base_list":
            for bc in c.children:
                if bc.type in ("identifier", "qualified_name", "generic_name"):
                    base_name = _node_text(bc)
                    # Heuristic: I-prefixed names are interfaces in C#
                    if base_name.startswith("I") and len(base_name) > 1 and base_name[1].isupper():
                        interfaces.append(base_name)
                    else:
                        base_classes.append(base_name)
        elif c.type == "declaration_list":
            methods = _extract_csharp_methods(c)
            # Recursively extract nested types (handled at caller level)

    if not name:
        return None

    return {
        "name": name,
        "kind": kind,
        "modifiers": mods,
        "base_classes": base_classes,
        "interfaces": interfaces,
        "methods": methods,
    }


def _parse_csharp(tree: ts.Tree, file_path: str) -> dict[str, Any]:
    """Parse a C# file AST into the entity schema."""
    root = tree.root_node
    namespace = ""
    imports: list[str] = []
    classes: list[dict] = []

    def _walk(node: ts.Node):
        nonlocal namespace
        for c in node.children:
            if c.type == "using_directive":
                # Extract the name (identifier or qualified_name)
                for uc in c.children:
                    if uc.type in ("identifier", "qualified_name"):
                        imports.append(_node_text(uc))
                        break

            elif c.type in ("namespace_declaration", "file_scoped_namespace_declaration"):
                for nc in c.children:
                    if nc.type in ("identifier", "qualified_name"):
                        namespace = _node_text(nc)
                        break
                # Walk inside namespace for type declarations
                _walk(c)

            elif c.type in ("class_declaration", "struct_declaration",
                            "interface_declaration", "enum_declaration",
                            "record_declaration"):
                type_info = _extract_csharp_type(c)
                if type_info:
                    classes.append(type_info)

            elif c.type == "declaration_list":
                _walk(c)

    _walk(root)

    return {
        "file_path": file_path,
        "namespace": namespace,
        "classes": classes,
        "imports": imports,
    }


# ===================================================================
# Java extraction
# ===================================================================


def _extract_java_invocations(body_node: ts.Node) -> list[str]:
    """Extract method call names from a Java method body."""
    calls: list[str] = []

    def _walk(node: ts.Node):
        if node.type == "method_invocation":
            # obj.method(...) or method(...)
            name_node = node.child_by_field_name("name")
            if name_node:
                calls.append(_node_text(name_node))
            else:
                for c in node.children:
                    if c.type == "identifier":
                        calls.append(_node_text(c))
                        break
        for child in node.children:
            _walk(child)

    _walk(body_node)
    return list(dict.fromkeys(calls))


def _extract_java_methods(body_node: ts.Node) -> list[dict]:
    """Extract methods from a Java class body."""
    methods = []
    for node in body_node.children:
        if node.type not in ("method_declaration", "constructor_declaration"):
            continue

        mods = _get_modifiers(node)
        name = ""
        ret_type = ""
        params_str = ""
        body = None

        for c in node.children:
            if c.type == "identifier" and not name:
                name = _node_text(c)
            elif c.type in ("type_identifier", "generic_type", "void_type",
                            "integral_type", "floating_point_type", "boolean_type",
                            "array_type", "scoped_type_identifier"):
                if not ret_type:
                    ret_type = _node_text(c)
            elif c.type == "formal_parameters":
                params_str = _node_text(c)[1:-1].strip()
            elif c.type == "block":
                body = c

        if node.type == "constructor_declaration":
            ret_type = ""

        calls = _extract_java_invocations(body) if body else []

        if name:
            methods.append({
                "name": name,
                "modifiers": mods,
                "return_type": ret_type or "void",
                "parameters": params_str,
                "calls": calls,
            })

    return methods


def _extract_java_type(node: ts.Node) -> dict | None:
    """Extract a class/interface/enum from a Java type declaration node."""
    kind_map = {
        "class_declaration": "class",
        "interface_declaration": "interface",
        "enum_declaration": "enum",
        "record_declaration": "record",
    }
    kind = kind_map.get(node.type)
    if not kind:
        return None

    mods = _get_modifiers(node)
    name = ""
    base_classes: list[str] = []
    interfaces: list[str] = []
    methods: list[dict] = []

    for c in node.children:
        if c.type == "identifier" and not name:
            name = _node_text(c)
        elif c.type == "superclass":
            for sc in c.children:
                if sc.type == "type_identifier":
                    base_classes.append(_node_text(sc))
        elif c.type == "super_interfaces":
            for si in c.children:
                if si.type == "type_list":
                    for t in si.children:
                        if t.type in ("type_identifier", "generic_type"):
                            interfaces.append(_node_text(t))
        elif c.type == "class_body" or c.type == "interface_body" or c.type == "enum_body":
            methods = _extract_java_methods(c)

    if not name:
        return None

    return {
        "name": name,
        "kind": kind,
        "modifiers": mods,
        "base_classes": base_classes,
        "interfaces": interfaces,
        "methods": methods,
    }


def _parse_java(tree: ts.Tree, file_path: str) -> dict[str, Any]:
    """Parse a Java file AST into the entity schema."""
    root = tree.root_node
    namespace = ""
    imports: list[str] = []
    classes: list[dict] = []

    for c in root.children:
        if c.type == "package_declaration":
            for pc in c.children:
                if pc.type == "scoped_identifier":
                    namespace = _node_text(pc)
                    break
                elif pc.type == "identifier":
                    namespace = _node_text(pc)

        elif c.type == "import_declaration":
            for ic in c.children:
                if ic.type in ("scoped_identifier", "identifier"):
                    imports.append(_node_text(ic))
                    break

        elif c.type in ("class_declaration", "interface_declaration",
                        "enum_declaration", "record_declaration"):
            type_info = _extract_java_type(c)
            if type_info:
                classes.append(type_info)

    return {
        "file_path": file_path,
        "namespace": namespace,
        "classes": classes,
        "imports": imports,
    }


# ===================================================================
# C/C++ extraction (basic — structs, functions, includes)
# ===================================================================


def _parse_c_cpp(tree: ts.Tree, file_path: str) -> dict[str, Any]:
    """Basic C/C++ entity extraction — functions, structs, includes."""
    root = tree.root_node
    imports: list[str] = []
    classes: list[dict] = []
    namespace = ""

    for c in root.children:
        if c.type == "preproc_include":
            for ic in c.children:
                if ic.type in ("string_literal", "system_lib_string"):
                    imports.append(_node_text(ic).strip('"<>'))

        elif c.type == "namespace_definition":
            for nc in c.children:
                if nc.type == "identifier":
                    namespace = _node_text(nc)
                    break

        elif c.type in ("struct_specifier", "class_specifier", "enum_specifier"):
            name = ""
            kind = c.type.replace("_specifier", "")
            for sc in c.children:
                if sc.type in ("type_identifier", "identifier"):
                    name = _node_text(sc)
                    break
            if name:
                classes.append({
                    "name": name,
                    "kind": kind,
                    "modifiers": "",
                    "base_classes": [],
                    "interfaces": [],
                    "methods": [],
                })

        elif c.type == "function_definition":
            name = ""
            ret_type = ""
            params_str = ""
            for fc in c.children:
                if fc.type == "function_declarator":
                    for dc in fc.children:
                        if dc.type == "identifier":
                            name = _node_text(dc)
                        elif dc.type == "parameter_list":
                            params_str = _node_text(dc)[1:-1].strip()
                elif fc.type in ("primitive_type", "type_identifier", "sized_type_specifier"):
                    ret_type = _node_text(fc)
            if name:
                # Represent top-level functions as methods in a pseudo-class
                if not classes or classes[-1]["name"] != "(global)":
                    classes.append({
                        "name": "(global)",
                        "kind": "module",
                        "modifiers": "",
                        "base_classes": [],
                        "interfaces": [],
                        "methods": [],
                    })
                classes[-1]["methods"].append({
                    "name": name,
                    "modifiers": "",
                    "return_type": ret_type or "void",
                    "parameters": params_str,
                    "calls": [],
                })

    return {
        "file_path": file_path,
        "namespace": namespace,
        "classes": classes,
        "imports": imports,
    }


# ===================================================================
# Python extraction
# ===================================================================


def _extract_python_invocations(body_node: ts.Node) -> list[str]:
    """Extract function/method call names from a Python function body."""
    calls: list[str] = []

    def _walk(node: ts.Node):
        if node.type == "call":
            func = node.child_by_field_name("function")
            if func:
                if func.type == "attribute":
                    attr = func.child_by_field_name("attribute")
                    if attr:
                        calls.append(_node_text(attr))
                elif func.type == "identifier":
                    calls.append(_node_text(func))
        for child in node.children:
            _walk(child)

    _walk(body_node)
    return list(dict.fromkeys(calls))


def _extract_python_methods(body_node: ts.Node) -> list[dict]:
    """Extract methods from a Python class body."""
    methods = []
    for node in body_node.children:
        if node.type != "function_definition":
            continue

        name = ""
        params_str = ""
        body = None
        decorators: list[str] = []

        for c in node.children:
            if c.type == "identifier" and not name:
                name = _node_text(c)
            elif c.type == "parameters":
                params_str = _node_text(c)[1:-1].strip()
            elif c.type == "block":
                body = c
            elif c.type == "decorator":
                decorators.append(_node_text(c))

        mods = " ".join(decorators)
        calls = _extract_python_invocations(body) if body else []

        # Determine return type from annotation if present
        ret_type = ""
        ret_node = node.child_by_field_name("return_type")
        if ret_node:
            ret_type = _node_text(ret_node)

        if name:
            methods.append({
                "name": name,
                "modifiers": mods,
                "return_type": ret_type or "",
                "parameters": params_str,
                "calls": calls,
            })

    return methods


def _parse_python(tree: ts.Tree, file_path: str) -> dict[str, Any]:
    """Parse a Python file AST into the entity schema."""
    root = tree.root_node
    imports: list[str] = []
    classes: list[dict] = []

    for c in root.children:
        if c.type in ("import_statement", "import_from_statement"):
            imports.append(_node_text(c))

        elif c.type == "class_definition":
            name = ""
            base_classes: list[str] = []
            methods: list[dict] = []

            for cc in c.children:
                if cc.type == "identifier" and not name:
                    name = _node_text(cc)
                elif cc.type == "argument_list":
                    for arg in cc.children:
                        if arg.is_named:
                            base_classes.append(_node_text(arg))
                elif cc.type == "block":
                    methods = _extract_python_methods(cc)

            if name:
                classes.append({
                    "name": name,
                    "kind": "class",
                    "modifiers": "",
                    "base_classes": base_classes,
                    "interfaces": [],
                    "methods": methods,
                })

        elif c.type == "function_definition":
            # Top-level functions go into a pseudo-class
            fname = ""
            params_str = ""
            body = None
            ret_type = ""
            decorators: list[str] = []

            for fc in c.children:
                if fc.type == "identifier" and not fname:
                    fname = _node_text(fc)
                elif fc.type == "parameters":
                    params_str = _node_text(fc)[1:-1].strip()
                elif fc.type == "block":
                    body = fc
                elif fc.type == "decorator":
                    decorators.append(_node_text(fc))

            ret_node = c.child_by_field_name("return_type")
            if ret_node:
                ret_type = _node_text(ret_node)

            calls = _extract_python_invocations(body) if body else []
            mods = " ".join(decorators)

            if fname:
                if not classes or classes[-1]["name"] != "(global)":
                    classes.append({
                        "name": "(global)",
                        "kind": "module",
                        "modifiers": "",
                        "base_classes": [],
                        "interfaces": [],
                        "methods": [],
                    })
                classes[-1]["methods"].append({
                    "name": fname,
                    "modifiers": mods,
                    "return_type": ret_type or "",
                    "parameters": params_str,
                    "calls": calls,
                })

    return {
        "file_path": file_path,
        "namespace": "",
        "classes": classes,
        "imports": imports,
    }


# ===================================================================
# TypeScript / JavaScript extraction
# ===================================================================


def _extract_js_ts_invocations(body_node: ts.Node) -> list[str]:
    """Extract function/method call names from a JS/TS body."""
    calls: list[str] = []

    def _walk(node: ts.Node):
        if node.type == "call_expression":
            func = node.child_by_field_name("function")
            if func:
                if func.type == "member_expression":
                    prop = func.child_by_field_name("property")
                    if prop:
                        calls.append(_node_text(prop))
                elif func.type == "identifier":
                    calls.append(_node_text(func))
        for child in node.children:
            _walk(child)

    _walk(body_node)
    return list(dict.fromkeys(calls))


def _extract_js_ts_methods(body_node: ts.Node) -> list[dict]:
    """Extract methods from a JS/TS class body."""
    methods = []
    for node in body_node.children:
        if node.type not in ("method_definition", "public_field_definition"):
            continue
        if node.type == "public_field_definition":
            continue  # skip fields

        name = ""
        params_str = ""
        body = None

        name_node = node.child_by_field_name("name")
        if name_node:
            name = _node_text(name_node)

        for c in node.children:
            if c.type == "formal_parameters":
                params_str = _node_text(c)[1:-1].strip()
            elif c.type == "statement_block":
                body = c

        calls = _extract_js_ts_invocations(body) if body else []

        # Extract return type annotation if present (TypeScript)
        ret_type = ""
        ret_node = node.child_by_field_name("return_type")
        if ret_node:
            ret_type = _node_text(ret_node).lstrip(":").strip()

        if name:
            methods.append({
                "name": name,
                "modifiers": "",
                "return_type": ret_type or "",
                "parameters": params_str,
                "calls": calls,
            })

    return methods


def _parse_js_ts(tree: ts.Tree, file_path: str) -> dict[str, Any]:
    """Parse a JavaScript/TypeScript file AST into the entity schema."""
    root = tree.root_node
    imports: list[str] = []
    classes: list[dict] = []

    for c in root.children:
        if c.type == "import_statement":
            imports.append(_node_text(c))

        elif c.type == "class_declaration":
            name = ""
            base_classes: list[str] = []
            methods: list[dict] = []

            name_node = c.child_by_field_name("name")
            if name_node:
                name = _node_text(name_node)

            # Heritage (extends/implements)
            for cc in c.children:
                if cc.type == "class_heritage":
                    for hc in cc.children:
                        if hc.type in ("identifier", "type_identifier",
                                       "member_expression"):
                            base_classes.append(_node_text(hc))
                elif cc.type == "class_body":
                    methods = _extract_js_ts_methods(cc)

            if name:
                classes.append({
                    "name": name,
                    "kind": "class",
                    "modifiers": "",
                    "base_classes": base_classes,
                    "interfaces": [],
                    "methods": methods,
                })

        elif c.type == "interface_declaration":
            name = ""
            name_node = c.child_by_field_name("name")
            if name_node:
                name = _node_text(name_node)
            if name:
                classes.append({
                    "name": name,
                    "kind": "interface",
                    "modifiers": "",
                    "base_classes": [],
                    "interfaces": [],
                    "methods": [],
                })

        elif c.type in ("function_declaration", "lexical_declaration",
                        "export_statement"):
            # Top-level functions
            func_nodes = []
            if c.type == "function_declaration":
                func_nodes = [c]
            elif c.type == "export_statement":
                # export function foo() ...
                for ec in c.children:
                    if ec.type in ("function_declaration", "class_declaration",
                                   "interface_declaration"):
                        func_nodes = [ec]
                        break

            for fn in func_nodes:
                if fn.type == "class_declaration":
                    # Handle exported classes
                    name = ""
                    name_node = fn.child_by_field_name("name")
                    if name_node:
                        name = _node_text(name_node)
                    base_classes_inner: list[str] = []
                    methods_inner: list[dict] = []
                    for cc in fn.children:
                        if cc.type == "class_heritage":
                            for hc in cc.children:
                                if hc.type in ("identifier", "type_identifier"):
                                    base_classes_inner.append(_node_text(hc))
                        elif cc.type == "class_body":
                            methods_inner = _extract_js_ts_methods(cc)
                    if name:
                        classes.append({
                            "name": name,
                            "kind": "class",
                            "modifiers": "export",
                            "base_classes": base_classes_inner,
                            "interfaces": [],
                            "methods": methods_inner,
                        })
                    continue

                if fn.type == "interface_declaration":
                    name = ""
                    name_node = fn.child_by_field_name("name")
                    if name_node:
                        name = _node_text(name_node)
                    if name:
                        classes.append({
                            "name": name,
                            "kind": "interface",
                            "modifiers": "export",
                            "base_classes": [],
                            "interfaces": [],
                            "methods": [],
                        })
                    continue

                if fn.type != "function_declaration":
                    continue

                fname = ""
                params_str = ""
                body = None

                name_node = fn.child_by_field_name("name")
                if name_node:
                    fname = _node_text(name_node)

                for fc in fn.children:
                    if fc.type == "formal_parameters":
                        params_str = _node_text(fc)[1:-1].strip()
                    elif fc.type == "statement_block":
                        body = fc

                ret_type = ""
                ret_node = fn.child_by_field_name("return_type")
                if ret_node:
                    ret_type = _node_text(ret_node).lstrip(":").strip()

                calls = _extract_js_ts_invocations(body) if body else []

                if fname:
                    if not classes or classes[-1]["name"] != "(global)":
                        classes.append({
                            "name": "(global)",
                            "kind": "module",
                            "modifiers": "",
                            "base_classes": [],
                            "interfaces": [],
                            "methods": [],
                        })
                    classes[-1]["methods"].append({
                        "name": fname,
                        "modifiers": "",
                        "return_type": ret_type or "",
                        "parameters": params_str,
                        "calls": calls,
                    })

    return {
        "file_path": file_path,
        "namespace": "",
        "classes": classes,
        "imports": imports,
    }


# ===================================================================
# Go extraction
# ===================================================================


def _extract_go_invocations(body_node: ts.Node) -> list[str]:
    """Extract function/method call names from a Go function body."""
    calls: list[str] = []

    def _walk(node: ts.Node):
        if node.type == "call_expression":
            func = node.child_by_field_name("function")
            if func:
                if func.type == "selector_expression":
                    field = func.child_by_field_name("field")
                    if field:
                        calls.append(_node_text(field))
                elif func.type == "identifier":
                    calls.append(_node_text(func))
        for child in node.children:
            _walk(child)

    _walk(body_node)
    return list(dict.fromkeys(calls))


def _parse_go(tree: ts.Tree, file_path: str) -> dict[str, Any]:
    """Parse a Go file AST into the entity schema."""
    root = tree.root_node
    imports: list[str] = []
    classes: list[dict] = []
    namespace = ""

    for c in root.children:
        if c.type == "package_clause":
            for pc in c.children:
                if pc.type == "package_identifier":
                    namespace = _node_text(pc)

        elif c.type == "import_declaration":
            imports.append(_node_text(c))

        elif c.type == "type_declaration":
            for spec in c.children:
                if spec.type == "type_spec":
                    name = ""
                    kind = "type"
                    name_node = spec.child_by_field_name("name")
                    if name_node:
                        name = _node_text(name_node)
                    type_node = spec.child_by_field_name("type")
                    if type_node:
                        if type_node.type == "struct_type":
                            kind = "struct"
                        elif type_node.type == "interface_type":
                            kind = "interface"
                    if name:
                        classes.append({
                            "name": name,
                            "kind": kind,
                            "modifiers": "",
                            "base_classes": [],
                            "interfaces": [],
                            "methods": [],
                        })

        elif c.type == "function_declaration":
            fname = ""
            params_str = ""
            body = None
            ret_type = ""

            name_node = c.child_by_field_name("name")
            if name_node:
                fname = _node_text(name_node)

            for fc in c.children:
                if fc.type == "parameter_list":
                    params_str = _node_text(fc)[1:-1].strip()
                elif fc.type == "block":
                    body = fc

            result_node = c.child_by_field_name("result")
            if result_node:
                ret_type = _node_text(result_node)

            calls = _extract_go_invocations(body) if body else []

            if fname:
                if not classes or classes[-1]["name"] != "(global)":
                    classes.append({
                        "name": "(global)",
                        "kind": "module",
                        "modifiers": "",
                        "base_classes": [],
                        "interfaces": [],
                        "methods": [],
                    })
                classes[-1]["methods"].append({
                    "name": fname,
                    "modifiers": "",
                    "return_type": ret_type or "",
                    "parameters": params_str,
                    "calls": calls,
                })

        elif c.type == "method_declaration":
            fname = ""
            params_str = ""
            receiver = ""
            body = None
            ret_type = ""

            name_node = c.child_by_field_name("name")
            if name_node:
                fname = _node_text(name_node)

            for fc in c.children:
                if fc.type == "parameter_list":
                    # First parameter_list is the receiver, second is params
                    if not receiver:
                        receiver = _node_text(fc)
                    else:
                        params_str = _node_text(fc)[1:-1].strip()
                elif fc.type == "block":
                    body = fc

            result_node = c.child_by_field_name("result")
            if result_node:
                ret_type = _node_text(result_node)

            calls = _extract_go_invocations(body) if body else []

            # Find or create the receiver type class
            receiver_type = receiver.strip("()").split()[-1] if receiver else ""
            receiver_type = receiver_type.lstrip("*")
            target_class = None
            if receiver_type:
                for cls in classes:
                    if cls["name"] == receiver_type:
                        target_class = cls
                        break
            if not target_class:
                target_class = {
                    "name": receiver_type or "(global)",
                    "kind": "struct" if receiver_type else "module",
                    "modifiers": "",
                    "base_classes": [],
                    "interfaces": [],
                    "methods": [],
                }
                classes.append(target_class)

            if fname:
                target_class["methods"].append({
                    "name": fname,
                    "modifiers": "",
                    "return_type": ret_type or "",
                    "parameters": params_str,
                    "calls": calls,
                })

    return {
        "file_path": file_path,
        "namespace": namespace,
        "classes": classes,
        "imports": imports,
    }


# ===================================================================
# Rust extraction
# ===================================================================


def _extract_rust_invocations(body_node: ts.Node) -> list[str]:
    """Extract function/method call names from a Rust function body."""
    calls: list[str] = []

    def _walk(node: ts.Node):
        if node.type == "call_expression":
            func = node.child_by_field_name("function")
            if func:
                if func.type == "field_expression":
                    field = func.child_by_field_name("field")
                    if field:
                        calls.append(_node_text(field))
                elif func.type == "identifier":
                    calls.append(_node_text(func))
                elif func.type == "scoped_identifier":
                    name_node = func.child_by_field_name("name")
                    if name_node:
                        calls.append(_node_text(name_node))
        for child in node.children:
            _walk(child)

    _walk(body_node)
    return list(dict.fromkeys(calls))


def _parse_rust(tree: ts.Tree, file_path: str) -> dict[str, Any]:
    """Parse a Rust file AST into the entity schema."""
    root = tree.root_node
    imports: list[str] = []
    classes: list[dict] = []

    for c in root.children:
        if c.type == "use_declaration":
            imports.append(_node_text(c))

        elif c.type == "struct_item":
            name = ""
            name_node = c.child_by_field_name("name")
            if name_node:
                name = _node_text(name_node)
            if name:
                classes.append({
                    "name": name,
                    "kind": "struct",
                    "modifiers": "",
                    "base_classes": [],
                    "interfaces": [],
                    "methods": [],
                })

        elif c.type == "enum_item":
            name = ""
            name_node = c.child_by_field_name("name")
            if name_node:
                name = _node_text(name_node)
            if name:
                classes.append({
                    "name": name,
                    "kind": "enum",
                    "modifiers": "",
                    "base_classes": [],
                    "interfaces": [],
                    "methods": [],
                })

        elif c.type == "trait_item":
            name = ""
            name_node = c.child_by_field_name("name")
            if name_node:
                name = _node_text(name_node)
            if name:
                classes.append({
                    "name": name,
                    "kind": "trait",
                    "modifiers": "",
                    "base_classes": [],
                    "interfaces": [],
                    "methods": [],
                })

        elif c.type == "impl_item":
            impl_type = ""
            trait_name = ""
            methods: list[dict] = []

            type_node = c.child_by_field_name("type")
            if type_node:
                impl_type = _node_text(type_node)

            trait_node = c.child_by_field_name("trait")
            if trait_node:
                trait_name = _node_text(trait_node)

            # Extract methods from impl body
            body_node = c.child_by_field_name("body")
            if body_node:
                for item in body_node.children:
                    if item.type != "function_item":
                        continue
                    fname = ""
                    params_str = ""
                    body = None
                    ret_type = ""
                    vis = ""

                    name_node = item.child_by_field_name("name")
                    if name_node:
                        fname = _node_text(name_node)

                    for fc in item.children:
                        if fc.type == "parameters":
                            params_str = _node_text(fc)[1:-1].strip()
                        elif fc.type == "block":
                            body = fc
                        elif fc.type == "visibility_modifier":
                            vis = _node_text(fc)

                    ret_node = item.child_by_field_name("return_type")
                    if ret_node:
                        ret_type = _node_text(ret_node)

                    calls = _extract_rust_invocations(body) if body else []

                    if fname:
                        methods.append({
                            "name": fname,
                            "modifiers": vis,
                            "return_type": ret_type or "",
                            "parameters": params_str,
                            "calls": calls,
                        })

            # Attach methods to existing type or create new entry
            target_class = None
            for cls in classes:
                if cls["name"] == impl_type:
                    target_class = cls
                    break
            if target_class:
                target_class["methods"].extend(methods)
                if trait_name:
                    target_class["interfaces"].append(trait_name)
            else:
                interfaces = [trait_name] if trait_name else []
                classes.append({
                    "name": impl_type or "(impl)",
                    "kind": "struct",
                    "modifiers": "",
                    "base_classes": [],
                    "interfaces": interfaces,
                    "methods": methods,
                })

        elif c.type == "function_item":
            fname = ""
            params_str = ""
            body = None
            ret_type = ""
            vis = ""

            name_node = c.child_by_field_name("name")
            if name_node:
                fname = _node_text(name_node)

            for fc in c.children:
                if fc.type == "parameters":
                    params_str = _node_text(fc)[1:-1].strip()
                elif fc.type == "block":
                    body = fc
                elif fc.type == "visibility_modifier":
                    vis = _node_text(fc)

            ret_node = c.child_by_field_name("return_type")
            if ret_node:
                ret_type = _node_text(ret_node)

            calls = _extract_rust_invocations(body) if body else []

            if fname:
                if not classes or classes[-1]["name"] != "(global)":
                    classes.append({
                        "name": "(global)",
                        "kind": "module",
                        "modifiers": "",
                        "base_classes": [],
                        "interfaces": [],
                        "methods": [],
                    })
                classes[-1]["methods"].append({
                    "name": fname,
                    "modifiers": vis,
                    "return_type": ret_type or "",
                    "parameters": params_str,
                    "calls": calls,
                })

    return {
        "file_path": file_path,
        "namespace": "",
        "classes": classes,
        "imports": imports,
    }


# ===================================================================
# Ruby extraction
# ===================================================================


_RUBY_VISIBILITY = {"private", "protected", "public"}
_RUBY_ATTR_MACROS = {"attr_accessor", "attr_reader", "attr_writer"}
_RUBY_MIXINS = {"include", "extend", "prepend"}
_RUBY_REQUIRES = {"require", "require_relative", "load"}


def _ruby_simple_name(node: ts.Node | None) -> str:
    """Return the rightmost simple name of a constant or scope_resolution node.

    `Foo::Bar::Baz` → `Baz`. Phase 9 matches inheritance/call edges by simple
    name, so scoped names must be reduced.
    """
    if node is None:
        return ""
    if node.type == "scope_resolution":
        name_field = node.child_by_field_name("name")
        if name_field is not None:
            return _node_text(name_field)
    return _node_text(node)


def _ruby_params_text(node: ts.Node | None) -> str:
    """Parameter text, stripping surrounding parens only when present.

    Ruby parens are optional (`def foo a, b`), so a naive slice would corrupt
    paren-less signatures.
    """
    if node is None:
        return ""
    txt = _node_text(node).strip()
    if txt.startswith("(") and txt.endswith(")"):
        return txt[1:-1].strip()
    return txt


def _extract_ruby_invocations(body_node: ts.Node | None) -> list[str]:
    """Extract method call names from a Ruby method body."""
    calls: list[str] = []

    def _walk(node: ts.Node):
        if node.type == "call":
            name_node = node.child_by_field_name("method")
            if name_node is not None and name_node.type in ("identifier", "constant"):
                calls.append(_node_text(name_node))
        for child in node.children:
            _walk(child)

    if body_node is not None:
        _walk(body_node)
    return list(dict.fromkeys(calls))


def _extract_ruby_method(node: ts.Node, visibility: str, force_self: bool) -> dict | None:
    """Build a method dict from a `method` or `singleton_method` node.

    Accepts the `name` field whatever its node type, so operator methods
    (`==`, `<=>`, `[]`, `[]=`) and setters (`foo=`) are captured, not just
    plain identifiers.
    """
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    name = _node_text(name_node)
    if not name:
        return None

    is_class_method = force_self or node.type == "singleton_method"
    mods: list[str] = []
    if is_class_method:
        mods.append("self")
    # Bare `private`/`protected` only governs instance methods in Ruby; it does
    # not apply to class (`self.`) methods.
    if visibility != "public" and not is_class_method:
        mods.append(visibility)

    body = node.child_by_field_name("body")
    return {
        "name": name,
        "modifiers": " ".join(mods),
        "return_type": "",
        "parameters": _ruby_params_text(node.child_by_field_name("parameters")),
        "calls": _extract_ruby_invocations(body),
    }


def _scan_ruby_body(body_node: ts.Node | None, methods: list[dict],
                    interfaces: list[str], force_self: bool = False) -> None:
    """Extract direct members (methods, mixins, attr accessors) from a class/module body.

    Tracks `private`/`protected`/`public` visibility state. Does NOT descend
    into nested class/module declarations — the caller recurses into those so
    their members are not misattributed to the enclosing type.
    """
    if body_node is None:
        return
    visibility = "public"
    for child in body_node.children:
        if not child.is_named:
            continue
        t = child.type
        if t == "identifier" and _node_text(child) in _RUBY_VISIBILITY:
            visibility = _node_text(child)
        elif t in ("method", "singleton_method"):
            m = _extract_ruby_method(child, visibility, force_self)
            if m:
                methods.append(m)
        elif t == "singleton_class":
            # `class << self` — methods inside are class-level
            _scan_ruby_body(child.child_by_field_name("body"), methods,
                            interfaces, force_self=True)
        elif t == "call":
            name_node = child.child_by_field_name("method")
            if name_node is None or name_node.type != "identifier":
                continue
            mname = _node_text(name_node)
            args = child.child_by_field_name("arguments")
            if mname in _RUBY_MIXINS:
                if args is not None:
                    for arg in args.named_children:
                        if arg.type in ("constant", "scope_resolution"):
                            interfaces.append(_ruby_simple_name(arg))
            elif mname in _RUBY_ATTR_MACROS:
                if args is not None:
                    for arg in args.named_children:
                        if arg.type == "simple_symbol":
                            sym = _node_text(arg).lstrip(":")
                            if sym:
                                methods.append({
                                    "name": sym, "modifiers": mname,
                                    "return_type": "", "parameters": "", "calls": [],
                                })
            elif mname in _RUBY_VISIBILITY and args is not None:
                for arg in args.named_children:
                    if arg.type in ("method", "singleton_method"):
                        # e.g. `private def foo`
                        m = _extract_ruby_method(arg, mname, force_self)
                        if m:
                            methods.append(m)
                    elif arg.type == "simple_symbol":
                        # e.g. `private :foo` — retro-mark an already-defined method
                        target = _node_text(arg).lstrip(":")
                        for existing in reversed(methods):
                            if existing["name"] == target:
                                if mname not in existing["modifiers"].split():
                                    existing["modifiers"] = (
                                        existing["modifiers"] + " " + mname).strip()
                                break


def _dedup_ruby_methods(methods: list[dict]) -> list[dict]:
    """Drop synthesized attr_* accessors that collide with a real method.

    `attr_accessor :foo` and a real `def foo` would otherwise emit two methods
    named "foo" that share one graph ID (Phase 8 keys method IDs by name). A
    real def wins over the synthesized accessor, and duplicate attr_* of the
    same name collapse to one. Distinct real methods that share a name (instance
    vs `self.`) are left as-is, matching the other language parsers.
    """
    real_names = {m["name"] for m in methods
                  if m["modifiers"] not in _RUBY_ATTR_MACROS}
    seen_attr: set[str] = set()
    out: list[dict] = []
    for m in methods:
        if m["modifiers"] in _RUBY_ATTR_MACROS:
            if m["name"] in real_names or m["name"] in seen_attr:
                continue
            seen_attr.add(m["name"])
        out.append(m)
    return out


def _process_ruby_container(node: ts.Node, scope: list[str], classes: list[dict],
                            ns_state: dict) -> None:
    """Emit a class/module entry for *node* and recurse into nested classes/modules."""
    name = _ruby_simple_name(node.child_by_field_name("name"))
    if not name:
        return
    kind = "class" if node.type == "class" else "module"

    base_classes: list[str] = []
    if kind == "class":
        sc = node.child_by_field_name("superclass")
        if sc is not None:
            for ref in sc.named_children:
                base_classes.append(_ruby_simple_name(ref))
                break  # Ruby has a single superclass

    methods: list[dict] = []
    interfaces: list[str] = []
    body = node.child_by_field_name("body")
    _scan_ruby_body(body, methods, interfaces)
    methods = _dedup_ruby_methods(methods)

    classes.append({
        "name": name,
        "kind": kind,
        "modifiers": "",
        "base_classes": base_classes,
        "interfaces": list(dict.fromkeys(interfaces)),
        "methods": methods,
    })

    # The first definition nested inside a module records the namespace path.
    if scope and not ns_state["set"]:
        ns_state["namespace"] = "::".join(scope)
        ns_state["set"] = True

    if body is not None:
        for child in body.children:
            if child.type in ("class", "module"):
                _process_ruby_container(child, scope + [name], classes, ns_state)


def _parse_ruby(tree: ts.Tree, file_path: str) -> dict[str, Any]:
    """Parse a Ruby file AST into the entity schema."""
    root = tree.root_node
    classes: list[dict] = []
    imports: list[str] = []
    ns_state = {"namespace": "", "set": False}

    # Imports: require/require_relative/load with a literal string argument and
    # no receiver (so `Kernel.load`/`YAML.load` are excluded). Walks the whole
    # tree to catch conditionally-guarded requires; dynamic args are skipped.
    def _walk_requires(node: ts.Node):
        if node.type == "call" and node.child_by_field_name("receiver") is None:
            name_node = node.child_by_field_name("method")
            if (name_node is not None and name_node.type == "identifier"
                    and _node_text(name_node) in _RUBY_REQUIRES):
                args = node.child_by_field_name("arguments")
                if args is not None and args.named_children:
                    first = args.named_children[0]
                    if first.type == "string":
                        for sc in first.named_children:
                            if sc.type == "string_content":
                                imports.append(_node_text(sc))
                                break
        for child in node.children:
            _walk_requires(child)

    _walk_requires(root)

    # Top-level definitions
    global_methods: list[dict] = []
    for child in root.children:
        if child.type in ("class", "module"):
            _process_ruby_container(child, [], classes, ns_state)
        elif child.type in ("method", "singleton_method"):
            m = _extract_ruby_method(child, "public", force_self=False)
            if m:
                global_methods.append(m)
        elif child.type == "singleton_class":
            _scan_ruby_body(child.child_by_field_name("body"), global_methods,
                            [], force_self=True)

    # Top-level functions go into a pseudo-class (matches Python/Go/Rust/C).
    if global_methods:
        global_methods = _dedup_ruby_methods(global_methods)
        classes.append({
            "name": "(global)",
            "kind": "module",
            "modifiers": "",
            "base_classes": [],
            "interfaces": [],
            "methods": global_methods,
        })

    return {
        "file_path": file_path,
        "namespace": ns_state["namespace"],
        "classes": classes,
        "imports": list(dict.fromkeys(imports)),
    }


# ===================================================================
# Kotlin extraction
# ===================================================================


_KOTLIN_MODIFIER_TYPES = {
    "visibility_modifier", "class_modifier", "inheritance_modifier",
    "member_modifier", "function_modifier", "property_modifier",
    "parameter_modifier", "platform_modifier",
}
# Type-bearing nodes used for return types, property types, and supertypes.
_KOTLIN_TYPE_NODES = (
    "user_type", "nullable_type", "parenthesized_type",
    "function_type", "dynamic_type", "not_nullable_type",
)


def _kotlin_child(node: ts.Node, *types: str) -> ts.Node | None:
    """First direct child whose type is one of *types* (anonymous tokens included)."""
    for ch in node.children:
        if ch.type in types:
            return ch
    return None


def _kotlin_descendant(node: ts.Node, type_name: str) -> ts.Node | None:
    """First depth-first descendant of the given type (node itself excluded)."""
    for ch in node.children:
        if ch.type == type_name:
            return ch
        found = _kotlin_descendant(ch, type_name)
        if found is not None:
            return found
    return None


def _kotlin_modifiers(node: ts.Node) -> str:
    """Space-joined modifier keywords from a declaration's `modifiers` child.

    Annotations live under the same `modifiers` wrapper but are skipped, so the
    result is just keywords like `data`, `abstract`, `private`, `open`.
    """
    mods: list[str] = []
    for ch in node.children:
        if ch.type == "modifiers":
            for m in ch.children:
                if m.type in _KOTLIN_MODIFIER_TYPES:
                    mods.append(_node_text(m))
    return " ".join(mods)


def _kotlin_body(node: ts.Node) -> ts.Node | None:
    """The `class_body`/`enum_class_body` child of a type declaration."""
    return _kotlin_child(node, "class_body", "enum_class_body")


def _kotlin_members(body: ts.Node):
    """Yield member nodes of a class body, flattening `ERROR` wrappers.

    The grammar parses a single-line body (e.g. `interface L { fun on() }`) as
    an `enum_class_body` whose members are nested inside an `ERROR` node; without
    flattening, those members would be silently dropped. This also makes member
    extraction resilient to partially-broken source.
    """
    for ch in body.children:
        if ch.type == "ERROR":
            yield from _kotlin_members(ch)
        else:
            yield ch


def _kotlin_is_enum(node: ts.Node) -> bool:
    """True if a `class_declaration` is an `enum class`."""
    for ch in node.children:
        if ch.type == "enum_class_body":
            return True
        if ch.type == "modifiers":
            for m in ch.children:
                if m.type == "class_modifier" and _node_text(m) == "enum":
                    return True
    return False


def _kotlin_type_name(node: ts.Node | None) -> str:
    """Rightmost simple name of a `user_type` (or a node that wraps one).

    `com.example.Base` -> `Base`, `List<String>` -> `List`. Phase 9 matches
    inheritance/call edges by simple name, so qualified and generic types are
    reduced to the bare name; type arguments are not descended into.
    """
    if node is None:
        return ""
    if node.type != "user_type":
        ut = _kotlin_descendant(node, "user_type")
        if ut is None:
            return ""
        node = ut
    name = ""
    for ch in node.children:
        if ch.type == "identifier":
            name = _node_text(ch)  # last direct identifier == rightmost segment
    return name


def _kotlin_call_name(callee: ts.Node | None) -> str:
    """Resolve the called name from a `call_expression` callee expression."""
    if callee is None:
        return ""
    if callee.type == "identifier":
        return _node_text(callee)
    if callee.type == "navigation_expression":
        name = ""
        for ch in callee.children:
            if ch.type == "identifier":
                name = _node_text(ch)  # rightmost selector is the method name
        return name
    return ""


def _extract_kotlin_invocations(body_node: ts.Node | None) -> list[str]:
    """Call names within a function body (deduped, order preserved)."""
    calls: list[str] = []

    def _walk(node: ts.Node):
        if node.type == "call_expression":
            callee = None
            for ch in node.children:
                if ch.type in ("value_arguments", "type_arguments", "annotated_lambda"):
                    continue
                callee = ch
                break
            name = _kotlin_call_name(callee)
            if name:
                calls.append(name)
        for child in node.children:
            _walk(child)

    if body_node is not None:
        _walk(body_node)
    return list(dict.fromkeys(calls))


def _extract_kotlin_function(node: ts.Node, extra_mod: str = "") -> dict | None:
    """Build a method dict from a `function_declaration` node.

    The return type sits positionally between `function_value_parameters` and
    the `function_body`, so it is captured by scanning that gap.
    """
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    name = _node_text(name_node)
    if not name:
        return None

    mods = _kotlin_modifiers(node)
    if extra_mod:
        mods = (extra_mod + " " + mods).strip()

    params = ""
    ret_type = ""
    body = None
    seen_params = False
    for ch in node.children:
        if ch.type == "function_value_parameters":
            params = _node_text(ch)[1:-1].strip()
            seen_params = True
        elif ch.type == "function_body":
            body = ch
            break
        elif seen_params and ch.type in _KOTLIN_TYPE_NODES:
            ret_type = _node_text(ch)

    return {
        "name": name,
        "modifiers": mods,
        "return_type": ret_type,
        "parameters": params,
        "calls": _extract_kotlin_invocations(body),
    }


def _extract_kotlin_property(node: ts.Node) -> dict | None:
    """Build a member dict from a `property_declaration` node.

    Properties are surfaced as zero-parameter members (mirroring the Ruby
    attr_* handling) so they appear in the graph. `modifiers` records the
    `val`/`var` kind plus any visibility/inheritance keywords; `calls` captures
    initializer, delegate (`by lazy { ... }`), and custom getter/setter calls so
    those edges are not lost.
    """
    var = _kotlin_descendant(node, "variable_declaration")
    if var is None:
        return None
    name_node = _kotlin_child(var, "identifier")
    if name_node is None:
        return None
    k = _kotlin_child(node, "val", "var")
    kind = k.type if k is not None else ""
    mods = _kotlin_modifiers(node)
    mods = (mods + " " + kind).strip() if mods else kind
    type_node = _kotlin_child(var, *_KOTLIN_TYPE_NODES)
    return {
        "name": _node_text(name_node),
        "modifiers": mods,
        "return_type": _node_text(type_node) if type_node is not None else "",
        "parameters": "",
        "calls": _extract_kotlin_invocations(node),
    }


def _extract_kotlin_class_param(cp: ts.Node) -> dict | None:
    """A primary-constructor `val`/`var` parameter -> property member."""
    name_node = _kotlin_child(cp, "identifier")
    if name_node is None:
        return None
    k = _kotlin_child(cp, "val", "var")
    kind = k.type if k is not None else ""
    mods = _kotlin_modifiers(cp)
    mods = (mods + " " + kind).strip() if mods else kind
    type_node = _kotlin_child(cp, *_KOTLIN_TYPE_NODES)
    return {
        "name": _node_text(name_node),
        "modifiers": mods,
        "return_type": _node_text(type_node) if type_node is not None else "",
        "parameters": "",
        "calls": _extract_kotlin_invocations(cp),
    }


def _extract_kotlin_constructor(node: ts.Node) -> dict:
    """A `secondary_constructor` -> method named `constructor` with its calls.

    Mirrors the Java/C# parsers, which surface constructors as methods; without
    this, call edges made in a secondary constructor body would be lost.
    """
    fvp = _kotlin_child(node, "function_value_parameters")
    params = _node_text(fvp)[1:-1].strip() if fvp is not None else ""
    return {
        "name": "constructor",
        "modifiers": _kotlin_modifiers(node),
        "return_type": "",
        "parameters": params,
        "calls": _extract_kotlin_invocations(_kotlin_child(node, "block")),
    }


def _process_kotlin_type(node: ts.Node, classes: list[dict]) -> None:
    """Emit a class/interface/object/enum entry and recurse into nested types."""
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = _node_text(name_node)
    if not name:
        return

    if node.type == "object_declaration":
        kind = "object"
    elif any(ch.type == "interface" for ch in node.children):
        # Interfaces reuse `class_declaration`; the keyword is an anonymous child.
        kind = "interface"
    elif _kotlin_is_enum(node):
        kind = "enum"
    else:
        kind = "class"

    base_classes: list[str] = []
    interfaces: list[str] = []
    specs = _kotlin_child(node, "delegation_specifiers")
    if specs is not None:
        for spec in specs.children:
            if spec.type != "delegation_specifier":
                continue
            # A superclass is constructed (has parens); interfaces are listed bare.
            if _kotlin_child(spec, "constructor_invocation") is not None:
                base_classes.append(_kotlin_type_name(spec))
            else:
                interfaces.append(_kotlin_type_name(spec))

    methods: list[dict] = []

    # `val`/`var` primary-constructor parameters are properties of the class.
    primary = _kotlin_child(node, "primary_constructor")
    if primary is not None:
        cps = _kotlin_child(primary, "class_parameters")
        if cps is not None:
            for cp in cps.children:
                if cp.type == "class_parameter" and _kotlin_child(cp, "val", "var"):
                    prop = _extract_kotlin_class_param(cp)
                    if prop:
                        methods.append(prop)

    body = _kotlin_body(node)
    if body is not None:
        for ch in _kotlin_members(body):
            if ch.type == "function_declaration":
                m = _extract_kotlin_function(ch)
                if m:
                    methods.append(m)
            elif ch.type == "property_declaration":
                p = _extract_kotlin_property(ch)
                if p:
                    methods.append(p)
            elif ch.type == "secondary_constructor":
                methods.append(_extract_kotlin_constructor(ch))
            elif ch.type == "anonymous_initializer":
                # `init { ... }` — keep its call edges under a synthetic member.
                init_calls = _extract_kotlin_invocations(ch)
                if init_calls:
                    methods.append({
                        "name": "init", "modifiers": "", "return_type": "",
                        "parameters": "", "calls": init_calls,
                    })
            elif ch.type == "companion_object":
                # Companion members behave like statics; fold into this type.
                cbody = _kotlin_body(ch)
                if cbody is not None:
                    for cch in _kotlin_members(cbody):
                        if cch.type == "function_declaration":
                            m = _extract_kotlin_function(cch, extra_mod="companion")
                            if m:
                                methods.append(m)
                        elif cch.type == "property_declaration":
                            p = _extract_kotlin_property(cch)
                            if p:
                                p["modifiers"] = ("companion " + p["modifiers"]).strip()
                                methods.append(p)

    classes.append({
        "name": name,
        "kind": kind,
        "modifiers": _kotlin_modifiers(node),
        "base_classes": base_classes,
        "interfaces": interfaces,
        "methods": methods,
    })

    # Nested types are emitted as their own entries (not merged into the parent).
    if body is not None:
        for ch in _kotlin_members(body):
            if ch.type in ("class_declaration", "object_declaration"):
                _process_kotlin_type(ch, classes)


def _kotlin_import_text(node: ts.Node) -> str:
    """Full import path, keeping wildcards (`.*`) and aliases (`as X`).

    A bare `qualified_identifier` would drop the trailing `.*`, making a wildcard
    import indistinguishable from importing a class of that name.
    """
    parts = _node_text(node).split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


def _kotlin_receiver_name(node: ts.Node) -> str:
    """Receiver type of an extension `function_declaration`, else "".

    `fun List<T>.foo()` -> `List`. The receiver `user_type` precedes the
    `function_value_parameters`; a non-extension function has no type there.
    """
    for ch in node.children:
        if ch.type == "function_value_parameters":
            return ""
        if ch.type in ("user_type", "nullable_type"):
            return _kotlin_type_name(ch)
    return ""


def _kotlin_find_or_make(classes: list[dict], name: str) -> dict:
    """Find a class entry by name, or append a stub (for extension receivers)."""
    for cls in classes:
        if cls["name"] == name and cls["name"] != "(global)":
            return cls
    entry = {
        "name": name, "kind": "class", "modifiers": "",
        "base_classes": [], "interfaces": [], "methods": [],
    }
    classes.append(entry)
    return entry


def _parse_kotlin(tree: ts.Tree, file_path: str) -> dict[str, Any]:
    """Parse a Kotlin file AST into the entity schema."""
    root = tree.root_node
    classes: list[dict] = []
    imports: list[str] = []
    namespace = ""
    global_methods: list[dict] = []

    # Pass 1: package, imports, and type declarations.
    for c in root.children:
        if c.type == "package_header":
            qid = _kotlin_child(c, "qualified_identifier")
            if qid is not None:
                namespace = _node_text(qid)
        elif c.type == "import":
            txt = _kotlin_import_text(c)
            if txt:
                imports.append(txt)
        elif c.type == "import_list":
            # Defensive: some grammar versions group imports under a list node.
            for imp in c.children:
                if imp.type == "import":
                    txt = _kotlin_import_text(imp)
                    if txt:
                        imports.append(txt)
        elif c.type in ("class_declaration", "object_declaration"):
            _process_kotlin_type(c, classes)

    # Pass 2: top-level functions/properties. An extension function attaches to
    # its receiver type's entry (now present if declared in this file), mirroring
    # how the Go parser attaches methods to their receiver struct.
    for c in root.children:
        if c.type == "function_declaration":
            m = _extract_kotlin_function(c)
            if m:
                recv = _kotlin_receiver_name(c)
                if recv:
                    m["modifiers"] = ("extension " + m["modifiers"]).strip()
                    _kotlin_find_or_make(classes, recv)["methods"].append(m)
                else:
                    global_methods.append(m)
        elif c.type == "property_declaration":
            p = _extract_kotlin_property(c)
            if p:
                global_methods.append(p)

    # Top-level functions/properties go into a pseudo-class (matches Go/Rust/Ruby).
    if global_methods:
        classes.append({
            "name": "(global)",
            "kind": "module",
            "modifiers": "",
            "base_classes": [],
            "interfaces": [],
            "methods": global_methods,
        })

    return {
        "file_path": file_path,
        "namespace": namespace,
        "classes": classes,
        "imports": list(dict.fromkeys(imports)),
    }


# ===================================================================
# Public API
# ===================================================================


_LANG_PARSERS = {
    "c_sharp": _parse_csharp,
    "java": _parse_java,
    "c": _parse_c_cpp,
    "cpp": _parse_c_cpp,
    "python": _parse_python,
    "typescript": _parse_js_ts,
    "tsx": _parse_js_ts,
    "javascript": _parse_js_ts,
    "go": _parse_go,
    "rust": _parse_rust,
    "ruby": _parse_ruby,
    "kotlin": _parse_kotlin,
}


def _node_source(node: ts.Node, source: bytes) -> str:
    """Extract the source text for an AST node."""
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


# Type declarations we split into member-level units
_TYPE_DECLS = {
    "class_declaration", "struct_declaration", "interface_declaration",
    "enum_declaration", "record_declaration",
    # Python
    "class_definition",
    # Go
    "type_declaration",
    # Rust
    "struct_item", "enum_item", "trait_item", "impl_item",
}

# Containers that hold type declarations
_CONTAINERS = {
    "namespace_declaration", "file_scoped_namespace_declaration", "declaration_list",
    # Rust
    "declaration_list",
}


def _collect_units(node: ts.Node, source: bytes, max_chars: int) -> list[str]:
    """Walk an AST node and collect semantic units that fit within max_chars.

    A "unit" is a complete, self-contained piece of code: a using block,
    a class, a method, a field, etc. Large classes are broken into
    member-level units. Oversized single members get character-split
    with a context header.
    """
    units: list[str] = []

    # Group consecutive using directives
    usings: list[str] = []
    i = 0
    children = node.children
    while i < len(children):
        child = children[i]
        if child.type in ("using_directive", "import_declaration",
                          "import_statement", "import_from_statement",
                          "use_declaration"):
            usings.append(_node_source(child, source))
            i += 1
            continue
        if usings:
            units.append("\n".join(usings))
            usings = []

        size = child.end_byte - child.start_byte

        if child.type in _TYPE_DECLS:
            if size <= max_chars:
                # Whole class fits — keep it as one unit
                units.append(_node_source(child, source))
            else:
                # Class too large — break into members
                class_name = ""
                for cc in child.children:
                    if cc.type == "identifier":
                        class_name = _node_source(cc, source)
                        break

                # Collect the class header (modifiers, name, base_list, opening brace)
                header_parts = []
                for cc in child.children:
                    if cc.type == "declaration_list":
                        break
                    header_parts.append(_node_source(cc, source))
                header = " ".join(header_parts)

                for cc in child.children:
                    if cc.type != "declaration_list":
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
                            # Single member too large (e.g., 2.95MB hex field)
                            # Character-split with context
                            member_name = ""
                            for mn in member.children:
                                if mn.type == "identifier":
                                    member_name = _node_source(mn, source)
                                    break
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
            # Recurse into namespace/declaration_list
            sub_units = _collect_units(child, source, max_chars)
            if sub_units:
                # Add namespace header to first sub-unit
                ns_name = ""
                for cc in child.children:
                    if cc.type in ("identifier", "qualified_name"):
                        ns_name = _node_source(cc, source)
                        break
                if ns_name and sub_units:
                    sub_units[0] = f"namespace {ns_name} {{\n{sub_units[0]}"
                    sub_units[-1] = f"{sub_units[-1]}\n}}"
                units.extend(sub_units)

        elif child.type == "package_declaration":
            units.append(_node_source(child, source))

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

    if usings:
        units.append("\n".join(usings))

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
