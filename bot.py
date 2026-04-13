#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
StyleBot — Telegram-бот для подбора образов и сборки чемодана.

Логика рекомендаций построена на правилах и ключевых словах (без внешних AI API).
Данные пользователей хранятся в JSON-файле на диске.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Конфигурация и логирование
# ---------------------------------------------------------------------------

load_dotenv()

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("stylebot")

DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_FILE = DATA_DIR / "users.json"

STYLES = ("casual", "classic", "sport", "elegant", "street")

# Максимум записей истории на пользователя (чтобы файл не разрастался бесконечно)
MAX_HISTORY = 40

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
REQUEST_TIMEOUT = 10


# ---------------------------------------------------------------------------
# Модель данных и работа с JSON
# ---------------------------------------------------------------------------


@dataclass
class WardrobeItem:
    """Одна позиция в гардеробе пользователя."""

    item: str
    category: str


@dataclass
class HistoryEntry:
    """Запись истории запросов."""

    text: str
    query_type: str
    date: str


@dataclass
class UserRecord:
    """Профиль пользователя и связанные данные."""

    user_id: int
    username: Optional[str] = None
    preferred_style: Optional[str] = None
    favorite_colors: list[str] = field(default_factory=list)
    disliked_colors: list[str] = field(default_factory=list)
    gender: Optional[str] = None
    seasonal_preferences: Optional[str] = None
    notes: str = ""
    wardrobe: list[dict[str, str]] = field(default_factory=list)
    history: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "UserRecord":
        return cls(
            user_id=int(d["user_id"]),
            username=d.get("username"),
            preferred_style=d.get("preferred_style"),
            favorite_colors=list(d.get("favorite_colors") or []),
            disliked_colors=list(d.get("disliked_colors") or []),
            gender=d.get("gender"),
            seasonal_preferences=d.get("seasonal_preferences"),
            notes=str(d.get("notes") or ""),
            wardrobe=list(d.get("wardrobe") or []),
            history=list(d.get("history") or []),
        )


def _ensure_data_dir() -> None:
    """Создаёт каталог для данных, если его ещё нет."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.exception("Не удалось создать каталог данных: %s", exc)


def load_storage() -> dict[str, Any]:
    """Загружает весь JSON-хранилище. При ошибке возвращает пустую структуру."""
    _ensure_data_dir()
    if not DATA_FILE.exists():
        return {"users": {}}
    try:
        with DATA_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "users" not in data:
            return {"users": {}}
        return data
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Ошибка чтения %s: %s", DATA_FILE, exc)
        return {"users": {}}


def save_storage(data: dict[str, Any]) -> None:
    """Атомарно сохраняет JSON (через временный файл + replace)."""
    _ensure_data_dir()
    tmp_path = DATA_FILE.with_suffix(".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp_path.replace(DATA_FILE)
    except OSError as exc:
        logger.exception("Ошибка записи %s: %s", DATA_FILE, exc)
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


def get_user_record(user_id: int) -> UserRecord:
    """Возвращает запись пользователя или создаёт новую."""
    storage = load_storage()
    key = str(user_id)
    raw = storage.get("users", {}).get(key)
    if isinstance(raw, dict):
        try:
            return UserRecord.from_dict(raw)
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("Повреждённая запись пользователя %s: %s", user_id, exc)
    return UserRecord(user_id=user_id)


def upsert_user_record(record: UserRecord) -> None:
    """Сохраняет запись пользователя в файл."""
    storage = load_storage()
    storage.setdefault("users", {})
    storage["users"][str(record.user_id)] = record.to_dict()
    save_storage(storage)


def append_history(record: UserRecord, text: str, query_type: str) -> None:
    """Добавляет запись в историю и обрезает по длине."""
    entry = {
        "text": text[:500],
        "query_type": query_type,
        "date": datetime.now(timezone.utc).isoformat(),
    }
    record.history.append(entry)
    if len(record.history) > MAX_HISTORY:
        record.history = record.history[-MAX_HISTORY:]


# ---------------------------------------------------------------------------
# Разбор намерений и сущностей из свободного текста (правила + ключевые слова)
# ---------------------------------------------------------------------------


class Intent:
    """Тип намерения пользователя."""

    WEATHER = "weather"
    OUTFIT = "outfit"
    TRIP = "trip"
    SAVE_PREFS = "save_prefs"
    SAVE_WARDROBE = "save_wardrobe"
    SHOW_PROFILE = "show_profile"
    UNKNOWN = "unknown"


TRIP_MARKERS = (
    "чемодан",
    "собери",
    "сбор",
    "поездк",
    "путешеств",
    "взять с собой",
    "что взять",
    "еду в",
    "еду на",
    "командировк",
    "отпуск",
    "тур",
    "билет",
    "в поездку",  # «что взять в поездку на море»
    "собрать",  # «помоги собрать чемодан»
    "на море",  # «на море на неделю»
    "в горы",
    "в рим",
    "в париж",
)

OUTFIT_MARKERS = (
    "что надеть",
    "как одеться",
    "образ",
    "подбери",
    "посоветуй",
    "наряд",
    "одеться",
    "стильно",
    "наден",
)

EVENT_KEYWORDS = {
    "учеба": ("университет", "школ", "учёб", "учеб", "колледж", "лекци", "пара "),
    "работа": ("офис", "работ", "open space", "опенспейс"),
    "прогулка": ("прогул", "гуля", "парк", "город"),
    "свидание": ("свидан", "романт"),
    "вечеринка": ("вечерин", "тусовк", "клуб", "party"),
    "путешествие": ("путешеств", "аэропорт", "вокзал"),
    "деловая встреча": ("делов", "переговор", "презентац", "инвестор"),
    "кафе_ресторан": ("кафе", "ресторан", "ужин", "бранч", "кофе"),
}

WEATHER_KEYWORDS = {
    "дождь": ("дожд", "осадк", "мокро"),
    "ветер": ("ветер", "ветрен"),
    "снег": ("снег", "мороз"),
    "жара": ("жар", "+25", "+30", "лето", "тепло"),
    "прохлада": ("прохлад", "+5", "+8", "+10", "осень", "весна"),
    "холод": ("холод", "минус", "-", "зим"),
}

TRIP_TYPE_KEYWORDS = {
    "город": ("рим", "париж", "петербург", "москв", "город", "city break", "сити"),
    "море": ("море", "пляж", "курорт", "средизем", "остров"),
    "горы": ("гор", "лыж", "сноуборд", "треккинг", "поход"),
    "командировка": ("командиров"),
    "уикенд": ("выходн", "уикенд", "weekend", "на 2 дня"),
}

STYLE_KEYWORDS = {
    "casual": ("casual", "кэжуал", "повседнев", "расслаблен"),
    "classic": ("classic", "классик", "делов", "строг"),
    "sport": ("sport", "спорт", "актив", "кроссовк"),
    "elegant": ("elegant", "элегант", "нарядн", "вечер"),
    "street": ("street", "стрит", "урбан", "оверсайз"),
}

COLOR_WORDS = {
    "красн": "красный",
    "син": "синий",
    "бел": "белый",
    "черн": "чёрный",
    "зелен": "зелёный",
    "желт": "жёлтый",
    "розов": "розовый",
    "фиолет": "фиолетовый",
    "коричнев": "коричневый",
    "сер": "серый",
    "беж": "бежевый",
    "оранж": "оранжевый",
}


def _normalize(text: str) -> str:
    return text.strip().lower()

CITY_TRANSLATIONS = {
    "москва": "moscow",
    "москве": "moscow",
    "москву": "moscow",
    "питер": "saint petersburg",
    "санкт-петербург": "saint petersburg",
    "спб": "saint petersburg",
    "казань": "kazan",
    "сочи": "sochi",
    "париж": "paris",
    "токио": "tokyo",
}
def normalize_city_name(city: str) -> str:
    city = city.strip().lower()

    # убираем слова времени
    for word in ["завтра", "сегодня", "послезавтра", "через неделю"]:
        city = city.replace(word, "")

    city = " ".join(city.split())

    # несколько частых специальных случаев
    special_cases = {
        "москве": "москва",
        "москву": "москва",
        "питере": "санкт-петербург",
        "спб": "санкт-петербург",
        "петербурге": "санкт-петербург",
    }

    if city in special_cases:
        return special_cases[city]

    # простые эвристики для русских падежей
    if city.endswith("е") and len(city) > 4:
        city = city[:-1]
    elif city.endswith("и") and len(city) > 4:
        city = city[:-1] + "ь"
    elif city.endswith("у") and len(city) > 4:
        city = city[:-1] + "а"

    return city

def get_coordinates(city: str) -> tuple[float, float]:
    """Ищет координаты города через Open-Meteo Geocoding API."""

    city = normalize_city_name(city)

    if not city:
        raise ValueError("Укажи город.")

    try:
        response = requests.get(
            GEOCODING_URL,
            params={"name": city, "count": 1, "language": "ru"},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        logger.exception("Ошибка geocoding API: %s", exc)
        raise RuntimeError("Сервис геокодинга временно недоступен.") from exc

    results = data.get("results") if isinstance(data, dict) else None
    if not results:
        raise ValueError(f"Город «{city}» не найден.")

    first = results[0]
    lat = first.get("latitude")
    lon = first.get("longitude")
    if lat is None or lon is None:
        raise RuntimeError("Не удалось получить координаты города.")
    return float(lat), float(lon)


def get_weather(city: str) -> float:
    """Возвращает текущую температуру в городе через Open-Meteo Forecast API."""
    latitude, longitude = get_coordinates(city)

    try:
        response = requests.get(
            FORECAST_URL,
            params={
                "latitude": latitude,
                "longitude": longitude,
                "current": "temperature_2m,weather_code",
            },
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        logger.exception("Ошибка weather API: %s", exc)
        raise RuntimeError("Сервис погоды временно недоступен.") from exc

    current = data.get("current") if isinstance(data, dict) else None
    temperature = current.get("temperature_2m") if isinstance(current, dict) else None
    if temperature is None:
        raise RuntimeError("Не удалось получить температуру.")

    return float(temperature)
def random_intro() -> str:
    phrases = [
        "Вот что я нашёл 👇",
        "Смотри 👇",
        "Вот актуальная информация 👇",
        "Проверил для тебя 👇"
    ]
    return random.choice(phrases)


def format_weather_response(city: str, temp: float) -> str:
    return (
        f"{random_intro()}\n\n"
        f"🌍 Город: {city.title()}\n"
        f"🌡 Температура: {temp}°C\n\n"
        f"{_clothes_by_temperature(temp)}"
    )

def _extract_city_after_phrase(text: str, phrase: str) -> Optional[str]:
    """Извлекает город после фразы (например, 'погода в')."""
    t = _normalize(text)
    idx = t.find(phrase)
    if idx == -1:
        return None
    city = text[idx + len(phrase) :].strip(" ?!.,")
    if not city:
        return None
    # Отсекаем хвосты: "что надеть в Москве вечером"
    city = re.split(r"(?:\?|,| вечером| утром| дн[её]м| при )", city, maxsplit=1)[0].strip()
    return city or None


def _clothes_by_temperature(temp: float) -> str:
    if temp < 5:
        return (
            "🧥 Рекомендация:\n"
            "— тёплая куртка\n"
            "— шарф\n"
            "— утеплённая обувь"
        )
    elif 5 <= temp < 15:
        return (
            "🧥 Рекомендация:\n"
            "— пальто или куртка\n"
            "— кофта\n"
            "— закрытая обувь"
        )
    elif 15 <= temp < 25:
        return (
            "👕 Рекомендация:\n"
            "— лёгкая одежда\n"
            "— кроссовки или кеды"
        )
    else:
        return (
            "☀️ Рекомендация:\n"
            "— футболка\n"
            "— шорты, юбка или лёгкое платье\n"
            "— лёгкая обувь"
        )


def detect_intent(text: str) -> str:
    """Грубая классификация намерения по ключевым словам."""
    t = _normalize(text)

    if any(m in t for m in ("профиль", "мои предпочтения", "сохранённые", "сохраненные")):
        return Intent.SHOW_PROFILE

    if "мой стиль" in t or "предпочитаемый стиль" in t:
        return Intent.SAVE_PREFS
    if "не люблю" in t or "нелюбим" in t or "не нравится цвет" in t:
        return Intent.SAVE_PREFS
    if "любим" in t and "цвет" in t:
        return Intent.SAVE_PREFS
    if "сезонн" in t and ("предпочт" in t or "люблю" in t):
        return Intent.SAVE_PREFS
    if re.search(r"\bпол\b", t) or " я мужчина" in t or " я женщина" in t:
        return Intent.SAVE_PREFS

    if "у меня есть" in t or "в гардеробе" in t or "у меня в шкафу" in t:
        return Intent.SAVE_WARDROBE

    trip_score = sum(1 for m in TRIP_MARKERS if m in t)
    outfit_score = sum(1 for m in OUTFIT_MARKERS if m in t)

    if trip_score > 0 and trip_score >= outfit_score:
        return Intent.TRIP
    if outfit_score > 0:
        return Intent.OUTFIT

    # Эвристика: «на N дней» без явного «образ» — чаще про поездку
    if re.search(r"\bна\s+\d+\s+(дн|дней|ноч)", t) and "образ" not in t:
        return Intent.TRIP

    if re.search(r"\bна\s+\d+\s+дн", t) and any(x in t for x in ("рим", "париж", "поезд", "море", "гор")):
        return Intent.TRIP

    return Intent.UNKNOWN


def extract_duration_days(text: str) -> Optional[int]:
    """Ищет фразы вида «на 5 дней», «на неделю»."""
    t = _normalize(text)
    m = re.search(r"на\s+(\d+)\s*(дн|дней|ноч)", t)
    if m:
        try:
            return max(1, min(30, int(m.group(1))))
        except ValueError:
            return None
    if "недел" in t:
        return 7
    if "уикенд" in t or "выходн" in t:
        return 2
    if "месяц" in t:
        return 14  # ограничим разумным максимумом для шаблона
    return None


def extract_entities(text: str) -> dict[str, Any]:
    """Извлекает контекст для генерации ответа."""
    t = _normalize(text)
    entities: dict[str, Any] = {
        "events": [],
        "weather": [],
        "styles": [],
        "trip_types": [],
    }

    for ev, kws in EVENT_KEYWORDS.items():
        if any(k in t for k in kws):
            entities["events"].append(ev)

    for w, kws in WEATHER_KEYWORDS.items():
        if any(k in t for k in kws):
            entities["weather"].append(w)

    for st, kws in STYLE_KEYWORDS.items():
        if any(k in t for k in kws):
            entities["styles"].append(st)

    for tt, kws in TRIP_TYPE_KEYWORDS.items():
        if any(k in t for k in kws):
            entities["trip_types"].append(tt)

    # температура вида +8, -5
    tm = re.findall(r"[+-]?\d{1,2}\s*°?c?", t.replace(" ", ""))
    if tm:
        entities["temperature_hints"] = tm[:3]

    entities["duration_days"] = extract_duration_days(text)
    return entities


def parse_style_from_text(text: str) -> Optional[str]:
    t = _normalize(text)
    for st in STYLES:
        if st in t:
            return st
    for st, kws in STYLE_KEYWORDS.items():
        if any(k in t for k in kws):
            return st
    return None


def parse_colors_from_text(text: str) -> tuple[list[str], list[str]]:
    """Возвращает (любимые, нелюбимые) по простым шаблонам."""
    t = _normalize(text)
    fav: list[str] = []
    bad: list[str] = []

    def collect(prefixes: tuple[str, ...], target: list[str]) -> None:
        for p in prefixes:
            if p in t:
                after = t.split(p, 1)[1]
                chunk = re.split(r"[.;!\n]", after)[0]
                for root, canon in COLOR_WORDS.items():
                    if root in chunk:
                        if canon not in target:
                            target.append(canon)

    collect(("любим", "люблю цвет", "нравится"), fav)
    collect(("не люблю", "нелюбим", "не нравится"), bad)

    # Явные перечисления после двоеточия
    if "любимые:" in t or "любимые цвета:" in t:
        part = t.split(":", 1)[1]
        for root, canon in COLOR_WORDS.items():
            if root in part.split("нелюбим")[0]:
                if canon not in fav:
                    fav.append(canon)

    return fav, bad


def parse_wardrobe_addition(text: str) -> Optional[tuple[str, str]]:
    """
    Пытается вытащить вещь из фраз «у меня есть ...».
    Возвращает (категория, описание) или None.
    """
    t = text.strip()
    low = _normalize(t)
    prefixes = ("у меня есть", "у меня в шкафу", "в гардеробе есть")
    body = None
    for p in prefixes:
        if low.startswith(p):
            body = t[len(p) :].strip(" :,-")
            break
    if not body:
        return None

    low_body = body.lower()
    category = "другое"
    if any(w in low_body for w in ("пальто", "куртк", "пуховик", "плащ")):
        category = "верхняя одежда"
    elif any(w in low_body for w in ("джинс", "брюк", "юбк", "шорт")):
        category = "низ"
    elif any(w in low_body for w in ("рубашк", "футболк", "свитер", "худи", "водолазк")):
        category = "верх"
    elif any(w in low_body for w in ("кроссовк", "ботинк", "туфл", "лофер", "сапог")):
        category = "обувь"
    elif any(w in low_body for w in ("сумк", "рюкзак", "шарф", "шапк", "ремень")):
        category = "аксессуары"

    return category, body


def parse_gender_note(text: str) -> Optional[str]:
    t = _normalize(text)
    if "мужчина" in t:
        return "мужской"
    if "женщина" in t:
        return "женский"
    return None


def parse_seasonal_note(text: str) -> Optional[str]:
    t = _normalize(text)
    if "сезон" not in t:
        return None
    # Берём короткий фрагмент после ключевого слова
    if "сезонн" in t:
        return text.strip()[:200]
    return None


# ---------------------------------------------------------------------------
# Генерация ответов (шаблоны + правила)
# ---------------------------------------------------------------------------


def _profile_hint(record: UserRecord) -> str:
    parts: list[str] = []
    if record.preferred_style:
        parts.append(f"учитываю твой стиль: {record.preferred_style}")
    if record.favorite_colors:
        parts.append("любимые цвета: " + ", ".join(record.favorite_colors))
    if record.disliked_colors:
        parts.append("избегаю: " + ", ".join(record.disliked_colors))
    if record.wardrobe:
        sample = ", ".join(w.get("item", "") for w in record.wardrobe[-3:])
        parts.append("опираюсь на твой гардероб: " + sample)
    if not parts:
        return ""
    return "✨ " + "; ".join(parts) + ".\n\n"


def _avoid_colors_line(record: UserRecord) -> str:
    if not record.disliked_colors:
        return ""
    return f"(Из нелюбимых оттенков лучше обойти: {', '.join(record.disliked_colors)}.)\n"


def build_outfit_reply(text: str, record: UserRecord) -> str:
    """Формирует рекомендацию образа по тексту и профилю."""
    ent = extract_entities(text)
    events = ent["events"] or ["повседневно"]
    weather = ent["weather"]
    styles = ent["styles"]

    style = styles[0] if styles else (record.preferred_style or "casual")

    event = events[0]
    event_outfit_map = {
        "учеба": {
            "верх": "базовый свитер или худи, поверх — рубашка оверсайз / оверширт",
            "низ": "прямые джинсы или чёрные брюки чинос",
            "обувь": "чистые кроссовки или лоферы",
            "верхняя": "парка или короткое пальто",
            "аксессуары": "рюкзак, часы",
        },
        "работа": {
            "верх": "рубашка или лёгкий джемпер в спокойном тоне",
            "низ": "брюки со стрелками или тёмные чинос",
            "обувь": "лоферы или минималистичные кроссовки (если дресс-код позволяет)",
            "верхняя": "пиджак или тренч",
            "аксессуары": "ремень в тон обуви, лаконичная сумка",
        },
        "прогулка": {
            "верх": "футболка + кардиган или overshirt",
            "низ": "свободные джинсы или джоггеры",
            "обувь": "удобные кроссовки",
            "верхняя": "ветровка или джинсовая куртка",
            "аксессуары": "шоппер или небольшой рюкзак, кепка при солнце",
        },
        "свидание": {
            "верх": "блузка/рубашка или аккуратный вязаный топ",
            "низ": "юбка миди или брюки с высокой посадкой",
            "обувь": "туфли или минималистичные ботильоны",
            "верхняя": "лёгкое пальто или пиджак",
            "аксессуары": "лаконичные серьги, небольшая сумка",
        },
        "вечеринка": {
            "верх": "топ с интересной текстурой или яркий акцент",
            "низ": "кожаные брюки или мини-юбка — по настроению",
            "обувь": "ботильоны или стильные кроссовки",
            "верхняя": "короткая куртка",
            "аксессуары": "цепочка, клатч",
        },
        "путешествие": {
            "верх": "удобный слойный комплект: футболка + худи",
            "низ": "джинсы с запасом по длине",
            "обувь": "кроссовки на устойчивой подошве",
            "верхняя": "пуховик или пальто по сезону",
            "аксессуары": "рюкзак, шарф",
        },
        "деловая встреча": {
            "верх": "рубашка + пиджак в сдержанной палитре",
            "низ": "классические брюки",
            "обувь": "туфли или строгие лоферы",
            "верхняя": "пальто или тренч",
            "аксессуары": "портфель, минимум украшений",
        },
        "кафе_ресторан": {
            "верх": "блузка/рубашка или лёгкий свитер",
            "низ": "брюки или джинсы тёмного цвета",
            "обувь": "лоферы или аккуратные кроссовки",
            "верхняя": "пальто",
            "аксессуары": "сумка через плечо",
        },
        "повседневно": {
            "верх": "базовая футболка + рубашка поверх",
            "низ": "прямые джинсы",
            "обувь": "кроссовки",
            "верхняя": "лёгкая куртка",
            "аксессуары": "рюкзак или tote",
        },
    }

    o = event_outfit_map.get(event, event_outfit_map["повседневно"])

    weather_lines: list[str] = []
    if "дождь" in weather:
        weather_lines.append("дождь → непромокаемая куртка или плащ, зонт")
    if "ветер" in weather:
        weather_lines.append("ветер → плотный шарф, ветровка")
    if "снег" in weather or "холод" in weather:
        weather_lines.append("холод → тёплый промежуточный слой, шапка и перчатки")
    if "жара" in weather:
        weather_lines.append("жара → льняные ткани, светлые оттенки, открытая обувь по месту")
    if "прохлада" in weather:
        weather_lines.append("прохлада → кардиган или лёгкий свитер под верх")

    style_tune = {
        "classic": "Собери палитру спокойнее, добавь пиджак или тренч.",
        "sport": "Больше технических тканей и кроссовок, слои для активного дня.",
        "elegant": "Чуть больше структуры в силуэте и аккуратных аксессуаров.",
        "street": "Смело с оверсайз, кепкой и фактурными материалами.",
        "casual": "Оставь всё максимально удобным и естественным.",
    }.get(style, "")

    header = "Вот что можно надеть:\n"
    body = (
        f"👕 Верх: {o['верх']}\n"
        f"👖 Низ: {o['низ']}\n"
        f"👟 Обувь: {o['обувь']}\n"
        f"🧥 Верхняя одежда: {o['верхняя']}\n"
        f"🧣 Аксессуары: {o['аксессуары']}\n"
    )

    why = (
        "Почему это подойдёт:\n"
        f"Образ уместен для сценария «{event}», стиль — {style}. "
    )
    if weather_lines:
        why += "Учёл погодные нюансы: " + "; ".join(weather_lines) + ". "
    if style_tune:
        why += style_tune

    hint = _profile_hint(record)
    avoid = _avoid_colors_line(record)

    return hint + header + avoid + body + "\n" + why


def build_trip_reply(text: str, record: UserRecord) -> str:
    """Формирует список для чемодана."""
    ent = extract_entities(text)
    days = ent.get("duration_days") or 3
    trip_types = ent["trip_types"] or ["город"]

    tt = trip_types[0]

    base_clothes = max(1, days // 2 + 1)
    underwear = days + 1
    socks = days + 1

    templates = {
        "город": {
            "одежда": f"{base_clothes} верха на каждый день/через день, 1 пиджак или оверширт, 1 джемпер",
            "обувь": "1 пара удобных кроссовок + 1 пара «на вечер»",
            "аксессуары": "солнечные очки, зонт по прогнозу",
            "документы": "паспорт/ID, страховка, брони отелей",
            "гигиена": "мини-косметичка, зубная щётка",
            "техника": "зарядка, powerbank, наушники",
        },
        "море": {
            "одежда": f"{max(2, days//3)} плавок/купальников, 2 пляжных накидки, лёгкие шорты/сарафан",
            "обувь": "сланцы + закрытая обувь для прогулок",
            "аксессуары": "панама, пляжная сумка",
            "документы": "паспорт, наличные/карта",
            "гигиена": "солнцезащитный крем, после солнца",
            "техника": "водонепроницаемый чехол для телефона",
        },
        "горы": {
            "одежда": "термобельё, флиска, штормовка, запасные носки",
            "обувь": "треккинговые ботинки с жёсткой подошвой",
            "аксессуары": "перчатки, бафф, очки",
            "документы": "ID, контакты спасательных служб",
            "гигиена": "компактный набор, пластыри",
            "техника": "фонарик, внешний аккумулятор",
        },
        "командировка": {
            "одежда": "2 рубашки, 1 пиджак, запасной галстук/шарф — по дресс-коду",
            "обувь": "классические туфли + складные кроссовки для дороги",
            "аксессуары": "портфель для документов",
            "документы": "билеты, приглашения, визитки",
            "гигиена": "дорожный набор",
            "техника": "ноутбук, переходники, HDMI/USB-C",
        },
        "уикенд": {
            "одежда": "2 комплекта на день + 1 «на вечер»",
            "обувь": "одна универсальная пара",
            "аксессуары": "сумка через плечо",
            "документы": "ID, банковские карты",
            "гигиена": "дорожный формат",
            "техника": "наушники, зарядка",
        },
    }

    pack = templates.get(tt, templates["город"])

    header = f"Чемодан на ~{days} дн.: сценарий «{tt}».\n\n"
    hint = _profile_hint(record)

    lines = (
        "Вот что стоит взять с собой:\n"
        f"👕 Одежда: {pack['одежда']} (+ нижнее бельё ×{underwear}, носки ×{socks})\n"
        f"👟 Обувь: {pack['обувь']}\n"
        f"🧴 Гигиена: {pack['гигиена']}\n"
        f"📄 Документы: {pack['документы']}\n"
        f"🔌 Техника: {pack['техника']}\n"
        f"🧣 Аксессуары: {pack['аксессуары']}\n"
    )

    tail = "\n💡 Совет: сложи вещи слоями и оставь место под сувениры.\n"
    return hint + header + lines + tail


def apply_preference_updates(text: str, record: UserRecord) -> list[str]:
    """Обновляет профиль по свободному тексту. Возвращает список изменений для ответа."""
    changes: list[str] = []
    t = _normalize(text)

    st = parse_style_from_text(text)
    if st and ("стиль" in t or "мой стиль" in t or st in t):
        record.preferred_style = st
        changes.append(f"стиль: {st}")

    fav, bad = parse_colors_from_text(text)
    if fav:
        for c in fav:
            if c not in record.favorite_colors:
                record.favorite_colors.append(c)
        changes.append("любимые цвета обновлены")
    if bad:
        for c in bad:
            if c not in record.disliked_colors:
                record.disliked_colors.append(c)
        changes.append("нелюбимые цвета учтены")

    g = parse_gender_note(text)
    if g:
        record.gender = g
        changes.append(f"пол: {g}")

    sn = parse_seasonal_note(text)
    if sn:
        record.seasonal_preferences = sn[:300]
        changes.append("сезонные предпочтения сохранены")

    # Общие заметки: если явно «заметк» или короткая фраза без других срабатываний
    if "заметк" in t and ":" in text:
        record.notes = text.split(":", 1)[1].strip()[:500]
        changes.append("заметки обновлены")

    return changes


def format_profile(record: UserRecord) -> str:
    lines = [
        "📇 Твой профиль StyleBot:",
        f"• Имя в Telegram: @{record.username or '—'}",
        f"• Стиль: {record.preferred_style or 'не указан'}",
        f"• Любимые цвета: {', '.join(record.favorite_colors) or '—'}",
        f"• Нелюбимые цвета: {', '.join(record.disliked_colors) or '—'}",
        f"• Пол: {record.gender or 'не указан'}",
        f"• Сезонные предпочтения: {record.seasonal_preferences or '—'}",
        f"• Заметки: {record.notes or '—'}",
        "",
        "👗 Гардероб (последние записи):",
    ]
    if not record.wardrobe:
        lines.append("— пока пусто. Напиши, например: «У меня есть чёрное пальто».")
    else:
        for w in record.wardrobe[-10:]:
            lines.append(f"• [{w.get('category', '?')}] {w.get('item', '')}")

    if record.history:
        lines.extend(["", "🕘 Последние запросы:"])
        for h in record.history[-5:]:
            lines.append(f"— ({h.get('query_type')}) {h.get('text', '')[:80]}")

    return "\n".join(lines)


def unknown_reply() -> str:
    return (
        "Я пока не уверен, как лучше помочь с этим запросом 🤔\n\n"
        "Попробуй, например:\n"
        "• «Что надеть в офис при +18?»\n"
        "• «Подбери образ на свидание вечером»\n"
        "• «Собери чемодан в Рим на 5 дней»\n"
        "• «Мой стиль casual» / «Я не люблю красный цвет»\n\n"
        "Команды: /help, /examples"
    )


# ---------------------------------------------------------------------------
# Обработчики Telegram
# ---------------------------------------------------------------------------


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Приветствие и краткое описание."""
    try:
        user = update.effective_user
        record = get_user_record(user.id)
        record.username = user.username
        upsert_user_record(record)

        text = (
            "Привет! Я StyleBot — твой небольшой помощник по стилю 👋\n\n"
            "Я умею:\n"
            "• подбирать образ на день с учётом погоды и события;\n"
            "• подсказывать, что взять в поездку;\n"
            "• запоминать твои предпочтения и вещи из гардероба.\n\n"
            "Пиши обычным языком или открой /help для команд.\n"
            "Примеры: /examples"
        )
        await update.message.reply_text(text)
    except Exception as exc:  # noqa: BLE001 — верхний уровень: логируем любые сбои Telegram
        logger.exception("Ошибка в /start: %s", exc)
        if update.message:
            await update.message.reply_text("Упс, что-то пошло не так. Попробуй ещё раз чуть позже.")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Справка по командам."""
    try:
        text = (
            "Команды StyleBot:\n"
            "/start — знакомство\n"
            "/help — эта справка\n"
            "/look — как лучше описать запрос на образ\n"
            "/trip — как описать поездку\n"
            "/profile — твои сохранённые данные\n"
            "/setstyle — выбрать стиль кнопками\n"
            "/setcolors — шаблон для любимых/нелюбимых цветов\n"
            "/wardrobe — как добавить вещь в гардероб\n"
            "/reset — очистить сохранённые данные\n"
            "/examples — примеры фраз\n"
        )
        await update.message.reply_text(text)
    except Exception as exc:
        logger.exception("Ошибка в /help: %s", exc)


async def cmd_look(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await update.message.reply_text(
            "Опиши ситуацию одной фразой: погода, место, стиль.\n"
            "Пример: «Что надеть в университет при +8 и дожде?»"
        )
    except Exception as exc:
        logger.exception("Ошибка в /look: %s", exc)


async def cmd_trip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await update.message.reply_text(
            "Напиши куда, на сколько дней и тип отдыха.\n"
            "Пример: «Собери чемодан в Рим на 5 дней» или «На море на неделю»."
        )
    except Exception as exc:
        logger.exception("Ошибка в /trip: %s", exc)


async def cmd_examples(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        text = (
            "Примеры запросов:\n\n"
            "👔 Образ:\n"
            "— Что надеть в офис летом?\n"
            "— Подбери образ на свидание вечером\n\n"
            "🧳 Поездка:\n"
            "— Собери чемодан в горы на 3 дня\n"
            "— Еду в Петербург на выходные\n\n"
            "💾 Профиль:\n"
            "— Мой стиль elegant\n"
            "— Я не люблю оранжевый цвет\n"
            "— У меня есть бежевое пальто и белые кроссовки"
        )
        await update.message.reply_text(text)
    except Exception as exc:
        logger.exception("Ошибка в /examples: %s", exc)


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user = update.effective_user
        record = get_user_record(user.id)
        record.username = user.username
        upsert_user_record(record)
        await update.message.reply_text(format_profile(record))
    except Exception as exc:
        logger.exception("Ошибка в /profile: %s", exc)


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        uid = update.effective_user.id
        storage = load_storage()
        storage.setdefault("users", {})
        if str(uid) in storage["users"]:
            del storage["users"][str(uid)]
            save_storage(storage)
        await update.message.reply_text("Готово — сохранённые данные очищены 🧹")
    except Exception as exc:
        logger.exception("Ошибка в /reset: %s", exc)


async def cmd_setstyle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inline-кнопки для выбора стиля."""
    try:
        keyboard = [
            [InlineKeyboardButton(s, callback_data=f"style:{s}") for s in STYLES[:3]],
            [InlineKeyboardButton(s, callback_data=f"style:{s}") for s in STYLES[3:]],
        ]
        await update.message.reply_text(
            "Выбери предпочитаемый стиль:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception as exc:
        logger.exception("Ошибка в /setstyle: %s", exc)


async def on_style_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        query = update.callback_query
        await query.answer()
        data = query.data or ""
        if not data.startswith("style:"):
            return
        style = data.split(":", 1)[1]
        if style not in STYLES:
            return
        user = query.from_user
        record = get_user_record(user.id)
        record.username = user.username
        record.preferred_style = style
        upsert_user_record(record)
        await query.edit_message_text(text=f"Сохранила стиль: {style} ✅")
    except Exception as exc:
        logger.exception("Ошибка в callback стиля: %s", exc)


async def cmd_setcolors(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await update.message.reply_text(
            "Отправь одним сообщением, например:\n"
            "Любимые: синий, белый. Нелюбимые: красный.\n\n"
            "Или: «Я не люблю зелёный цвет» — тоже сработает."
        )
    except Exception as exc:
        logger.exception("Ошибка в /setcolors: %s", exc)


async def cmd_wardrobe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await update.message.reply_text(
            "Напиши простым языком:\n"
            "«У меня есть чёрное пальто»\n"
            "«У меня есть белые кроссовки»\n\n"
            "Я постараюсь определить категорию автоматически."
        )
    except Exception as exc:
        logger.exception("Ошибка в /wardrobe: %s", exc)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Свободный текст: маршрутизация по намерению."""
    if not update.message or not update.message.text:
        return

    raw_text = update.message.text
    user = update.effective_user

    try:
        record = get_user_record(user.id)
        record.username = user.username

# Погодный сценарий: "погода в город"
city_for_weather = _extract_city_after_phrase(raw_text, "погода в")
if city_for_weather:
    try:
        temp = get_weather(city_for_weather)
        await update.message.reply_text(
            format_weather_response(city_for_weather, temp)
        )
    except Exception as exc:
        await update.message.reply_text(f"Ошибка: {str(exc)}")
    return

# Погодный сценарий: "что надеть в город"
city_for_outfit = _extract_city_after_phrase(raw_text, "что надеть в")
if city_for_outfit:
    try:
        temp = get_weather(city_for_outfit)
        await update.message.reply_text(
            format_weather_response(city_for_outfit, temp)
        )
    except Exception as exc:
        await update.message.reply_text(f"Ошибка: {str(exc)}")
    return

        intent = detect_intent(raw_text)

        # Явные «любимые/нелюбимые» без смены intent на outfit
        if intent == Intent.UNKNOWN:
            if "любимые:" in _normalize(raw_text) or "нелюбимые:" in _normalize(raw_text):
                intent = Intent.SAVE_PREFS

        if intent == Intent.SHOW_PROFILE:
            await update.message.reply_text(format_profile(record))
            return

        if intent == Intent.SAVE_PREFS:
            changes = apply_preference_updates(raw_text, record)
            if not changes:
                # Попытка разобрать формат /setcolors
                fav, bad = parse_colors_from_text(raw_text)
                if fav or bad:
                    if fav:
                        for c in fav:
                            if c not in record.favorite_colors:
                                record.favorite_colors.append(c)
                    if bad:
                        for c in bad:
                            if c not in record.disliked_colors:
                                record.disliked_colors.append(c)
                    changes.append("цвета обновлены")
            if changes:
                upsert_user_record(record)
                await update.message.reply_text("Сохранила: " + "; ".join(changes) + " 💾")
            else:
                await update.message.reply_text(
                    "Не смогла вытащить предпочтения из сообщения. Пример: «Мой стиль casual»."
                )
            return

        if intent == Intent.SAVE_WARDROBE:
            parsed = parse_wardrobe_addition(raw_text)
            if parsed:
                cat, item = parsed
                record.wardrobe.append({"category": cat, "item": item})
                upsert_user_record(record)
                await update.message.reply_text(f"Добавила в гардероб [{cat}]: {item} ✅")
            else:
                await update.message.reply_text("Опиши, пожалуйста, чуть явнее: «У меня есть ...»")
            return

        if intent == Intent.TRIP:
            reply = build_trip_reply(raw_text, record)
            append_history(record, raw_text, Intent.TRIP)
            upsert_user_record(record)
            await update.message.reply_text(reply)
            return

        if intent == Intent.OUTFIT:
            reply = build_outfit_reply(raw_text, record)
            append_history(record, raw_text, Intent.OUTFIT)
            upsert_user_record(record)
            await update.message.reply_text(reply)
            return

        # Дополнительная эвристика: короткие вопросы «что надеть»
        if "надеть" in _normalize(raw_text):
            reply = build_outfit_reply(raw_text, record)
            append_history(record, raw_text, Intent.OUTFIT)
            upsert_user_record(record)
            await update.message.reply_text(reply)
            return

        await update.message.reply_text(unknown_reply())

    except Exception as exc:
        logger.exception("Ошибка обработки текста: %s", exc)
        await update.message.reply_text("Произошла ошибка при обработке сообщения. Попробуй ещё раз.")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Глобальный обработчик ошибок PTB."""
    logger.error("Exception while handling update:", exc_info=context.error)


def main() -> None:
    """Точка входа: проверка токена и запуск polling."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        logger.error("Не задан TELEGRAM_BOT_TOKEN. Создай файл .env по образцу .env.example")
        raise SystemExit(1)

    application = Application.builder().token(token).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("look", cmd_look))
    application.add_handler(CommandHandler("trip", cmd_trip))
    application.add_handler(CommandHandler("profile", cmd_profile))
    application.add_handler(CommandHandler("setstyle", cmd_setstyle))
    application.add_handler(CommandHandler("setcolors", cmd_setcolors))
    application.add_handler(CommandHandler("wardrobe", cmd_wardrobe))
    application.add_handler(CommandHandler("reset", cmd_reset))
    application.add_handler(CommandHandler("examples", cmd_examples))

    application.add_handler(CallbackQueryHandler(on_style_callback, pattern=r"^style:"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    application.add_error_handler(on_error)

    logger.info("StyleBot запущен. Ожидаю сообщения…")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
