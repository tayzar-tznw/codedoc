package iryo.probe;

import iryo.billing.FeeCalculator;
import iryo.billing.PointMath;
import iryo.billing.Validator;
import iryo.data.ChartCache;
import iryo.data.Registry;
import iryo.data.Repository;
import iryo.data.RepositoryFactory;
import iryo.data.SqlPatientRepository;
import iryo.domain.Appointment;
import iryo.domain.AuditedEntity;
import iryo.domain.ChartArchive;
import iryo.domain.ChartEntry;
import iryo.domain.Entity;
import iryo.domain.Money;
import iryo.domain.Patient;
import iryo.interop.CsvExporter;
import iryo.interop.Exporter;
import iryo.interop.PatientIdKit;
import iryo.interop.XmlExporter;
import iryo.kernel.Guard;
import iryo.kernel.MailNotifier;
import iryo.kernel.Notifier;
import iryo.kernel.PaymentPolicy;
import iryo.kernel.ServiceCollection;
import iryo.kernel.ServiceProvider;
import iryo.kernel.StandardPolicy;
import iryo.kernel.UrgentPolicy;
import iryo.kernel.logging.Logger;
import iryo.probe.shadow.ShadowSide;
import iryo.probe.xpr.AdminSide;
import iryo.probe.xpr.ReceptionSide;
import iryo.reporting.VitalSequence;
import iryo.scheduling.AppointmentBook;
import iryo.scheduling.SmsNotifier;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.PrintStream;
import java.io.Serializable;
import java.lang.invoke.SerializedLambda;
import java.lang.reflect.Constructor;
import java.lang.reflect.Method;
import java.lang.reflect.Modifier;
import java.math.BigDecimal;
import java.nio.charset.StandardCharsets;
import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Enumeration;
import java.util.List;
import java.util.function.IntUnaryOperator;
import java.util.function.Predicate;
import java.util.function.Supplier;
import java.util.jar.JarEntry;
import java.util.jar.JarFile;

import static iryo.reporting.KarteKit.average;
import static iryo.reporting.KarteKit.heading;

/**
 * ground truth の意味論検証プローブ。製品コードを一切改変せず、
 *   (1) 式の静的型(ボックス化した実行時クラス)と計算値、
 *   (2) 直列化可能メソッド参照から取り出す {@link SerializedLambda} の束縛先記述子、
 *   (3) リフレクション(宣言クラス・戻り値型・既定メソッド判定・コンストラクタ・jar 走査)、
 *   (4) 標準出力の実測
 * だけで、cases.py / expected_resolutions.json が依拠する束縛主張を実行時に検証する。
 *
 * <p>すべて成立すれば「SEMANTIC PROBE PASSED」を出力する。1 件でも崩れれば、
 * 検証番号と主張を添えた {@link AssertionError} で停止する。</p>
 */
public final class Program {
    private static int checks;

    private Program() {
    }

    public static void main(String[] args) {
        overloadChecks();
        twinChecks();
        crossProjectChecks();
        containerChecks();
        inheritanceChecks();
        defaultMethodChecks();
        structureChecks();
        shadowingChecks();
        methodReferenceChecks();
        sequenceChecks();

        System.out.println("検証項目: " + checks + " 件");
        System.out.println("SEMANTIC PROBE PASSED");
    }

    // ------------------------------------------------------------------
    // 1. オーバーロード解決(計算値・静的型・束縛先記述子で betterness を固定)
    // ------------------------------------------------------------------
    private static void overloadChecks() {
        var fee = new FeeCalculator();

        Object basic = fee.calculate(288, 146);
        check(basic instanceof Integer && (Integer) basic == 434,
                "calculate(288, 146) は厳密一致の (int,int) → int 434");

        Object wide = fee.calculate(288, 146L);
        check(wide instanceof Long && (Long) wide == 434L,
                "calculate(288, 146L) は拡大変換で (long,long) → long 434");

        Object split = fee.calculate(70.5, 2);
        check(split instanceof Double && (Double) split == 72.5,
                "calculate(70.5, 2) は (double,double) → double 72.5");

        check(fee.calculate(288, 146, 52) == 486,
                "calculate(288, 146, 52) は 3 引数版で 486");
        check(boundSignature((IntTernary) fee::calculate)
                        .equals("iryo/billing/FeeCalculator#calculate(III)I"),
                "3 引数の束縛先は厳密一致 (int,int,int)(可変長引数版に勝つ)");

        check(fee.calculate(100, 100, 100, 100) == 360,
                "4 引数は可変長引数版へ展開され、3 件超で ×0.9 逓減 → 360");
        check(fee.calculate(new int[] {120, 80}) == 200,
                "配列直渡しは可変長引数版の通常形(逓減なし)→ 200");
        check(boundSignature((IntArray) fee::calculate)
                        .equals("iryo/billing/FeeCalculator#calculate([I)I"),
                "配列 1 引数の束縛先は calculate(int...)");
        check(boundSignature((IntBinary) fee::calculate)
                        .equals("iryo/billing/FeeCalculator#calculate(II)I"),
                "2 引数の束縛先は (int,int)(可変長引数版ではない)");

        check("同期実行".equals(fee.run(() -> { })),
                "void 互換ラムダ () -> { } は run(Runnable)");
        check("非同期実行".equals(fee.run(() -> "集計完了")),
                "値互換ラムダ () -> \"集計完了\" は run(Supplier<String>)");

        Object scaled = fee.scale(100, 2);
        check(scaled instanceof Integer && (Integer) scaled == 200,
                "scale(100, 2) は scale(int,int) → int 200");
        Object regional = fee.scale(100, 1.08);
        check(regional instanceof Double && Math.abs((Double) regional - 108.0) < 1e-9,
                "scale(100, 1.08) は scale(int,double) → double 108.0");

        check("番号請求: null".equals(fee.bill(null)),
                "bill(null) は最も具体的な bill(String)(bill(Object) ではない)");
        check("拡大変換受付: 42".equals(fee.route(42)),
                "route(42) は拡大変換 route(long)(ボックス化 route(Integer) に勝つ)");
        check("ボックス化受付: 42".equals(fee.route(Integer.valueOf(42))),
                "route(Integer.valueOf(42)) は route(Integer)");

        int calculateOverloads = 0;
        for (Method method : FeeCalculator.class.getMethods()) {
            if (method.getName().equals("calculate")) {
                calculateOverloads++;
            }
        }
        check(calculateOverloads == 5, "FeeCalculator.calculate は 5 種のオーバーロードを持つ");

        // リフレクション二重確認: 期待した引数型のメソッドを直接呼び、式の計算値と突き合わせる。
        check(invoke(fee, "calculate", types(long.class, long.class), 288L, 146L).equals(wide)
                        && !invoke(fee, "calculate", types(int.class, int.class), 288, 146).equals(wide),
                "リフレクション: calculate(long,long) 直接呼び出しが 288+146L と一致し (int,int) 版とは型が違う");
        check(invoke(fee, "calculate", types(int.class, int.class, int.class), 288, 146, 52).equals(486)
                        && invoke(fee, "calculate", types(int[].class), (Object) new int[] {288, 146, 52})
                        .equals(486),
                "リフレクション: 3 引数版と可変長引数版は別メソッドとして共存する");
        check(invoke(fee, "run", types(Runnable.class), (Runnable) () -> { }).equals("同期実行")
                        && invoke(fee, "run", types(Supplier.class), (Supplier<String>) () -> "集計完了")
                        .equals("非同期実行"),
                "リフレクション: run(Runnable) と run(Supplier) が別メソッドとして存在する");
        check(invoke(fee, "bill", types(String.class), (Object) null).equals("番号請求: null"),
                "リフレクション: bill(String) 直接呼び出しが bill(null) の値と一致");
    }

    // ------------------------------------------------------------------
    // 2. パッケージ間の同名ツインとコンパイル済み境界
    // ------------------------------------------------------------------
    private static void twinChecks() {
        var houseLog = new Logger("院内");
        houseLog.log("日次点検");
        var dispenseLog = new iryo.pharmacy.Logger();
        dispenseLog.log("日次点検");
        check("[記録] 日次点検".equals(houseLog.lines().get(0)),
                "kernel Logger.log(String) は [記録] 書式");
        check("[調剤] 日次点検".equals(dispenseLog.lines().get(0))
                        && iryo.pharmacy.Logger.class.getDeclaredMethods().length == 2,
                "pharmacy Logger.log(String) は [調剤] 書式(log と lines だけの別系統)");

        houseLog.log("要確認データ", 2);
        check("[重要度2] 要確認データ".equals(houseLog.lines().get(houseLog.lines().size() - 1)),
                "kernel Logger.log(String,int) は第 2 引数指定時のみ選ばれる");
        houseLog.log(new IllegalStateException("集計対象外データ"));
        check("[例外] 集計対象外データ".equals(houseLog.lines().get(houseLog.lines().size() - 1)),
                "kernel Logger.log(Exception) は例外引数で選ばれる");

        check(new Validator().validate("H12345678")
                        && !new iryo.pharmacy.Validator().validate("H12345678"),
                "Validator ツイン: 保険証番号は会計 true / 薬局 false");
        check(new iryo.pharmacy.Validator().validate("Rp) アモキシシリン 250mg")
                        && !new Validator().validate("Rp) アモキシシリン 250mg"),
                "Validator ツイン: 処方箋様式は薬局 true / 会計 false");

        check(PointMath.toYen(15) == 150 && iryo.pharmacy.PointMath.toYen(15) == 162,
                "PointMath ツイン: 会計 ×10 = 150 / 薬局窓口 ceil(×10.8) = 162");
        check(PointMath.roundTo10(1264) == 1260,
                "roundTo10 は会計側だけが持つ(1264 → 1260)");

        check(new FeeCalculator().calculate(30, 10) == 40
                        && new iryo.pharmacy.DoseCalculator().calculate(30, 10) == 300,
                "calculate(30,10) 同名同引数: 会計は加算 40 / 薬局は乗算 300");
        legacyPointTableCheck();

        check(new iryo.billing.text.ReceiptFormatter().format(1).startsWith("[会計]")
                        && new iryo.legacy.claims.text.ReceiptFormatter().format(1).startsWith("[旧レセ]")
                        && new iryo.interop.text.ReceiptFormatter().format(1).startsWith("[連携]"),
                "ReceiptFormatter 三兄弟: [会計] / [旧レセ] / [連携]");

        check(Money.class.isRecord() && !iryo.pharmacy.Money.class.isRecord(),
                "Money ツイン: domain は record / pharmacy は通常クラス");
        check(Money.class.getRecordComponents().length == 1
                        && Money.class.getRecordComponents()[0].getName().equals("amount")
                        && new Money(new BigDecimal("1200")).amount().compareTo(new BigDecimal("1200")) == 0,
                "domain Money は成分 amount の正準アクセサを持つ");
        check("2000円".equals(new Money(new BigDecimal("1200")).plus(new Money(new BigDecimal("800"))).toString())
                        && throwsNullPointer(() -> new Money(null)),
                "record Money の plus と compact コンストラクタの null 検査");
        check("52000".equals(new iryo.pharmacy.Money(new BigDecimal("52000")).amount.toPlainString()),
                "pharmacy Money は record ではなく公開フィールド amount を持つ");

        Class<?> northWard = iryo.domain.north.Ward.class;
        Class<?> southWard = iryo.domain.south.Ward.class;
        check("北病棟".equals(new iryo.domain.north.Ward().locate())
                        && "南病棟".equals(new iryo.domain.south.Ward().locate())
                        && northWard != southWard
                        && northWard.getSimpleName().equals(southWard.getSimpleName()),
                "同名 Ward は北棟/南棟で別クラス");
    }

    @SuppressWarnings("deprecation") // 検証プローブは意図的に旧点数表へ触れる
    private static void legacyPointTableCheck() {
        check(new iryo.legacy.claims.PointTable().calculate(30, 10) == 1040
                        && new iryo.legacy.claims.OldTable().calculate(30, 10) == 1040
                        && iryo.legacy.claims.OldTable.class.getSuperclass()
                                == iryo.legacy.claims.PointTable.class,
                "旧レセコン PointTable.calculate は経過措置 +1000 → 1040(OldTable が継承)");
    }

    // ------------------------------------------------------------------
    // 3. XPR: 同一手続き行がプロジェクトの import で分岐する
    // ------------------------------------------------------------------
    private static void crossProjectChecks() {
        String[] reception = ReceptionSide.run();
        String[] admin = AdminSide.run();

        check(reception[0].equals("[記録] 日次点検")
                        && reception[1].equals("true")
                        && reception[2].equals("150円"),
                "XPR 受付側(billing/kernel import): [記録] / true / 150円");
        check(admin[0].equals("[調剤] 日次点検")
                        && admin[1].equals("false")
                        && admin[2].equals("162円"),
                "XPR 管理側(pharmacy import): [調剤] / false / 162円");
        check(!reception[0].equals(admin[0])
                        && !reception[1].equals(admin[1])
                        && !reception[2].equals(admin[2]),
                "XPR: 同一手続き行の観測値 3 件すべてがプロジェクト間で分岐する");
    }

    // ------------------------------------------------------------------
    // 4. DI マイクロコンテナ(最後勝ち・登録順・コンストラクタ注入・ファクトリ)
    // ------------------------------------------------------------------
    private static void containerChecks() {
        var services = new ServiceCollection();
        services.addSingleton(Notifier.class, MailNotifier.class);
        services.addSingleton(Notifier.class, SmsNotifier.class);
        services.addSingleton(Repository.class, SqlPatientRepository.class);
        services.addSingleton(Logger.class, () -> new Logger("検証"));
        ServiceProvider provider = services.buildProvider();

        Notifier single = provider.get(Notifier.class);
        check(single instanceof SmsNotifier,
                "get(Notifier) は最後の登録 SmsNotifier(MailNotifier を上書き)");
        check("SMS送信: 受付を開始しました"
                        .equals(captureOut(() -> provider.get(Notifier.class).send("受付を開始しました"))),
                "解決されたインスタンスは scheduling 側の実装本体へ届く");

        List<Notifier> all = provider.getAll(Notifier.class);
        check(all.size() == 2
                        && all.get(0) instanceof MailNotifier
                        && all.get(1) instanceof SmsNotifier,
                "getAll(Notifier) は登録順 [MailNotifier, SmsNotifier]");

        Repository<?> repository = provider.get(Repository.class);
        check(repository instanceof SqlPatientRepository,
                "get(Repository) はインタフェース登録から実装 SqlPatientRepository へ");
        Object found = repository.find(3);
        check(found instanceof Patient && ((Patient) found).id() == 3,
                "SqlPatientRepository.find(3) は Patient(3) を返す");
        check(provider.get(Logger.class).lines().contains("[記録] 患者検索 3"),
                "コンストラクタ注入: 実装は登録済み Logger シングルトンを受け取っている");

        Constructor<?>[] constructors = SqlPatientRepository.class.getConstructors();
        check(constructors.length == 1 && constructors[0].getParameterTypes()[0] == Logger.class,
                "リフレクション: SqlPatientRepository の public コンストラクタは Logger 1 引数");

        check("検証".equals(provider.get(Logger.class).owner())
                        && provider.get(Logger.class) == provider.get(Logger.class),
                "ファクトリラムダ登録はサプライヤの生成物を単一インスタンスで返す");
        check(throwsIllegalState(() -> provider.get(Exporter.class)),
                "未登録サービスは推測されず IllegalStateException になる");
    }

    // ------------------------------------------------------------------
    // 5. 継承(スキップレベル・共変戻り値・静的隠蔽・祖父母メンバ)
    // ------------------------------------------------------------------
    private static void inheritanceChecks() {
        var patient = new Patient(5);

        check(method(Patient.class, "describe").getDeclaringClass() == Patient.class
                        && method(AuditedEntity.class, "describe").getDeclaringClass() == Entity.class
                        && !declares(AuditedEntity.class, "describe"),
                "スキップレベル: describe() は Patient と Entity が宣言し AuditedEntity は宣言しない");
        AuditedEntity ledgerView = patient;
        check("患者 5".equals(ledgerView.describe()) && "エンティティ 5".equals(new Entity(5).describe()),
                "中間型レシーバでも仮想ディスパッチは Patient.describe へ");

        check(method(Patient.class, "clone").getReturnType() == Patient.class
                        && method(Entity.class, "clone").getReturnType() == Entity.class,
                "共変戻り値: Patient.clone は Patient、Entity.clone は Entity を返す");
        check(((Entity) patient).clone() instanceof Patient,
                "基底型経由の clone でも実行時は共変オーバーライドが働く");

        check(Entity.create().getClass() == Entity.class
                        && AuditedEntity.create().getClass() == AuditedEntity.class,
                "静的メソッド隠蔽: Entity.create と AuditedEntity.create は別の実体を返す");
        check(method(Patient.class, "create").getDeclaringClass() == AuditedEntity.class
                        && declaredMethod(AuditedEntity.class, "create").getReturnType() == AuditedEntity.class,
                "Patient から見た create() の静的束縛先は AuditedEntity(Entity.create を隠蔽)");

        check(method(Patient.class, "touch").getDeclaringClass() == Entity.class && !patient.touched(),
                "祖父母 Entity.touch は Patient から到達でき、初期状態は未接触");
        patient.touch();
        patient.audit("受付済");
        check(patient.touched() && "受付済;".equals(patient.auditTrail()),
                "祖父母 touch と中間 audit の状態が Patient インスタンスに反映される");

        check(iryo.kernel.Logger.class.getSuperclass() == Logger.class
                        && !declares(iryo.kernel.Logger.class, "log")
                        && method(iryo.kernel.Logger.class, "log", String.class).getDeclaringClass()
                                == Logger.class,
                "kernel の互換窓口 Logger は logging.Logger を継承し log を宣言しない");
        check("既定".equals(new Logger().owner())
                        && "既定".equals(new iryo.kernel.Logger().owner())
                        && "薬局窓口".equals(new Logger("薬局窓口").owner()),
                "コンストラクタ連鎖 this(\"既定\") が既定所有者を与える");
        check(new Patient().id() == 0 && "患者7".equals(new Patient(7).name()),
                "Patient のコンストラクタ連鎖 this(id) → this(id, \"患者\"+id)");
    }

    // ------------------------------------------------------------------
    // 6. 既定インタフェース実装(DIM)
    // ------------------------------------------------------------------
    private static void defaultMethodChecks() {
        PaymentPolicy standard = new StandardPolicy();
        PaymentPolicy urgent = new UrgentPolicy();

        Method standardFee = method(StandardPolicy.class, "fee");
        check(standard.fee() == 0.05
                        && standardFee.getDeclaringClass() == PaymentPolicy.class
                        && standardFee.isDefault(),
                "StandardPolicy.fee() はインタフェース既定実装(5%)");
        Method urgentFee = method(UrgentPolicy.class, "fee");
        check(urgent.fee() == 0.25
                        && urgentFee.getDeclaringClass() == UrgentPolicy.class
                        && !urgentFee.isDefault(),
                "UrgentPolicy.fee() はクラス側の上書き実装(25%)");
        check("既定方針".equals(PaymentPolicy.policyName())
                        && Modifier.isStatic(declaredMethod(PaymentPolicy.class, "policyName").getModifiers())
                        && !hasPublicMethod(StandardPolicy.class, "policyName"),
                "インタフェース static メソッドは修飾呼び出し専用で実装クラスへ継承されない");

        ReviewPolicy review = new ReviewPolicy() { };
        check(new CombinedPolicy().fee() == 0.05 && review.fee() == 0.10,
                "ダイヤモンド継承: PaymentPolicy.super.fee() が選択され ReviewPolicy 既定 0.10 は使われない");
    }

    // ------------------------------------------------------------------
    // 7. 構造(入れ子型・内部クラス・実装数・ジェネリックアリティ)
    // ------------------------------------------------------------------
    private static void structureChecks() {
        Class<?> compactor = forName("iryo.domain.ChartArchive$Compactor");
        check(Modifier.isStatic(compactor.getModifiers())
                        && compactor.getEnclosingClass() == ChartArchive.class,
                "Class.forName(\"iryo.domain.ChartArchive$Compactor\") は static 入れ子型を返す");
        check("アーカイブ圧縮を開始".equals(captureOut(() -> new ChartArchive.Compactor().start())),
                "入れ子型 Compactor.start() の出力");

        var archive = new ChartArchive();
        var janitor = archive.new Janitor();
        check("保管索引を清掃".equals(captureOut(janitor::clean))
                        && !Modifier.isStatic(ChartArchive.Janitor.class.getModifiers())
                        && ChartArchive.Janitor.class.getDeclaredConstructors()[0].getParameterTypes()[0]
                                == ChartArchive.class,
                "内部クラス Janitor は outer.new で生成され、外側インスタンスを保持する");

        List<Class<?>> corpus = corpusClasses();
        check(corpus.size() >= 45 && jarCount() == 9,
                "コーパス 9 アーティファクトの全クラスを走査できる");
        check(implementationsOf(Repository.class, corpus)
                        .equals(List.of("iryo.data.SqlPatientRepository")),
                "Repository の実装はコーパス全体でちょうど 1 件(iface→唯一実装の昇格対象)");
        check(implementationsOf(Exporter.class, corpus)
                        .equals(List.of("iryo.interop.CsvExporter", "iryo.interop.XmlExporter"))
                        && "CSV出力".equals(new CsvExporter().export())
                        && "XML出力".equals(new XmlExporter().export()),
                "Exporter の実装は 2 件(昇格を拒否すべき対)");
        check(implementationsOf(Notifier.class, corpus)
                        .equals(List.of("iryo.kernel.MailNotifier", "iryo.scheduling.SmsNotifier")),
                "Notifier の実装 2 件はアーティファクトを跨ぐ");

        check(ChartCache.class.getTypeParameters().length == 2
                        && iryo.legacy.claims.ChartCache.class.getTypeParameters().length == 1,
                "ChartCache ツイン: data は 2 型引数 / legacy は 1 型引数");
        var karte = new ChartCache<String, ChartEntry>();
        karte.put("外来", new ChartEntry(7, "経過良好"));
        var oldCache = new iryo.legacy.claims.ChartCache<Integer>();
        oldCache.put(32);
        check(karte.get("外来").patientId() == 7
                        && "経過良好".equals(karte.get("外来").text())
                        && oldCache.get() == 32,
                "構築型の get/put が型引数どおりに解決する");
        check("新規記載".equals(Registry.<ChartEntry>create(ChartEntry.class).text()),
                "型ウィットネス Registry.<ChartEntry>create の生成物");

        var defaultRepo = RepositoryFactory.createDefault();
        check("患者 2".equals(defaultRepo.find(2).describe()) && defaultRepo.ping(),
                "静的ファクトリ RepositoryFactory.createDefault は実装型を返す");
        check("P-042".equals(PatientIdKit.normalize(" p 042 "))
                        && "薬局部門".equals(Guard.notEmpty("薬局部門", "部門名"))
                        && "5".equals(Guard.notNull("5", "値")),
                "interop / kernel の静的ユーティリティ");

        var book = new AppointmentBook();
        var slot = LocalDateTime.of(2026, 4, 1, 9, 30);
        book.reserve(new Appointment(3, 7, slot));
        check(book.countFor(7) == 1 && book.countFor(9) == 0
                        && "予約3 (患者7 / 2026-04-01 09:30)".equals(new Appointment(3, 7, slot).summary()),
                "scheduling の予約台帳と domain Appointment の書式");
    }

    // ------------------------------------------------------------------
    // 8. 静的インポートと遮蔽
    // ------------------------------------------------------------------
    private static void shadowingChecks() {
        check(ShadowSide.shadowed(15) == 165 && PointMath.toYen(15) == 150,
                "クラス自身の宣言が静的インポート toYen を遮蔽する(×11 = 165)");
        check(ShadowSide.imported(15) == 150,
                "同じファイルでも完全修飾なら会計側 PointMath.toYen(×10 = 150)");
        check("【日報】".equals(heading("日報")) && average(118, 132, 145, 121) == 129,
                "遮蔽されない静的インポート heading / average は KarteKit へ束縛");
    }

    // ------------------------------------------------------------------
    // 9. メソッド参照
    // ------------------------------------------------------------------
    private static void methodReferenceChecks() {
        Supplier<Logger> loggerFactory = Logger::new;
        check("既定".equals(loggerFactory.get().owner())
                        && boundSignature((SerializableSupplier<Logger>) Logger::new)
                                .equals("iryo/kernel/logging/Logger#<init>()V"),
                "コンストラクタ参照 Logger::new は引数なし <init> へ束縛される");

        var logger = new Logger("統計");
        logger.log("要確認データ", 2);
        Runnable flushJob = logger::flush;
        flushJob.run();
        check(logger.lines().size() == 1
                        && boundSignature((SerializableRunnable) logger::flush)
                                .equals("iryo/kernel/logging/Logger#flush()V"),
                "束縛インスタンス参照 logger::flush は Logger.flush を呼ぶ(記録は増えない)");

        IntUnaryOperator houseRate = PointMath::toYen;
        IntUnaryOperator pharmacyRate = iryo.pharmacy.PointMath::toYen;
        check(houseRate.applyAsInt(15) == 150 && pharmacyRate.applyAsInt(15) == 162,
                "静的メソッド参照 PointMath::toYen は所属パッケージごとに別実装へ束縛");
    }

    // ------------------------------------------------------------------
    // 10. 独自 fluent 列 と java.util.stream
    // ------------------------------------------------------------------
    private static void sequenceChecks() {
        var vitals = new VitalSequence(118, 132, 145, 121);
        check(vitals.total() == 516,
                "可変長コンストラクタ VitalSequence(int...) の合計は 516");
        check(vitals.filter(value -> value > 130).map(value -> value - 130).total() == 17
                        && method(VitalSequence.class, "filter", Predicate.class).getReturnType()
                                == VitalSequence.class,
                "独自 fluent 列 filter/map/total は VitalSequence を返しつつ 17 を得る");
        check(List.of(118, 132, 145, 121).stream()
                        .filter(value -> value > 130)
                        .mapToInt(value -> value - 130)
                        .sum() == 17,
                "java.util.stream の等価計算も 17(束縛先は java.base 側)");
        check(List.of(180, 90, 200).stream()
                        .filter(value -> value > 150)
                        .mapToInt(Integer::intValue)
                        .sum() == 380
                        && List.of(70, 88, 64).stream()
                        .filter(value -> value > 65)
                        .mapToInt(value -> value - 60)
                        .sum() == 38,
                "受付 ReportFlow の 380 と管理 ReportDigest の 38");
    }

    // ------------------------------------------------------------------
    // ダイヤモンド継承の再現(AccountingFlow の入れ子宣言と同形)
    // ------------------------------------------------------------------
    private interface ReviewPolicy {
        default double fee() {
            return 0.10;
        }
    }

    private static final class CombinedPolicy implements PaymentPolicy, ReviewPolicy {
        @Override
        public double fee() {
            return PaymentPolicy.super.fee();
        }
    }

    // ------------------------------------------------------------------
    // 補助
    // ------------------------------------------------------------------
    private static void check(boolean condition, String claim) {
        checks++;
        if (!condition) {
            throw new AssertionError("検証失敗 #" + checks + ": " + claim);
        }
    }

    private interface IntBinary extends Serializable {
        int apply(int a, int b);
    }

    private interface IntTernary extends Serializable {
        int apply(int a, int b, int c);
    }

    private interface IntArray extends Serializable {
        int apply(int[] points);
    }

    private interface SerializableSupplier<T> extends Supplier<T>, Serializable {
    }

    private interface SerializableRunnable extends Runnable, Serializable {
    }

    /**
     * 直列化可能な関数型インタフェースへ代入したメソッド参照から、コンパイラが選んだ
     * 実体メソッド(クラス・名前・引数記述子)を取り出す。C# の {@code Delegate.Method}
     * に相当する「どのオーバーロードに束縛されたか」の直接証拠になる。
     */
    private static String boundSignature(Serializable methodReference) {
        try {
            Method writeReplace = methodReference.getClass().getDeclaredMethod("writeReplace");
            writeReplace.setAccessible(true);
            var lambda = (SerializedLambda) writeReplace.invoke(methodReference);
            return lambda.getImplClass() + "#" + lambda.getImplMethodName()
                    + lambda.getImplMethodSignature();
        } catch (ReflectiveOperationException error) {
            throw new IllegalStateException("メソッド参照の束縛先を取得できません", error);
        }
    }

    private static Class<?>[] types(Class<?>... parameterTypes) {
        return parameterTypes;
    }

    private static Object invoke(Object target, String name, Class<?>[] parameterTypes, Object... arguments) {
        try {
            Method resolved = target.getClass().getDeclaredMethod(name, parameterTypes);
            return resolved.invoke(target, arguments);
        } catch (ReflectiveOperationException error) {
            throw new IllegalStateException("リフレクション呼び出しに失敗: " + name, error);
        }
    }

    private static Method method(Class<?> owner, String name, Class<?>... parameterTypes) {
        try {
            return owner.getMethod(name, parameterTypes);
        } catch (NoSuchMethodException error) {
            throw new IllegalStateException("公開メソッドがありません: " + owner.getName() + "#" + name, error);
        }
    }

    private static Method declaredMethod(Class<?> owner, String name, Class<?>... parameterTypes) {
        try {
            return owner.getDeclaredMethod(name, parameterTypes);
        } catch (NoSuchMethodException error) {
            throw new IllegalStateException("宣言メソッドがありません: " + owner.getName() + "#" + name, error);
        }
    }

    private static boolean hasPublicMethod(Class<?> owner, String name, Class<?>... parameterTypes) {
        try {
            owner.getMethod(name, parameterTypes);
            return true;
        } catch (NoSuchMethodException absent) {
            return false;
        }
    }

    private static boolean declares(Class<?> owner, String name) {
        for (Method declared : owner.getDeclaredMethods()) {
            if (declared.getName().equals(name)) {
                return true;
            }
        }
        return false;
    }

    private static Class<?> forName(String binaryName) {
        try {
            return Class.forName(binaryName);
        } catch (ClassNotFoundException error) {
            throw new IllegalStateException("型を読み込めません: " + binaryName, error);
        }
    }

    /** クラスパス上の jp.iryo アーティファクト jar に含まれる全 iryo.* クラス。 */
    private static List<Class<?>> corpusClasses() {
        List<Class<?>> found = new ArrayList<>();
        for (String entry : classpathJars()) {
            try (var jar = new JarFile(entry)) {
                for (Enumeration<JarEntry> entries = jar.entries(); entries.hasMoreElements();) {
                    String name = entries.nextElement().getName();
                    if (!name.endsWith(".class")) {
                        continue;
                    }
                    String binaryName = name.substring(0, name.length() - ".class".length())
                            .replace('/', '.');
                    if (binaryName.startsWith("iryo.")) {
                        found.add(Class.forName(binaryName, false, Program.class.getClassLoader()));
                    }
                }
            } catch (IOException | ClassNotFoundException error) {
                throw new IllegalStateException("コーパスクラスを走査できません: " + entry, error);
            }
        }
        return found;
    }

    private static List<String> classpathJars() {
        List<String> jars = new ArrayList<>();
        for (String entry : System.getProperty("java.class.path").split(java.io.File.pathSeparator)) {
            if (entry.endsWith(".jar")) {
                jars.add(entry);
            }
        }
        return jars;
    }

    private static int jarCount() {
        return classpathJars().size();
    }

    private static List<String> implementationsOf(Class<?> service, List<Class<?>> corpus) {
        List<String> names = new ArrayList<>();
        for (Class<?> candidate : corpus) {
            if (candidate.isInterface() || Modifier.isAbstract(candidate.getModifiers())) {
                continue;
            }
            if (service.isAssignableFrom(candidate)) {
                names.add(candidate.getName());
            }
        }
        Collections.sort(names);
        return names;
    }

    private static String captureOut(Runnable action) {
        PrintStream original = System.out;
        var buffer = new ByteArrayOutputStream();
        try (var capture = new PrintStream(buffer, true, StandardCharsets.UTF_8)) {
            System.setOut(capture);
            action.run();
        } finally {
            System.setOut(original);
        }
        return buffer.toString(StandardCharsets.UTF_8).strip();
    }

    private static boolean throwsNullPointer(Runnable action) {
        try {
            action.run();
            return false;
        } catch (NullPointerException expected) {
            return true;
        }
    }

    private static boolean throwsIllegalState(Runnable action) {
        try {
            action.run();
            return false;
        } catch (IllegalStateException expected) {
            return true;
        }
    }
}
