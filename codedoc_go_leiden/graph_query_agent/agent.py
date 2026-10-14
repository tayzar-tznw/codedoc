"""Graph Query Agent.

root_agent (orchestrator) delegates to graph_agent, which runs Spanner Graph
GQL for structural code analysis. The graph nodes also carry the generated
summaries, so no separate documentation-search agent is needed.
"""

import sys

from google.adk.agents import Agent
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.tools import ToolContext
from google.cloud import spanner

# All env-derived settings come from config.py (which loads the root .env).
from .config import GRAPH_NAME, MODEL, PROJECT_ID, SPANNER_DATABASE, SPANNER_INSTANCE

_db = None


def _settings_line() -> str:
    project = PROJECT_ID or "ADC / gcloud default (GOOGLE_CLOUD_PROJECT is not set)"
    return (f"[graph_query_agent] graph {GRAPH_NAME} on Spanner "
            f"{SPANNER_INSTANCE}/{SPANNER_DATABASE}, project: {project}")


# Printed to stderr when the agent starts (adk run, MCP server, webapp).
print(_settings_line(), file=sys.stderr)


def _get_database():
    global _db
    if _db is None:
        client = spanner.Client(project=PROJECT_ID, disable_builtin_metrics=True)
        # The project the client library settled on: the setting, else the
        # ADC / gcloud default (same labels as the pipeline's "Project:" line).
        resolved = getattr(client, "project", None)
        if not resolved:
            shown = "ADC / gcloud default (unresolved)"
        elif PROJECT_ID:
            shown = resolved
        else:
            shown = f"{resolved} (ADC / gcloud default)"
        print(f"[graph_query_agent] Spanner project: {shown}", file=sys.stderr)
        instance = client.instance(SPANNER_INSTANCE)
        _db = instance.database(SPANNER_DATABASE)
    return _db


def run_gql_query(gql_query: str, tool_context: ToolContext) -> dict:
    """Execute a GQL query against the Spanner code knowledge graph.

    Use this for structural code analysis of Go code:
    - Calls between functions and methods, interface implementations, embedding
    - Package imports, file dependencies, impact analysis
    - Listing packages, types, functions, methods, directories, topics

    Args:
        gql_query: A valid GQL query that starts with the GRAPH line given in
            the instruction.

    Returns:
        dict: Query results or error.
    """
    database = _get_database()
    try:
        results = []
        with database.snapshot() as snapshot:
            result_set = snapshot.execute_sql(gql_query)
            fields = []
            try:
                if hasattr(result_set, 'fields') and result_set.fields:
                    fields = [f.name for f in result_set.fields]
            except Exception:
                pass
            for row in result_set:
                if not fields:
                    try:
                        fields = [f.name for f in result_set.fields]
                    except Exception:
                        fields = [f"col_{i}" for i in range(len(row))]
                row_dict = {}
                for i, val in enumerate(row):
                    key = fields[i] if i < len(fields) else f"col_{i}"
                    row_dict[key] = str(val)[:500] if val is not None else None
                results.append(row_dict)
        return {"status": "success", "query": gql_query, "row_count": len(results), "results": results[:50]}
    except Exception as e:
        return {"status": "error", "query": gql_query, "error_message": str(e)[:500]}


# ===================================================================
# Agent 1: Graph Agent (Spanner Graph GQL)
# ===================================================================

# Schema and conventions (spec §6). Plain strings, not f-strings: the text
# holds GQL property maps and Go code in braces.
_IDENTITY = """
## リポジトリと同一性
- 全ノードに `repo`(リポジトリ名)があります。複数のリポジトリが 1 つのグラフに同居し、同じ名前でも repo が違えば別ノードです。特定のリポジトリに絞るときは `WHERE n.repo = '<repo>'` を付けてください。
- `module` は Go のモジュールパス(go.mod の module 行。例 `example.com/iryo/billing`)です。`package` と `Packages.import_path` はパッケージの import パス、`Files.package_name` はパッケージ句の名前です。1 つのリポジトリに複数のモジュールがあります。
- 完全な名前は、型が `fqcn`、関数とメソッドが `fqmn` で、どちらも `<import パス>.<qualname>` の形です(例 `example.com/iryo/billing.FeeCalculator.Calculate`)。単純名 `name` は重複するので、厳密に指すときは `fqcn` / `fqmn` を使ってください。
- ファイルごとにある `<package>`、`<init>`、`init#k`、`_#k` と、ビルド制約で分かれた同名の宣言(ビルドの双子)は、同じパッケージの中で `fqcn` / `fqmn` が重なります。`Files.path` か `file_id` で区別してください。
"""

_NODE_LINES = """
## ノードラベル
- Files (file_id, file_name, extension, directory, path, origin, generated, package, package_name, module, build_constraint, build_included, summary, repo, embedding)
- Packages (package_id, import_path, name, directory, module, origin, file_count, summary, repo, embedding)
- Classes (class_id, name, qualname, package, fqcn, file_id, kind, type_params, embeds, start_line, end_line, origin, module, summary, repo, embedding)
- Methods (method_id, name, class_id, file_id, qualname, fqmn, signature, member_kind, receiver, type_params, has_body, start_line, end_line, origin, module, entry_point, summary, repo, embedding)
- Directories (dir_id, name, summary, repo, embedding)
"""

_NODE_NOTES = """
`Classes` は Go の型(struct、interface、名前付きの型、型エイリアス)と、ファイルごとの擬似型 `<package>` です。`Methods` は関数、メソッド、インターフェースのメソッド、`init` 関数、クロージャ、`<init>` です。
`Packages` はディレクトリとパッケージ句の名前の組ごとに 1 行です。同じディレクトリの外部テストパッケージ(import パスの末尾が `_test`)は別の行になります。
"""

_SUMMARY_NOTES = """`embedding` はベクトルなので返さないでください。自然文の要約は全ノードの `summary` にあります。`--wiki` を実行する前は Go のドキュメントコメント(無ければ空)です。
"""

_EDGE_LINES = """
## エッジラベル
- PackageImports (edge_id, source_file, target_package, import_path, alias, kind): Files → Packages。import 先がこのリポジトリのパッケージに確定したもの。kind = normal | dot | blank、alias は別名(無ければ空)。標準ライブラリや依存モジュールの import は含まない
- FileDependsOn (edge_id, source_file, target_file, resolution): Files → Files。確定した呼び出しと埋め込みの相手のファイル(自分自身を除く)。import は含まない
- ClassEmbeds (edge_id, child_class, embedded_class, pointer, resolution): Classes → Classes。型に埋め込まれた型。pointer = true は `*T` の埋め込み
- ClassImplements (edge_id, class_id, interface_id, pointer_receiver, resolution): Classes → Classes。型がこのリポジトリのインターフェースを満たす。pointer_receiver = true は `*T` だけが満たす
- MethodCalls (edge_id, caller_method, callee_method, callee_name, resolution, site_kind, call_line): Methods → Methods。確定した呼び出しだけ
- PossiblyCalls (edge_id, caller_method, callee_method, callee_name, reason, candidate_count, open_world): Methods → Methods。実行時に呼ばれうる候補
- FileDefinesClass (edge_id, file_id, class_id): Files → Classes
- ClassDefinesMethod (edge_id, class_id, method_id): Classes → Methods。型の宣言と別のファイルにあるメソッドもその型につながる(メソッドのファイルは Methods.file_id)
- DirContainsFile (edge_id, dir_id, file_id): Directories → Files
- PackageContainsFile (edge_id, package_id, file_id): Packages → Files
"""

_CROSS_REPO_EDGE_LINES = """- CrossRepoCalls (edge_id, caller_method, callee_method, source_repo, target_repo, symbol, site_kind): Methods → Methods。別リポジトリの関数・メソッドの呼び出し
- CrossRepoImports (edge_id, source_file, target_package, source_repo, target_repo, import_path): Files → Packages。別リポジトリのパッケージの import
- CrossRepoRef (edge_id, source_class, target_class, source_repo, target_repo, symbol, kind): Classes → Classes。別リポジトリの型の埋め込み(kind = embeds)と実装(kind = implements)
"""

_REPRESENTATION = """
## コードの表し方
- 各ファイルに擬似型 `<package>` が 1 つあります(Classes.kind = 'package'、qualname = '<package>')。そのファイルのパッケージ直下の関数、`init` 関数、`<init>` は、この型のメソッドです。
- qualname の形:
  - 関数は `F`、メソッドは `T.M` です。値レシーバーとポインタレシーバーは名前で区別せず、レシーバーは `receiver` 列(例 `*FeeCalculator`)にあります。型パラメータは名前に入らず、`type_params` 列にあります。
  - インターフェースのメソッドは `I.M` で、Classes 行はインターフェースです。
  - クロージャ(関数リテラル)は `F.func1` です。入れ子は `F.func1.func1` で、番号は親ごとにソース順です。パッケージレベルの変数の初期化式の中では `<init>.func1` です。
  - `init` 関数は `init#1`、`init#2`(ファイルごとの連番)、空白の関数 `func _()` は `_#1`、`_#2` です。
  - `<init>` はファイルのパッケージレベルの変数の初期化式をまとめた擬似関数で、そこからの呼び出しの呼び出し元になります。
- Methods.member_kind: func | method | interface_method | init | closure | var_init
- Classes.kind: struct | interface | named | alias | package
- origin: package(ライブラリのパッケージ)| command(`package main`)| test(同じパッケージの `_test.go`)| xtest(外部テストパッケージの `_test.go`)
- `entry_point` は `func main` の行にだけ入るコマンド名(ディレクトリ名)です。
- Files の `generated` は生成コード(`// Code generated … DO NOT EDIT.`)、`build_constraint` は `//go:build` の式、`build_included` は既定のビルド構成(GOOS、GOARCH、タグ)でコンパイルされるかどうかです。`has_body` = false は本体の無い宣言(アセンブリや `//go:linkname`)です。
- `generated`、`build_included`、`has_body`、`pointer`、`pointer_receiver`、`open_world` は BOOL です。`= true` / `= false` で比べてください。
"""

_CALLS = """
## 確定したエッジと候補
- MethodCalls は、相手がこのリポジトリの関数・メソッド・クロージャ・インターフェースのメソッドに確定したときだけ作られます。エッジが無いことは、呼んでいないことを意味しません。
- 標準ライブラリや依存モジュールへの呼び出し(external)、組み込み関数(builtin)、相手が実行時にしか決まらない呼び出し(dynamic)、確定できなかった呼び出し(unresolved)はエッジになりません。型変換 `T(x)` は呼び出しに数えません。
- MethodCalls.site_kind: call | go | defer | range
- MethodCalls.resolution:
  - types: go/types が相手を確定した
  - convention:funclit: その場で呼ぶ関数リテラル `func(){…}()`(go と defer を含む)。相手はクロージャ
  - convention:func-alias: 一度だけ束縛され、再代入もアドレスの取得も無いローカル変数の関数値
  - convention:linkname: 本体の無い宣言の `//go:linkname` の相手
  - convention:range: range-over-func(`for v := range f`)の暗黙の呼び出し
- PossiblyCalls.reason:
  - implementation: インターフェースのメソッドの呼び出しの、このリポジトリの実装の候補
  - variant: ファイルのパッケージ変種(本体とテスト)で相手が食い違った
- candidate_count は候補の数です。候補が POSSIBLY_CALLS_MAX_CANDIDATES を超える呼び出しは PossiblyCalls にも載りません。

## インターフェースの呼び出し
- パッケージで宣言した名前付きのインターフェースのメソッドの呼び出しは、そのメソッド(member_kind = 'interface_method')への MethodCalls になります。そのインターフェースを制約にした型パラメータを通した呼び出しも同じです。
- 実行時に呼ばれうる実装は PossiblyCalls(reason = 'implementation')です。候補は受け手の静的な型を満たす、このリポジトリの型のメソッドです。Go のインターフェースは宣言なしに満たされるので、実装が 1 つしかなくても MethodCalls にはなりません。
- 名前の無いインターフェース(インターフェース型リテラル、関数の中で宣言したインターフェース、インラインの制約)のメソッドにはノードがありません。その呼び出しは MethodCalls にならず、このリポジトリの実装があれば ambiguous(PossiblyCalls、reason = 'implementation')、無ければ dynamic です。
- `open_world` = true は、リポジトリの外の型(標準ライブラリや依存モジュールの型。非公開の型を含む)もそのインターフェースを満たすことを示します。実際の相手が PossiblyCalls のどれでもないことがあります。
- インターフェースがリポジトリの外(`io.Writer` など)のときは MethodCalls がありませんが、このリポジトリの実装は PossiblyCalls に載ります。組み込みの `error` の `Error()` は builtin で、候補はありません。
"""

_STATUSES = """
## 呼び出しの status と理由
解析は呼び出しごとに status を決めます。グラフに入るのは resolved(MethodCalls)と、ambiguous とインターフェースの候補(PossiblyCalls)だけです。status と理由はグラフの列にはありません。エッジが無い理由を問われたら、次の意味で説明してください。
- status:
  - resolved: このリポジトリの相手が 1 つに決まった(関数、メソッド、クロージャ、インターフェースのメソッド)
  - external: 相手がリポジトリの外(標準ライブラリ、依存モジュール、cgo、別リポジトリ)。別リポジトリの相手は crossref が CrossRepoCalls にする
  - builtin: 組み込み関数(len、append など)と、組み込みの error インターフェースの Error メソッド
  - ambiguous: 相手が 1 つに決まらず、候補が PossiblyCalls にある。ファイルのパッケージ変種で相手が食い違った(reason = 'variant')、または名前の無いインターフェースのメソッドを呼んだ(reason = 'implementation')
  - dynamic: 関数値(変数、引数、フィールド、map やスライスの要素)の呼び出しと、このリポジトリに実装の無い、名前の無いインターフェースのメソッドの呼び出し
  - unresolved: 確かな答えが無い
- unresolved の理由:
  - missing-module: 必要なモジュールがオフラインで見つからない(`--download-modules` で取得できる)
  - incomplete-type: 型の欠けた埋め込みがあり、昇格したメソッドが実行時には別の相手になりうる
  - excluded-by-build: どのビルド構成でもコンパイルされないファイル
  - toolchain: go 行がツールチェーンより新しいグループ
  - no-declaration: 相手の位置を含むノードが無い
  - stale: 実行中にファイルが変わった
  - syntax-error: 構文エラーより後ろの位置
  - unscanned: スキャンしないディレクトリ(testdata など)やシンボリックリンクの重複
  - outside-repo: リポジトリの外のディレクトリ(ピアではない)
  - no-module: go.mod の下に無いファイル
  - load-error: パッケージを読み込めなかった
"""

_BOUNDARIES = """
## 境界
- モジュールどうし、パッケージどうしの呼び出しは通常の MethodCalls です。境界をまたぐかどうかは両端の `module` / `package` を比べて判断してください。
- 同じリポジトリの別モジュールへの呼び出しがエッジになるのは、go コマンドがリポジトリ内のソースをコンパイルするとき(go.work のワークスペースか、リポジトリ内へのローカルの replace)だけです。それ以外は公開版への external で、エッジはありません。
- パッケージの依存は PackageImports(ファイル → パッケージ)です。パッケージ単位にまとめるときは PackageContainsFile でファイルの属するパッケージをたどってください。
- 別リポジトリへの依存を表すのは CrossRepoCalls(呼び出し)、CrossRepoImports(import)、CrossRepoRef(埋め込みと実装)だけです。通常のエッジはリポジトリをまたぎません。
"""

# GQL cookbook: key -> (title, query without the GRAPH line). Each query is
# checked against NODE_SPEC / EDGE_SPEC by tests/test_agent_instruction.py and
# `python -m scripts.gql_lint`.
_COOKBOOK: dict[str, tuple[str, str]] = {
    "callers": ("この関数・メソッドを呼んでいるのは誰か(確定した呼び出し)", """\
MATCH (caller:Methods)-[e:MethodCalls]->(callee:Methods {fqmn: 'example.com/iryo/billing.FeeCalculator.Calculate'})
RETURN caller.fqmn, caller.module, e.resolution, e.site_kind, e.call_line"""),
    "callees": ("この関数・メソッドが呼んでいるもの(確定した呼び出し)", """\
MATCH (caller:Methods {fqmn: 'example.com/iryo/reception-desk/internal/flows/accounting.Run'})-[e:MethodCalls]->(callee:Methods)
RETURN callee.fqmn, callee.member_kind, callee.module, e.resolution, e.site_kind, e.call_line"""),
    "possible_callees": ("インターフェースの呼び出しの実装の候補(呼ばれうるもの)", """\
MATCH (caller:Methods {fqmn: 'example.com/iryo/reception-desk/internal/flows/accounting.charge'})-[p:PossiblyCalls]->(callee:Methods)
RETURN callee.fqmn, p.callee_name, p.reason, p.candidate_count, p.open_world"""),
    "methods": ("型のメソッド一覧", """\
MATCH (c:Classes {fqcn: 'example.com/iryo/billing.FeeCalculator'})-[:ClassDefinesMethod]->(m:Methods)
RETURN m.qualname, m.member_kind, m.receiver, m.signature"""),
    "package_functions": ("パッケージ直下の関数(擬似型 <package> のメソッド)", """\
MATCH (f:Files {package: 'example.com/iryo/billing'})-[:FileDefinesClass]->(c:Classes {kind: 'package'})-[:ClassDefinesMethod]->(m:Methods)
RETURN f.path, m.qualname, m.member_kind, m.signature"""),
    "package_types": ("パッケージの型", """\
MATCH (p:Packages {import_path: 'example.com/iryo/billing'})-[:PackageContainsFile]->(f:Files)-[:FileDefinesClass]->(c:Classes)
WHERE c.kind <> 'package'
RETURN c.fqcn, c.kind, f.path"""),
    "implementers": ("インターフェースを満たすこのリポジトリの型", """\
MATCH (t:Classes)-[e:ClassImplements]->(i:Classes {fqcn: 'example.com/iryo/kernel/policy.PaymentPolicy'})
RETURN t.fqcn, t.kind, t.module, e.pointer_receiver"""),
    "interfaces_of": ("型が満たすこのリポジトリのインターフェース", """\
MATCH (t:Classes {fqcn: 'example.com/iryo/kernel/policy.UrgentPolicy'})-[e:ClassImplements]->(i:Classes)
RETURN i.fqcn, i.module, e.pointer_receiver"""),
    "embeds": ("埋め込みの木: 型が埋め込んでいる型(3 階層まで)", """\
MATCH (t:Classes {fqcn: 'example.com/iryo/domain/patient.Inpatient'})-[:ClassEmbeds]->{1,3}(inner:Classes)
RETURN DISTINCT inner.fqcn, inner.kind"""),
    "embedded_by": ("この型を埋め込んでいる型", """\
MATCH (outer:Classes)-[e:ClassEmbeds]->(t:Classes {fqcn: 'example.com/iryo/kernel/policy.BasePolicy'})
RETURN outer.fqcn, outer.module, e.pointer"""),
    "package_imports": ("パッケージの依存: このパッケージが import するリポジトリ内のパッケージ", """\
MATCH (p:Packages {import_path: 'example.com/iryo/reception-desk/internal/flows/accounting'})-[:PackageContainsFile]->(f:Files)-[e:PackageImports]->(dep:Packages)
RETURN DISTINCT dep.import_path, dep.module, e.kind"""),
    "package_dependents": ("このパッケージを import しているパッケージ", """\
MATCH (src:Packages)-[:PackageContainsFile]->(f:Files)-[:PackageImports]->(p:Packages {import_path: 'example.com/iryo/billing'})
RETURN DISTINCT src.import_path, src.module"""),
    "file_dependents": ("このファイルに依存しているファイル(影響範囲、3 ホップまで)", """\
MATCH (dep:Files)-[:FileDependsOn]->{1,3}(f:Files {path: 'billing/fee.go'})
RETURN DISTINCT dep.path, dep.package"""),
    "module_calls": ("2 つのモジュールの間の呼び出し(同じリポジトリ内)", """\
MATCH (caller:Methods)-[e:MethodCalls]->(callee:Methods)
WHERE caller.module = 'example.com/iryo/reception-desk' AND callee.module = 'example.com/iryo/billing'
RETURN caller.fqmn, callee.fqmn, e.site_kind, e.call_line"""),
    "module_matrix": ("モジュールをまたぐ呼び出しの数", """\
MATCH (caller:Methods)-[e:MethodCalls]->(callee:Methods)
WHERE caller.repo = 'iryo-kanri-system' AND caller.module <> callee.module
RETURN caller.module, callee.module, COUNT(*) AS calls
GROUP BY caller.module, callee.module
ORDER BY calls DESC"""),
    "entry_points": ("コマンド(func main)の一覧", """\
MATCH (m:Methods)
WHERE m.repo = 'iryo-kanri-system' AND m.entry_point <> ''
RETURN m.entry_point, m.fqmn, m.module"""),
    "cross_repo_calls": ("別リポジトリからの呼び出し(影響分析)", """\
MATCH (caller:Methods)-[x:CrossRepoCalls]->(callee:Methods)
WHERE callee.repo = 'shared'
RETURN caller.repo, caller.fqmn, callee.fqmn, x.site_kind"""),
    "cross_repo_imports": ("別リポジトリのパッケージを import しているファイル", """\
MATCH (f:Files)-[x:CrossRepoImports]->(p:Packages)
WHERE p.repo = 'shared'
RETURN f.repo, f.path, p.import_path"""),
    "cross_repo_refs": ("別リポジトリの型の埋め込みと実装", """\
MATCH (src:Classes)-[r:CrossRepoRef]->(tgt:Classes)
WHERE tgt.repo = 'shared'
RETURN src.repo, src.fqcn, tgt.fqcn, r.kind"""),
}

# The topic layer: its table lines, notes and recipes, by topic source.
# codedoc_go builds topics with Gemini in the wiki track ("llm").
# codedoc_go_leiden builds them with Leiden community detection on the resolved
# graph (leiden spec §4) and switches the whole layer by setting
# TOPIC_SOURCE = "leiden"; nothing else in this file changes.
TOPIC_SOURCE = "leiden"  # LEIDEN-HOOK

_TOPICS: dict[str, dict] = {
    "llm": {
        "nodes": "- Topics (topic_id, name, summary, repo, embedding)\n",
        "edges": "- FileBelongsToTopic (edge_id, file_id, topic_id): Files → Topics(機能トピック)\n",
        "notes": (
            "`Topics` は Gemini がまとめた機能トピックで、Go のモジュール(go.mod)ではありません。"
            "Go のモジュールを問われたら `Files.module` などの `module` 列を、パッケージを問われたら"
            " `Packages` か `package` 列を使ってください。トピックは `--wiki` で作られます。\n"),
        "cookbook": {
            "topic_files": ("機能トピックに属するファイル", """\
MATCH (f:Files)-[:FileBelongsToTopic]->(t:Topics {name: '会計'})
RETURN f.path, f.package"""),
        },
    },
    "leiden": {
        "nodes": ("- Topics (topic_id, name, summary, repo, embedding, level, parent_topic_id, size, "
                  "file_count, cohesion, conductance, hub_class_id, label_source, basis, "
                  "membership_hash)\n"),
        "edges": (
            "- FileBelongsToTopic (edge_id, file_id, topic_id, role): Files → Topics。ファイルの型が属する"
            "葉のトピックすべて(「未分類」を含む)。宣言(型と、クロージャを除く関数・メソッド)が最も多い"
            "トピックが role = primary、ほかは secondary\n"
            "- ClassBelongsToTopic (edge_id, class_id, topic_id, role): Classes → Topics。型(擬似型 "
            "`<package>` を含む)が属するトピック。型はレベル 1 のトピックに属し、そのトピックがサブトピックに"
            "分かれたときは、そのうちの 1 つにも属する\n"
            "- TopicDependsOn (edge_id, source_topic, target_topic, weight, call_count): Topics → Topics。"
            "葉のトピックの間の向きのある依存で、確定した呼び出し(インターフェースのメソッドの呼び出しを"
            "含む)、埋め込み、実装から作る。weight はその重みつきの強さ。call_count は呼び出し元と呼び出し先の"
            "メソッドの組の数(重複を除く)で、呼び出しの箇所の数ではない。埋め込みと実装だけの行では 0\n"
            "- TopicContainsTopic (edge_id, parent_topic, child_topic): Topics → Topics。トピックとサブトピック\n"),
        "notes": (
            "`Topics` は確定した呼び出し、埋め込み、実装から Leiden 法で求めた構造のトピック"
            "(basis = 'structure')で、Go のモジュール(go.mod)ではありません。Go のモジュールを問われたら"
            " `Files.module` などの `module` 列を、パッケージを問われたら `Packages` か `package` 列を"
            "使ってください。\n"
            "- level 1 がトピック、level 2 がサブトピックで、`parent_topic_id` と TopicContainsTopic が"
            "親子をつなぎます。サブトピックに分かれなかったトピックは葉です。\n"
            "- 分かれたトピックの型は、ClassBelongsToTopic にレベル 1 とサブトピックの 2 行があります。"
            "型を 1 回ずつ数えるときは `t.level = 1` で絞ってください(すべての型がレベル 1 のトピックの"
            "どれか 1 つに属します)。\n"
            "- 名前の出どころは label_source です: hub(ハブの型の名前。`--wiki` の前)| llm(Gemini の命名)"
            "| inherited(前回の実行から引き継いだ名前)| fixed(「未分類」の決まった名前)。ハブの型は "
            "`hub_class_id` です(「未分類」には無い)。\n"
            "- ClassBelongsToTopic.role: primary(クラスタリングで決まった)| attached-file(エッジが無く、"
            "同じファイルの型に合わせた)| attached-test | attached-generated | attached-excluded(テスト、"
            "生成コード、ビルドから外れるコードはクラスタリングから外し、後で最もつながりの強いトピックに"
            "付けた)| unassigned(どこにも付かず「未分類」トピックに入った型)。\n"
            "- size は型の数、file_count はファイルの数です。cohesion(内部の結びつき)と conductance"
            "(外へ出るつながりの割合)は構造の指標です。\n"),
        "cookbook": {
            "topic_files": ("トピックに属するファイル", """\
MATCH (f:Files)-[b:FileBelongsToTopic]->(t:Topics {name: '会計'})
RETURN f.path, f.package, b.role"""),
            "topic_members": ("トピックに属する型(役割つき)", """\
MATCH (c:Classes)-[b:ClassBelongsToTopic]->(t:Topics {name: '会計'})
RETURN c.fqcn, c.kind, b.role"""),
            "class_topic": ("型が属するトピック(レベル 1 と、分かれていればサブトピック)", """\
MATCH (c:Classes {fqcn: 'example.com/iryo/billing.FeeCalculator'})-[b:ClassBelongsToTopic]->(t:Topics)
RETURN t.name, t.level, b.role
ORDER BY t.level"""),
            "topic_type_counts": ("トピックごとの型の数(役割別、各型を 1 回だけ数える)", """\
MATCH (c:Classes)-[b:ClassBelongsToTopic]->(t:Topics)
WHERE t.repo = 'iryo-kanri-system' AND t.level = 1
RETURN t.name, b.role, COUNT(*) AS types
GROUP BY t.name, b.role
ORDER BY t.name, types DESC"""),
            "topic_dependencies": ("トピックの間の依存(呼び出すメソッドの組の多い順)", """\
MATCH (a:Topics)-[d:TopicDependsOn]->(b:Topics)
WHERE a.repo = 'iryo-kanri-system'
RETURN a.name, b.name, d.call_count, d.weight
ORDER BY d.call_count DESC"""),
            "topic_tree": ("トピックの木(トピックとサブトピック)", """\
MATCH (parent:Topics)-[:TopicContainsTopic]->(child:Topics)
WHERE parent.repo = 'iryo-kanri-system'
RETURN parent.name, child.name, child.size, child.label_source"""),
        },
    },
}


def _topics(source: str) -> dict:
    if source not in _TOPICS:
        raise ValueError(f"unknown topic source {source!r} (expected one of {sorted(_TOPICS)})")
    return _TOPICS[source]


def cookbook(topics: str = TOPIC_SOURCE) -> dict[str, tuple[str, str]]:
    return {**_COOKBOOK, **_topics(topics)["cookbook"]}


def build_instruction(graph_name: str, topics: str = TOPIC_SOURCE) -> str:
    t = _topics(topics)
    recipes = "\n\n".join(f"### {title}:\nGRAPH {graph_name}\n{query}"
                          for title, query in cookbook(topics).values())
    return (
        "あなたは Spanner Graph を使って Go コードの構造を分析するエージェントです。\n"
        "ユーザーの質問に対して GQL クエリを実行し、結果に基づいて答えてください。\n\n"
        f"GQL クエリは必ず `GRAPH {graph_name}` で始めてください。"
        "最後の RETURN ではノードやエッジそのものではなく、そのプロパティを返してください。\n"
        + _IDENTITY
        + _NODE_LINES + t["nodes"] + _NODE_NOTES + t["notes"] + _SUMMARY_NOTES
        + _EDGE_LINES + t["edges"] + _CROSS_REPO_EDGE_LINES
        + _REPRESENTATION + _CALLS + _STATUSES + _BOUNDARIES
        + "\n## クエリ例\n\n" + recipes
        + "\n\n日本語で回答してください。"
    )


GQL_COOKBOOK = cookbook(TOPIC_SOURCE)
GRAPH_AGENT_INSTRUCTION = build_instruction(GRAPH_NAME, TOPIC_SOURCE)


def _graph_instruction(_ctx: ReadonlyContext) -> str:
    # An InstructionProvider bypasses ADK's {state} templating, so the GQL
    # braces and qualnames such as `init#1` or `<init>.func1` reach the model
    # verbatim.
    return GRAPH_AGENT_INSTRUCTION


graph_agent = Agent(
    name="graph_agent",
    model=MODEL,
    description=(
        "Go コードの構造を Spanner Graph で分析するエージェント。"
        "パッケージ、型、関数、メソッドの一覧、呼び出し関係、インターフェースの実装、埋め込み、"
        "パッケージの依存、影響範囲分析など構造的な質問に使う。"
    ),
    instruction=_graph_instruction,
    tools=[run_gql_query],
)

# ===================================================================
# Root: Orchestrator
# ===================================================================

root_agent = Agent(
    name="graph_query_agent",
    model=MODEL,
    sub_agents=[graph_agent],
    description="コードベースの構造に関する質問に答えるオーケストレーター。graph_agent に委譲する。",
    instruction="""あなたはコードベースに関する質問に答えるオーケストレーターエージェントです。
質問の種類に応じて、適切なサブエージェントに処理を委譲してください。

## サブエージェントの役割

### graph_agent:
- パッケージの関数一覧、型のメソッド一覧
- パッケージの依存、ファイルの依存関係リスト
- インターフェースの実装と型の埋め込み
- 呼び出し元と呼び出し先、モジュールやリポジトリをまたぐ呼び出し
- 変更影響範囲分析
- 数値的・構造的なデータが必要な質問
""",
)
