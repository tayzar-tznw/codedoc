package iryo.domain;

/**
 * カルテの長期保管庫。
 */
public final class ChartArchive {
    /**
     * アーカイブ圧縮を行う独立ジョブ。
     */
    public static class Compactor {
        public void start() {
            System.out.println("アーカイブ圧縮を開始");
        }
    }

    /**
     * 保管庫インスタンスに結び付いた清掃担当。
     */
    public class Janitor {
        public void clean() {
            System.out.println("保管索引を清掃");
        }
    }
}
