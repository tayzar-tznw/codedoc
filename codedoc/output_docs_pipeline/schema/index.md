# データベーススキーマ

図書管理システムのためのデータベースです。書籍、著者、カテゴリ、会員、および貸出履歴の情報を管理し、効率的な図書の貸出・返却業務をサポートします。

## ER図

```mermaid
erDiagram
    authors ||--o{ books : "books.author_id"
    books }o--o{ categories : "book_categories"
    books ||--o{ loans : "loans.book_id"
    members ||--o{ loans : "loans.member_id"
    authors {
        integer id PK
        string name
        text biography
        integer birth_year
        string nationality
        datetime created_at
        datetime updated_at
    }
    books {
        integer id PK
        string title
        string isbn
        string type
        integer author_id FK
        integer published_year
        integer price_cents
        string currency
        integer copies_total
        integer copies_available
        text synopsis
        datetime created_at
        datetime updated_at
    }
    categories {
        integer id PK
        string name
        text description
        datetime created_at
        datetime updated_at
    }
    book_categories {
        integer id PK
        integer book_id FK
        integer category_id FK
        datetime created_at
        datetime updated_at
    }
    members {
        integer id PK
        string name
        string email
        date joined_on
        boolean active
        datetime created_at
        datetime updated_at
    }
    loans {
        integer id PK
        integer book_id FK
        integer member_id FK
        date checked_out_on
        date due_on
        date returned_on
        datetime created_at
        datetime updated_at
    }
```

## テーブル一覧

| テーブル | 説明 |
|------|------|
| [authors](authors.md) | 書籍の著者の情報を管理するテーブルです。 |
| [books](books.md) | 管理対象の書籍情報を保持するテーブルです。単一テーブル継承（STI）用のカラムも含みます。 |
| [categories](categories.md) | 書籍のジャンルやカテゴリを管理するテーブルです。 |
| [book_categories](book_categories.md) | 書籍とカテゴリの多対多の関係を結ぶ中間テーブルです。 |
| [members](members.md) | 図書サービスの利用会員情報を管理するテーブルです。 |
| [loans](loans.md) | 会員への書籍の貸出履歴および現在の貸出ステータスを管理するテーブルです。 |
