package iryo.interop;

/**
 * CSV 形式の外部提出データを作成する。
 */
public final class CsvExporter implements Exporter {
    @Override
    public String export() {
        return "CSV出力";
    }
}
