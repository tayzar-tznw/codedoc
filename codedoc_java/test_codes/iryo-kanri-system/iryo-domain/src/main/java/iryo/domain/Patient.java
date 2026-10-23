package iryo.domain;

/**
 * 患者台帳のエンティティ。
 */
public class Patient extends AuditedEntity {
    private String name;

    public Patient() {
        this(0);
    }

    public Patient(int id) {
        this(id, "患者" + id);
    }

    public Patient(int id, String name) {
        super(id);
        this.name = name;
    }

    @Override
    public String describe() {
        return "患者 " + id;
    }

    @Override
    public Patient clone() {
        return new Patient(id, name);
    }

    public String name() {
        return name;
    }

    public void rename(String name) {
        this.name = name;
    }
}
