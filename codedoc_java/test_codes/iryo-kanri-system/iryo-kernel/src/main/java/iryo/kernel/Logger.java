package iryo.kernel;

/**
 * 旧来の単一パッケージ構成を利用する端末向けのロガー互換窓口。
 * 新しい業務コードは {@link iryo.kernel.logging.Logger} を直接利用する。
 */
public final class Logger extends iryo.kernel.logging.Logger {
    public Logger() {
        super();
    }

    public Logger(String owner) {
        super(owner);
    }
}
