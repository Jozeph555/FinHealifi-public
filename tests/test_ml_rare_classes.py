"""
Тести навчання ML-категоризатора з рідкісними класами.

train_and_save прибирає з навчання класи з кількістю прикладів нижче
MIN_EXAMPLES_PER_CLASS (_drop_rare_classes, UserWarning з назвою класу й
кількістю) — для default і персональних моделей; приклади в БД не
змінюються. Якщо після цього залишається менше 2 класів —
NotEnoughClassesError до запису файлів моделей.

Тести викликають train_and_save: _load_pool читає db_session, embedder —
детермінована numpy-заглушка, файли моделей пишуться в tmp_path.
"""
import pickle
import uuid
from collections import Counter

import numpy as np
import pytest
from imblearn.over_sampling import SMOTE
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split

import app.services.ml_categorizer as ml_module
from app.models.category_training_example import CategoryTrainingExample

RARE = "Навчання та саморозвиток"


class FakeEmbedder:
    """Детерміновані ембедінги для заданої кількості текстів; серіалізується pickle."""

    def encode(self, texts, show_progress_bar=False):
        return np.random.default_rng(7).normal(size=(len(texts), 8))


@pytest.fixture
def training(db_session, monkeypatch, tmp_path):
    original_load_pool = ml_module._load_pool

    async def _load_pool_from_test_session(session=None):
        return await original_load_pool(session=db_session)

    monkeypatch.setattr(ml_module, "_load_pool", _load_pool_from_test_session)
    monkeypatch.setattr(ml_module, "_get_embedding_model", lambda: FakeEmbedder())
    monkeypatch.setattr(ml_module, "ARTIFACTS_DIR", tmp_path)
    return tmp_path


async def _add_examples(db_session, user_id, category, n):
    for i in range(n):
        db_session.add(CategoryTrainingExample(
            id=str(uuid.uuid4()), user_id=user_id, source_transaction_id=str(uuid.uuid4()),
            description=f"{category} {i}", category=category, source="manual_confirmed",
        ))
    await db_session.flush()


def _load(path):
    with open(path, "rb") as f:
        return pickle.load(f)


def _rare_warnings(recwarn, category):
    return [str(w.message) for w in recwarn if issubclass(w.category, UserWarning) and category in str(w.message)]


# --- (а, б) клас з 1 / 2 прикладами: навчання проходить, класу немає в моделях ------

@pytest.mark.parametrize("rare_n", [1, 2])
async def test_rare_class_is_dropped_and_training_succeeds(db_session, real_user, training, recwarn, rare_n):
    # 30 власних прикладів -> ще й персоналізована модель (MIN_OWN_EXAMPLES).
    await _add_examples(db_session, real_user.id, "Продукти", ml_module.MIN_OWN_EXAMPLES)
    await _add_examples(db_session, real_user.id, RARE, rare_n)

    result = await ml_module.train_and_save()

    default_clf = _load(training / "default_clf.pkl")
    user_clf = _load(training / f"user_{real_user.id}_clf.pkl")
    assert set(default_clf.classes_) == {"Продукти", "Борги та кредити", "Розваги"}
    assert set(user_clf.classes_) == set(default_clf.classes_)
    assert result["personalized_users"] == [real_user.id]
    [message] = _rare_warnings(recwarn, RARE)
    assert f"'{RARE}': {rare_n}" in message
    # приклади в пулі (БД) лишились
    assert (await ml_module._load_pool(session=db_session))[1].count(RARE) == rare_n


# --- (в) клас рівно на порозі — у навчанні ------------------------------------------

async def test_class_at_threshold_is_trained(db_session, real_user, training, recwarn):
    await _add_examples(db_session, real_user.id, "Продукти", 5)
    await _add_examples(db_session, real_user.id, RARE, ml_module.MIN_EXAMPLES_PER_CLASS)

    await ml_module.train_and_save()

    assert RARE in set(_load(training / "default_clf.pkl").classes_)
    assert _rare_warnings(recwarn, RARE) == []


# --- (г) менше 2 класів після відсікання — зрозуміла помилка, артефакти не пишуться ---

async def test_fewer_than_two_classes_after_drop_is_clear_error(db_session, real_user, training, monkeypatch):
    monkeypatch.setattr(ml_module, "SYNTHETIC_SEEDS", [])
    await _add_examples(db_session, real_user.id, "Продукти", 5)
    await _add_examples(db_session, real_user.id, RARE, 1)

    with pytest.raises(ValueError, match="щонайменше 2 класи") as exc:
        await ml_module.train_and_save()

    assert type(exc.value).__name__ == "NotEnoughClassesError"
    assert list(training.iterdir()) == []


# --- (д) пул без рідкісних класів: ті самі класи й коефіцієнти --------------------

def _reference_fit(embeddings, labels):
    # Еталонна послідовність split -> SMOTE -> LogisticRegression.
    X_train, X_test, y_train, y_test = train_test_split(
        embeddings, labels, test_size=0.2, random_state=42, stratify=labels
    )
    k = min(5, min(Counter(y_train).values()) - 1)
    X_res, y_res = SMOTE(random_state=42, k_neighbors=k).fit_resample(X_train, y_train)
    clf = LogisticRegression(max_iter=1000, random_state=42).fit(X_res, y_res)
    return clf, X_test


async def test_pool_without_rare_classes_unchanged(db_session, real_user, training, recwarn):
    await _add_examples(db_session, real_user.id, "Продукти", 8)
    await _add_examples(db_session, real_user.id, "Транспорт", 4)
    texts, labels, _ = await ml_module._load_pool(session=db_session)

    result = await ml_module.train_and_save()

    default_clf = _load(training / "default_clf.pkl")
    reference, X_test = _reference_fit(FakeEmbedder().encode(texts), labels)
    assert result["pool_size"] == len(texts)
    assert list(default_clf.classes_) == list(reference.classes_)
    assert list(default_clf.predict(X_test)) == list(reference.predict(X_test))
    assert np.allclose(default_clf.coef_, reference.coef_)
    assert not [w for w in recwarn if "рідкісні класи" in str(w.message)]
