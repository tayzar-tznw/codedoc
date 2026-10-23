package iryo.domain;

/**
 * 台帳エンティティの基底クラス。
 */
public class Entity {
    protected int id;
    private boolean touched;

    public Entity() {
        this(0);
    }

    public Entity(int id) {
        this.id = id;
    }

    public String describe() {
        return "エンティティ " + id;
    }

    public static Entity create() {
        return new Entity();
    }

    public void touch() {
        touched = true;
    }

    public boolean touched() {
        return touched;
    }

    public int id() {
        return id;
    }

    @Override
    public Entity clone() {
        return new Entity(id);
    }
}
