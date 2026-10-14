# book_categories

書籍とカテゴリの多対多の関係を結ぶ中間テーブルです。

## カラム

| カラム | 型 | 制約 | 説明 |
|------|----|------|------|
| id | integer | PK, NOT NULL | 中間テーブルID（自動採番） |
| book_id | integer | NOT NULL | 書籍ID（booksテーブルへの外部キー） |
| category_id | integer | NOT NULL | カテゴリID（categoriesテーブルへの外部キー） |
| created_at | datetime | NOT NULL | 作成日時 |
| updated_at | datetime | NOT NULL | 更新日時 |

## インデックス

- `book_id, category_id` (UNIQUE) — `index_book_categories_on_book_id_and_category_id`
- `book_id` — `index_book_categories_on_book_id`
- `category_id` — `index_book_categories_on_category_id`

## 外部キー

- `book_id` → books.id
- `category_id` → categories.id

---
[← データベーススキーマ](index.md)
