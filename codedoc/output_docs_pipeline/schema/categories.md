# categories

書籍のジャンルやカテゴリを管理するテーブルです。

## カラム

| カラム | 型 | 制約 | 説明 |
|------|----|------|------|
| id | integer | PK, NOT NULL | カテゴリID（自動採番） |
| name | string | NOT NULL, UNIQUE | カテゴリ名 |
| description | text |  | カテゴリの説明 |
| created_at | datetime | NOT NULL | 作成日時 |
| updated_at | datetime | NOT NULL | 更新日時 |

## インデックス

- `name` (UNIQUE) — `index_categories_on_name`

## 外部キー

なし

## リレーション

- books → categories (N-N): 書籍とカテゴリは多対多の関係にあり、中間テーブル book_categories を介して結び付けられます。

---
[← データベーススキーマ](index.md)
