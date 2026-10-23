package iryo.data;

import iryo.domain.Patient;
import iryo.kernel.logging.Logger;

/**
 * 患者台帳の SQL 実装。共通ロガーはコンストラクタで受け取る。
 */
public final class SqlPatientRepository implements Repository<Patient> {
    private final Logger logger;

    public SqlPatientRepository(Logger logger) {
        this.logger = logger;
    }

    @Override
    public Patient find(int id) {
        logger.log("患者検索 " + id);
        return new Patient(id);
    }

    public boolean ping() {
        return true;
    }
}
