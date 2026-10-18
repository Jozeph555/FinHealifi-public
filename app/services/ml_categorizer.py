"""
ML-категоризація транзакцій за описом: ембедінги sentence-transformers +
LogisticRegression.

Застосовується через POST /mono/recategorize-pending-ml до pending-транзакцій
користувача. Навчальні приклади беруться з таблиці category_training_examples,
яку наповнюють лише правила (MCC, ключові слова) і ручне підтвердження
категорії; власні передбачення моделі в пул не потрапляють.

Моделі:
- default — навчається на всьому пулі, використовується для всіх користувачів;
- персональна — для користувача з щонайменше MIN_OWN_EXAMPLES власних
  прикладів; навчається на всьому пулі з вагою OWN_EXAMPLE_WEIGHT для його
  прикладів.
"""
import pickle
import warnings
from collections import Counter
from pathlib import Path

from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report
from imblearn.over_sampling import SMOTE
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import AsyncSessionLocal
from app.models.category_training_example import CategoryTrainingExample
from app.services.mcc_mapping import INCOME_CATEGORIES, TRANSFER_CATEGORIES, CASH_CATEGORY
from app.services.statement_importer import _get_embedding_model

# Каталог файлів моделей у корені репозиторію.
ARTIFACTS_DIR = Path(__file__).resolve().parents[2] / "ml_artifacts"

# Категорії, яких модель не вчиться з опису: pending, готівка (визначається за
# MCC 6010/6011), перекази й дохід. Цей список не збігається з
# forecasting.EXPENSE_EXCLUDED_CATEGORIES: готівка в прогнозі — витрата.
EXCLUDED_CATEGORIES = {"pending", CASH_CATEGORY} | TRANSFER_CATEGORIES | INCOME_CATEGORIES

# Класи, які модель ніколи не призначає: перекази й дохід ставляться лише
# ключовими словами або вручну.
ML_FORBIDDEN_CATEGORIES = TRANSFER_CATEGORIES | INCOME_CATEGORIES


def is_ml_assignable(category: str | None) -> bool:
    """Перевіряє, чи може модель призначати категорію.

    Застосовується і до пулу навчання, і до передбачень: файл моделі,
    навчений раніше, може містити заборонений клас.
    """
    return category is not None and category not in ML_FORBIDDEN_CATEGORIES

# Синтетичні приклади для класів, яких мало в реальних даних.
SYNTHETIC_SEEDS = [
    ("Погашення кредиту", "Борги та кредити"),
    ("Мінімальний платіж по картці", "Борги та кредити"),
    ("Оплата розстрочки", "Борги та кредити"),
    ("monobank кредит", "Борги та кредити"),
    ("Приват24 кредит", "Борги та кредити"),
    ("Netflix", "Розваги"),
    ("Spotify", "Розваги"),
    ("Steam", "Розваги"),
    ("PlayStation Store", "Розваги"),
    ("Кінотеатр", "Розваги"),
    ("Планетарій", "Розваги"),
]

# Мінімум власних прикладів користувача для персональної моделі.
MIN_OWN_EXAMPLES = 30
# Вага власного прикладу користувача відносно решти пулу (sample_weight).
OWN_EXAMPLE_WEIGHT = 3
# Передбачення з упевненістю нижче цього порогу позначаються як low_confidence.
LOW_CONFIDENCE_THRESHOLD = 0.75
# Мінімум прикладів класу в пулі для навчання; рідші класи модель не
# передбачає. Стратифікований split з test_size=0.2 залишає в train щонайменше
# 2 приклади класу, тож SMOTE отримує k_neighbors >= 1.
MIN_EXAMPLES_PER_CLASS = 3


class NotEnoughClassesError(ValueError):
    """Після відсікання рідкісних класів у пулі менше двох класів."""


async def _load_pool(session: AsyncSession | None = None):
    """Завантажує пул навчальних прикладів разом із синтетичними.

    Приклади з категоріями EXCLUDED_CATEGORIES відкидаються. Якщо серед них є
    класи ML_FORBIDDEN_CATEGORIES, видається одне UserWarning з підсумком.

    Args:
        session: Сесія БД; якщо не задано, відкривається власна.

    Returns:
        Трійка списків (texts, labels, owners); owner синтетичних прикладів — None.
    """
    if session is not None:
        rows = (await session.execute(select(CategoryTrainingExample))).scalars().all()
    else:
        async with AsyncSessionLocal() as own_session:
            rows = (await own_session.execute(select(CategoryTrainingExample))).scalars().all()

    candidates = [(r.description, r.category, r.user_id) for r in rows]
    candidates += [(text, label, None) for text, label in SYNTHETIC_SEEDS]

    texts, labels, owners = [], [], []
    blocked = Counter()
    for text, label, owner in candidates:
        if label in EXCLUDED_CATEGORIES:
            if not is_ml_assignable(label):
                blocked[label] += 1
            continue
        texts.append(text)
        labels.append(label)
        owners.append(owner)

    if blocked:
        warnings.warn(
            "Пул навчання: відкинуто приклади класів, які ML не призначає "
            f"(лише ключові слова/вручну): {dict(blocked)}",
            UserWarning,
            stacklevel=2,
        )

    return texts, labels, owners


def _drop_rare_classes(texts, labels, owners):
    """Прибирає з навчальної вибірки класи з менш ніж MIN_EXAMPLES_PER_CLASS прикладами.

    Приклади в БД не змінюються. Про відкинуті класи видається UserWarning.

    Returns:
        Трійка списків (texts, labels, owners) без рідкісних класів.

    Raises:
        NotEnoughClassesError: Після відсікання залишилося менше двох класів.
    """
    rare = {label: n for label, n in Counter(labels).items() if n < MIN_EXAMPLES_PER_CLASS}
    if rare:
        warnings.warn(
            f"Пул навчання: рідкісні класи (< {MIN_EXAMPLES_PER_CLASS} прикладів) не вчаться — "
            f"ML їх не передбачає, лише ручний вибір: {rare}",
            UserWarning,
            stacklevel=2,
        )
        kept = [i for i, label in enumerate(labels) if label not in rare]
        texts = [texts[i] for i in kept]
        labels = [labels[i] for i in kept]
        owners = [owners[i] for i in kept]

    n_classes = len(set(labels))
    if n_classes < 2:
        raise NotEnoughClassesError(
            f"Для навчання потрібно щонайменше 2 класи з >= {MIN_EXAMPLES_PER_CLASS} прикладів "
            f"кожен, у пулі після відсікання рідкісних класів — {n_classes}. "
            "Додай приклади (ручне підтвердження категорій) і перезапусти навчання."
        )
    return texts, labels, owners


def _fit_resampled(X_train, y_train):
    """Навчає default-класифікатор: SMOTE + LogisticRegression.

    Кожен клас у y_train має мати щонайменше 2 приклади.
    """
    class_counts = Counter(y_train)
    min_samples = min(class_counts.values())
    k_neighbors = min(5, min_samples - 1)
    smote = SMOTE(random_state=42, k_neighbors=k_neighbors)
    X_train_res, y_train_res = smote.fit_resample(X_train, y_train)

    clf = LogisticRegression(max_iter=1000, random_state=42)
    clf.fit(X_train_res, y_train_res)
    return clf


def _fit_default_classifier(embeddings, labels):
    """Навчає default-класифікатор на 80 % пулу і повертає (модель, звіт на решті 20 %)."""
    X_train, X_test, y_train, y_test = train_test_split(
        embeddings, labels, test_size=0.2, random_state=42, stratify=labels
    )
    clf = _fit_resampled(X_train, y_train)

    report = classification_report(y_test, clf.predict(X_test))
    return clf, report


def _fit_personalized_classifier(embeddings, labels, owners, user_id):
    """Навчає персональний класифікатор на всьому пулі без SMOTE.

    Приклади користувача мають вагу OWN_EXAMPLE_WEIGHT, решта — 1.
    """
    sample_weight = [OWN_EXAMPLE_WEIGHT if owner == user_id else 1 for owner in owners]
    clf = LogisticRegression(max_iter=1000, random_state=42)
    clf.fit(embeddings, labels, sample_weight=sample_weight)
    return clf


async def train_and_save(evaluation_report=None):
    """Навчає моделі на поточному пулі й зберігає їх у ARTIFACTS_DIR.

    Зберігає embedder.pkl, default_clf.pkl і user_<id>_clf.pkl для кожного
    користувача з щонайменше MIN_OWN_EXAMPLES власних прикладів. Запущений
    сервер підхоплює нові файли лише після перезапуску: класифікатори
    кешуються в пам'яті процесу.

    Args:
        evaluation_report: Опційна функція (texts, labels, embeddings) -> str;
            її результат друкується перед навчанням.

    Returns:
        {"pool_size": розмір пулу, "personalized_users": id користувачів з персональною моделлю}.
    """
    texts, labels, owners = await _load_pool()
    texts, labels, owners = _drop_rare_classes(texts, labels, owners)
    print(f"Всього прикладів у пулі: {len(texts)}")
    print(f"Розподіл класів: {Counter(labels)}")

    model = _get_embedding_model()
    print("\nГенерація embeddings...")
    embeddings = model.encode(texts, show_progress_bar=True)

    ARTIFACTS_DIR.mkdir(exist_ok=True)

    with open(ARTIFACTS_DIR / "embedder.pkl", "wb") as f:
        pickle.dump(model, f)

    if evaluation_report is not None:
        print(evaluation_report(texts, labels, embeddings))

    print("\nТренування default_clf...")
    default_clf, _ = _fit_default_classifier(embeddings, labels)
    with open(ARTIFACTS_DIR / "default_clf.pkl", "wb") as f:
        pickle.dump(default_clf, f)

    own_counts = Counter(o for o in owners if o is not None)
    personalized_users = [uid for uid, n in own_counts.items() if n >= MIN_OWN_EXAMPLES]
    print(
        f"\nПерсоналізовані моделі: {len(personalized_users)} користувач(ів) "
        f"із ≥{MIN_OWN_EXAMPLES} власних прикладів"
    )
    for user_id in personalized_users:
        clf = _fit_personalized_classifier(embeddings, labels, owners, user_id)
        with open(ARTIFACTS_DIR / f"user_{user_id}_clf.pkl", "wb") as f:
            pickle.dump(clf, f)

    print("\nМодель(і) збережено в", ARTIFACTS_DIR)
    return {"pool_size": len(texts), "personalized_users": personalized_users}


# --- Застосування: кеш класифікаторів у пам'яті процесу -------------------
# Файли моделей читаються з диска один раз, при першому зверненні.

_default_clf_cache = None
_user_clf_cache: dict[str, object] = {}


def _load_pickle(path: Path):
    """Читає об'єкт з pickle-файлу."""
    with open(path, "rb") as f:
        return pickle.load(f)


def get_default_classifier():
    """Повертає default-класифікатор (з кешу процесу)."""
    global _default_clf_cache
    if _default_clf_cache is None:
        _default_clf_cache = _load_pickle(ARTIFACTS_DIR / "default_clf.pkl")
    return _default_clf_cache


def get_classifier_for_user(user_id: str):
    """Повертає персональний класифікатор користувача або default, якщо персонального немає."""
    if user_id not in _user_clf_cache:
        user_path = ARTIFACTS_DIR / f"user_{user_id}_clf.pkl"
        _user_clf_cache[user_id] = (
            _load_pickle(user_path) if user_path.exists() else get_default_classifier()
        )
    return _user_clf_cache[user_id]


def predict_category(description: str, clf, threshold: float = 0.6):
    """Передбачає категорію за описом транзакції.

    Args:
        description: Опис транзакції.
        clf: Класифікатор з predict_proba і classes_.
        threshold: Мінімальна ймовірність, з якою передбачення приймається.

    Returns:
        Трійка (категорія, ймовірність, метод): метод 'ml', якщо ймовірність
        не нижча за threshold; інакше категорія None і метод 'pending'.
    """
    embedding = _get_embedding_model().encode([description])
    proba = clf.predict_proba(embedding)[0]
    max_proba = float(max(proba))

    if max_proba >= threshold:
        return clf.classes_[proba.argmax()], max_proba, "ml"
    return None, max_proba, "pending"
