package iryo.domain;

/**
 * 監査証跡を持つ中間エンティティ。
 *
 * <p>{@link #describe()} は上書きせず、基底実装を継承する。</p>
 */
public class AuditedEntity extends Entity {
    private String auditTrail = "";

    public AuditedEntity() {
        this(0);
    }

    public AuditedEntity(int id) {
        super(id);
    }

    public static AuditedEntity create() {
        return new AuditedEntity();
    }

    public void audit(String note) {
        auditTrail += note + ";";
    }

    public String auditTrail() {
        return auditTrail;
    }
}
