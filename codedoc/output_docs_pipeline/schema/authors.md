# authors

書籍の著者の情報を管理するテーブルです。

## カラム

| カラム | 型 | 制約 | 説明 |
|------|----|------|------|
| id | integer | PK, NOT NULL | 著者ID（自動採番） |
| name | string | NOT NULL | 著者名 |
| biography | text |  | 著者の経歴・プロフィール |
| birth_year | integer |  | 誕生年 |
| nationality | string |  | 国籍 |
| created_at | datetime | NOT NULL | 作成日時 |
| updated_at | datetime | NOT NULL | 更新日時 |

## インデックス

- `name` — `index_authors_on_name`

## 外部キー

なし

## リレーション

- authors → books (1-N): 1人の著者は複数の書籍を執筆することができ、各書籍は必ず1人の著者に紐付きます。

---
[← データベーススキーマ](index.md)
