# Датасет Berka (PKDD'99 Financial)

Дані в git не зберігаються (див. `.gitignore`), тут лише опис вивантаження.

- Джерело: публічний сервер MariaDB `relational.fel.cvut.cz:3306`, база `financial`,
  користувач `guest` (лише читання). Опис: https://relational.fel.cvut.cz/dataset/Financial
- Ліцензія: на сторінці джерела не вказана.
- Дата вивантаження: 2026-10-19
- Скрипт: `python -m scripts.analysis.berka_audit export`
- Формат: CSV, UTF-8, заголовок у першому рядку; NULL — `\N`, порожній рядок — порожнє поле;
  рядки впорядковані за первинним ключем.

## Розбіжність опису і даних: `VYBER` у `trans.type`

У гайді Berka (Guide to the Financial Data Set) `VYBER` описано лише як значення `operation`
(«зняття готівки»); для `type` описано тільки `PRIJEM` (надходження) і `VYDAJ` (списання).
У даних `VYBER` є й у `type`: 16 666 рядків, усі з `operation = VYBER`; баланс після них
лише зменшується (у 0 % рядків попередній баланс + amount = баланс, у 72,5 % попередній
баланс − amount = баланс; решта не перевіряється через невідомий порядок транзакцій
усередині дня).

Опис полів звірено за вторинним джерелом
(https://webpages.charlotte.edu/mirsad/itcs6265/group1/transaction_domain.html):
сторінка гайду на is.vsfs.cz на момент вивантаження відкривалась лише через анти-бот JS-перевірку.

Аудит (`python -m scripts.analysis.berka_audit audit`) за замовчуванням вважає витратами
`type` ∈ {`VYDAJ`, `VYBER`}; для перевірки чутливості — `--expense-types=VYDAJ`.

| Файл | Рядків (без заголовка) | Стовпців | sha256 |
|---|---:|---:|---|
| `account.csv` | 4500 | 4 | `7b73fbf635edee867f48de2def090140631769e28898b822cd989504714ac95f` |
| `card.csv` | 892 | 4 | `8e5adf2e2a845abe78329ce93ee5ca25c3a68268a01a20eb66ae8fa48ef200b5` |
| `client.csv` | 5369 | 4 | `09f87f3c95cbcc77fbff92dbfe1361b63aa6a4800f569b8f329db9a8879b3916` |
| `disp.csv` | 5369 | 4 | `83f11ef399aaf45c9dca07e6dc506271f486575dc1a92413e2832cde4aa1f16b` |
| `district.csv` | 77 | 16 | `834b26a7fa7bb6ea8703c6df3cce2d0ac42cef4245dde5291e6ef1c6d5b4f53d` |
| `loan.csv` | 682 | 7 | `2ae8e45073ce5334112d0af74420fe9668a45d6430298e3f34bd725e122c4e96` |
| `order.csv` | 6471 | 6 | `de57bf457a103f0b6f6e23a54a2177c889ea0cac4c440bf534dd6c7950521426` |
| `trans.csv` | 1056320 | 10 | `851e6ee007c62d44a7899d80f045cfcda20277a9dd14832548ffa74b51072253` |
