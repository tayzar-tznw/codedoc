# books

管理対象の書籍情報を保持するテーブルです。単一テーブル継承（STI）用のカラムも含みます。

## カラム

| カラム | 型 | 制約 | 説明 |
|------|----|------|------|
| id | integer | PK, NOT NULL | 書籍ID（自動採番） |
| title | string | NOT NULL | 書籍のタイトル |
| isbn | string | UNIQUE | ISBNコード |
| type | string |  | 単一テーブル継承（STI）用のクラス名 |
| author_id | integer | NOT NULL | 著者ID（authorsテーブルへの外部キー） |
| published_year | integer |  | 出版年 |
| price_cents | integer | NOT NULL, DEFAULT: 0 | 価格（セント単位） |
| currency | string | NOT NULL, DEFAULT: 'USD' | 通貨単位 |
| copies_total | integer | NOT NULL, DEFAULT: 1 | 総蔵書数 |
| copies_available | integer | NOT NULL, DEFAULT: 1 | 貸出可能数 |
| synopsis | text |  | あらすじ・概要 |
| created_at | datetime | NOT NULL | 作成日時 |
| updated_at | datetime | NOT NULL | 更新日時 |

## インデックス

- `author_id` — `index_books_on_author_id`
- `isbn` (UNIQUE) — `index_books_on_isbn`
- `type` — `index_books_on_type`

## 外部キー

- `author_id` → authors.id

## リレーション

- authors → books (1-N): 1人の著者は複数の書籍を執筆することができ、各書籍は必ず1人の著者に紐付きます。
- books → categories (N-N): 書籍とカテゴリは多対多の関係にあり、中間テーブル book_categories を介して結び付けられます。
- books → loans (1-N): 1冊の書籍は、複数回にわたって貸出（履歴）される可能性があります。

---
[← データベーススキーマ](index.md)
