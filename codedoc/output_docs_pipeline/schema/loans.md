# loans

会員への書籍の貸出履歴および現在の貸出ステータスを管理するテーブルです。

## カラム

| カラム | 型 | 制約 | 説明 |
|------|----|------|------|
| id | integer | PK, NOT NULL | 貸出ID（自動採番） |
| book_id | integer | NOT NULL | 書籍ID（booksテーブルへの外部キー） |
| member_id | integer | NOT NULL | 会員ID（membersテーブルへの外部キー） |
| checked_out_on | date | NOT NULL | 貸出日 |
| due_on | date | NOT NULL | 返却期限日 |
| returned_on | date |  | 実際の返却日（未返却の場合はNULL） |
| created_at | datetime | NOT NULL | 作成日時 |
| updated_at | datetime | NOT NULL | 更新日時 |

## インデックス

- `book_id` — `index_loans_on_book_id`
- `member_id, returned_on` — `index_loans_on_member_id_and_returned_on`
- `member_id` — `index_loans_on_member_id`

## 外部キー

- `book_id` → books.id
- `member_id` → members.id

## リレーション

- books → loans (1-N): 1冊の書籍は、複数回にわたって貸出（履歴）される可能性があります。
- members → loans (1-N): 1人の会員は、複数回にわたって書籍を借りる（貸出履歴を持つ）ことができます。

---
[← データベーススキーマ](index.md)
