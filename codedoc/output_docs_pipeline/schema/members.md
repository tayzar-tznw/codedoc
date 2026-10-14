# members

図書サービスの利用会員情報を管理するテーブルです。

## カラム

| カラム | 型 | 制約 | 説明 |
|------|----|------|------|
| id | integer | PK, NOT NULL | 会員ID（自動採番） |
| name | string | NOT NULL | 会員氏名 |
| email | string | UNIQUE | メールアドレス |
| joined_on | date |  | 入会日 |
| active | boolean | NOT NULL, DEFAULT: true | アクティブ状態フラグ |
| created_at | datetime | NOT NULL | 作成日時 |
| updated_at | datetime | NOT NULL | 更新日時 |

## インデックス

- `email` (UNIQUE) — `index_members_on_email`

## 外部キー

なし

## リレーション

- members → loans (1-N): 1人の会員は、複数回にわたって書籍を借りる（貸出履歴を持つ）ことができます。

---
[← データベーススキーマ](index.md)
