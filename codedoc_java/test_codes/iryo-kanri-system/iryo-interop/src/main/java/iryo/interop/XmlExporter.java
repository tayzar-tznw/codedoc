package iryo.interop;

/**
 * XML 形式の外部提出データを作成する。
 */
public final class XmlExporter implements Exporter {
    @Override
    public String export() {
        return "XML出力";
    }
}
