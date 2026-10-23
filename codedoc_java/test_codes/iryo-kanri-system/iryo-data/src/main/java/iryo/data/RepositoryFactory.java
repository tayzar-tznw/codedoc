package iryo.data;

import iryo.kernel.logging.Logger;

/**
 * バッチ処理向けの既定リポジトリを生成する。
 */
public final class RepositoryFactory {
    private RepositoryFactory() {
    }

    public static SqlPatientRepository createDefault() {
        return new SqlPatientRepository(new Logger("工場"));
    }
}
