# -*- coding: utf-8 -*-
"""Ground truth for iryo-kanri-system (Go), the source of record.

Each case points at a call site by file + a snippet that occurs once in that
file + the callee token inside the snippet (`occurrence` counts token matches
inside the snippet). No line numbers live here: generate_manifest.py derives
them into manifest.json. `expect` follows Go semantics under the fixture's
build (go1.27.2, GOOS=linux GOARCH=amd64 CGO_ENABLED=0, no tags) and the
spec's rules (§5: interface calls resolve to the interface method with the
implementers in candidates and runtime_targets; func values are dynamic
unless the func-alias rule proves a single binding; build-excluded files are
unresolved). Nothing here is copied from resolver output.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "_gt"))
from gt_lib import ambiguous, case, external, plain, resolved, target  # noqa: E402

MK = "example.com/iryo/kernel"
MD = "example.com/iryo/domain"
MDA = "example.com/iryo/data"
MB = "example.com/iryo/billing"
MPH = "example.com/iryo/pharmacy"
MS = "example.com/iryo/scheduling"
MRP = "example.com/iryo/reporting"
MLC = "example.com/iryo/legacy-claims"
MIO = "example.com/iryo/interop"
MTF = "example.com/thirdparty/textfmt"
MRD = "example.com/iryo/reception-desk"
MAD = "example.com/iryo/admin-console"
MPP = "example.com/iryo/patient-portal"
UUID = "example.com/thirdparty/uuidlite"
YAML = "example.com/thirdparty/yamlish"

RF = "reception-desk/internal/flows/"
RD = "reception-desk"
AD = "admin-console"
PP = "patient-portal"
KT = "kernel-tests"
BT = "billing-tests"


def m(qualname, module, file):
    return target(qualname, module, "method", file)


def c(qualname, module, file):
    return target(qualname, module, "class", file)


KLOG_NEW = m("New", MK, "kernel/logging/logging.go")
KLOG_LOG = m("Logger.Log", MK, "kernel/logging/logging.go")
KLOG_LINES = m("Logger.Lines", MK, "kernel/logging/logging.go")
PLOG_NEW = m("New", MPH, "pharmacy/logging/logging.go")
PLOG_LOG = m("Logger.Log", MPH, "pharmacy/logging/logging.go")
PLOG_LINES = m("Logger.Lines", MPH, "pharmacy/logging/logging.go")
CACHE_NEW = m("New", MDA, "data/cache/cache.go")
BVAL = m("Validator.Validate", MB, "billing/validation/validator.go")
PVAL = m("Validator.Validate", MPH, "pharmacy/validation/validator.go")
BTOYEN = m("ToYen", MB, "billing/pointmath/pointmath.go")
PTOYEN = m("ToYen", MPH, "pharmacy/pointmath/pointmath.go")
FEE_CALC = m("FeeCalculator.Calculate", MB, "billing/fee.go")
DOSE_CALC = m("DoseCalculator.Calculate", MPH, "pharmacy/dose.go")
TABLE_CALC = m("PointTable.Calculate", MLC, "legacy-claims/pointtable.go")
ROUND = m("Round", MB, "billing/fee.go")
ADDTAX = m("AddTax", MB, "billing/fee.go")
BFMT = m("ReceiptFormatter.Format", MB, "billing/text/receipt.go")
LFMT = m("ReceiptFormatter.Format", MLC, "legacy-claims/text.go")
IFMT = m("ReceiptFormatter.Format", MIO, "interop/text/receipt.go")
ENTITY_DESCRIBE = m("Entity.Describe", MD, "domain/patient/entity.go")
PATIENT_DESCRIBE = m("Patient.Describe", MD, "domain/patient/entity.go")
ENTITY_TOUCH = m("Entity.Touch", MD, "domain/patient/entity.go")
ENTITY_SAVE = m("Entity.Save", MD, "domain/patient/entity.go")
PATIENT_SAVE = m("Patient.Save", MD, "domain/patient/entity.go")
NOTIFIER_SEND = m("Notifier.Send", MK, "kernel/notify/notify.go")
MAIL_SEND = m("MailNotifier.Send", MK, "kernel/notify/notify.go")
SMS_SEND = m("SmsNotifier.Send", MS, "scheduling/sms.go")
BASE_FEE = m("BasePolicy.Fee", MK, "kernel/policy/policy.go")
URGENT_FEE = m("UrgentPolicy.Fee", MK, "kernel/policy/policy.go")
MS_PUT = m("MemoryStore.Put", MDA, "data/store/store.go")
MS_GET = m("MemoryStore.Get", MDA, "data/store/store.go")
CC_PUT = m("ChartCache.Put", MDA, "data/cache/cache.go")
CC_GET = m("ChartCache.Get", MDA, "data/cache/cache.go")
MEMSINK_WRITE = m("MemorySink.Write", MRP, "reporting/trail.go")
BASE_LABEL = m("BasePolicy.Label", MK, "kernel/policy/policy.go")
SLOT_LABEL = m("Slot.Label", MS, "scheduling/book.go")
SINK_WRITE = m("Sink.Write", MRP, "reporting/trail.go")
TF = "third_party/textfmt/textfmt.go"
TF_NEWTABLE = m("NewTable", MTF, TF)
TF_PADRIGHT = m("PadRight", MTF, TF)
TF_RENDER = m("Table.Render", MTF, TF)
TF_APPLY = m("Apply", MTF, TF)
REGISTER = m("Register", MK, "kernel/registry/registry.go")
NORMALIZE = m("normalize", MK, "kernel/clock/clock.go")
CLOSING_RUN = m("Run", MRD, RF + "closing/daily_close.go")
BATCH_RUN = m("Run", MAD, "admin-console/internal/batch/daily_batch.go")
D = RF + "dispatch/dispatch.go"
CO = "admin-console/internal/console/console.go"

CASES: list[dict] = []
DECL_PINS: list[dict] = []
XPR_PAIRS: list[dict] = []

# ── XPR: byte-identical lines, different imports (受付の日次締めと管理の日次バッチ) ──
XC = RF + "closing/daily_close.go"
XB = "admin-console/internal/batch/daily_batch.go"
CASES += [
    case("XPR-001", XC, 'ledger := logging.New("日次点検")', "New", "call", resolved(KLOG_NEW),
         "XPR-101 とバイト単位で同じ行。このファイルは example.com/iryo/kernel/logging を import する",
         distractors=[PLOG_NEW]),
    case("XPR-002", XC, 'ledger.Log("日次点検")', "Log", "call", resolved(KLOG_LOG),
         "受け手 ledger は共通基盤の *logging.Logger。XPR-102 と同じ行", distractors=[PLOG_LOG]),
    case("XPR-003", XC, 'ok := check.Validate("H12345678")', "Validate", "call", resolved(BVAL),
         "会計の validation.Validator。実行結果も分かれる (こちらは true、XPR-103 は false)",
         distractors=[PVAL]),
    case("XPR-004", XC, "yen := pointmath.ToYen(15)", "ToYen", "call", resolved(BTOYEN),
         "会計の pointmath.ToYen は 150 円。XPR-104 の同じ行は薬局の 162 円", distractors=[PTOYEN]),
    case("XPR-005", XC, "for _, line := range ledger.Lines() {", "Lines", "call", resolved(KLOG_LINES),
         "range の右辺の呼び出しは通常の call site。XPR-105 と同じ行", distractors=[PLOG_LINES]),
    case("XPR-101", XB, 'ledger := logging.New("日次点検")', "New", "call", resolved(PLOG_NEW),
         "XPR-001 と同じ行。import が example.com/iryo/pharmacy/logging なので薬局の New",
         entry=AD, distractors=[KLOG_NEW]),
    case("XPR-102", XB, 'ledger.Log("日次点検")', "Log", "call", resolved(PLOG_LOG),
         "受け手は薬局の *logging.Logger", entry=AD, distractors=[KLOG_LOG]),
    case("XPR-103", XB, 'ok := check.Validate("H12345678")', "Validate", "call", resolved(PVAL),
         "薬局の validation.Validator (処方箋の様式判定) なので false", entry=AD, distractors=[BVAL]),
    case("XPR-104", XB, "yen := pointmath.ToYen(15)", "ToYen", "call", resolved(PTOYEN),
         "薬局の pointmath.ToYen (院内レート)", entry=AD, distractors=[BTOYEN]),
    case("XPR-105", XB, "for _, line := range ledger.Lines() {", "Lines", "call", resolved(PLOG_LINES),
         "薬局の Logger.Lines", entry=AD, distractors=[KLOG_LINES]),
]
XPR_PAIRS += [
    {"kind": "line", "cases": ["XPR-001", "XPR-101"]},
    {"kind": "line", "cases": ["XPR-002", "XPR-102"]},
    {"kind": "line", "cases": ["XPR-003", "XPR-103"]},
    {"kind": "line", "cases": ["XPR-004", "XPR-104"]},
    {"kind": "line", "cases": ["XPR-005", "XPR-105"]},
]

# ── TWIN: same-named declarations in different modules ─────────────────────────
PF = RF + "pharmacyflow/pharmacyflow.go"
AC = RF + "accounting/accounting.go"
REP = RF + "reports/reports.go"
CASES += [
    case("TWIN-001", PF, 'houseLog.Log("処方箋受領")', "Log", "call", resolved(KLOG_LOG),
         "TWIN-002 と引数まで同じ。klog 別名で import した共通基盤 Logger", distractors=[PLOG_LOG]),
    case("TWIN-002", PF, 'dispenseLog.Log("処方箋受領")', "Log", "call", resolved(PLOG_LOG),
         "plog 別名で import した薬局の同名型 Logger。別モジュールの別宣言", distractors=[KLOG_LOG]),
    case("TWIN-003", PF, 'rx.Validate("Rp アモキシシリン 250mg")', "Validate", "call", resolved(PVAL),
         "処方箋の Validator (薬局)", distractors=[BVAL]),
    case("TWIN-004", PF, 'insurance.Validate("H12345678")', "Validate", "call", resolved(BVAL),
         "保険証の Validator (会計)", distractors=[PVAL]),
    case("TWIN-005", PF, "dose.Calculate(30, 10)", "Calculate", "call", resolved(DOSE_CALC),
         "Calculate 三兄弟の一つ。薬局の用量計算 (乗算)", distractors=[FEE_CALC, TABLE_CALC]),
    case("TWIN-006", PF, "fee.Calculate(30, 10)", "Calculate", "call", resolved(FEE_CALC),
         "会計の FeeCalculator.Calculate (ポインタ受け手)", distractors=[DOSE_CALC, TABLE_CALC]),
    case("TWIN-007", PF, "table.Calculate(30, 10)", "Calculate", "call", resolved(TABLE_CALC),
         "旧レセコンの PointTable.Calculate (経過措置で +1000)", distractors=[FEE_CALC, DOSE_CALC]),
    case("TWIN-008", AC, "current.Format(int(total))", "Format", "call", resolved(BFMT),
         "ReceiptFormatter 三兄弟の一つ目 (会計)", distractors=[LFMT, IFMT]),
    case("TWIN-009", AC, "legacy.Format(int(total))", "Format", "call", resolved(LFMT),
         "三兄弟の二つ目。モジュール example.com/iryo/legacy-claims のパッケージ句は rezept",
         distractors=[BFMT, IFMT]),
    case("TWIN-010", AC, "bridge.Format(int(total))", "Format", "call", resolved(IFMT),
         "三兄弟の三つ目 (外部連携、itext 別名)", distractors=[BFMT, LFMT]),
    case("TWIN-011", REP, "nw.Beds()", "Beds", "call",
         resolved(m("Ward.Beds", MD, "domain/wards/north/ward.go")),
         "北病棟と南病棟は同じ型名 Ward を別パッケージに持つ",
         distractors=[m("Ward.Beds", MD, "domain/wards/south/ward.go")]),
    case("TWIN-012", REP, "sw.Beds()", "Beds", "call",
         resolved(m("Ward.Beds", MD, "domain/wards/south/ward.go")),
         "南病棟の Ward.Beds", distractors=[m("Ward.Beds", MD, "domain/wards/north/ward.go")]),
    case("TWIN-013", PF, 'houseLog := klog.New("薬局窓口")', "New", "call", resolved(KLOG_NEW),
         "New という名前の関数はリポジトリ内に三つ (共通基盤と薬局の logging、data/cache) あり、標準ライブラリにも errors.New と log.New がある",
         distractors=[PLOG_NEW, CACHE_NEW]),
    case("TWIN-014", "data/repository/repository.go", 'r.log.Log("検索 " + strconv.Itoa(id))', "Log", "call",
         resolved(KLOG_LOG), "フィールド log の型 *logging.Logger (共通基盤) から決まる", distractors=[PLOG_LOG]),
]

# ── EMB: embedding, promotion, shadowing ─────────────────────────────────────────
CI = RF + "checkin/checkin.go"
CASES += [
    case("EMB-001", CI, "ae.Touch()", "Touch", "call", resolved(ENTITY_TOUCH),
         "AuditedEntity に埋め込んだ Entity の (*Entity).Touch。昇格の深さ 1。ae はアドレス可能なので & が補われる"),
    case("EMB-002", CI, "pt.Touch()", "Touch", "call", resolved(ENTITY_TOUCH),
         "Patient → AuditedEntity → Entity。昇格の深さ 2"),
    case("EMB-003", CI, "ip.Touch()", "Touch", "call", resolved(ENTITY_TOUCH),
         "Inpatient → Patient → AuditedEntity → Entity。昇格の深さ 3"),
    case("EMB-004", CI, "pt.Describe()", "Describe", "call", resolved(PATIENT_DESCRIBE),
         "Patient 自身の Describe (深さ 0) が深さ 2 の Entity.Describe を隠す", distractors=[ENTITY_DESCRIBE]),
    case("EMB-005", CI, "ip.Describe()", "Describe", "call", resolved(PATIENT_DESCRIBE),
         "Inpatient では深さ 1 の Patient.Describe が深さ 3 の Entity.Describe に勝つ",
         distractors=[ENTITY_DESCRIBE]),
    case("EMB-006", CI, "ip.Patient.AuditedEntity.Entity.Describe()", "Describe", "call",
         resolved(ENTITY_DESCRIBE), "埋め込みフィールドを明示的にたどると Entity 自身のメソッド",
         distractors=[PATIENT_DESCRIBE]),
    case("EMB-007", CI, 'ip.Record("入院")', "Record", "call",
         resolved(m("AuditedEntity.Record", MD, "domain/patient/entity.go")),
         "ポインタ受け手のメソッドが深さ 2 で昇格する。同名の関数 audit.Record とは別物",
         distractors=[m("Record", MK, "kernel/audit/audit.go")]),
    case("EMB-008", "domain/visit/visit.go", 'return v.Describe() + " / " + v.Reason', "Describe", "call",
         resolved(PATIENT_DESCRIBE), "ポインタ埋め込み *patient.Patient から昇格した Patient.Describe",
         distractors=[ENTITY_DESCRIBE]),
    case("EMB-009", "domain/patient/entity.go", 'return "患者保存 " + p.Name + " / " + p.Entity.Save()',
         "Save", "call", resolved(ENTITY_SAVE),
         "Patient.Save の中から昇格フィールド p.Entity を通して Entity.Save を呼ぶ。自分自身 (Patient.Save) ではない",
         distractors=[PATIENT_SAVE]),
    case("EMB-010", AC, "standard.Fee(10000)", "Fee", "call", resolved(BASE_FEE),
         "StandardPolicy は Fee を持たず、埋め込んだ BasePolicy.Fee が昇格する (C# の既定実装に相当)",
         distractors=[URGENT_FEE]),
    case("EMB-011", AC, "urgent.Fee(10000)", "Fee", "call", resolved(URGENT_FEE),
         "UrgentPolicy 自身の Fee が昇格した BasePolicy.Fee を隠す", distractors=[BASE_FEE]),
    case("EMB-012", AC, "urgent.BasePolicy.Fee(10000)", "Fee", "call", resolved(BASE_FEE),
         "埋め込みフィールドを明示した選択は隠された側を呼ぶ", distractors=[URGENT_FEE]),
    case("EMB-013", "reporting/trail.go", 'return t.Write(t.Name + ": " + msg)', "Write", "call",
         resolved(SINK_WRITE, candidates=[MEMSINK_WRITE, SINK_WRITE], runtime=[MEMSINK_WRITE], open_world=True),
         "構造体 AuditTrail に埋め込んだインタフェース Sink 経由の呼び出しはインタフェース呼び出し。"
         "候補は Sink の実装: *MemorySink と、埋め込みインタフェースで Sink を満たす AuditTrail (二段目のディスパッチ、"
         "候補は Sink.Write 自身)。デモでは *MemorySink が入る。admin-console の go.mod の replace がこのモジュールの"
         "ディレクトリを別のグループでもビルドに入れるので、open_world は true (spec R54)"),
    case("EMB-014", "reporting/trail.go", 'return j.Write("[公開] " + msg)', "Write", "call",
         resolved(m("Journal.Write", MRP, "reporting/trail.go"), candidates=[MEMSINK_WRITE, SINK_WRITE],
                  runtime=[MEMSINK_WRITE], open_world=True),
         "Journal を満たす AuditTrail は自分の埋め込みインタフェース Sink で Write を得る (二段目のディスパッチ、"
         "候補は Sink.Write)。デモは AuditTrail を渡し、その Sink が *MemorySink なので実行時は MemorySink.Write。"
         "admin-console の go.mod の replace がこのモジュールのディレクトリを別のグループでもビルドに入れるので、"
         "open_world は true (spec R54)"),
    case("EMB-015", REP, "list.Put(7, pt)", "Put", "call", resolved(MS_PUT),
         "ジェネリック型の具体化 store.MemoryStore[patient.Patient] を埋め込み、ポインタメソッド Put が昇格する",
         distractors=[CC_PUT]),
    case("EMB-016", "reception-desk/internal/flows/maintenance/maintenance.go", "lock.Lock()", "Lock", "call",
         external("std"), "埋め込んだ標準ライブラリの sync.Mutex から昇格した (*Mutex).Lock"),
]

# ── IFC: interface dispatch (open world, spec §5.4) ──────────────────────────────
NT = "kernel/notify/notify.go"
CASES += [
    case("IFC-001", NT, "out = append(out, n.Send(msg))", "Send", "call",
         resolved(NOTIFIER_SEND, candidates=[MAIL_SEND, SMS_SEND], runtime=[MAIL_SEND, SMS_SEND], open_world=True),
         "実装が二つ (共通基盤の MailNotifier と予約モジュールの *SmsNotifier)。受付デモは両方を渡す。"
         "admin-console の go.mod の replace がこのモジュールのディレクトリを別のグループでもビルドに入れるので、"
         "open_world は true (spec R54)"),
    case("IFC-002", NT, "line := tn.Send(msg)", "Send", "call",
         resolved(NOTIFIER_SEND, candidates=[SMS_SEND], runtime=[SMS_SEND], open_world=True),
         "TrackedNotifier は Notifier を埋め込む。Send を宣言するのは埋め込まれた Notifier。候補は受け手の静的型 "
         "TrackedNotifier の実装 (*SmsNotifier だけ。MailNotifier は Sent を持たない)。admin-console の go.mod の "
         "replace がこのモジュールのディレクトリを別のグループでもビルドに入れるので、open_world は true (spec R54)",
         distractors=[MAIL_SEND]),
    case("IFC-003", NT, "itoa(tn.Sent())", "Sent", "call",
         resolved(m("TrackedNotifier.Sent", MK, NT), candidates=[m("SmsNotifier.Sent", MS, "scheduling/sms.go")],
                  runtime=[m("SmsNotifier.Sent", MS, "scheduling/sms.go")], open_world=True),
         "TrackedNotifier 自身が宣言する Sent。実装は一つでも格上げしない。admin-console の go.mod の replace が"
         "このモジュールのディレクトリを別のグループでもビルドに入れるので、open_world は true (spec R54)"),
    case("IFC-004", "kernel/audit/audit.go", 'l.Printf("監査 %s 件数=%d", event, count)', "Printf", "call",
         resolved(m("Logger.Printf", MK, "kernel/audit/audit.go"),
                  candidates=[m("ConsoleLogger.Printf", MK, "kernel/audit/audit.go")], open_world=True),
         "リポジトリ内の実装は *ConsoleLogger だけだが、標準ライブラリの *log.Logger も満たす (open world)。"
         "受付デモが渡すのは log.New の *log.Logger なので、実行時にリポジトリ内の関数は走らない"),
    case("IFC-005", "kernel/clock/clock.go", 'return c.Now().Format("2006-01-02")', "Now", "call",
         resolved(m("Clock.Now", MK, "kernel/clock/clock.go"),
                  candidates=[m("OfficeClock.Now", MK, "kernel/clock/clock.go"),
                              m("fakeClock.Now", MK, "kernel/clock/clock_test.go")],
                  runtime=[m("fakeClock.Now", MK, "kernel/clock/clock_test.go"),
                           m("OfficeClock.Now", MK, "kernel/clock/clock.go")],
                  open_world=True),
         "テストだけの偽実装 fakeClock (clock_test.go) も候補 (test_only)。kernel のテストは TestStamp で fakeClock、"
         "TestLabel で OfficeClock を渡す。受付デモも受付フローの v.Dated から OfficeClock で到達する。admin-console の "
         "go.mod の replace がこのモジュールのディレクトリを別のグループでもビルドに入れるので、open_world は true "
         "(spec R54)", entry=KT),
    case("IFC-006", "interop/exporters/exporters.go", "out = append(out, e.Export(rows))", "Export", "call",
         resolved(m("Exporter.Export", MIO, "interop/exporters/exporters.go"),
                  candidates=[m("CsvExporter.Export", MIO, "interop/exporters/exporters.go"),
                              m("XmlExporter.Export", MIO, "interop/exporters/exporters.go")],
                  runtime=[m("CsvExporter.Export", MIO, "interop/exporters/exporters.go"),
                           m("XmlExporter.Export", MIO, "interop/exporters/exporters.go")],
                  open_world=False),
         "実装二つ。保守フローは両方を渡す"),
    case("IFC-007", "interop/exporters/exporters.go", "return a.ArchiveAll(year)", "ArchiveAll", "call",
         resolved(m("Archiver.ArchiveAll", MIO, "interop/exporters/exporters.go"), open_world=False),
         "実装が一つもないインタフェース。デモは nil を渡すので、この呼び出しには到達しない", entry=None),
    case("IFC-008", "interop/records/records.go", "return r.Kind()", "Kind", "call",
         resolved(m("Record.Kind", MIO, "interop/records/records.go"),
                  candidates=[m("Claim.Kind", MIO, "interop/records/records.go"),
                              m("Refund.Kind", MIO, "interop/records/records.go")],
                  runtime=[m("Claim.Kind", MIO, "interop/records/records.go"),
                           m("Refund.Kind", MIO, "interop/records/records.go")],
                  open_world=False),
         "未公開メソッド sealed() を持つ封印インタフェース。他のパッケージの型は records の型を埋め込まない限り実装できず、"
         "そういう型はない"),
    case("IFC-009", "pharmacy/dose.go", "out = append(out, s.Stamp(l))", "Stamp", "call",
         resolved(m("Stamper.Stamp", MPH, "pharmacy/dose.go"),
                  candidates=[m("RubberStamp.Stamp", MPH, "pharmacy/dose.go")],
                  runtime=[m("RubberStamp.Stamp", MPH, "pharmacy/dose.go")], open_world=True),
         "リポジトリ内の実装が一つでも RubberStamp.Stamp の MethodCalls には格上げしない (Go は暗黙の実装、spec §5.4)。"
         "admin-console の go.mod の replace がこのモジュールのディレクトリを別のグループでもビルドに入れるので、"
         "open_world は true (spec R54)"),
    case("IFC-010", AC, "return p.Fee(amount)", "Fee", "call",
         resolved(m("PaymentPolicy.Fee", MK, "kernel/policy/policy.go"), candidates=[BASE_FEE, URGENT_FEE],
                  runtime=[BASE_FEE, URGENT_FEE], open_world=False),
         "StandardPolicy は昇格した BasePolicy.Fee で満たす。charge は StandardPolicy と UrgentPolicy で呼ばれる"),
    case("IFC-011", CI, "return repo.Find(id)", "Find", "call",
         resolved(m("Repository.Find", MDA, "data/repository/repository.go"),
                  candidates=[m("SqlPatientRepository.Find", MDA, "data/repository/repository.go")],
                  runtime=[m("SqlPatientRepository.Find", MDA, "data/repository/repository.go")], open_world=False),
         "ジェネリックなインタフェース Repository[patient.Patient]。ターゲットは原型のメソッド"),
    case("IFC-012", CI, 'notify.Notifier.Send(notify.MailNotifier{To: "ward@iryo.example"}, "病棟連絡")', "Send",
         "call", resolved(NOTIFIER_SEND, candidates=[MAIL_SEND, SMS_SEND], runtime=[MAIL_SEND], open_world=False),
         "インタフェースのメソッド式。インタフェース呼び出しと同じ扱い"),
    case("IFC-013", "scheduling/book.go", 'return b.notifier.Send(s.Name + " 様の予約")', "Send", "call",
         resolved(NOTIFIER_SEND, candidates=[MAIL_SEND, SMS_SEND], runtime=[SMS_SEND], open_world=False),
         "インタフェース型のフィールド経由。受付デモの予約簿は *SmsNotifier を持つ"),
    case("IFC-014", "reception-desk/internal/flows/maintenance/maintenance.go",
         'fmt.Fprintln(w, csv.Export([]string{"直接"}))', "Export", "call",
         resolved(m("CsvExporter.Export", MIO, "interop/exporters/exporters.go")),
         "具体型への型アサーションの結果なので静的な呼び出し",
         distractors=[m("Exporter.Export", MIO, "interop/exporters/exporters.go"),
                      m("XmlExporter.Export", MIO, "interop/exporters/exporters.go")]),
    case("IFC-015", TF, "s = f.Format(s)", "Format", "call",
         resolved(m("Formatter.Format", MTF, TF),
                  candidates=[m("Bracket.Format", MTF, TF), m("Upper.Format", MTF, TF)],
                  runtime=[m("Upper.Format", MTF, TF), m("Bracket.Format", MTF, TF)], open_world=True),
         "リポジトリ内フォーク textfmt の Formatter は標準ライブラリの time.Time (Format(layout string) string) "
         "にも偶然満たされる (open world)。受付デモの Heading は Upper と Bracket を渡す",
         distractors=[BFMT, LFMT, IFMT]),
    case("IFC-016", REP, "out = append(out, l.Label())", "Label", "call",
         ambiguous(BASE_LABEL, SLOT_LABEL, runtime=[BASE_LABEL, SLOT_LABEL]),
         "関数の中で宣言した名前付きインタフェース labeler はノードを持たないので、無名インタフェースと同じく "
         "リポジトリ内の実装を候補にした ambiguous (spec 付録 B R2)。囲む関数 labelsOf を名指ししてはいけない。"
         "帳票フローは UrgentPolicy (昇格した BasePolicy.Label) と Slot を渡す",
         distractors=[m("labelsOf", MRD, REP), m("Label", MK, "kernel/clock/clock.go")]),
    case("IFC-017", CO, 'fmt.Fprintln(w, n.Send("月次締め完了"))', "Send", "call",
         resolved(NOTIFIER_SEND, candidates=[MAIL_SEND], runtime=[MAIL_SEND], open_world=False),
         "管理コンソールのプログラムは予約モジュールを読み込まないので、候補は MailNotifier.Send だけ "
         "(候補はそのグループの読み込みにある型、master plan C2)。受付側の同じインタフェースの呼び出しとは候補が違う",
         entry=AD, distractors=[SMS_SEND]),
    case("IFC-018", "interop/lookup/lookup.go", "code, err := s.Get()", "Get", "call",
         resolved(m("Source.Get", MIO, "interop/lookup/lookup.go"),
                  candidates=[m("Fixed.Get", MIO, "interop/lookup/lookup.go")],
                  runtime=[m("Fixed.Get", MIO, "interop/lookup/lookup.go")], open_world=False),
         "R52: Tracker は uuidlite.Sequence を埋め込み、ポインタのメソッド Get() float64 を持つ。オフラインでは "
         "埋め込みの型が不正になり、go/types は Tracker をどのインタフェースも満たすと答えるが、自分の Get が勝ち、"
         "シグネチャも違うので、どちらのモードでも Source を満たさない。候補は Fixed だけで、Tracker との実装の組も"
         "作らない。候補の集合は閉じていて、記録に candidates_partial は付かない。オフラインの open_world は決めない",
         entry=RD, distractors=[m("Tracker.Get", MIO, "interop/idkit/tracker.go")],
         offline=resolved(m("Source.Get", MIO, "interop/lookup/lookup.go"),
                          candidates=[m("Fixed.Get", MIO, "interop/lookup/lookup.go")],
                          runtime=[m("Fixed.Get", MIO, "interop/lookup/lookup.go")])),
]

# ── GEN: generics ─────────────────────────────────────────────────────────────────
CASES += [
    case("GEN-001", REP, "s := store.NewMemoryStore[patient.Patient]()", "NewMemoryStore", "call",
         resolved(m("NewMemoryStore", MDA, "data/store/store.go")),
         "明示的な具体化 (index 形)。トークンは基底の識別子。ターゲットは原型の宣言"),
    case("GEN-002", REP, "s.Put(1, pt)", "Put", "call", resolved(MS_PUT),
         "ジェネリック型 MemoryStore[T] のメソッド", distractors=[CC_PUT]),
    case("GEN-003", REP, "return s.Count()", "Count", "call",
         resolved(m("MemoryStore.Count", MDA, "data/store/store.go")),
         "引数の型はジェネリックな型エイリアス store.Store[string] (= MemoryStore[string])"),
    case("GEN-004", REP, 'store.FirstOf([]string{"初診", "再診"})', "FirstOf", "call",
         resolved(m("FirstOf", MDA, "data/store/store.go")), "型引数の推論"),
    case("GEN-005", REP, "charts := cache.New[string, int]()", "New", "call", resolved(CACHE_NEW),
         "型引数二つの明示的な具体化 (IndexListExpr)。リポジトリ内の New は他に二つ (共通基盤と薬局の logging) ある",
         distractors=[KLOG_NEW, PLOG_NEW]),
    case("GEN-006", "domain/patient/describe.go", "out = append(out, it.Describe())", "Describe", "call",
         resolved(m("Describer.Describe", MD, "domain/patient/describe.go"),
                  candidates=[ENTITY_DESCRIBE, PATIENT_DESCRIBE], runtime=[PATIENT_DESCRIBE], open_world=True),
         "型パラメータの制約 Describer のメソッド (typeparam)。候補は Describer を満たす型のメソッドで、"
         "AuditedEntity と Inpatient と visit.Visit は昇格で満たす。帳票フローは Inpatient で具体化する。"
         "admin-console の go.mod の replace がこのモジュールのディレクトリを別のグループでもビルドに入れるので、"
         "open_world は true (spec R54)"),
    case("GEN-007", REP, "patient.DescribeAll(inpatients)", "DescribeAll", "call",
         resolved(m("DescribeAll", MD, "domain/patient/describe.go")), "制約付き型パラメータの推論"),
    case("GEN-008", "data/store/store.go", "out = append(out, f(x))", "f", "call",
         plain("dynamic", runtime=[m("label", MRD, REP)]),
         "ジェネリック関数の関数型引数の呼び出しは dynamic。帳票フローは strings.ToUpper (標準ライブラリ) と label を渡す"),
    case("GEN-009", REP, "store.Sum([]int{288, 73, 225})", "Sum", "call",
         resolved(m("Sum", MDA, "data/store/store.go")), "型集合の制約 (~int | ~int64 | ~float64)"),
    case("GEN-010", REP, 'bp, _ := charts.Get("血圧")', "Get", "call", resolved(CC_GET),
         "ChartCache.Get", distractors=[MS_GET]),
    case("GEN-011", "reporting/labels.go", "out = append(out, it.Label())", "Label", "call",
         ambiguous(BASE_LABEL, SLOT_LABEL, runtime=[SLOT_LABEL]),
         "インラインの制約 interface{ Label() string } のメソッドはノードを持たないので、リポジトリ内の実装を候補にした "
         "ambiguous (spec 付録 B R2)。囲む関数 LabelAll を名指ししてはいけない。帳票フローは Slot で具体化する",
         distractors=[m("LabelAll", MRP, "reporting/labels.go"), m("Label", MK, "kernel/clock/clock.go")]),
]

# ── FV: func values, aliases, closures, go/defer, method values, range-over-func ──
CASES += [
    case("FV-001", D, "fmt.Fprintln(w, round(1234))", "round", "call", resolved(ROUND),
         "func-alias: ローカル変数に関数を一度だけ束縛し、再代入もアドレス取得もクロージャ内の代入もない"),
    case("FV-002", D, 'logf("別名呼び出し")', "logf", "call", resolved(KLOG_LOG),
         "func-alias: 具体型の受け手を持つメソッド値 log.Log", distractors=[PLOG_LOG]),
    case("FV-003", D, "fmt.Fprintln(w, double(21))", "double", "call", resolved(m("Run.func1", MRD, D)),
         "func-alias: 関数リテラルの束縛。ターゲットはクロージャ Run.func1"),
    case("FV-004", D, "fmt.Fprintln(w, pick(500))", "pick", "call", plain("dynamic", runtime=[ADDTAX]),
         "if の中で再代入されるので単一束縛でない。実行時は AddTax"),
    case("FV-005", D, "fmt.Fprintln(w, keep(777), *ref != nil)", "keep", "call",
         plain("dynamic", runtime=[ROUND]), "&keep でアドレスを取られるので証明できない"),
    case("FV-006", D, "fmt.Fprintln(w, late(800))", "late", "call", plain("dynamic", runtime=[ADDTAX]),
         "クロージャの中で再代入される。実行時は AddTax"),
    case("FV-007", D, "return fn(yen)", "fn", "call", plain("dynamic", runtime=[ADDTAX]),
         "引数の関数値。apply は AddTax だけで呼ばれる"),
    case("FV-008", D, 'fmt.Fprintln(w, Hooks["tax"](2000))', "Hooks", "call", plain("dynamic", runtime=[ADDTAX]),
         "パッケージ変数の map の要素の呼び出し (index 形、トークンは Hooks)"),
    case("FV-009", D, "fmt.Fprintln(w, Fallback(1999))", "Fallback", "call", plain("dynamic", runtime=[ROUND]),
         "パッケージレベルの関数変数はテストが書き換えられるので単一束縛とみなさない (spec §5.5)"),
    case("FV-010", D, 'fmt.Fprintln(w, route.Handle("route"))', "Handle", "call", plain("dynamic"),
         "関数型フィールド。実行時は strings.ToUpper (標準ライブラリ) なのでリポジトリ内の関数は走らない"),
    case("FV-011", D, "fmt.Fprintln(w, add(3))", "add", "call",
         plain("dynamic", runtime=[m("makeAdder.func1", MRD, D)]),
         "呼び出し結果 (クロージャ) を束縛した変数。func-alias の条件 (関数、メソッド値、関数リテラル) に当たらない"),
    case("FV-012", D, "go func() {", "func", "go", resolved(m("Run.func3", MRD, D)),
         "go 文の関数リテラル (funclit)。ターゲットはクロージャ Run.func3"),
    case("FV-013", D, "go worker(&wg, results, 1)", "worker", "go", resolved(m("worker", MRD, D)),
         "名前付き関数の go 文"),
    case("FV-014", D, "fmt.Fprintln(w, func() string {", "func", "call", resolved(m("Run.func4", MRD, D)),
         "その場で呼ぶ関数リテラル (IIFE)。トークンは func キーワード"),
    case("FV-015", D, 'defer func() { fmt.Fprintln(w, "集計", n) }()', "func", "defer",
         resolved(m("tally.func1", MRD, D)), "defer 文の関数リテラル。tally の defer はこれ一つ"),
    case("FV-016", AC, "defer report(w, sess)", "report", "defer", resolved(m("report", MRD, AC)),
         "名前付き関数の defer 文。settle の defer はこれ一つ"),
    case("FV-017", CI, 'fmt.Fprintln(w, send("呼出"))', "send", "call",
         resolved(NOTIFIER_SEND, candidates=[MAIL_SEND, SMS_SEND], runtime=[MAIL_SEND], open_world=False),
         "インタフェースを受け手とするメソッド値の単一束縛は、インタフェース呼び出しと同じに扱う (spec §5.5)。"
         "notifiers[0] は MailNotifier"),
    case("FV-018", AC, "fmt.Fprintln(w, (*billing.FeeCalculator).Calculate(calc, 2, 3))", "Calculate", "call",
         resolved(FEE_CALC), "メソッド式 (*T).M の呼び出し", distractors=[DOSE_CALC, TABLE_CALC]),
    case("FV-019", REP, "for slot := range book.Each {", "Each", "range",
         resolved(m("AppointmentBook.Each", MS, "scheduling/book.go")),
         "range-over-func: メソッド値を反復子として range する暗黙の呼び出し (convention:range)"),
    case("FV-020", REP, "for n := range scheduling.Countdown {", "Countdown", "range",
         resolved(m("Countdown", MS, "scheduling/book.go")), "パッケージ関数を反復子として range する"),
    case("FV-021", "kernel/registry/registry.go", "return normalizer(s)", "normalizer", "call",
         plain("dynamic", runtime=[m("<init>.func1", MK, "kernel/registry/registry.go")]),
         "パッケージ変数に入れた関数リテラル。ターゲットのクロージャは <init>.func1"),
    case("FV-022", D, 'fmt.Fprintln(w, write("別名経由の記録"))', "write", "call",
         resolved(SINK_WRITE, candidates=[MEMSINK_WRITE, SINK_WRITE], runtime=[MEMSINK_WRITE], open_world=False),
         "受け手 trail は具体型 AuditTrail だが、選ばれるメソッドは埋め込みインタフェース Sink の Write。"
         "メソッド値の別名はインタフェース呼び出しと同じに扱い、具体メソッドへの func-alias にはしない (spec 付録 B R36)"),
    case("FV-023", D, 'fmt.Fprintln(w, stamp(pharmacy.RubberStamp{Name: "受付"}, "控え"))', "stamp", "call",
         resolved(m("RubberStamp.Stamp", MPH, "pharmacy/dose.go")),
         "具体型のメソッド式 RubberStamp.Stamp を一度だけ束縛した func-alias (spec 付録 B R36)",
         distractors=[m("Stamper.Stamp", MPH, "pharmacy/dose.go")]),
    case("FV-024", D, 'fmt.Fprintln(w, kind(records.Refund{No: "R-2"}))', "kind", "call",
         resolved(m("Record.Kind", MIO, "interop/records/records.go"),
                  candidates=[m("Claim.Kind", MIO, "interop/records/records.go"),
                              m("Refund.Kind", MIO, "interop/records/records.go")],
                  runtime=[m("Refund.Kind", MIO, "interop/records/records.go")], open_world=False),
         "インタフェースのメソッド式 records.Record.Kind の別名はインタフェース呼び出し (spec 付録 B R36)"),
]

# ── BLT / CNV: builtins and conversions ─────────────────────────────────────────
MT = "reception-desk/internal/flows/maintenance/maintenance.go"
CASES += [
    case("BLT-001", MT, "size := unsafe.Sizeof(pt)", "Sizeof", "call", plain("builtin"),
         "unsafe.Sizeof は組み込み ($GOROOT/src/unsafe)"),
    case("BLT-002", MT, "shortest := min(len(codes), 2)", "min", "call", plain("builtin"), "組み込みの min"),
    case("BLT-003", MT, "clear(counts)", "clear", "call", plain("builtin"), "組み込みの clear"),
    case("BLT-004", MT, "visits := new(len(codes) * 2)", "new", "call", plain("builtin"),
         "Go 1.26 の式を取る new"),
    case("BLT-005", "kernel/guard/guard.go", 'panic(fmt.Sprintf("負の値: %d", n))', "panic", "call",
         plain("builtin"), "組み込みの panic。振り分けフローの recovered で実際に起きる"),
    case("BLT-006", MT, "fmt.Fprintln(w, done.Error())", "Error", "call", plain("builtin"),
         "宣言済みの error インタフェースの Error は組み込み扱いで、候補を持たない (master plan C2)。"
         "実行時の型は errors.New のもので、リポジトリ内の RequiredError.Error は走らない",
         distractors=[m("RequiredError.Error", MK, "kernel/guard/guard.go")]),
    case("CNV-001", AC, "consultation := money.Yen(2880)", "Yen", "call", plain("conversion"),
         "名前付き型 money.Yen への変換。呼び出しではない", distractors=[m("Yen.Add", MD, "domain/money/money.go")]),
    case("CNV-002", AC, "current.Format(int(total))", "int", "call", plain("conversion"), "基本型 int への変換"),
    case("CNV-003", AC, "medication := money.Points(146).ToYen()", "Points", "call", plain("conversion"),
         "変換の結果に対するメソッド呼び出しの内側"),
    case("CNV-004", AC, "medication := money.Points(146).ToYen()", "ToYen", "call",
         resolved(m("Points.ToYen", MD, "domain/money/money.go")),
         "変換の結果に対するメソッド呼び出しは普通に解決する", distractors=[BTOYEN, PTOYEN]),
    case("CNV-005", D, "steps := []Step{Step(double), billing.Round}", "Step", "call", plain("conversion"),
         "リポジトリ内の関数型 Step への変換 (一つ目の Step は複合リテラルの型)", occurrence=2),
    case("CNV-006", "patient-portal/internal/server/server.go",
         'mux.Handle("/health", http.HandlerFunc(p.handleHealth))', "HandlerFunc", "call", plain("conversion"),
         "標準ライブラリの関数型 http.HandlerFunc への変換", entry=PP),
]

# ── BLD: build constraints ─────────────────────────────────────────────────────────
PL = "kernel/platform/"
CASES += [
    case("BLD-001", PL + "platform.go", 'return Name() + ":" + homeDir() + ":" + Accel()', "Name", "call",
         resolved(m("Name", MK, PL + "platform_linux.go")),
         "ビルドツイン。linux/amd64 では platform_linux.go が入る",
         distractors=[m("Name", MK, PL + "platform_windows.go"), m("Name", MK, PL + "platform_other.go")]),
    case("BLD-002", PL + "platform.go", 'return Name() + ":" + homeDir() + ":" + Accel()', "homeDir", "call",
         resolved(m("homeDir", MK, PL + "platform_linux.go")), "未公開関数のビルドツイン",
         distractors=[m("homeDir", MK, PL + "platform_windows.go"), m("homeDir", MK, PL + "platform_other.go")]),
    case("BLD-003", PL + "platform.go", 'return Name() + ":" + homeDir() + ":" + Accel()', "Accel", "call",
         resolved(m("Accel", MK, PL + "accel_nocgo.go")),
         "//go:build cgo と !cgo のツイン。CGO_ENABLED=0 なので accel_nocgo.go",
         distractors=[m("Accel", MK, PL + "accel_cgo.go")]),
    case("BLD-004", PL + "platform_windows.go", "return windowsName()", "windowsName", "call",
         plain("unresolved", reason="excluded-by-build"),
         "ファイル名の _windows でビルドから外れるファイルの site。名前の推測もしない", entry=None,
         distractors=[m("windowsName", MK, PL + "platform_windows.go")]),
    case("BLD-005", PL + "platform_debug.go", 'return fmt.Sprintf("debug:%s", Name())', "Name", "call",
         plain("unresolved", reason="excluded-by-build"),
         "独自タグ iryodebug のファイルは既定のビルドに入らない", entry=None,
         distractors=[m("Name", MK, PL + "platform_linux.go")]),
    case("BLD-006", PL + "platform_debug.go", 'return fmt.Sprintf("debug:%s", Name())', "Sprintf", "call",
         plain("unresolved", reason="excluded-by-build"),
         "除外ファイルでは標準ライブラリの呼び出しも unresolved", entry=None),
]

# ── TST: tests ─────────────────────────────────────────────────────────────────────
CK = "kernel/clock/"
CASES += [
    case("TST-001", CK + "clock_test.go", 'if got := normalize("  受付 "); got != "受付" {', "normalize", "call",
         resolved(NORMALIZE), "同じパッケージのテストから未公開関数", entry=KT),
    case("TST-002", CK + "clock_ext_test.go", 'if got := clock.Normalize(" 会計 "); got != "会計" {',
         "Normalize", "call", resolved(m("Normalize", MK, CK + "export_test.go")),
         "外部テストパッケージ clock_test から export_test.go の関数", entry=KT,
         distractors=[m("Normalize", MK, "kernel/registry/registry.go")]),
    case("TST-003", CK + "clock_ext_test.go", 'if got := clock.NormalizeHook(" 薬局 "); got != "薬局" {',
         "NormalizeHook", "call", plain("dynamic", runtime=[NORMALIZE]),
         "export_test.go のパッケージ変数に入れた関数値", entry=KT),
    case("TST-004", CK + "export_test.go", "return normalize(label)", "normalize", "call", resolved(NORMALIZE),
         "export_test.go の中の呼び出し", entry=KT),
    case("TST-005", CK + "clock_test.go", 'if got := Stamp(fakeClock{}); got != "2000-01-02" {', "Stamp",
         "call", resolved(m("Stamp", MK, CK + "clock.go")), "テストから製品コードの関数", entry=KT),
    case("TST-006", CK + "clock_ext_test.go", "day := time.Date(2026, 10, 13, 0, 0, 0, 0, time.UTC)", "Date",
         "call", external("std"), "外部テストから標準ライブラリ", entry=KT),
    case("TST-007", "kernel/escalation/escalation.go", 'return p.Ward + ":" + p.Level()', "Level", "call",
         ambiguous(m("rule.Level", MK, "kernel/escalation/escalation.go"),
                   m("Policy.Level", MK, "kernel/escalation/escalation_test.go"),
                   runtime=[m("rule.Level", MK, "kernel/escalation/escalation.go"),
                            m("Policy.Level", MK, "kernel/escalation/escalation_test.go")]),
         "製品のパッケージでは昇格した rule.Level、テスト変種では escalation_test.go が宣言する Policy.Level "
         "(深さ 0) が勝つ。変種が食い違うので ambiguous (variant)。kernel のテストでは Policy.Level、"
         "受付デモの保守フロー (製品のパッケージ) では rule.Level が走る",
         entry=KT),
    case("TST-008", "billing/fee_test.go", "if got := c.Calculate(10, 3); got != 30 {", "Calculate", "call",
         resolved(FEE_CALC), "会計のパッケージ内テスト", entry=BT, distractors=[DOSE_CALC, TABLE_CALC]),
]

# ── GNC: generated code ─────────────────────────────────────────────────────────────
LC = "legacy-claims/"
CASES += [
    case("GNC-001", LC + "codes_gen.go", "return normalizeCode(codeTable[strings.ToUpper(code)])",
         "normalizeCode", "call", resolved(m("normalizeCode", MLC, LC + "codes.go")),
         "生成ファイル (// Code generated ... DO NOT EDIT.) から手書きのファイルへ"),
    case("GNC-002", LC + "codes_gen.go", "return normalizeCode(codeTable[strings.ToUpper(code)])", "ToUpper",
         "call", external("std"), "生成ファイルの中の標準ライブラリ呼び出し"),
    case("GNC-003", LC + "codes.go", 'return code + ":" + LookupCode(code)', "LookupCode", "call",
         resolved(m("LookupCode", MLC, LC + "codes_gen.go")), "手書きのファイルから生成ファイルの関数へ"),
    case("GNC-004", MT, "rezept.KnownCodes()", "KnownCodes", "call",
         resolved(m("KnownCodes", MLC, LC + "codes_gen.go")), "他のモジュールから生成ファイルの関数へ"),
]

# ── INI: package initializers ───────────────────────────────────────────────────────
RG = "kernel/registry/"
CASES += [
    case("INI-001", RG + "registry.go", 'var defaultName = strings.ToUpper("iryo")', "ToUpper", "call",
         external("std"), "パッケージ変数の初期化式。呼び出し元はこのファイルの <init>。goracle は初期化式に計数器を"
         "置けず、呼び先も標準ライブラリなので、実行されてもオラクルには見えない (入口なし)", entry=None),
    case("INI-002", RG + "registry.go", "var banner = makeBanner(defaultName)", "makeBanner", "call",
         resolved(m("makeBanner", MK, RG + "registry.go")), "呼び出し元は <init>"),
    case("INI-003", RG + "registry.go", "return strings.TrimSpace(s)", "TrimSpace", "call", external("std"),
         "パッケージ変数に入れた関数リテラルの中。呼び出し元は <init>.func1"),
    case("INI-004", RG + "registry.go", 'Register("echo", echo)', "Register", "call", resolved(REGISTER),
         "一つ目の init 関数の中。呼び出し元は init#1"),
    case("INI-005", RG + "registry.go", 'Register("upper", strings.ToUpper)', "Register", "call",
         resolved(REGISTER), "同じファイルの二つ目の init 関数。呼び出し元は init#2"),
    case("INI-006", RG + "defaults.go", 'Register("trim", strings.TrimSpace)', "Register", "call",
         resolved(REGISTER), "init の番号はファイルごと。defaults.go の init は init#1"),
]

# ── EXT: stdlib and third-party modules ─────────────────────────────────────────────
TA = "billing/tariff/tariff.go"
IK = "interop/idkit/idkit.go"
SV = "patient-portal/internal/server/server.go"
CASES += [
    case("EXT-001", MT, 'audit.Record(log.New(w, "", 0), "保守", 2)', "New", "call", external("std"),
         "標準ライブラリの log.New", distractors=[KLOG_NEW, PLOG_NEW, CACHE_NEW]),
    case("EXT-002", MT, 'done := errors.New("保守完了")', "New", "call", external("std"),
         "標準ライブラリの errors.New", distractors=[KLOG_NEW, PLOG_NEW, CACHE_NEW]),
    case("EXT-003", MT, 'fmt.Fprintln(w, sb.String(), day.Format("2006-01-02"), lock.beds, done)', "Format",
         "call", external("std"), "time.Time.Format。リポジトリ内の Format が五つある",
         distractors=[BFMT, LFMT, IFMT, m("Upper.Format", MTF, TF)]),
    case("EXT-004", TA, "doc, err := yamlish.Parse(source)", "Parse", "call", external(YAML),
         "第三者モジュール (_proxy の yamlish v1.1.0)"),
    case("EXT-005", TA, "v, ok := t.doc.Get(code)", "Get", "call", external(YAML),
         "第三者モジュールの型 *yamlish.Document のメソッド", distractors=[MS_GET, CC_GET]),
    case("EXT-006", TA, "return t.doc.Keys()", "Keys", "call", external(YAML),
         "v1.1.0 で追加されたメソッド。モジュールの版が解決を決める"),
    case("EXT-007", IK, "id := uuidlite.NewFrom(p.seq)", "NewFrom", "call", external(UUID), "第三者モジュール uuidlite"),
    case("EXT-008", IK, 'return u.String() + "/" + u.Short(), nil', "Short", "call", external(UUID),
         "v1.1.0 で追加された UUID.Short"),
    case("EXT-009", SV, 'name := r.URL.Query().Get("name")', "Get", "call", external("std"),
         "url.Values.Get", entry=PP, distractors=[MS_GET, CC_GET]),
    case("EXT-010", SV, 'mux.HandleFunc("/fees", p.handleFees)', "HandleFunc", "call", external("std"),
         "メソッド値 p.handleFees は参照で呼び出しではない。ハンドラは net/http から呼ばれる", entry=PP),
    case("EXT-011", SV, "mux.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, path, nil))", "ServeHTTP",
         "call", external("std"), "ポートを開かず httptest でハンドラを動かす", entry=PP),
]

# ── LNK: //go:linkname and assembly ─────────────────────────────────────────────────
CASES += [
    case("LNK-001", "kernel/metrics/metrics.go", "return bump(counter, n)", "bump", "call",
         resolved(m("bump", MK, "kernel/internal/tally/tally.go")),
         "本体のない宣言 bump は //go:linkname で internal/tally.bump を引き込む。ターゲットはリンク先",
         distractors=[m("bump", MK, "kernel/metrics/metrics.go")]),
    case("LNK-002", "kernel/metrics/metrics.go", "return nanotime() >= start", "nanotime", "call",
         external("std"), "//go:linkname で runtime.nanotime を引き込む。リンク先は標準ライブラリ",
         distractors=[m("nanotime", MK, "kernel/metrics/metrics.go")]),
    case("LNK-003", "kernel/fastmath/sum.go", "total = AddPoints(total, p)", "AddPoints", "call",
         resolved(m("AddPoints", MK, "kernel/fastmath/add_amd64.go")),
         "本体のない宣言でアセンブリ (add_amd64.s) が実装する。!amd64 のツインは add_generic.go",
         distractors=[m("AddPoints", MK, "kernel/fastmath/add_generic.go")]),
]

# ── MOD: cross-module calls (workspace, replace, published version) ─────────────────
TRD = "reception-desk/internal/textreport/textreport.go"
TAD = "admin-console/internal/textreport/textreport.go"
CASES += [
    case("MOD-001", TRD, "t := textfmt.NewTable()", "NewTable", "call", resolved(TF_NEWTABLE),
         "受付は go.work のモジュール。go.work が use するリポジトリ内フォーク third_party/textfmt "
         "(モジュールパス example.com/thirdparty/textfmt) が go.mod の require v1.1.0 に勝つ"),
    case("MOD-002", TRD, 'return textfmt.PadRight(label, 12) + strconv.Itoa(yen) + "円"', "PadRight", "call",
         resolved(TF_PADRIGHT), "フォークの PadRight (全角を幅 2 と数える)"),
    case("MOD-101", TAD, "t := textfmt.NewTable()", "NewTable", "call", external(MTF),
         "管理コンソールは go.work の外 (GOWORK=off の単一モジュール)。置き換えのない "
         "example.com/thirdparty/textfmt は公開版 v1.1.0。リポジトリ内に同じパスのフォークがあっても external",
         entry=AD, distractors=[TF_NEWTABLE]),
    case("MOD-102", TAD, 'return textfmt.PadRight(label, 12) + strconv.Itoa(yen) + "円"', "PadRight", "call",
         external(MTF), "公開版の PadRight (rune 数で数える)。出力の桁が受付と違う", entry=AD,
         distractors=[TF_PADRIGHT]),
    case("MOD-003", CO, 'if err := guard.Require(w, "writer"); err != nil {', "Require", "call",
         resolved(m("Require", MK, "kernel/guard/guard.go")),
         "管理コンソールの go.mod の replace example.com/iryo/kernel => ../kernel でリポジトリ内のソース", entry=AD),
    case("MOD-004", CO, "fmt.Fprintln(w, calc.Calculate(288, 1))", "Calculate", "call", resolved(FEE_CALC),
         "replace 経由の会計モジュール", entry=AD, distractors=[DOSE_CALC, TABLE_CALC]),
    case("MOD-005", CO, 'fmt.Fprintln(w, monthly.Render([]patient.Patient{{Name: "伊藤"}}))', "Render", "call",
         resolved(m("MonthlyReport.Render", MRP, "reporting/monthly.go")), "replace 経由の帳票モジュール",
         entry=AD, distractors=[TF_RENDER]),
    case("MOD-006", SV, 'fmt.Fprintf(w, "診察料 %d円", p.fees.Calculate(points, 1))', "Calculate", "call",
         resolved(FEE_CALC), "go.work の中の別モジュール (患者ポータル → 会計)。net/http から呼ばれるハンドラの中",
         entry=PP, distractors=[DOSE_CALC, TABLE_CALC]),
    case("MOD-007", MT, 'fmt.Fprintln(w, rezept.CodeLabel("a001"), rezept.KnownCodes())', "CodeLabel", "call",
         resolved(m("CodeLabel", MLC, LC + "codes.go")),
         "インポートパスの最後の要素 legacy-claims とパッケージ句 rezept が違う。別名なしで rezept として参照する"),
    case("MOD-008", "billing/fee.go", "return guard.EnsurePositive(points*count) * c.Rate / 10", "EnsurePositive",
         "call", resolved(m("EnsurePositive", MK, "kernel/guard/guard.go")),
         "go.mod に require がなくても go.work の use で別モジュールのパッケージに届く"),
]
XPR_PAIRS += [
    {"kind": "line", "cases": ["MOD-001", "MOD-101"]},
    {"kind": "line", "cases": ["MOD-002", "MOD-102"]},
    {"kind": "file", "files": [TRD, TAD]},
]

# ── UNR: negative controls ─────────────────────────────────────────────────────────
CASES += [
    case("UNR-001", CI, "return v.(interface{ Describe() string }).Describe()", "Describe", "call",
         ambiguous(ENTITY_DESCRIBE, PATIENT_DESCRIBE, runtime=[PATIENT_DESCRIBE]),
         "無名インタフェースのメソッドはノードを持たないので MethodCalls にしない。リポジトリ内の実装を候補にした "
         "ambiguous (master plan C5)。候補は Describe() string を持つ型 (Entity と Patient、昇格で満たす "
         "AuditedEntity、Inpatient、visit.Visit) のメソッド。同名のパッケージ関数 platform.Describe はメソッドではない",
         occurrence=2,
         distractors=[m("describeAny", MRD, CI), m("Describe", MK, "kernel/platform/platform.go")]),
    case("UNR-002", D, 'fmt.Fprintln(w, checker.Validate("H12345678"))', "Validate", "call",
         plain("dynamic", runtime=[m("looksInsured", MRD, D)]),
         "Validate はメソッドではなく関数型のフィールド。同名メソッドが二つあるが辺にしない",
         distractors=[BVAL, PVAL]),
    case("UNR-003", MT, "fmt.Fprintln(w, method.Call(nil)[0].String())", "Call", "call", external("std"),
         "reflect.Value.Call。実際に走る Patient.Describe は reflect の中から呼ばれ、この site には現れない",
         distractors=[PATIENT_DESCRIBE]),
    case("UNR-004", MT, 'method := reflect.ValueOf(pt).MethodByName("Describe")', "MethodByName", "call",
         external("std"), "文字列で名前を引く。名前の推測で Describe に結ばない",
         distractors=[PATIENT_DESCRIBE, ENTITY_DESCRIBE]),
]

# ── declaration pins (node coverage) ─────────────────────────────────────────────
K = "kernel/"
DECL_PINS += [
    c("Tracker", MIO, "interop/idkit/tracker.go"),
    m("Source.Get", MIO, "interop/lookup/lookup.go"),
    c("<package>", MK, K + "logging/logging.go"),
    c("Logger", MK, K + "logging/logging.go"),
    KLOG_NEW, KLOG_LOG, KLOG_LINES,
    c("Logger", MK, K + "audit/audit.go"),
    m("Logger.Printf", MK, K + "audit/audit.go"),
    m("ConsoleLogger.Printf", MK, K + "audit/audit.go"),
    m("Require", MK, K + "guard/guard.go"),
    m("EnsurePositive", MK, K + "guard/guard.go"),
    m("RequiredError.Error", MK, K + "guard/guard.go"),
    c("Notifier", MK, NT),
    NOTIFIER_SEND,
    c("TrackedNotifier", MK, NT),
    m("TrackedNotifier.Sent", MK, NT),
    MAIL_SEND,
    m("Broadcast", MK, NT),
    c("PaymentPolicy", MK, K + "policy/policy.go"),
    BASE_FEE, URGENT_FEE,
    m("Clock.Now", MK, CK + "clock.go"),
    m("OfficeClock.Now", MK, CK + "clock.go"),
    c("fakeClock", MK, CK + "clock_test.go"),
    m("fakeClock.Now", MK, CK + "clock_test.go"),
    m("TestStamp", MK, CK + "clock_test.go"),
    m("Normalize", MK, CK + "export_test.go"),
    m("Name", MK, PL + "platform_linux.go"),
    m("Name", MK, PL + "platform_windows.go"),
    m("windowsName", MK, PL + "platform_windows.go"),
    m("Name", MK, PL + "platform_other.go"),
    m("Debug", MK, PL + "platform_debug.go"),
    m("Accel", MK, PL + "accel_cgo.go"),
    m("Accel", MK, PL + "accel_nocgo.go"),
    m("AddPoints", MK, K + "fastmath/add_amd64.go"),
    m("AddPoints", MK, K + "fastmath/add_generic.go"),
    m("bump", MK, K + "metrics/metrics.go"),
    m("nanotime", MK, K + "metrics/metrics.go"),
    m("bump", MK, K + "internal/tally/tally.go"),
    m("<init>", MK, RG + "registry.go"),
    m("<init>.func1", MK, RG + "registry.go"),
    m("init#1", MK, RG + "registry.go"),
    m("init#2", MK, RG + "registry.go"),
    m("init#1", MK, RG + "defaults.go"),
    m("rule.Level", MK, "kernel/escalation/escalation.go"),
    m("Policy.Level", MK, "kernel/escalation/escalation_test.go"),
    c("Entity", MD, "domain/patient/entity.go"),
    c("Inpatient", MD, "domain/patient/entity.go"),
    ENTITY_DESCRIBE, PATIENT_DESCRIBE, ENTITY_TOUCH,
    m("AuditedEntity.Record", MD, "domain/patient/entity.go"),
    c("Describer", MD, "domain/patient/describe.go"),
    m("DescribeAll", MD, "domain/patient/describe.go"),
    c("Visit", MD, "domain/visit/visit.go"),
    c("Yen", MD, "domain/money/money.go"),
    c("Ward", MD, "domain/wards/north/ward.go"),
    c("Ward", MD, "domain/wards/south/ward.go"),
    c("Repository", MDA, "data/repository/repository.go"),
    m("Repository.Find", MDA, "data/repository/repository.go"),
    m("SqlPatientRepository.Find", MDA, "data/repository/repository.go"),
    c("MemoryStore", MDA, "data/store/store.go"),
    c("Store", MDA, "data/store/store.go"),
    c("FeeCalculator", MB, "billing/fee.go"),
    FEE_CALC,
    m("FeeCalculator.Scaled", MB, "billing/fee.go"),
    m("TestCalculate", MB, "billing/fee_test.go"),
    DOSE_CALC, PLOG_LOG,
    m("Stamper.Stamp", MPH, "pharmacy/dose.go"),
    SMS_SEND,
    SLOT_LABEL,
    m("LabelAll", MRP, "reporting/labels.go"),
    m("AppointmentBook.Each", MS, "scheduling/book.go"),
    m("AppointmentBook.All.func1", MS, "scheduling/book.go"),
    c("AuditTrail", MRP, "reporting/trail.go"),
    SINK_WRITE, MEMSINK_WRITE,
    TABLE_CALC,
    m("LookupCode", MLC, LC + "codes_gen.go"),
    c("<package>", MLC, LC + "codes_gen.go"),
    m("Record.Kind", MIO, "interop/records/records.go"),
    m("Archiver.ArchiveAll", MIO, "interop/exporters/exporters.go"),
    TF_PADRIGHT,
    m("main", MRD, "reception-desk/cmd/reception-desk/main.go"),
    m("Run", MRD, "reception-desk/internal/desk/desk.go"),
    CLOSING_RUN,
    m("Run.func3", MRD, D),
    m("makeAdder.func1", MRD, D),
    m("recovered.func1", MRD, D),
    m("main", MAD, "admin-console/cmd/admin-console/main.go"),
    BATCH_RUN,
    m("main", MPP, "patient-portal/cmd/patient-portal/main.go"),
    m("Portal.handleFees", MPP, SV),
]

# ── The exact ClassImplements and ClassEmbeds sets (spec R50) ──────────
# Derived by hand from Go's method sets and embedding, never from resolver
# output, then checked against a go/types listing of the fixture. Offline and
# --env give the same sets: no pair involves a type of a module that is
# missing offline (billing/tariff and interop/idkit declare no such pair).
# - ClassImplements: every package-level, non-generic, non-interface named
#   type (test files included) against every in-repo, package-level,
#   non-generic interface that has methods. pointer_receiver: only *T
#   implements. Not expected: the generic Repository[T] (SqlPatientRepository
#   implements only its instance Repository[patient.Patient], which has no
#   node), the constraint store.Number, interfaces that satisfy interfaces
#   (Journal and Sink), function-local interfaces (reports' labeler) and the
#   predeclared error (RequiredError.Error). Spec R52: idkit.Tracker embeds
#   uuidlite.Sequence (invalid offline) and has its own *Tracker Get() float64,
#   which hides any promoted Get and differs from Source.Get, so no
#   Tracker -> lookup.Source edge in either mode, and no ClassEmbeds row for
#   the external Sequence.
# - ClassEmbeds: embedded struct fields, and an interface's embedded
#   interfaces, whose type is declared in the repo. pointer: embedded as *T.
#   An instance points at its generic type (PatientList embeds
#   store.MemoryStore[patient.Patient]); wardLock's sync.Mutex is external.
REPO = "iryo-kanri-system"


def edge(table, source, target, **flags):
    """One expected class edge; endpoints are F3-style labels "<repo>:<relpath>:<qualname>"."""
    return {"table": table, "source": f"{REPO}:{source}", "target": f"{REPO}:{target}", **flags}


PE = "domain/patient/entity.go:"
PD = "domain/patient/describe.go:Describer"
EX = "interop/exporters/exporters.go:"
RC = "interop/records/records.go:"
NT = "kernel/notify/notify.go:"
PO = "kernel/policy/policy.go:"
TR = "reporting/trail.go:"
TF = "third_party/textfmt/textfmt.go:"

CLASS_EDGES = [
    edge("ClassEmbeds", PE + "AuditedEntity", PE + "Entity", pointer=False),
    edge("ClassEmbeds", PE + "Patient", PE + "AuditedEntity", pointer=False),
    edge("ClassEmbeds", PE + "Inpatient", PE + "Patient", pointer=False),
    edge("ClassEmbeds", "domain/visit/visit.go:Visit", PE + "Patient", pointer=True),
    edge("ClassEmbeds", "kernel/escalation/escalation.go:Policy", "kernel/escalation/escalation.go:rule",
         pointer=False),
    edge("ClassEmbeds", NT + "TrackedNotifier", NT + "Notifier", pointer=False),
    edge("ClassEmbeds", PO + "StandardPolicy", PO + "BasePolicy", pointer=False),
    edge("ClassEmbeds", PO + "UrgentPolicy", PO + "BasePolicy", pointer=False),
    edge("ClassEmbeds", RF + "reports/reports.go:PatientList", "data/store/store.go:MemoryStore", pointer=False),
    edge("ClassEmbeds", TR + "AuditTrail", TR + "Sink", pointer=False),
    # Describe: Entity's own, promoted through AuditedEntity, shadowed by
    # Patient's, promoted to Inpatient, and to Visit through *Patient
    edge("ClassImplements", PE + "Entity", PD, pointer_receiver=False),
    edge("ClassImplements", PE + "AuditedEntity", PD, pointer_receiver=False),
    edge("ClassImplements", PE + "Patient", PD, pointer_receiver=False),
    edge("ClassImplements", PE + "Inpatient", PD, pointer_receiver=False),
    edge("ClassImplements", "domain/visit/visit.go:Visit", PD, pointer_receiver=False),
    edge("ClassImplements", EX + "CsvExporter", EX + "Exporter", pointer_receiver=False),
    edge("ClassImplements", EX + "XmlExporter", EX + "Exporter", pointer_receiver=False),
    edge("ClassImplements", RC + "Claim", RC + "Record", pointer_receiver=False),
    edge("ClassImplements", "interop/lookup/lookup.go:Fixed", "interop/lookup/lookup.go:Source",
         pointer_receiver=False),
    edge("ClassImplements", RC + "Refund", RC + "Record", pointer_receiver=False),
    edge("ClassImplements", "kernel/audit/audit.go:ConsoleLogger", "kernel/audit/audit.go:Logger",
         pointer_receiver=True),
    edge("ClassImplements", "kernel/clock/clock.go:OfficeClock", "kernel/clock/clock.go:Clock",
         pointer_receiver=False),
    edge("ClassImplements", "kernel/clock/clock_test.go:fakeClock", "kernel/clock/clock.go:Clock",
         pointer_receiver=False),
    edge("ClassImplements", NT + "MailNotifier", NT + "Notifier", pointer_receiver=False),
    edge("ClassImplements", "scheduling/sms.go:SmsNotifier", NT + "Notifier", pointer_receiver=True),
    edge("ClassImplements", "scheduling/sms.go:SmsNotifier", NT + "TrackedNotifier", pointer_receiver=True),
    edge("ClassImplements", PO + "BasePolicy", PO + "PaymentPolicy", pointer_receiver=False),
    edge("ClassImplements", PO + "StandardPolicy", PO + "PaymentPolicy", pointer_receiver=False),
    edge("ClassImplements", PO + "UrgentPolicy", PO + "PaymentPolicy", pointer_receiver=False),
    edge("ClassImplements", "pharmacy/dose.go:RubberStamp", "pharmacy/dose.go:Stamper", pointer_receiver=False),
    # Write: MemorySink's pointer method; AuditTrail's comes from its embedded Sink
    edge("ClassImplements", TR + "MemorySink", TR + "Sink", pointer_receiver=True),
    edge("ClassImplements", TR + "MemorySink", TR + "Journal", pointer_receiver=True),
    edge("ClassImplements", TR + "AuditTrail", TR + "Sink", pointer_receiver=False),
    edge("ClassImplements", TR + "AuditTrail", TR + "Journal", pointer_receiver=False),
    edge("ClassImplements", TF + "Upper", TF + "Formatter", pointer_receiver=False),
    edge("ClassImplements", TF + "Bracket", TF + "Formatter", pointer_receiver=False),
]
