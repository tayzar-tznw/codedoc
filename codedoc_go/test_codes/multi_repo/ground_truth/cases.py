# -*- coding: utf-8 -*-
"""Ground truth for multi_repo (F3, source of record).

Three repos ingested side by side, each a peer of the others (spec §8.2):
shared (modules example.com/iryo/shared and example.com/iryo/textkit), web
and api. Case files are fixture-relative. Peer targets carry "repo" and are
external with peer=true; crossref turns them into the exact edge set
CROSS_REPO_EDGES below (spec §8.3), the only source of record:
generate_manifest.py writes it into manifest.json and into
ground_truth/expected_cross_repo_edges.json, the file evaluate reads.
Expectations follow the go command (go1.27.2, linux/amd64, CGO_ENABLED=0)
with the peers substituted by replace, as a developer's go.work or replace
would do. Seen from web, example.com/iryo/textkit is provided by two peers
(shared and api's fork), so it is not substituted and resolves as without
peers; api's service replaces it with its own fork, so no peer replace is
added there either (spec §8.2, ruling Q3).
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "_gt"))
from gt_lib import case, external, peer, plain, resolved, target  # noqa: E402

REPOS = [{"name": "shared", "dir": "shared"}, {"name": "web", "dir": "web"}, {"name": "api", "dir": "api"}]

SHARED = "example.com/iryo/shared"
TEXTKIT = "example.com/iryo/textkit"
WEB = "example.com/iryo/web"



def sh(qualname, file):
    return target(qualname, SHARED, "method", "shared/" + file, repo="shared")


NEW = sh("New", "log/logger.go")
INFO = sh("Logger.Info", "log/logger.go")
NEWPRICE = sh("NewPrice", "money/price.go")
ADD = sh("Price.Add", "money/price.go")
AMOUNT = sh("Price.Amount", "money/price.go")
FORMAT = sh("Price.Format", "money/price.go")
ROUND = sh("Round", "money/round.go")
NOW = sh("Clock.Now", "contracts/clock.go")
STAMP = sh("Audit.Stamp", "contracts/audit.go")
SHARED_BANNER = target("Banner", TEXTKIT, "method", "shared/textkit/textkit.go", repo="shared")
FORK_BANNER = target("Banner", TEXTKIT, "method", "api/forks/textkit/textkit.go")
FORK_BANNER_PEER = target("Banner", TEXTKIT, "method", "api/forks/textkit/textkit.go", repo="api")

SYSTEM_NOW = target("SystemClock.Now", WEB, "method", "web/clock/clock.go")

O = "web/orders/orders.go"
B = "api/service/billing/billing.go"

CASES: list[dict] = []
DECL_PINS: list[dict] = []

# ── web: a single module; peer shared substituted by a replace in its -modfile copy (both modes, R12, R45) ─
CASES += [
    case("XRP-001", O, 'log.New("注文")', "New", "call", peer(NEW, module=SHARED),
         "ピア shared のパッケージ関数。web の go.mod は example.com/iryo/shared v1.0.0 を require する",
         "web-demo"),
    case("XRP-002", O, "money.NewPrice(baseYen)", "NewPrice", "call", peer(NEWPRICE, module=SHARED),
         "CrossRepoCalls OrderService.Total -> NewPrice (同じ組の二つ目の呼び出しは一本にまとまる)", "web-demo"),
    case("XRP-003", O, "base.Add(tax)", "Add", "call", peer(ADD, module=SHARED), "ピアの型の値レシーバのメソッド", "web-demo"),
    case("XRP-004", O, 's.Info("合計 "', "Info", "call", peer(INFO, module=SHARED),
         "埋め込んだ *log.Logger から昇格したメソッド。web の checks にある同名の Logger.Info ではない",
         "web-demo", distractors=[target("Logger.Info", WEB, "method", "web/checks/checks.go")]),
    case("XRP-005", O, "sum.Format()", "Format", "call", peer(FORMAT, module=SHARED),
         "Format はピアの作業中のソースにだけある。_proxy の公開版 v1.0.0 には無いので、ピアの置き換えが"
         "効いていなければ解決できない", "web-demo"),
    case("XRP-006", O, "money.Round(sum.Amount())", "Round", "call", peer(ROUND, module=SHARED), "別ファイル round.go の関数",
         "web-demo"),
    case("XRP-007", O, "sum.Amount()", "Amount", "call", peer(AMOUNT, module=SHARED), "戻り値の型 money.Price を経由", "web-demo"),
    case("XRP-008", "web/clock/clock.go", "c.Now().Format", "Now", "call",
         peer(NOW, module=SHARED, candidates=[SYSTEM_NOW], runtime=[SYSTEM_NOW]),
         "ピアのインターフェースの呼び出し。静的な行き先はピアのインターフェースメソッド (CrossRepoCalls)、"
         "候補は web の実装 SystemClock.Now (PossiblyCalls はリポジトリ内)", "web-demo"),
    case("XRP-009", "web/clock/clock.go", "now := time.Now()", "Now", "call", external("std"),
         "標準ライブラリ。辺にしない", "web-demo"),
    case("NEG-001", "web/checks/checks.go", 'textkit.Banner("checks")', "Banner", "call", external(TEXTKIT),
         "example.com/iryo/textkit は shared と api (フォーク) の二つのピアが持つので置き換えない。"
         "ピアがない場合と同じく公開版 (_proxy の v1.0.0、モジュールキャッシュ) になり、crossref は結ばない。"
         "オフラインでは unresolved (missing-module)", "web-demo",
         distractors=[SHARED_BANNER, FORK_BANNER_PEER], offline=plain("unresolved", "missing-module")),
    case("NEG-003", "web/checks/checks.go", "uuidlite.FromInt(42)", "FromInt", "call",
         external("example.com/thirdparty/uuidlite"),
         "どのリポジトリも持たない第三者モジュール (_proxy)。crossref の対象外。オフラインでは未回答", "web-demo"),
    case("NEG-002", "web/checks/checks.go", "l.Info(textkit", "Info", "call",
         resolved(target("Logger.Info", WEB, "method", "web/checks/checks.go")),
         "単純名が同じ Logger.Info でも web 自身の型。名前でピアに結び付けない", "web-demo", distractors=[INFO]),
    case("WEB-001", "web/cmd/web/main.go", "svc.Total(1234)", "Total", "call",
         resolved(target("OrderService.Total", WEB, "method", O)), "リポジトリ内の呼び出しは通常の MethodCalls",
         "web-demo"),
]

# ── api: a go.work (notify, service); peer shared substituted in the generated go.work; the fork via service's replace ─
CASES += [
    case("XRP-010", B, 'log.New("請求")', "New", "call", peer(NEW, module=SHARED),
         "ワークスペースのグループでは置き換えは生成した go.work の replace", "api-demo"),
    case("XRP-011", B, 's.logger.Info("請求 " + label)', "Info", "call", peer(INFO, module=SHARED), "フィールドの型 *log.Logger",
         "api-demo"),
    case("XRP-012", B, "amount.Format()", "Format", "call", peer(FORMAT, module=SHARED), "引数の型 money.Price", "api-demo"),
    case("XRP-018", B, "s.logger.Info(inv.Stamp())", "Info", "call", peer(INFO, module=SHARED),
         "同じ呼び出し元 Record からの二つ目のピア呼び出し", "api-demo"),
    case("XRP-013", B, "inv.Stamp()", "Stamp", "call", peer(STAMP, module=SHARED),
         "埋め込んだ contracts.Audit から昇格したメソッド (Audit は作業中のソースにだけある)", "api-demo"),
    case("API-001", B, "textkit.Banner(label)", "Banner", "call", resolved(FORK_BANNER),
         "service の go.mod は example.com/iryo/textkit を require し、replace => ../forks/textkit でリポジトリ内の"
         "フォークを指す。グループのモジュールが自分で replace するパスにはピアの replace を足さない (仕様 §8.2)。"
         "足すと go.work の replace が勝ち、ピアの Banner になる", "api-demo", distractors=[SHARED_BANNER]),
    case("API-002", B, "mail.Compose(", "Compose", "call",
         resolved(target("Compose", "example.com/iryo/api/notify", "method", "api/notify/mail/mail.go")),
         "ワークスペースの別モジュール notify への呼び出し", "api-demo"),
    case("XRP-014", "api/notify/mail/mail.go", 'log.New("mail")', "New", "call", peer(NEW, module=SHARED),
         "パッケージ変数の初期化子の呼び出し。呼び出し元は <init>", "api-demo"),
    case("XRP-015", "api/notify/mail/mail.go", "outbox.Info(body)", "Info", "call", peer(INFO, module=SHARED),
         "パッケージ変数の型 *log.Logger", "api-demo"),
    case("XRP-016", "api/service/cmd/api/main.go", "money.NewPrice(5000)", "NewPrice", "call", peer(NEWPRICE, module=SHARED),
         "main からピアへの呼び出し", "api-demo"),
    case("XRP-017", "api/service/cmd/api/main.go", ".Stamp())", "Stamp", "call", peer(STAMP, module=SHARED),
         "戻り値の型 AuditedInvoice に昇格した Audit.Stamp", "api-demo"),
    case("API-003", "api/service/cmd/api/main.go", "svc.Charge(", "Charge", "call",
         resolved(target("InvoiceService.Charge", "example.com/iryo/api/service", "method", B)),
         "リポジトリ内の呼び出し", "api-demo"),
    case("NEG-004", "api/tools/report/report.go", "money.NewPrice(yen)", "NewPrice", "call", plain("unresolved"),
         "tools は go.work の外の単一モジュールで、example.com/iryo/shared を require しない (宣言のないピア依存)。"
         "ピアは置き換えず、go コマンドも cannot find module providing package で止まる。ダウンロードモードの "
         "-mod=mod は shared の require を go.mod のコピーに足すが、コピーは元に戻す (改訂 R45) ので、"
         "読み込みは開発者のビルドと同じく止まり unresolved のまま", None,
         distractors=[NEWPRICE]),
    case("NEG-005", "api/legacy/main.go", "money.Round(p.Amount())", "Round", "call", external(SHARED),
         "legacy は go mod vendor した vendor/ を持つ。vendor のコピー (公開版 v1.0.0) がビルドされるので"
         "ピアは置き換えず external (§5.3 行 5)。辺にしない", "legacy-demo", distractors=[ROUND]),
    case("NEG-006", "api/legacy/main.go", "money.Legacy(", "Legacy", "call", external(SHARED),
         "Legacy は公開版にだけあり、ピアの作業中のソースには無い", "legacy-demo"),
    case("NEG-007", "api/legacy/main.go", ".Add(money.NewPrice(20))", "Add", "call", external(SHARED),
         "両方の版にあるメソッドでも vendor のコピーが勝つ", "legacy-demo", distractors=[ADD]),
]

# ── shared: the peer itself ─────────────────────────────────────────────────
CASES += [
    case("SHR-001", "shared/money/price.go", "Round(p.yen)", "Round", "call",
         resolved(target("Round", SHARED, "method", "shared/money/round.go")),
         "ピアのリポジトリ自身では通常のリポジトリ内の辺", "shared-test"),
    case("SHR-002", "shared/textkit/textkit.go", '"[" + Banner(s)', "Banner", "call",
         resolved(target("Banner", TEXTKIT, "method", "shared/textkit/textkit.go")),
         "shared の入れ子のモジュール example.com/iryo/textkit の中", "textkit-test"),
]

CROSS_REPO_EDGES: list[dict] = [
    # CrossRepoImports: file -> package "<repo>:<dir>|<clause name>"
    {"table": "CrossRepoImports", "kind": None, "source": "api:notify/mail/mail.go",
     "target": "shared:log|log", "import_path": "example.com/iryo/shared/log"},
    {"table": "CrossRepoImports", "kind": None, "source": "api:service/billing/billing.go",
     "target": "shared:contracts|contracts", "import_path": "example.com/iryo/shared/contracts"},
    {"table": "CrossRepoImports", "kind": None, "source": "api:service/billing/billing.go",
     "target": "shared:log|log", "import_path": "example.com/iryo/shared/log"},
    {"table": "CrossRepoImports", "kind": None, "source": "api:service/billing/billing.go",
     "target": "shared:money|money", "import_path": "example.com/iryo/shared/money"},
    {"table": "CrossRepoImports", "kind": None, "source": "api:service/cmd/api/main.go",
     "target": "shared:money|money", "import_path": "example.com/iryo/shared/money"},
    {"table": "CrossRepoImports", "kind": None, "source": "web:clock/clock.go",
     "target": "shared:contracts|contracts", "import_path": "example.com/iryo/shared/contracts"},
    {"table": "CrossRepoImports", "kind": None, "source": "web:orders/orders.go",
     "target": "shared:log|log", "import_path": "example.com/iryo/shared/log"},
    {"table": "CrossRepoImports", "kind": None, "source": "web:orders/orders.go",
     "target": "shared:money|money", "import_path": "example.com/iryo/shared/money"},
    # CrossRepoRef: class -> class, kind embeds | implements
    {"table": "CrossRepoRef", "kind": "embeds", "source": "api:service/billing/billing.go:AuditedInvoice",
     "target": "shared:contracts/audit.go:Audit"},
    {"table": "CrossRepoRef", "kind": "implements", "source": "web:clock/clock.go:SystemClock",
     "target": "shared:contracts/clock.go:Clock"},
    {"table": "CrossRepoRef", "kind": "embeds", "source": "web:orders/orders.go:OrderService",
     "target": "shared:log/logger.go:Logger"},
    # CrossRepoCalls: caller method -> callee method
    {"table": "CrossRepoCalls", "kind": None, "source": "api:notify/mail/mail.go:<init>",
     "target": "shared:log/logger.go:New"},
    {"table": "CrossRepoCalls", "kind": None, "source": "api:notify/mail/mail.go:Compose",
     "target": "shared:log/logger.go:Logger.Info"},
    {"table": "CrossRepoCalls", "kind": None, "source": "api:service/billing/billing.go:InvoiceService.Charge",
     "target": "shared:log/logger.go:Logger.Info"},
    {"table": "CrossRepoCalls", "kind": None, "source": "api:service/billing/billing.go:InvoiceService.Charge",
     "target": "shared:money/price.go:Price.Format"},
    {"table": "CrossRepoCalls", "kind": None, "source": "api:service/billing/billing.go:InvoiceService.Record",
     "target": "shared:contracts/audit.go:Audit.Stamp"},
    {"table": "CrossRepoCalls", "kind": None, "source": "api:service/billing/billing.go:InvoiceService.Record",
     "target": "shared:log/logger.go:Logger.Info"},
    {"table": "CrossRepoCalls", "kind": None, "source": "api:service/billing/billing.go:NewInvoiceService",
     "target": "shared:log/logger.go:New"},
    {"table": "CrossRepoCalls", "kind": None, "source": "api:service/cmd/api/main.go:main",
     "target": "shared:contracts/audit.go:Audit.Stamp"},
    {"table": "CrossRepoCalls", "kind": None, "source": "api:service/cmd/api/main.go:main",
     "target": "shared:money/price.go:NewPrice"},
    {"table": "CrossRepoCalls", "kind": None, "source": "web:clock/clock.go:Stamp",
     "target": "shared:contracts/clock.go:Clock.Now"},
    {"table": "CrossRepoCalls", "kind": None, "source": "web:orders/orders.go:NewOrderService",
     "target": "shared:log/logger.go:New"},
    {"table": "CrossRepoCalls", "kind": None, "source": "web:orders/orders.go:OrderService.Total",
     "target": "shared:log/logger.go:Logger.Info"},
    {"table": "CrossRepoCalls", "kind": None, "source": "web:orders/orders.go:OrderService.Total",
     "target": "shared:money/price.go:NewPrice"},
    {"table": "CrossRepoCalls", "kind": None, "source": "web:orders/orders.go:OrderService.Total",
     "target": "shared:money/price.go:Price.Add"},
    {"table": "CrossRepoCalls", "kind": None, "source": "web:orders/orders.go:OrderService.Total",
     "target": "shared:money/price.go:Price.Amount"},
    {"table": "CrossRepoCalls", "kind": None, "source": "web:orders/orders.go:OrderService.Total",
     "target": "shared:money/price.go:Price.Format"},
    {"table": "CrossRepoCalls", "kind": None, "source": "web:orders/orders.go:OrderService.Total",
     "target": "shared:money/round.go:Round"},
]

DECL_PINS += [
    {"qualname": "Price.Format", "module": SHARED, "kind": "method", "file": "shared/money/price.go"},
    {"qualname": "Clock", "module": SHARED, "kind": "class", "file": "shared/contracts/clock.go"},
    {"qualname": "OrderService", "module": WEB, "kind": "class", "file": O},
    {"qualname": "<init>", "module": "example.com/iryo/api/notify", "kind": "method",
     "file": "api/notify/mail/mail.go"},
]

LAYOUT_EXCEPTIONS = [
    "api/tools/report/report.go: imports example.com/iryo/shared/money, which no module reachable",
]
