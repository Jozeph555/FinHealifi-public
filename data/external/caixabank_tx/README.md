# Набір Kaggle «Financial Transactions Dataset: Analytics»

Дані в git не зберігаються (див. `.gitignore`), тут лише опис завантаження.

- Джерело: https://www.kaggle.com/datasets/computingvictor/transactions-fraud-datasets
  (автор на Kaggle — ComputingVictor).
- Ліцензія: Apache 2.0 (з метаданих Kaggle-сторінки набору).
- Дата завантаження: 2026-11-04, вручну з Kaggle-сторінки.
- Набір, імовірно, синтетичний; походження даних не підтверджене.
- Аудит: `python -m scripts.analysis.caixabank_audit audit`.
- `cards_data.csv` містить колонки `card_number` і `cvv`; скрипт аудиту цей файл не читає.

| Файл | sha256 |
|---|---|
| `cards_data.csv` | `894f52fbe1a2d4ea71c98256d51ea95ed5d1b9fb5348c0ccacca4fc6af633cd0` |
| `mcc_codes.json` | `ebe6c6869188b1af6cf4c8a904d4b59a6863b4fd7a63424d945d6789e122df1c` |
| `train_fraud_labels.json` | `7f66b38200764f0c5658127f6fe8077fa2e520d1b93570548ce2347d685da1c8` |
| `transactions_data.csv` | `26a5365ffa3423b41080989afcbeaa7f838d2cb504c21c6bf13471b2c13872d6` |
| `users_data.csv` | `5d0e5f5a1bc49ade9bd2d2de2ad98b9fba78e79d3d777895074af48decef02b6` |
