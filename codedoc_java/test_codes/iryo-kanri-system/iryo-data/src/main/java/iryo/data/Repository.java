package iryo.data;

/**
 * 識別番号で台帳エンティティを取得するリポジトリ。
 *
 * @param <T> 取得するエンティティ型
 */
public interface Repository<T> {
    T find(int id);
}
