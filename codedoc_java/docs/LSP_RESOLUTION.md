# Phase 1.6 — jdtls によるシンボル解決

Phase 1.6 が Java の呼び出しサイトを「どの宣言に束縛されるか」まで厳密に解決する仕組みと、誤ったエッジを 1 本も出さないためのゲートを説明します。
設定キーとトラブルシューティングは [../README.md](../README.md)、解決結果がどのテーブル・ID になるかは [GRAPH_SCHEMA.md](GRAPH_SCHEMA.md)、複数アーティファクトに固有の話は [CROSS_PROJECT.md](CROSS_PROJECT.md) に譲ります。

実装は `graph_generator/resolution.py` (本体)、`java_decls.py` (宣言インデックスとサイト抽出)、`lsp_client.py` (LSP クライアント) です。パイプラインからは `pipeline.py` の `phase1c_semantic_resolution` が `run_resolution` を呼びます。

## 何が問題か

tree-sitter (Phase 1.5) は「どの行にどんな名前が書いてあるか」しか分かりません。名前だけでエッジを張ると、同梱コーパス `test_codes/iryo-kanri-system` の次のような箇所で必ず間違えます。

| 区分 | コーパスの例 | 名前だけでは決まらない理由 |
|---|---|---|
| オーバーロード | `reception-desk` の `AccountingFlow.java` で `fee.calculate(288, 146L)`。`FeeCalculator.calculate` は `(int, int)` `(long, long)` `(double, double)` `(int, int, int)` `(int...)` の 5 宣言 | `146L` が `long` 版への束縛を決める。拡大変換と varargs の優先順位はコンパイラの規則 |
| 別アーティファクトの同名 | `DailyCloseFlow.java` (reception-desk) と `DailyBatch.java` (admin-console) の `run()` は処理行がバイト単位で同一。`new Logger()` は前者で `iryo.kernel.logging.Logger`、後者で `iryo.pharmacy.Logger` | 違いは `import` 文だけ |
| static import と完全修飾名 | 同じ `AccountingFlow.java` で `toYen(126)` は `import static iryo.billing.PointMath.toYen` 経由、数行下の `iryo.pharmacy.PointMath.toYen(126)` は別アーティファクト | 受け手が書かれていない呼び出しは import を解決しないと決まらない |
| `var` の連鎖 | `CheckInFlow.java` の `var patient = defaultRepo.find(2); patient.describe()` | 戻り値型を追わないと `describe()` の宣言が決まらない |
| 継承 | `new OldTable().calculate(30, 10)` は継承元 `iryo.legacy.claims.PointTable.calculate(int, int)` に束縛。`Entity → AuditedEntity → Patient` の三段継承 | 宣言は別の型にある |
| 既定メソッド | `standard.fee()` は `iryo.kernel.PaymentPolicy.fee()` (default)、`combined.fee()` は `AccountingFlow$CombinedPolicy.fee()` (上書き) | 受け手の静的型と上書きの有無を見る必要がある |
| DI | `notifier.send(...)`。`Program.buildContainer()` は `Notifier` を `MailNotifier` → `SmsNotifier` の順に 2 回登録し、後勝ちで SMS が動く | 静的には `Notifier.send(String)`、実行時は `SmsNotifier.send(String)` |
| 実装が複数 | `Repository<T>` の実装は `SqlPatientRepository` の 1 つ、`Exporter` は `CsvExporter` / `XmlExporter` の 2 つ | 1 つなら実装へ進めてよいが、2 つなら決められない |
| jar 境界 | プロジェクト間の呼び出しはすべて `_feed/` の jar を越える | 定義がソースツリーの外にある |

Phase 1.6 の方針は 1 つです。確信できないサイトは `unresolved` または `ambiguous` として記録し、エッジにしない。誤エッジゼロを網羅率より優先します。

## なぜ言語サーバーか

Eclipse JDT Language Server (jdtls) は、Eclipse の Java コンパイラ基盤 (JDT Core) と Maven 統合 (m2e) を Language Server Protocol で切り出したものです。`pom.xml` から依存 jar のクラスパスを組み立て、javac と同じ意味解析 (オーバーロード解決、import と static import の解決、型推論、継承と既定メソッドのディスパッチ) で答えます。Phase 1.6 は、人がエディタで 1 箇所ずつ行う「定義へ移動」を全呼び出しサイトに機械的に実行しているのと同じです。

使う LSP メッセージは少数です。

| メッセージ | 用途 |
|---|---|
| `initialize` / `initialized` / `textDocument/didOpen` | セッション確立とファイル登録 |
| `language/status` 通知 (jdtls 独自) | `type == "ServiceReady"` をロード完了の合図にする |
| `textDocument/documentSymbol` | ロード完了の確認プローブ |
| `textDocument/definition` | 呼び出しサイト → 宣言位置。解決の主経路 |
| `textDocument/hover` | 束縛先シグネチャの突き合わせ。シンボル文字列を hover から作ることはしない |
| `java/classFileContents` (jdtls 独自) | jar や JDK の型のソース取得。`-sources.jar` があれば実ソース、なければ逆コンパイル結果 |
| サーバー → クライアント要求 | `workspace/configuration`、`client/registerCapability`、`window/workDoneProgress/create`、`workspace/workspaceFolders` に応答。未知の要求には `-32601` |

代償は、JDK 21 と jdtls の導入、`pom.xml` と事前ビルド済みの依存、1 セッションあたりアイドル時 RSS 約 1.2 GB (`-Xmx700m`)、プロジェクトのロード待ち (コーパス 12 プロジェクトで直列 144 秒) です。サーバーの応答も完全ではありません。record のコンストラクタ・アクセサに対する `definition` は範囲が `0,0` に縮退し、`ServiceReady` の後もクラスパスの束縛が数秒遅れます。そのため各段に検証を挟みます。

## 仕組み

### 1. Maven プロジェクトの発見 — `discover_projects`

- 対象ディレクトリを走査して `pom.xml` を持つディレクトリを集めます (`SKIP_DIRS` は枝刈り)。
- 各 `.java` は最寄りの祖先 `pom.xml` に帰属し、その `<artifactId>` が **artifact** になります (`_pom_gav`。無ければディレクトリ名。`groupId` / `version` は `<parent>` からも読む)。
- jdtls セッションは、祖先方向に連続する `pom.xml` の最上位ディレクトリ (reactor ルート) 単位で 1 つ立てます。コーパスは独立した 12 プロジェクトなので 12 セッションです。
- 祖先に `pom.xml` が無いファイルは `fallback_files` へ (ノードのみ)。
- 全 pom の `(groupId, artifactId, version)` → artifactId の表 `gav_map` を作り、jar 内ターゲットをリポジトリ内の宣言に写像するときに使います。

### 2. セッションの起動と ready 判定 — `ProjectSession`

起動コマンドは `JAVA_LSP_CMD -configuration <tmp>/configuration -data <tmp>/data --jvm-arg=-Dosgi.configuration.area=<tmp>/configuration --jvm-arg=-Xms128m --jvm-arg=-Xmx700m`、作業ディレクトリはプロジェクトルート、`JAVA_HOME` は `JAVA21_HOME` です。`-data` と構成領域をセッション固有にするのは必須です (共有すると `~/.eclipse` に書きに行って失敗するか、2 セッションがロックで固まる)。一時ディレクトリは終了時に削除します。

`initialize` には `classFileContentsSupport = true`、`maven.downloadSources = true`、Gradle インポート無効、`JAVA_LSP_MAVEN_SETTINGS` (設定時) を渡し、同じ Java 設定を `workspace/configuration` の応答にも返します。

ready 判定は 3 段です (`start_and_wait_ready` と `_semantic_warmup`)。

1. `language/status` の `ServiceReady` を待つ (上限 `min(JAVA_LSP_TIMEOUT_S, 180)` 秒。間に合わなくても次へ)。
2. プロジェクト内で最大のファイルを `didOpen` し、`documentSymbol` が非空を返すまで 2 秒間隔で待つ。`ServiceReady` 受信済みなら空リストも ready とみなす。この待ちの上限が `JAVA_LSP_TIMEOUT_S` で、超えると `LspTimeout` (`initialize` の 120 秒、1 の待ち、3 のウォームアップは別枠)。
3. `documentSymbol` は構文木しか証明しないので、このプロジェクト外の非 JDK パッケージを import しているファイルの呼び出しサイトを選び (`_select_warmup_site`)、`definition` が答えるまで最大 90 秒待つ。事前検証では `ServiceReady` の約 3.5 秒後にようやく jar 越しの定義が返りました。

`_run_all` は `asyncio.Semaphore(JAVA_LSP_CONCURRENCY)` でセッションを並行させ、ファイル数の多いプロジェクトから始めます。`LspTimeout` で落ちたプロジェクトだけ、並行波の後に 1 回直列で再試行します。接続系のエラーは再試行しません。

`lsp_client.LspClient` は Content-Length フレーミングの JSON-RPC 2.0 を stdio で話す最小実装です。逆コンパイル結果を受けるための 4 MiB ストリーム上限、サーバー死亡時の全 in-flight 要求の即時失敗、`language/status` のパラメータ履歴 (述語付き待機用) を持ちます。サーバーの stderr は捨てます。

### 3. 宣言インデックスとサイト抽出 — `java_decls`

`build_decl_index` は artifact ごとに全宣言を **SYMBOL_GRAMMAR** で索引化します。

| 規則 | 例 |
|---|---|
| 型はパッケージ修飾。入れ子型は `$`。宣言された型パラメータを保持 | `iryo.domain.ChartArchive$Compactor`、`iryo.data.Repository<T>` |
| メソッドは仮引数**型**リスト付き。ジェネリックメソッドは名前に型パラメータ | `iryo.billing.FeeCalculator.calculate(int, int)`、`iryo.kernel.ServiceProvider.get<T>(Class<T>)` |
| コンストラクタは `<init>` | `iryo.kernel.logging.Logger.<init>(String)` |
| 仮引数型は単純名に正規化。配列・varargs・ワイルドカード・ジェネリック引数は原文の綴り | `calculate(int...)`、`run(Supplier<String>)`、`Map<String, Integer>` |
| record は正準コンストラクタとアクセサを合成 (明示宣言があればそれを優先) | `iryo.domain.Money.<init>(BigDecimal)`、`iryo.domain.Money.amount()` |
| `default` / `static` / `private` の interface メソッドは `has_body` | `iryo.kernel.PaymentPolicy.fee()` |
| 匿名クラス・ローカルクラスのメンバーは索引化しない | その中の呼び出しは外側のメンバーに帰属 |

`kind` は `type` / `method` / `constructor` の 3 値です。索引は宣言のバイトスパンを持ち、`decl_at` (位置を含む最内側の宣言)、`enclosing_member` (呼び出し元の帰属)、`ctors_of` / `members_of` (兄弟オーバーロード) で引けます。逆コンパイルスタブに出る `public Foo();` は tree-sitter が ERROR ノードにしますが、コンストラクタとして拾います。

`extract_call_sites` は 4 種のサイトを取り出します。

| `site_kind` | 抽出元 | アンカー |
|---|---|---|
| `invocation` | `method_invocation`、および `this(...)` / `super(...)` | メソッド名 (連鎖は `this` / `super`)。実引数数と受け手のテキストも保持 |
| `object_creation` | `object_creation_expression` (匿名クラス本体付きも) | 型名の右端。実引数数を保持 |
| `class_literal` | `addSingleton` / `addScoped` / `addTransient` の実引数 2 つがともに `X.class` のとき | それぞれの型名。役割 `service` / `impl` と登録グループ |
| `method_reference` | `method_reference` | `x::m` は `m`、`T::new` は型名 (token は `new`) |

### 4. サイトの解決 — `_resolve_site`

1. `enclosing_member` で呼び出し元メンバーを決め、ファイルの artifact を `enclosing_artifact` に入れます。
2. `textDocument/definition` を投げます。要求エラーはそのサイトだけを縮退させます (`had_def_error`)。
3. 返った位置ごとに `_resolve_location` で候補を作ります。
   - **リポジトリ内の `file://`**: そのファイルの artifact の索引で `decl_at`。解析対象ツリー外の `file://` は候補なし (推測しない)。
   - **`jdt://`** (jar / JDK): `_artifact_from_jdt_uri` で artifact ラベルを決めます (URI に埋め込まれた Maven 座標、JDK モジュール名、jar 名から。規則は [CROSS_PROJECT.md](CROSS_PROJECT.md))。URI をそのまま `java/classFileContents` に渡して本文を取り、`decompiled_index` で同じ `build_decl_index` にかけます (`/jdt/<artifact>/<型>.java` の合成パス)。`_candidates_from_external_index` が位置・サイト種別・hover で候補を絞ります。ラベルがリポジトリ内 artifact なら候補をソースツリーの実宣言に写像し直します。外なら `external` の材料になります。
4. `invocation` (`this` / `super` を除く) では hover の先頭 `{language: "java"}` ブロックからコンテナ・メンバー名・仮引数型を取り (`_parse_hover_signature`、`_norm_hover_param` で綴りを正規化)、候補とその兄弟オーバーロードから仮引数型が完全一致する 1 件を選びます (`_select_by_hover`)。候補が 1 件でも名前・引数数が hover と食い違えば採りません (`_hover_agrees`)。
5. 候補が無い、または選べないとき、hover が名指す `(コンテナ, メンバー, 型リスト)` がリポジトリ全体で一意で、かつ「definition がエラーだった」か「`jdt://` の型名が一致する」という裏付けがあるときだけ救済します (`_hover_rescue` と `_corroborated`)。
6. 候補が複数残れば `ambiguous` (`candidate_count`)、無ければ `external` (外部ターゲットがあれば) か `unresolved`。1 件に決まりリポジトリ内なら `resolved` (`resolved_via = "lsp"`)。

### 5. 後段の付け替え

- `_apply_di_registry` (プロジェクトごと): `ServiceCollection` への登録を解釈し、インターフェース呼び出しを実装へ付け替えます (次々節)。
- `_apply_interface_impl_upgrade` (全プロジェクト終了後): `resolved` (lsp) のターゲットが本体なしの interface メンバーで、そのインターフェースを `extends` / `implements` するリポジトリ内の interface 以外の型 (抽象クラスを含む) の同名メンバーがちょうど 1 つなら実装へ付け替えます (`resolved_via = "iface-impl"`、元は `static_target`)。同じ単純名の interface が複数あれば付け替えません。

### 6. UTF-16 桁

LSP の `position.character` は UTF-16 コードユニットです。サイト抽出時は tree-sitter のバイト桁を `_byte_to_utf16` で変換し、宣言スパン照合時は `DeclIndex._char16_to_byte` で戻します。日本語を含む行でも桁がずれません。

### 7. 出力 — `run_resolution`

宣言一覧 (ロード成功プロジェクトのみ)、全サイト、`ambiguous_symbols`、`stats` をまとめ、`_write_artifact` が一時ファイル経由で `<OUTPUT_DIR>/resolution.json` に置き換えます。

## 誤エッジゼロの保証

| ゲート | 落とす条件 |
|---|---|
| hover 型リスト選別 | 仮引数型の完全一致が 1 件に絞れない。候補 1 件でも名前・引数数が hover と違う |
| hover 救済の一意性と裏付け | リポジトリ全体で一意でない。definition が「きれいに null」だった (真のターゲットが外部の可能性) |
| コンストラクタ引数数 | 逆コンパイル側で範囲が型に落ちた `new`: 実引数数が一致する ctor がちょうど 1 つでない。varargs や実引数より多い仮引数の ctor が同居する。実引数数を持たない `T::new` |
| コンストラクタ連鎖 | `this()` / `super()` は hover が型名しか返さないので、候補が複数なら `ambiguous` |
| 暗黙コンストラクタ | ctor 宣言の無い型への `new X()` は型シンボル (`kind: type`) に解決。Phase 9 はエッジにしない |
| iface-impl | 実装がちょうど 1 つでない。同名 interface が複数。メンバーに本体がある (default など) |
| DI | 次節の信用条件を満たさない登録は無視。実装側メンバーが無い・抽象なら付け替えない |
| 重複シンボル | 同一 artifact 内で同じメンバー fq シンボルが複数ファイルにある → `ambiguous_symbols` に記録し、位置を null にして Phase 9 が除外 |
| 要求単位の縮退 | definition / hover / classFileContents のエラーはそのサイトだけ |
| ツリー外 `file://` | 候補なし |

サイトの `status`:

| status | 意味 | エッジ |
|---|---|---|
| `resolved` | リポジトリ内の宣言に一意に解決 (`target.file` / `line` 付き) | 作る (型レベルターゲットを除く) |
| `external` | JDK モジュールまたは外部 jar に解決。`target.artifact` はラベル、`file` / `line` は null | 作らない (記録のみ) |
| `ambiguous` | 候補複数 (`candidate_count`) | 作らない |
| `unresolved` | definition なし、またはゲートで拒否 | 作らない |

`resolved_via` は `lsp` (definition + hover の通常経路)、`di` (DI 登録から実装へ)、`iface-impl` (一意実装へ) の 3 値で、`resolved` 以外では null です。

解決結果からどのエッジを作るか (MethodCalls の採用条件、ClassInherits の一意単純名ルール) は [GRAPH_SCHEMA.md](GRAPH_SCHEMA.md) が定めます。

## DI の解決 — 静的エッジとランタイムエッジ

`notifier.send("...")` はコンパイラ意味論では `iryo.kernel.Notifier.send(String)` に束縛されます。実行時に動くのは DI コンテナが注入した実装です。Phase 1.6 は両方を残します。

1. **登録の検出**: `services.addSingleton(Notifier.class, SmsNotifier.class)` のように実引数 2 つがともにクラスリテラルの `add*()` から `class_literal` サイト 2 つを取り、それぞれを LSP で型解決します。
2. **登録の信用**: 同じファイルでそのリテラルの直前にある同名の `add*()` 呼び出し自体が `resolved` / `external` に解決され、そのターゲットのコンテナ単純名が `ServiceCollection` であるときだけ信用します。
3. **レジストリ**: service → impl を (ファイル, 行) 昇順で作り、最後の登録が勝ちます。同じプロジェクトのサイトにだけ適用します。
4. **付け替え**: `resolved` な invocation のターゲットのコンテナが登録済み service なら、impl 側の同名メンバー (実在し本体を持つ場合のみ) へ付け替え、元を `static_target` に残します (`resolved_via = "di"`)。

2026-09-18 のコーパス実行では `CheckInFlow.java` の 2 サイトが `di` になりました。

```text
CheckInFlow.java:22  notifier.send(...)  -> iryo.kernel.Notifier.send(String)        via=lsp  (static_target)
                                         -> iryo.scheduling.SmsNotifier.send(String) via=di   (target)
CheckInFlow.java:26  repo.find(1)        -> iryo.data.Repository<T>.find(int)        via=lsp
                                         -> iryo.data.SqlPatientRepository.find(int) via=di
```

`MailNotifier.send(String)` をターゲットにするサイトは 0 件で、後勝ちが効いています。

解釈しないもの: ファクトリラムダ登録 (`addSingleton(Logger.class, () -> new Logger("受付端末"))`)、`Class` 以外の引数や変数に入れた `Class`、`ServiceCollection` 以外のコンテナ (Spring / Guice / Dagger)。該当サイトは静的なインターフェースターゲットのまま (実装が 1 つなら `iface-impl`) 残り、誤った実装には配線されません。登録が複数ファイルに分かれている場合は、実行順ではなくファイルパスと行の昇順で後勝ちを決めます (静的な近似)。

## resolution.json

場所は `<OUTPUT_DIR>/resolution.json`。パスはすべて対象ディレクトリからの相対です。

| キー | 内容 |
|---|---|
| `version` | `2` |
| `target_dir` | 対象ディレクトリの絶対パス |
| `lsp` | `{enabled, cmd}`。`JAVA_LSP_ENABLED` と `JAVA_LSP_CMD` の実効値 |
| `solutions[]` | セッションごとの結果。`sln` = ルートの `pom.xml`、`status` = `resolved` / `fallback`、`reason` = 失敗時の `例外名: メッセージ`、`files` = 担当ファイル |
| `fallback_files[]` | どの `pom.xml` にも帰属しなかった `.java`。LSP 無効時は全 `.java` |
| `declarations[]` | ロード成功プロジェクトの全宣言。`artifact` / `symbol` / `kind` (`type` / `method` / `constructor`) / `file` / `line` / `container` (所属型。型自身はパッケージ) / `namespace` (パッケージ) |
| `sites[]` | 全サイト。`file` / `line` / `col` (UTF-16) / `token` / `site_kind` / `enclosing` (呼び出し元メンバーのシンボル) / `enclosing_artifact` / `status` / `target` (`symbol` / `artifact` / `kind` / `file` / `line`) / `static_target` (`symbol` / `artifact`) / `resolved_via` / `candidate_count` |
| `ambiguous_symbols[]` | 降格した重複宣言 `{artifact, symbol}` |
| `stats` | status 別サイト数、`declarations`、`solutions_resolved`、`solutions_fallback` |

サイトの例 (2026-09-18 の実行より):

```json
{"file": "reception-desk/src/main/java/iryo/reception/flows/DailyCloseFlow.java",
 "line": 21, "col": 28, "token": "toYen", "site_kind": "invocation",
 "enclosing": "iryo.reception.flows.DailyCloseFlow.run()", "enclosing_artifact": "reception-desk",
 "status": "resolved",
 "target": {"symbol": "iryo.billing.PointMath.toYen(int)", "artifact": "iryo-billing", "kind": "method",
            "file": "iryo-billing/src/main/java/iryo/billing/PointMath.java", "line": 10},
 "static_target": null, "resolved_via": "lsp", "candidate_count": null}
```

この呼び出しは `iryo-billing-1.0.0.jar` 越しに `jdt://` で返りますが、GAV `jp.iryo:iryo-billing:1.0.0` が `gav_map` にあるので実ソースの 10 行目に写像されています。同じファイルの `ledger.lines().get(0)` は `java.util.List<E>.get(int)` / `java.base` の `external` です。

## フォールバック (ノードのみ)

次の `.java` は Files / Classes / Methods ノードだけになり、MethodCalls / ClassInherits は作られません。

1. 祖先に `pom.xml` が無い (`fallback_files`)。
2. プロジェクトのロード・解析に失敗した (`LspTimeout` は直列再試行後、`LspConnectionError`、`LspError`、ランチャ不在の `FileNotFoundError` を含む `OSError`)。`solutions[].status = "fallback"`。
3. `JAVA_LSP_ENABLED=false` (全 `.java`)。

1 と 2 のとき Phase 1.6 は「解析できなかった Java ファイル: N 件」に続けて `GUIDANCE_JA` (JDK 21、jdtls 導入、`pom.xml`、`mvn install` の案内) を表示します。3 のときは「JAVA_LSP_ENABLED=false — セマンティック解決を行いません。」の通知だけです。ガイダンスにある `JAVA_LSP_CMD=~/tools/...` の `~` は展開されないので、実際には絶対パスを書きます。縮退はプロジェクト単位で独立し、他のプロジェクトのエッジは通常どおり作られます。縮退したファイルのノード ID はパスと名前から作られ、`artifact` / `fq_symbol` などの列は NULL です。

## 正しさの検証

| 検証 | 内容 | 結果 (2026-09-18) |
|---|---|---|
| SemanticProbe | `ground_truth/SemanticProbe/build.sh`。実 JVM 上で束縛主張 87 件を検証 (`SerializedLambda` 記述子、リフレクション、jar 走査、標準出力)。製品コードには触れない | 「検証項目: 87 件」「SEMANTIC PROBE PASSED」 |
| ユニットテスト | `pytest -m "not integration"`。偽 LSP サーバー (`tests/fake_lsp_server.py`) と台本駆動の client で、上のゲートを 1 つずつ確認 | 124 passed, 1 skipped |
| 統合テスト | `pytest -m integration`。実 jdtls で `iryo-billing` + `reception-desk` を解決。両ルートのロード、5 オーバーロードの宣言同一性、捏造ターゲットなし、`enclosing_artifact`、jar 越しの `Logger.log(String)` → `iryo-kernel`、`PointMath.toYen(int)` の in-repo 写像 | 6 passed (38 秒) |
| 全コーパス実行 | 製品コード 12 プロジェクトに `run_resolution` (`JAVA_LSP_CONCURRENCY=1`、jdtls 1.61.0) | 下表 |

| 指標 | 実測 (2026-09-18) |
|---|---|
| プロジェクト | 12 / 12 が `resolved` (fallback 0、`fallback_files` 0) |
| `.java` / 宣言 | 57 ファイル / 227 件 (method 125、type 63、constructor 39) |
| サイト | 403 件 (invocation 314、object_creation 78、class_literal 6、method_reference 5) |
| `resolved` | 208 件 (`lsp` 206、`di` 2、`iface-impl` 0)。うち型レベルターゲット 36 件 |
| `external` | 194 件 (`java.base` 182、`jdk.httpserver` 11、`java.desktop` 1) |
| `unresolved` / `ambiguous` / `ambiguous_symbols` | 1 / 0 / 0 |
| 所要時間 | 144 秒 (2026-09-17 の計測は 157 秒) |

`AccountingFlow.java` の `calculate` 6 呼び出しは 5 オーバーロードへ期待どおり分かれ、XPR 対の同一行は `iryo-kernel` / `iryo-billing` と `iryo-pharmacy` に分岐しました。`iface-impl` が 0 件なのは、`Repository<T>.find(int)` が先に DI で付け替わり、`Exporter` は実装が 2 つで拒否、`PaymentPolicy.fee()` は本体ありで対象外だからです。

唯一の `unresolved` は `reception-desk/.../ReportFlow.java:44` の `Supplier<Logger> loggerFactory = Logger::new;` です。jdtls は `Logger` の型宣言を返し、hover も型名だけです。`Logger` には `<init>()` と `<init>(String)` があり、コンストラクタ参照には実引数数が無いため引数数ガードが拒否します。誤った束縛ではなく、解決の拒否です。

## 制限

- 対象は `.java` のみ。`.jsp` / `.kt` / `.groovy` / `.scala` / `.gradle` は Phase 1.5 も 1.6 も通らない。
- Maven のみ。Gradle インポートは無効で、`pom.xml` の無いプロジェクトはノードのみ。
- リフレクションと動的ディスパッチは解決しない。実行時多相でモデル化するのは DI レジストリと一意実装アップグレードだけ。
- メソッド参照はエッジにならない。`x::m` は jdtls が関数型インターフェースの抽象メソッド (`Runnable.run()`、`HttpHandler.handle(HttpExchange)` など) を返すため `external`、`T::new` は上記のとおり `unresolved` か型レベル。
- 暗黙コンストラクタへの `new` と DI 登録の `X.class` は型レベルで、MethodCalls にならない。
- DI の後勝ちはプロジェクト内の (ファイルパス, 行) の昇順で決まり、実行順は反映しない。対象は `ServiceCollection` の `add*(Class, Class)` 形のみ。
- `external` のシンボル表記は添付ソースか逆コンパイル表示に依存する。照合は `artifact` ラベル単位。
- `enclosing` の無いサイト (フィールド初期化子、static 初期化子) はエッジにならない。
- ClassInherits は Phase 1.6 の definition ではなく一意単純名ルールで作る ([GRAPH_SCHEMA.md](GRAPH_SCHEMA.md))。
