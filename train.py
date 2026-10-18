"""
Перенавчання ML-категоризатора на пулі навчальних прикладів.

Викликає app.services.ml_categorizer.train_and_save(): друкує групову оцінку
якості (scripts/analysis/categorizer_eval.py) і зберігає embedder.pkl,
default_clf.pkl та user_<id>_clf.pkl у ml_artifacts/. Запущений сервер
підхоплює нові файли лише після перезапуску.

Приклади класів, яких модель не призначає (ML_FORBIDDEN_CATEGORIES),
відкидаються з попередженням; навчання продовжується.

Запуск: python train.py
"""
import asyncio

from app.services.ml_categorizer import train_and_save
from scripts.analysis.categorizer_eval import evaluation_report


if __name__ == "__main__":
    asyncio.run(train_and_save(evaluation_report=evaluation_report))
