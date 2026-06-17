import logging
import json
import asyncio
import requests
import base64
import nest_asyncio
from datetime import datetime, timedelta
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes
)

# ============================================================
# ВСТАВЬ СВОИ КЛЮЧИ ЗДЕСЬ
# ============================================================
TELEGRAM_TOKEN = "8990506089:AAFnbITzx3DmtkvlQH3_OK8q0Iy92_U5YzA"
GROQ_API_KEY = "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"
# ============================================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

user_diaries = {}
user_states = {}
user_profiles = {}


def get_today():
    return datetime.now().strftime("%Y-%m-%d")


def get_diary(user_id, date=None):
    d = date or get_today()
    if user_id not in user_diaries:
        user_diaries[user_id] = {}
    if d not in user_diaries[user_id]:
        user_diaries[user_id][d] = []
    return user_diaries[user_id][d]


def add_to_diary(user_id, entry):
    get_diary(user_id).append(entry)


def format_diary(user_id):
    diary = get_diary(user_id)
    if not diary:
        return "Дневник пуст — ты ещё ничего не записал сегодня."
    lines = ["📋 *Дневник питания на сегодня:*\n"]
    total_cal = total_p = total_f = total_c = 0
    for i, e in enumerate(diary, 1):
        lines.append(f"{i}. {e['food']} — 🔥{e['calories']} ккал | Б:{e['protein']}г Ж:{e['fat']}г У:{e['carbs']}г")
        total_cal += e['calories']
        total_p += e['protein']
        total_f += e['fat']
        total_c += e['carbs']
    profile = user_profiles.get(user_id, {})
    norm = profile.get('norm', {})
    lines.append(f"\n*Итого:* 🔥{total_cal} ккал | Б:{total_p}г | Ж:{total_f}г | У:{total_c}г")
    if norm:
        left = norm.get('calories', 0) - total_cal
        lines.append(f"*Осталось до нормы:* {max(0, left)} ккал")
    return "\n".join(lines)


def format_week_stats(user_id):
    lines = ["📈 *Статистика за 7 дней:*\n"]
    total_cal = 0
    days_with_data = 0
    for i in range(7):
        date = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        diary = get_diary(user_id, date)
        if diary:
            day_cal = sum(e['calories'] for e in diary)
            day_p = sum(e['protein'] for e in diary)
            day_f = sum(e['fat'] for e in diary)
            day_c = sum(e['carbs'] for e in diary)
            label = "Сегодня" if i == 0 else f"{i} дн. назад"
            lines.append(f"*{label}:* 🔥{day_cal} ккал | Б:{day_p}г Ж:{day_f}г У:{day_c}г")
            total_cal += day_cal
            days_with_data += 1
    if days_with_data == 0:
        return "Пока нет данных. Начни записывать что ешь!"
    avg = total_cal // days_with_data
    lines.append(f"\n*Среднее в день:* {avg} ккал")
    return "\n".join(lines)


def format_profile(profile):
    lines = ["⚙️ *Твои предпочтения:*\n"]
    lines.append(f"🍽 Любишь: {profile.get('likes', 'не указано')}")
    lines.append(f"🚫 Не ешь: {profile.get('dislikes', 'не указано')}")
    lines.append(f"💰 Бюджет: {profile.get('budget', 'не указано')}")
    lines.append(f"⏰ Время: {profile.get('time', 'не указано')}")
    lines.append(f"👥 Человек: {profile.get('persons', 'не указано')}")
    return "\n".join(lines)


# ============================================================
# GROQ — АСИНХРОННЫЕ ВЫЗОВЫ
# ============================================================

def _ask_groq_sync(messages, max_tokens=2000):
    response = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json"
        },
        json={
            "model": "llama-3.3-70b-versatile",
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": max_tokens
        },
        timeout=30
    )
    return response.json()["choices"][0]["message"]["content"].strip()


async def ask_groq(messages, max_tokens=2000):
    return await asyncio.to_thread(_ask_groq_sync, messages, max_tokens)


def _ask_groq_vision_sync(photo_base64):
    response = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json"
        },
        json={
            "model": "meta-llama/llama-4-scout-17b-16e-instruct",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": """Ты нутрициолог. Определи продукт на фото и КБЖУ на порцию.
Ответь СТРОГО в формате JSON без пояснений:
{"food": "название", "calories": число, "protein": число, "fat": число, "carbs": число, "comment": "комментарий на русском"}
Числа целые."""},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{photo_base64}"}}
                ]
            }],
            "max_tokens": 500
        },
        timeout=30
    )
    return response.json()["choices"][0]["message"]["content"].strip()


async def ask_groq_vision(photo_base64):
    return await asyncio.to_thread(_ask_groq_vision_sync, photo_base64)


# ============================================================
# КЛАВИАТУРЫ
# ============================================================

def get_main_keyboard():
    keyboard = [
        [KeyboardButton("🍳 Что приготовить?"), KeyboardButton("📸 КБЖУ блюда")],
        [KeyboardButton("👪 КБЖУ на порцию"), KeyboardButton("🛒 Список покупок")],
        [KeyboardButton("⚖️ Моя норма КБЖУ"), KeyboardButton("🔥 Стрип питания")],
        [KeyboardButton("📊 Мой дневник"), KeyboardButton("📈 Статистика")],
        [KeyboardButton("⚙️ Предпочтения"), KeyboardButton("🗑 Очистить дневник")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def make_keyboard(options, skip=True):
    keyboard = [[KeyboardButton(opt)] for opt in options]
    if skip:
        keyboard.append([KeyboardButton("Пропустить")])
    keyboard.append([KeyboardButton("◀️ Назад")])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)


def make_choice_keyboard(options, extra=None):
    keyboard = [[KeyboardButton(opt)] for opt in options]
    if extra:
        keyboard.append([KeyboardButton(e) for e in extra])
    keyboard.append([KeyboardButton("◀️ Назад")])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)


# ============================================================
# ВОПРОСЫ
# ============================================================

PROFILE_QUESTIONS = [
    ("likes", "🍽 Что любишь есть? (через запятую)", None),
    ("dislikes", "🚫 Что не ешь или не любишь?", None),
    ("budget", "💰 Бюджет на еду в день?", ["До 300 руб", "300-600 руб", "600-1000 руб", "Без ограничений"]),
    ("time", "⏰ Время на готовку?", ["До 15 минут", "До 30 минут", "До часа", "Не ограничено"]),
    ("persons", "👥 На сколько человек готовишь?", ["1", "2", "3-4", "5+"]),
]

COOK_QUESTIONS = [
    ("meal_type", "🍽 Какой приём пищи?", ["Завтрак", "Обед", "Ужин", "Перекус", "Любой"]),
    ("days", "📅 На сколько дней?", ["1 день", "2-3 дня", "На неделю"]),
    ("mood", "😋 Есть пожелания по блюду?", None),
]

NORM_QUESTIONS = [
    ("gender", "👤 Пол?", ["Мужской", "Женский"]),
    ("age", "🎂 Возраст (лет)?", None),
    ("weight", "⚖️ Вес (кг)?", None),
    ("height", "📏 Рост (см)?", None),
    ("activity", "🏃 Активность?", ["Сидячая работа", "Лёгкая (1-3 тренировки)", "Средняя (3-5 тренировок)", "Высокая (каждый день)"]),
    ("goal", "🎯 Цель?", ["Похудеть", "Поддержать вес", "Набрать массу"]),
]

STRIP_QUESTIONS = [
    ("goal", "🎯 Твоя цель?", ["Похудеть", "Набрать массу", "Рельеф", "Здоровое питание"]),
    ("current", "📊 Как сейчас питаешься? (опиши или пропусти)", None),
]

PORTION_QUESTIONS = [
    ("food", "🍽 Что за блюдо?", None),
    ("weight", "⚖️ Сколько грамм?", None),
]

SHOPPING_QUESTIONS = [
    ("recipe", "📝 Название блюда для списка покупок:", None),
    ("persons", "👥 На сколько человек?", ["1", "2", "3-4", "5+"]),
]


# ============================================================
# ГЕНЕРАЦИЯ
# ============================================================

async def generate_variants(profile, cook_data, shown_dishes=None):
    days = cook_data.get('days', '1 день')
    persons = profile.get('persons', '1')
    exclude = ""
    if shown_dishes:
        exclude = f"\nНЕ предлагай эти блюда: {', '.join(shown_dishes)}"

    shelf_note = ""
    if "неделю" in days:
        shelf_note = "Блюда должны храниться 5-7 дней или замораживаться."
    elif "2-3" in days:
        shelf_note = "Блюда должны храниться 2-3 дня в холодильнике."

    prompt = f"""Ты шеф-повар. Предложи РОВНО 3 разных варианта блюд.
Приём пищи: {cook_data.get('meal_type', 'любой')}, на {days}, на {persons} человек.
Любит: {profile.get('likes', 'всё')}. Не ест: {profile.get('dislikes', 'ничего')}.
Бюджет: {profile.get('budget', 'средний')}. Время: {profile.get('time', 'до часа')}.
Пожелания: {cook_data.get('mood', 'нет')}.
{shelf_note}{exclude}

ПРАВИЛА: каждое блюдо самодостаточное (один белок + один гарнир), никаких двух круп, варианты разные по типу.

Ответь СТРОГО в формате (только 3 строки):
1. [Название] — [описание] — [время] — [стоимость]
2. [Название] — [описание] — [время] — [стоимость]
3. [Название] — [описание] — [время] — [стоимость]"""

    return await ask_groq([{"role": "user", "content": prompt}], max_tokens=500)


async def generate_full_recipe(dish_name, profile, cook_data):
    days = cook_data.get('days', '1 день')
    persons = profile.get('persons', '1')
    shelf = f"Укажи как хранить и разогревать (готовим на {days})." if "день" not in days else ""

    prompt = f"""Шеф-повар. Рецепт: "{dish_name}", на {persons} человек. {shelf}

🍽 *{dish_name}*
👥 Порций: X | ⏰ Время: X | 💰 Стоимость: X руб

📝 *Ингредиенты:*
[список]

👨‍🍳 *Приготовление:*
[пошаговый рецепт]

💡 *Совет:* [совет]"""

    return await ask_groq([{"role": "user", "content": prompt}], max_tokens=2000)


async def calculate_norm(data):
    prompt = f"""Рассчитай суточную норму КБЖУ по формуле Миффлина-Сан Жеора.
Пол: {data.get('gender')}, возраст: {data.get('age')}, вес: {data.get('weight')} кг, рост: {data.get('height')} см.
Активность: {data.get('activity')}, цель: {data.get('goal')}.
Ответь СТРОГО в формате JSON:
{{"calories": число, "protein": число, "fat": число, "carbs": число, "explanation": "объяснение"}}"""
    text = await ask_groq([{"role": "user", "content": prompt}], max_tokens=400)
    text = text.replace("```json", "").replace("```", "").strip()
    return json.loads(text)


async def generate_strip(data, profile):
    prompt = f"""Диетолог. Стрип питания под цель.
Цель: {data.get('goal')}. Питание сейчас: {data.get('current', 'не указано')}.
Любит: {profile.get('likes', 'всё')}, не ест: {profile.get('dislikes', 'ничего')}.
Бюджет: {profile.get('budget', 'средний')}.
Дай: принципы питания (3-5 пунктов), что есть/избегать, распределение КБЖУ, 3 совета."""
    return await ask_groq([{"role": "user", "content": prompt}], max_tokens=1500)


async def calculate_portion(food, weight):
    prompt = f"""КБЖУ для "{food}" весом {weight}г.
JSON: {{"calories": число, "protein": число, "fat": число, "carbs": число, "comment": "комментарий"}}"""
    text = await ask_groq([{"role": "user", "content": prompt}], max_tokens=300)
    text = text.replace("```json", "").replace("```", "").strip()
    return json.loads(text)


async def generate_shopping(recipe, persons):
    prompt = f"""Список продуктов для "{recipe}" на {persons} человек.
🛒 *Список: {recipe}* | 👥 {persons} чел
[категория]: - продукт — кол-во — цена
Итого: ~X руб"""
    return await ask_groq([{"role": "user", "content": prompt}], max_tokens=800)


async def show_variants(update, user_id, state):
    await update.message.reply_text("⏳ Подбираю варианты...")
    profile = user_profiles.get(user_id, {})
    shown = state.get("shown_dishes", [])
    variants = await generate_variants(profile, state["data"], shown)
    state["mode"] = "choose"
    state["profile"] = profile

    lines = [l.strip() for l in variants.strip().split("\n") if l.strip()]
    dishes = []
    for line in lines[:3]:
        parts = line.split("—")
        name = parts[0].strip().lstrip("123. ").strip()
        dishes.append(name)

    state["dishes"] = dishes
    shown.extend(dishes)
    state["shown_dishes"] = shown

    keyboard = make_choice_keyboard(dishes, extra=["🔄 Другие варианты"])
    await update.message.reply_text(
        "🍽 *Вот 3 варианта:*\n\n" + variants + "\n\nВыбери блюдо:",
        parse_mode="Markdown", reply_markup=keyboard
    )


# ============================================================
# СТАРТ
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id in user_states:
        del user_states[update.effective_user.id]
    await update.message.reply_text(
        "👋 Привет! Я твой кулинарный помощник.\n\nВыбери что хочешь сделать 👇",
        reply_markup=get_main_keyboard()
    )


# ============================================================
# ФОТО
# ============================================================

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = user_states.get(user_id, {})
    mode = state.get("mode")

    if mode not in (None, "kbzhu_photo"):
        await update.message.reply_text("Сейчас ответь на вопрос текстом 😊")
        return

    await update.message.reply_text("📸 Анализирую фото...")
    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        photo_bytes = await file.download_as_bytearray()
        photo_base64 = base64.b64encode(photo_bytes).decode("utf-8")
        text = await ask_groq_vision(photo_base64)
        text = text.replace("```json", "").replace("```", "").strip()
        result = json.loads(text)
        food_name = result.get("food", "Продукт")
        entry = {"food": food_name, "calories": result["calories"], "protein": result["protein"], "fat": result["fat"], "carbs": result["carbs"]}
        add_to_diary(user_id, entry)
        total_cal = sum(e["calories"] for e in get_diary(user_id))
        if user_id in user_states:
            del user_states[user_id]
        await update.message.reply_text(
            f"✅ *{food_name}*\n\n🔥 {result['calories']} ккал | Б:{result['protein']}г | Ж:{result['fat']}г | У:{result['carbs']}г\n\n💬 {result.get('comment','')}\n\n📊 Всего за день: *{total_cal} ккал*",
            parse_mode="Markdown", reply_markup=get_main_keyboard()
        )
    except Exception as e:
        logger.error(f"Фото: {e}")
        await update.message.reply_text("😕 Не смог распознать. Попробуй другое фото или напиши текстом.")


# ============================================================
# СООБЩЕНИЯ
# ============================================================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.effective_user.id
    state = user_states.get(user_id, {})
    mode = state.get("mode")

    if text == "◀️ Назад":
        if user_id in user_states:
            del user_states[user_id]
        await update.message.reply_text("Главное меню:", reply_markup=get_main_keyboard())
        return

    if text == "🍳 Что приготовить?":
        profile = user_profiles.get(user_id, {})
        if profile.get("likes"):
            user_states[user_id] = {"mode": "cook_confirm", "data": {}, "shown_dishes": []}
            summary = f"🍽 {profile.get('likes','—')}\n🚫 {profile.get('dislikes','—')}\n💰 {profile.get('budget','—')}\n⏰ {profile.get('time','—')}\n👥 {profile.get('persons','—')}"
            await update.message.reply_text(
                f"⚙️ *Твои предпочтения:*\n\n{summary}\n\nИспользовать или изменить?",
                parse_mode="Markdown",
                reply_markup=make_keyboard(["✅ Оставить прежние", "✏️ Изменить"], skip=False)
            )
        else:
            user_states[user_id] = {"mode": "cook_profile", "step": 0, "data": {}, "shown_dishes": []}
            _, q, opts = PROFILE_QUESTIONS[0]
            await update.message.reply_text("👋 Расскажи о себе!\n\n" + q, reply_markup=make_keyboard(opts))
        return

    if text == "📸 КБЖУ блюда":
        user_states[user_id] = {"mode": "kbzhu_photo"}
        await update.message.reply_text("📸 Отправь фото или напиши название блюда:", reply_markup=ReplyKeyboardMarkup([[KeyboardButton("◀️ Назад")]], resize_keyboard=True))
        return

    if text == "👪 КБЖУ на порцию":
        user_states[user_id] = {"mode": "portion", "step": 0, "data": {}}
        await update.message.reply_text("👪 *КБЖУ на порцию*\n\n🍽 Что за блюдо?", parse_mode="Markdown", reply_markup=ReplyKeyboardMarkup([[KeyboardButton("◀️ Назад")]], resize_keyboard=True))
        return

    if text == "🛒 Список покупок":
        user_states[user_id] = {"mode": "shopping", "step": 0, "data": {}}
        await update.message.reply_text("🛒 *Список покупок*\n\n📝 Название блюда:", parse_mode="Markdown", reply_markup=ReplyKeyboardMarkup([[KeyboardButton("◀️ Назад")]], resize_keyboard=True))
        return

    if text == "⚖️ Моя норма КБЖУ":
        user_states[user_id] = {"mode": "norm", "step": 0, "data": {}}
        _, q, opts = NORM_QUESTIONS[0]
        await update.message.reply_text("⚖️ *Норма КБЖУ*\n\n" + q, parse_mode="Markdown", reply_markup=make_keyboard(opts, skip=False))
        return

    if text == "🔥 Стрип питания":
        user_states[user_id] = {"mode": "strip", "step": 0, "data": {}}
        _, q, opts = STRIP_QUESTIONS[0]
        await update.message.reply_text("🔥 *Стрип питания*\n\n" + q, parse_mode="Markdown", reply_markup=make_keyboard(opts, skip=False))
        return

    if text == "📊 Мой дневник":
        await update.message.reply_text(format_diary(user_id), parse_mode="Markdown", reply_markup=get_main_keyboard())
        return

    if text == "📈 Статистика":
        await update.message.reply_text(format_week_stats(user_id), parse_mode="Markdown", reply_markup=get_main_keyboard())
        return

    if text == "⚙️ Предпочтения":
        profile = user_profiles.get(user_id, {})
        if profile.get("likes"):
            user_states[user_id] = {"mode": "profile_confirm"}
            await update.message.reply_text(
                format_profile(profile) + "\n\nЧто хочешь сделать?",
                parse_mode="Markdown",
                reply_markup=make_keyboard(["✏️ Изменить предпочтения"], skip=False)
            )
        else:
            user_states[user_id] = {"mode": "profile", "step": 0, "data": {}}
            _, q, opts = PROFILE_QUESTIONS[0]
            await update.message.reply_text("⚙️ *Настройка предпочтений*\n\n" + q, parse_mode="Markdown", reply_markup=make_keyboard(opts))
        return

    if text == "🗑 Очистить дневник":
        today = get_today()
        if user_id in user_diaries:
            user_diaries[user_id][today] = []
        await update.message.reply_text("🗑 Дневник очищен!", reply_markup=get_main_keyboard())
        return

    if mode == "cook_confirm":
        if text == "✅ Оставить прежние":
            user_states[user_id] = {"mode": "cook", "step": 0, "data": {}, "shown_dishes": state.get("shown_dishes", [])}
            _, q, opts = COOK_QUESTIONS[0]
            await update.message.reply_text(q, reply_markup=make_keyboard(opts))
        elif text == "✏️ Изменить":
            user_states[user_id] = {"mode": "cook_profile", "step": 0, "data": {}, "shown_dishes": []}
            _, q, opts = PROFILE_QUESTIONS[0]
            await update.message.reply_text(q, reply_markup=make_keyboard(opts))
        return

    if mode == "cook_profile":
        step = state["step"]
        key, _, _ = PROFILE_QUESTIONS[step]
        if text != "Пропустить":
            state["data"][key] = text
        step += 1
        state["step"] = step
        if step < len(PROFILE_QUESTIONS):
            _, q, opts = PROFILE_QUESTIONS[step]
            await update.message.reply_text(q, reply_markup=make_keyboard(opts) if opts else ReplyKeyboardMarkup([[KeyboardButton("Пропустить")], [KeyboardButton("◀️ Назад")]], resize_keyboard=True))
        else:
            if user_id not in user_profiles:
                user_profiles[user_id] = {}
            user_profiles[user_id].update(state["data"])
            user_states[user_id] = {"mode": "cook", "step": 0, "data": {}, "shown_dishes": state.get("shown_dishes", [])}
            _, q, opts = COOK_QUESTIONS[0]
            await update.message.reply_text("✅ Сохранено!\n\n" + q, reply_markup=make_keyboard(opts))
        return

    if mode == "cook":
        step = state["step"]
        key, _, _ = COOK_QUESTIONS[step]
        if text != "Пропустить":
            state["data"][key] = text
        step += 1
        state["step"] = step
        if step < len(COOK_QUESTIONS):
            _, q, opts = COOK_QUESTIONS[step]
            await update.message.reply_text(q, reply_markup=make_keyboard(opts) if opts else ReplyKeyboardMarkup([[KeyboardButton("Пропустить")], [KeyboardButton("◀️ Назад")]], resize_keyboard=True))
        else:
            try:
                await show_variants(update, user_id, state)
            except Exception as e:
                logger.error(e)
                del user_states[user_id]
                await update.message.reply_text("😕 Ошибка. Попробуй ещё раз.", reply_markup=get_main_keyboard())
        return

    if mode == "choose":
        if text == "🔄 Другие варианты":
            try:
                await show_variants(update, user_id, state)
            except Exception as e:
                logger.error(e)
                await update.message.reply_text("😕 Ошибка.")
            return

        await update.message.reply_text(f"👨‍🍳 Готовлю рецепт *{text}*...", parse_mode="Markdown")
        try:
            profile = state.get("profile", {})
            recipe = await generate_full_recipe(text, profile, state["data"])
            del user_states[user_id]
            await update.message.reply_text(recipe, parse_mode="Markdown", reply_markup=get_main_keyboard())
        except Exception as e:
            logger.error(e)
            del user_states[user_id]
            await update.message.reply_text("😕 Ошибка.", reply_markup=get_main_keyboard())
        return

    if mode == "kbzhu_photo":
        await update.message.reply_text("⏳ Считаю КБЖУ...")
        try:
            prompt = f"""КБЖУ для "{text}" на стандартную порцию.
JSON: {{"calories": число, "protein": число, "fat": число, "carbs": число, "comment": "комментарий"}}"""
            r = await ask_groq([{"role": "user", "content": prompt}], max_tokens=300)
            r = r.replace("```json", "").replace("```", "").strip()
            result = json.loads(r)
            entry = {"food": text, "calories": result["calories"], "protein": result["protein"], "fat": result["fat"], "carbs": result["carbs"]}
            add_to_diary(user_id, entry)
            total_cal = sum(e["calories"] for e in get_diary(user_id))
            del user_states[user_id]
            await update.message.reply_text(
                f"✅ *{text}*\n\n🔥 {result['calories']} ккал | Б:{result['protein']}г | Ж:{result['fat']}г | У:{result['carbs']}г\n\n💬 {result.get('comment','')}\n\n📊 Всего за день: *{total_cal} ккал*",
                parse_mode="Markdown", reply_markup=get_main_keyboard()
            )
        except Exception as e:
            logger.error(e)
            await update.message.reply_text("😕 Напиши точнее.")
        return

    if mode == "portion":
        step = state["step"]
        key, _, _ = PORTION_QUESTIONS[step]
        state["data"][key] = text
        step += 1
        state["step"] = step
        if step < len(PORTION_QUESTIONS):
            _, q, _ = PORTION_QUESTIONS[step]
            await update.message.reply_text(q)
        else:
            await update.message.reply_text("⏳ Считаю...")
            try:
                result = await calculate_portion(state["data"]["food"], state["data"]["weight"])
                food = state["data"]["food"]
                weight = state["data"]["weight"]
                entry = {"food": f"{food} {weight}г", "calories": result["calories"], "protein": result["protein"], "fat": result["fat"], "carbs": result["carbs"]}
                add_to_diary(user_id, entry)
                total_cal = sum(e["calories"] for e in get_diary(user_id))
                del user_states[user_id]
                await update.message.reply_text(
                    f"✅ *{food} — {weight}г*\n\n🔥 {result['calories']} ккал | Б:{result['protein']}г | Ж:{result['fat']}г | У:{result['carbs']}г\n\n💬 {result.get('comment','')}\n\n📊 Всего за день: *{total_cal} ккал*",
                    parse_mode="Markdown", reply_markup=get_main_keyboard()
                )
            except Exception as e:
                logger.error(e)
                del user_states[user_id]
                await update.message.reply_text("😕 Ошибка.", reply_markup=get_main_keyboard())
        return

    if mode == "shopping":
        step = state["step"]
        key, _, opts = SHOPPING_QUESTIONS[step]
        state["data"][key] = text
        step += 1
        state["step"] = step
        if step < len(SHOPPING_QUESTIONS):
            _, q, opts = SHOPPING_QUESTIONS[step]
            await update.message.reply_text(q, reply_markup=make_keyboard(opts, skip=False) if opts else ReplyKeyboardMarkup([[KeyboardButton("◀️ Назад")]], resize_keyboard=True))
        else:
            await update.message.reply_text("⏳ Составляю список...")
            try:
                result = await generate_shopping(state["data"].get("recipe", ""), state["data"].get("persons", "1"))
                del user_states[user_id]
                await update.message.reply_text(result, parse_mode="Markdown", reply_markup=get_main_keyboard())
            except Exception as e:
                logger.error(e)
                del user_states[user_id]
                await update.message.reply_text("😕 Ошибка.", reply_markup=get_main_keyboard())
        return

    if mode == "norm":
        step = state["step"]
        key, _, _ = NORM_QUESTIONS[step]
        state["data"][key] = text
        step += 1
        state["step"] = step
        if step < len(NORM_QUESTIONS):
            _, q, opts = NORM_QUESTIONS[step]
            await update.message.reply_text(q, reply_markup=make_keyboard(opts, skip=False) if opts else ReplyKeyboardMarkup([[KeyboardButton("◀️ Назад")]], resize_keyboard=True))
        else:
            await update.message.reply_text("⏳ Рассчитываю...")
            try:
                result = await calculate_norm(state["data"])
                if user_id not in user_profiles:
                    user_profiles[user_id] = {}
                user_profiles[user_id]["norm"] = result
                del user_states[user_id]
                await update.message.reply_text(
                    f"⚖️ *Твоя норма КБЖУ:*\n\n🔥 {result['calories']} ккал\n🥩 Белки: {result['protein']}г\n🧈 Жиры: {result['fat']}г\n🍞 Углеводы: {result['carbs']}г\n\n💬 {result.get('explanation','')}",
                    parse_mode="Markdown", reply_markup=get_main_keyboard()
                )
            except Exception as e:
                logger.error(e)
                del user_states[user_id]
                await update.message.reply_text("😕 Ошибка.", reply_markup=get_main_keyboard())
        return

    if mode == "strip":
        step = state["step"]
        key, _, _ = STRIP_QUESTIONS[step]
        if text != "Пропустить":
            state["data"][key] = text
        step += 1
        state["step"] = step
        if step < len(STRIP_QUESTIONS):
            _, q, opts = STRIP_QUESTIONS[step]
            await update.message.reply_text(q, reply_markup=make_keyboard(opts) if opts else ReplyKeyboardMarkup([[KeyboardButton("Пропустить")], [KeyboardButton("◀️ Назад")]], resize_keyboard=True))
        else:
            await update.message.reply_text("⏳ Составляю план...")
            try:
                profile = user_profiles.get(user_id, {})
                result = await generate_strip(state["data"], profile)
                del user_states[user_id]
                await update.message.reply_text(f"🔥 *Стрип питания:*\n\n{result}", parse_mode="Markdown", reply_markup=get_main_keyboard())
            except Exception as e:
                logger.error(e)
                del user_states[user_id]
                await update.message.reply_text("😕 Ошибка.", reply_markup=get_main_keyboard())
        return

    if mode == "profile_confirm":
        if text == "✏️ Изменить предпочтения":
            user_states[user_id] = {"mode": "profile", "step": 0, "data": {}}
            _, q, opts = PROFILE_QUESTIONS[0]
            await update.message.reply_text(q, reply_markup=make_keyboard(opts))
        return

    if mode == "profile":
        step = state["step"]
        key, _, _ = PROFILE_QUESTIONS[step]
        if text != "Пропустить":
            state["data"][key] = text
        step += 1
        state["step"] = step
        if step < len(PROFILE_QUESTIONS):
            _, q, opts = PROFILE_QUESTIONS[step]
            await update.message.reply_text(q, reply_markup=make_keyboard(opts) if opts else ReplyKeyboardMarkup([[KeyboardButton("Пропустить")], [KeyboardButton("◀️ Назад")]], resize_keyboard=True))
        else:
            if user_id not in user_profiles:
                user_profiles[user_id] = {}
            user_profiles[user_id].update(state["data"])
            del user_states[user_id]
            await update.message.reply_text("✅ Предпочтения сохранены!", reply_markup=get_main_keyboard())
        return

    # КБЖУ по тексту
    await update.message.reply_text("⏳ Считаю КБЖУ...")
    try:
        prompt = f"""КБЖУ для "{text}" на стандартную порцию.
JSON: {{"calories": число, "protein": число, "fat": число, "carbs": число, "comment": "комментарий"}}
Числа целые."""
        r = await ask_groq([{"role": "user", "content": prompt}], max_tokens=300)
        r = r.replace("```json", "").replace("```", "").strip()
        result = json.loads(r)
        entry = {"food": text, "calories": result["calories"], "protein": result["protein"], "fat": result["fat"], "carbs": result["carbs"]}
        add_to_diary(user_id, entry)
        total_cal = sum(e["calories"] for e in get_diary(user_id))
        await update.message.reply_text(
            f"✅ *{text}*\n\n🔥 {result['calories']} ккал | Б:{result['protein']}г | Ж:{result['fat']}г | У:{result['carbs']}г\n\n💬 {result.get('comment','')}\n\n📊 Всего за день: *{total_cal} ккал*",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(e)
        await update.message.reply_text("😕 Напиши точнее, например: *борщ 300г*", parse_mode="Markdown")


async def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("✅ Бот запущен!")
    await app.run_polling()


if __name__ == "__main__":
    nest_asyncio.apply()
    asyncio.get_event_loop().run_until_complete(main())
