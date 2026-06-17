import logging
import json
import asyncio
import requests
import base64
import os
from datetime import datetime, timedelta
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes
)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

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
                    {"type": "text", "text": """Определи продукт или блюдо на фото и посчитай КБЖУ на порцию.
Ответь СТРОГО в формате JSON без лишнего текста:
{"food": "название на русском", "calories": число, "protein": число, "fat": число, "carbs": число, "comment": "короткий комментарий"}
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


def get_main_keyboard():
    keyboard = [
        [KeyboardButton("🍳 Что приготовить?"), KeyboardButton("📸 КБЖУ блюда")],
        [KeyboardButton("👪 КБЖУ на порцию"), KeyboardButton("🛒 Список покупок")],
        [KeyboardButton("⚖️ Моя норма КБЖУ"), KeyboardButton("🔥 План питания")],
        [KeyboardButton("📊 Мой дневник"), KeyboardButton("📈 Статистика")],
        [KeyboardButton("⚙️ Предпочтения"), KeyboardButton("🗑 Очистить дневник")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def make_keyboard(options, skip=True):
    if not options:
        options = []
    keyboard = [[KeyboardButton(opt)] for opt in options]
    if skip:
        keyboard.append([KeyboardButton("Пропустить")])
    keyboard.append([KeyboardButton("◀️ Назад")])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)


def make_choice_keyboard(options, extra=None):
    if not options:
        options = []
    keyboard = [[KeyboardButton(opt)] for opt in options]
    if extra:
        keyboard.append([KeyboardButton(e) for e in extra])
    keyboard.append([KeyboardButton("◀️ Назад")])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)


PROFILE_QUESTIONS = [
    ("likes", "Что любишь есть? Напиши через запятую — например: суп, картошка, курица", None),
    ("dislikes", "Что не ешь или не любишь?", None),
    ("budget", "Сколько в день тратишь на еду?", ["До 300 руб", "300–600 руб", "600–1000 руб", "Без ограничений"]),
    ("time", "Сколько времени готов потратить на готовку?", ["До 15 минут", "До 30 минут", "До часа", "Не ограничено"]),
    ("persons", "На сколько человек обычно готовишь?", ["1", "2", "3–4", "5+"]),
]

COOK_QUESTIONS = [
    ("meal_type", "Что готовим?", ["Завтрак", "Обед", "Ужин", "Перекус", "Неважно"]),
    ("days", "На сколько дней?", ["На сегодня", "На 2–3 дня", "На всю неделю"]),
    ("ingredients", "Есть продукты дома которые надо использовать? Напиши что есть — или нажми Пропустить", None),
    ("mood", "Есть пожелания? Например: что-то лёгкое, суп, мясное... Или пропусти", None),
]

NORM_QUESTIONS = [
    ("gender", "Укажи пол:", ["Мужской", "Женский"]),
    ("age", "Сколько лет?", None),
    ("weight", "Вес в кг?", None),
    ("height", "Рост в см?", None),
    ("activity", "Как проходит твой день?", ["Сижу за компьютером", "Немного хожу пешком", "Работаю физически", "Активно тренируюсь"]),
    ("goal", "Какая цель?", ["Похудеть", "Держать вес", "Набрать массу"]),
]

STRIP_QUESTIONS = [
    ("goal", "Какая цель?", ["Похудеть", "Набрать массу", "Рельеф", "Просто питаться правильно"]),
    ("current", "Как сейчас питаешься? Опиши коротко или пропусти", None),
]

PORTION_QUESTIONS = [
    ("food", "Что за блюдо?", None),
    ("weight", "Сколько грамм?", None),
]

SHOPPING_QUESTIONS = [
    ("recipe", "Для какого блюда составить список покупок?", None),
    ("persons", "На сколько человек?", ["1", "2", "3–4", "5+"]),
]


async def generate_variants(profile, cook_data, shown_dishes=None):
    days = cook_data.get('days', 'на сегодня')
    persons = profile.get('persons', '1')
    ingredients = cook_data.get('ingredients', '')
    exclude = f"\nНЕ предлагай эти блюда (уже показаны): {', '.join(shown_dishes)}" if shown_dishes else ""

    shelf_note = ""
    if "неделю" in days:
        shelf_note = "Блюда должны хорошо храниться 4–5 дней в холодильнике или морозилке."
    elif "2–3" in days:
        shelf_note = "Блюда должны храниться 2–3 дня в холодильнике."

    ingredients_note = f"\nВАЖНО: пользователь хочет использовать эти продукты которые уже есть дома: {ingredients}. Предлагай блюда именно из них или с минимальными добавками." if ingredients else ""

    prompt = f"""Ты помогаешь обычному человеку решить что приготовить дома на обычной кухне.
Предложи РОВНО 3 варианта простой домашней еды.

Данные:
- Приём пищи: {cook_data.get('meal_type', 'неважно')}
- Готовим: {days}, на {persons} человек
- Любит: {profile.get('likes', 'обычную домашнюю еду')}
- Не ест: {profile.get('dislikes', 'ничего особенного')}
- Бюджет: {profile.get('budget', 'средний')}
- Время: {profile.get('time', 'до часа')}
- Пожелания: {cook_data.get('mood', 'нет')}
{ingredients_note}
{shelf_note}
{exclude}

ПРАВИЛА:
- Только простая домашняя еда: борщ, щи, картошка с мясом, макароны с подливой, гречка с котлетой, жаркое, омлет, блины, пюре, тушёная капуста и т.д.
- Никакой ресторанной еды и экзотики
- Продукты из обычного магазина или рынка
- Каждое блюдо логичное: суп — это суп, второе — белок плюс один гарнир
- Три варианта разные (например: суп, второе блюдо, быстрый вариант)

Ответь СТРОГО в формате (только 3 строки, без лишнего текста):
1. [Название] — [одно предложение что это] — [время] — [примерная стоимость]
2. [Название] — [одно предложение что это] — [время] — [примерная стоимость]
3. [Название] — [одно предложение что это] — [время] — [примерная стоимость]"""

    return await ask_groq([{"role": "user", "content": prompt}], max_tokens=500)


async def generate_full_recipe(dish_name, profile, cook_data):
    days = cook_data.get('days', 'на сегодня')
    persons = profile.get('persons', '1')
    ingredients = cook_data.get('ingredients', '')
    shelf = f"Блюдо готовим на {days} — укажи как хранить и разогревать." if "сегодня" not in days else ""
    ing_note = f"Используй в рецепте эти продукты которые есть дома: {ingredients}." if ingredients else ""

    prompt = f"""Напиши простой домашний рецепт блюда "{dish_name}" на {persons} человек.
{ing_note}
{shelf}

Формат ответа:

🍽 *{dish_name}*
👥 Порций: X | ⏰ Время: X мин | 💰 ~X руб

📝 *Что нужно:*
— ингредиент — количество
(список всех ингредиентов)

👨‍🍳 *Как готовить:*
1. первый шаг
2. второй шаг
(и так далее, простыми словами)

{"🧊 *Хранение:* [как хранить и разогревать]" if "сегодня" not in days else ""}

💡 *Совет:* [один простой совет]"""

    return await ask_groq([{"role": "user", "content": prompt}], max_tokens=2000)


async def calculate_norm(data):
    prompt = f"""Рассчитай суточную норму КБЖУ.
Пол: {data.get('gender')}, возраст: {data.get('age')} лет, вес: {data.get('weight')} кг, рост: {data.get('height')} см.
Активность: {data.get('activity')}, цель: {data.get('goal')}.
Используй формулу Миффлина-Сан Жеора.
Ответь СТРОГО в формате JSON без лишнего текста:
{{"calories": число, "protein": число, "fat": число, "carbs": число, "explanation": "простое объяснение на русском"}}"""
    text = await ask_groq([{"role": "user", "content": prompt}], max_tokens=400)
    text = text.replace("```json", "").replace("```", "").strip()
    return json.loads(text)


async def generate_strip(data, profile):
    prompt = f"""Составь простой план питания для обычного человека.
Цель: {data.get('goal')}.
Как питается сейчас: {data.get('current', 'не указано')}.
Любит: {profile.get('likes', 'обычную еду')}, не ест: {profile.get('dislikes', 'ничего особенного')}.
Бюджет: {profile.get('budget', 'средний')}.

Напиши простым языком без медицинских терминов:
1. Главные принципы питания под эту цель (3–4 пункта)
2. Что есть чаще, что реже
3. Примерное распределение по дням
4. 3 простых совета которые легко выполнить"""
    return await ask_groq([{"role": "user", "content": prompt}], max_tokens=1500)


async def calculate_portion(food, weight):
    prompt = f"""Посчитай КБЖУ для "{food}" весом {weight} грамм.
Ответь СТРОГО в формате JSON:
{{"calories": число, "protein": число, "fat": число, "carbs": число, "comment": "короткий комментарий"}}
Числа целые."""
    text = await ask_groq([{"role": "user", "content": prompt}], max_tokens=300)
    text = text.replace("```json", "").replace("```", "").strip()
    return json.loads(text)


async def generate_shopping(recipe, persons):
    prompt = f"""Составь список продуктов для покупки чтобы приготовить "{recipe}" на {persons} человек.
Простой список из обычного магазина.

Формат:
🛒 *Список покупок: {recipe}*
👥 На {persons} чел.

Мясо/рыба:
— продукт — количество — ~цена

Овощи:
— ...

Крупы/макароны:
— ...

Другое:
— ...

💰 Итого: ~X руб"""
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
        "🍽 *Вот 3 варианта:*\n\n" + variants + "\n\nВыбери что приготовить:",
        parse_mode="Markdown", reply_markup=keyboard
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id in user_states:
        del user_states[update.effective_user.id]
    await update.message.reply_text(
        "👋 Привет! Я помогу решить главный вопрос дня — что приготовить 😄\n\n"
        "Умею:\n"
        "🍳 Предлагать простые домашние рецепты\n"
        "📸 Считать КБЖУ по фото или тексту\n"
        "🛒 Составлять список покупок\n"
        "📊 Вести дневник питания\n\n"
        "Выбери что хочешь 👇",
        reply_markup=get_main_keyboard()
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = user_states.get(user_id, {})
    mode = state.get("mode")

    if mode not in (None, "kbzhu_photo"):
        await update.message.reply_text("Сейчас ответь на вопрос текстом 😊")
        return

    await update.message.reply_text("📸 Смотрю что на фото...")
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
            f"✅ *{food_name}*\n\n"
            f"🔥 {result['calories']} ккал | Б:{result['protein']}г | Ж:{result['fat']}г | У:{result['carbs']}г\n\n"
            f"💬 {result.get('comment','')}\n\n"
            f"📊 Всего за день: *{total_cal} ккал*",
            parse_mode="Markdown", reply_markup=get_main_keyboard()
        )
    except Exception as e:
        logger.error(f"Фото: {e}")
        await update.message.reply_text("😕 Не смог распознать. Попробуй другое фото или напиши текстом.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.effective_user.id
    state = user_states.get(user_id, {})
    mode = state.get("mode")

    if text == "◀️ Назад":
        if user_id in user_states:
            del user_states[user_id]
        await update.message.reply_text("Главное меню 👇", reply_markup=get_main_keyboard())
        return

    if text == "🍳 Что приготовить?":
        profile = user_profiles.get(user_id, {})
        if profile.get("likes"):
            user_states[user_id] = {"mode": "cook_confirm", "data": {}, "shown_dishes": []}
            summary = (f"🍽 {profile.get('likes','—')}\n"
                      f"🚫 {profile.get('dislikes','—')}\n"
                      f"💰 {profile.get('budget','—')}\n"
                      f"⏰ {profile.get('time','—')}\n"
                      f"👥 {profile.get('persons','—')}")
            await update.message.reply_text(
                f"У меня уже есть твои предпочтения:\n\n{summary}\n\nИспользовать их?",
                reply_markup=make_keyboard(["✅ Да, использовать", "✏️ Изменить"], skip=False)
            )
        else:
            user_states[user_id] = {"mode": "cook_profile", "step": 0, "data": {}, "shown_dishes": []}
            _, q, opts = PROFILE_QUESTIONS[0]
            await update.message.reply_text(
                "Давай я узнаю немного о тебе — это поможет подбирать подходящие рецепты 😊\n\n" + q,
                reply_markup=make_keyboard(opts) if opts else make_keyboard([])
            )
        return

    if text == "📸 КБЖУ блюда":
        user_states[user_id] = {"mode": "kbzhu_photo"}
        await update.message.reply_text(
            "Отправь фото еды или упаковки — посчитаю КБЖУ.\nИли напиши название блюда текстом 👇",
            reply_markup=ReplyKeyboardMarkup([[KeyboardButton("◀️ Назад")]], resize_keyboard=True)
        )
        return

    if text == "👪 КБЖУ на порцию":
        user_states[user_id] = {"mode": "portion", "step": 0, "data": {}}
        await update.message.reply_text(
            "Считаю КБЖУ на конкретную порцию.\n\nЧто за блюдо?",
            reply_markup=ReplyKeyboardMarkup([[KeyboardButton("◀️ Назад")]], resize_keyboard=True)
        )
        return

    if text == "🛒 Список покупок":
        user_states[user_id] = {"mode": "shopping", "step": 0, "data": {}}
        await update.message.reply_text(
            "Составлю список продуктов для покупки.\n\nДля какого блюда?",
            reply_markup=ReplyKeyboardMarkup([[KeyboardButton("◀️ Назад")]], resize_keyboard=True)
        )
        return

    if text == "⚖️ Моя норма КБЖУ":
        user_states[user_id] = {"mode": "norm", "step": 0, "data": {}}
        _, q, opts = NORM_QUESTIONS[0]
        await update.message.reply_text(
            "Рассчитаю твою суточную норму КБЖУ 📊\n\n" + q,
            reply_markup=make_keyboard(opts, skip=False)
        )
        return

    if text == "🔥 План питания":
        user_states[user_id] = {"mode": "strip", "step": 0, "data": {}}
        _, q, opts = STRIP_QUESTIONS[0]
        await update.message.reply_text(
            "Составлю простой план питания под твою цель 🔥\n\n" + q,
            reply_markup=make_keyboard(opts, skip=False)
        )
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
            await update.message.reply_text(
                "⚙️ Настроим предпочтения — это поможет подбирать подходящие рецепты.\n\n" + q,
                reply_markup=make_keyboard(opts) if opts else make_keyboard([])
            )
        return

    if text == "🗑 Очистить дневник":
        today = get_today()
        if user_id in user_diaries:
            user_diaries[user_id][today] = []
        await update.message.reply_text("🗑 Дневник очищен!", reply_markup=get_main_keyboard())
        return

    # ПОДТВЕРЖДЕНИЕ ПРЕДПОЧТЕНИЙ
    if mode == "cook_confirm":
        if text == "✅ Да, использовать":
            user_states[user_id] = {"mode": "cook", "step": 0, "data": {}, "shown_dishes": state.get("shown_dishes", [])}
            _, q, opts = COOK_QUESTIONS[0]
            await update.message.reply_text(q, reply_markup=make_keyboard(opts, skip=False))
        elif text == "✏️ Изменить":
            user_states[user_id] = {"mode": "cook_profile", "step": 0, "data": {}, "shown_dishes": []}
            _, q, opts = PROFILE_QUESTIONS[0]
            await update.message.reply_text(q, reply_markup=make_keyboard(opts) if opts else make_keyboard([]))
        return

    # СБОР ПРОФИЛЯ ПЕРЕД ГОТОВКОЙ
    if mode == "cook_profile":
        step = state["step"]
        key, _, _ = PROFILE_QUESTIONS[step]
        if text != "Пропустить":
            state["data"][key] = text
        step += 1
        state["step"] = step
        if step < len(PROFILE_QUESTIONS):
            _, q, opts = PROFILE_QUESTIONS[step]
            await update.message.reply_text(q, reply_markup=make_keyboard(opts) if opts else make_keyboard([]))
        else:
            if user_id not in user_profiles:
                user_profiles[user_id] = {}
            user_profiles[user_id].update(state["data"])
            user_states[user_id] = {"mode": "cook", "step": 0, "data": {}, "shown_dishes": state.get("shown_dishes", [])}
            _, q, opts = COOK_QUESTIONS[0]
            await update.message.reply_text("Отлично, запомнил! 👍\n\n" + q, reply_markup=make_keyboard(opts, skip=False))
        return

    # ВОПРОСЫ ЧТО ПРИГОТОВИТЬ
    if mode == "cook":
        step = state["step"]
        key, _, _ = COOK_QUESTIONS[step]
        if text != "Пропустить":
            state["data"][key] = text
        step += 1
        state["step"] = step
        if step < len(COOK_QUESTIONS):
            _, q, opts = COOK_QUESTIONS[step]
            await update.message.reply_text(q, reply_markup=make_keyboard(opts) if opts else make_keyboard([]))
        else:
            try:
                await show_variants(update, user_id, state)
            except Exception as e:
                logger.error(e)
                del user_states[user_id]
                await update.message.reply_text("😕 Что-то пошло не так. Попробуй ещё раз.", reply_markup=get_main_keyboard())
        return

    # ВЫБОР БЛЮДА
    if mode == "choose":
        if text == "🔄 Другие варианты":
            try:
                await show_variants(update, user_id, state)
            except Exception as e:
                logger.error(e)
                await update.message.reply_text("😕 Что-то пошло не так.")
            return

        await update.message.reply_text(f"Отлично! Готовлю рецепт для *{text}*... ⏳", parse_mode="Markdown")
        try:
            profile = state.get("profile", {})
            recipe = await generate_full_recipe(text, profile, state["data"])
            del user_states[user_id]
            await update.message.reply_text(recipe, parse_mode="Markdown", reply_markup=get_main_keyboard())
        except Exception as e:
            logger.error(e)
            del user_states[user_id]
            await update.message.reply_text("😕 Что-то пошло не так. Попробуй ещё раз.", reply_markup=get_main_keyboard())
        return

    # КБЖУ ПО ТЕКСТУ
    if mode == "kbzhu_photo":
        await update.message.reply_text("⏳ Считаю...")
        try:
            prompt = f"""Посчитай КБЖУ для "{text}" на стандартную порцию.
Ответь СТРОГО в формате JSON:
{{"calories": число, "protein": число, "fat": число, "carbs": число, "comment": "короткий комментарий"}}
Числа целые."""
            r = await ask_groq([{"role": "user", "content": prompt}], max_tokens=300)
            r = r.replace("```json", "").replace("```", "").strip()
            result = json.loads(r)
            entry = {"food": text, "calories": result["calories"], "protein": result["protein"], "fat": result["fat"], "carbs": result["carbs"]}
            add_to_diary(user_id, entry)
            total_cal = sum(e["calories"] for e in get_diary(user_id))
            del user_states[user_id]
            await update.message.reply_text(
                f"✅ *{text}*\n\n"
                f"🔥 {result['calories']} ккал | Б:{result['protein']}г | Ж:{result['fat']}г | У:{result['carbs']}г\n\n"
                f"💬 {result.get('comment','')}\n\n"
                f"📊 Всего за день: *{total_cal} ккал*",
                parse_mode="Markdown", reply_markup=get_main_keyboard()
            )
        except Exception as e:
            logger.error(e)
            await update.message.reply_text("😕 Напиши точнее, например: борщ, гречка с котлетой, яблоко.")
        return

    # КБЖУ НА ПОРЦИЮ
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
                    f"✅ *{food} — {weight}г*\n\n"
                    f"🔥 {result['calories']} ккал | Б:{result['protein']}г | Ж:{result['fat']}г | У:{result['carbs']}г\n\n"
                    f"💬 {result.get('comment','')}\n\n"
                    f"📊 Всего за день: *{total_cal} ккал*",
                    parse_mode="Markdown", reply_markup=get_main_keyboard()
                )
            except Exception as e:
                logger.error(e)
                del user_states[user_id]
                await update.message.reply_text("😕 Что-то пошло не так.", reply_markup=get_main_keyboard())
        return

    # СПИСОК ПОКУПОК
    if mode == "shopping":
        step = state["step"]
        key, _, opts = SHOPPING_QUESTIONS[step]
        state["data"][key] = text
        step += 1
        state["step"] = step
        if step < len(SHOPPING_QUESTIONS):
            _, q, opts = SHOPPING_QUESTIONS[step]
            await update.message.reply_text(q, reply_markup=make_keyboard(opts, skip=False) if opts else make_keyboard([]))
        else:
            await update.message.reply_text("⏳ Составляю список...")
            try:
                result = await generate_shopping(state["data"].get("recipe", ""), state["data"].get("persons", "1"))
                del user_states[user_id]
                await update.message.reply_text(result, parse_mode="Markdown", reply_markup=get_main_keyboard())
            except Exception as e:
                logger.error(e)
                del user_states[user_id]
                await update.message.reply_text("😕 Что-то пошло не так.", reply_markup=get_main_keyboard())
        return

    # НОРМА КБЖУ
    if mode == "norm":
        step = state["step"]
        key, _, _ = NORM_QUESTIONS[step]
        state["data"][key] = text
        step += 1
        state["step"] = step
        if step < len(NORM_QUESTIONS):
            _, q, opts = NORM_QUESTIONS[step]
            await update.message.reply_text(q, reply_markup=make_keyboard(opts, skip=False) if opts else make_keyboard([]))
        else:
            await update.message.reply_text("⏳ Рассчитываю...")
            try:
                result = await calculate_norm(state["data"])
                if user_id not in user_profiles:
                    user_profiles[user_id] = {}
                user_profiles[user_id]["norm"] = result
                del user_states[user_id]
                await update.message.reply_text(
                    f"⚖️ *Твоя суточная норма:*\n\n"
                    f"🔥 Калории: *{result['calories']} ккал*\n"
                    f"🥩 Белки: *{result['protein']}г*\n"
                    f"🧈 Жиры: *{result['fat']}г*\n"
                    f"🍞 Углеводы: *{result['carbs']}г*\n\n"
                    f"💬 {result.get('explanation','')}",
                    parse_mode="Markdown", reply_markup=get_main_keyboard()
                )
            except Exception as e:
                logger.error(e)
                del user_states[user_id]
                await update.message.reply_text("😕 Что-то пошло не так.", reply_markup=get_main_keyboard())
        return

    # ПЛАН ПИТАНИЯ
    if mode == "strip":
        step = state["step"]
        key, _, _ = STRIP_QUESTIONS[step]
        if text != "Пропустить":
            state["data"][key] = text
        step += 1
        state["step"] = step
        if step < len(STRIP_QUESTIONS):
            _, q, opts = STRIP_QUESTIONS[step]
            await update.message.reply_text(q, reply_markup=make_keyboard(opts) if opts else make_keyboard([]))
        else:
            await update.message.reply_text("⏳ Составляю план...")
            try:
                profile = user_profiles.get(user_id, {})
                result = await generate_strip(state["data"], profile)
                del user_states[user_id]
                await update.message.reply_text(f"🔥 *План питания:*\n\n{result}", parse_mode="Markdown", reply_markup=get_main_keyboard())
            except Exception as e:
                logger.error(e)
                del user_states[user_id]
                await update.message.reply_text("😕 Что-то пошло не так.", reply_markup=get_main_keyboard())
        return

    # ПРОФИЛЬ — ПОДТВЕРЖДЕНИЕ
    if mode == "profile_confirm":
        if text == "✏️ Изменить предпочтения":
            user_states[user_id] = {"mode": "profile", "step": 0, "data": {}}
            _, q, opts = PROFILE_QUESTIONS[0]
            await update.message.reply_text(q, reply_markup=make_keyboard(opts) if opts else make_keyboard([]))
        return

    # ПРОФИЛЬ — ЗАПОЛНЕНИЕ
    if mode == "profile":
        step = state["step"]
        key, _, _ = PROFILE_QUESTIONS[step]
        if text != "Пропустить":
            state["data"][key] = text
        step += 1
        state["step"] = step
        if step < len(PROFILE_QUESTIONS):
            _, q, opts = PROFILE_QUESTIONS[step]
            await update.message.reply_text(q, reply_markup=make_keyboard(opts) if opts else make_keyboard([]))
        else:
            if user_id not in user_profiles:
                user_profiles[user_id] = {}
            user_profiles[user_id].update(state["data"])
            del user_states[user_id]
            await update.message.reply_text("✅ Предпочтения сохранены!", reply_markup=get_main_keyboard())
        return

    # ОБЫЧНЫЙ ТЕКСТ — КБЖУ
    await update.message.reply_text("⏳ Считаю КБЖУ...")
    try:
        prompt = f"""Посчитай КБЖУ для "{text}" на стандартную порцию.
Ответь СТРОГО в формате JSON:
{{"calories": число, "protein": число, "fat": число, "carbs": число, "comment": "короткий комментарий"}}
Числа целые."""
        r = await ask_groq([{"role": "user", "content": prompt}], max_tokens=300)
        r = r.replace("```json", "").replace("```", "").strip()
        result = json.loads(r)
        entry = {"food": text, "calories": result["calories"], "protein": result["protein"], "fat": result["fat"], "carbs": result["carbs"]}
        add_to_diary(user_id, entry)
        total_cal = sum(e["calories"] for e in get_diary(user_id))
        await update.message.reply_text(
            f"✅ *{text}*\n\n"
            f"🔥 {result['calories']} ккал | Б:{result['protein']}г | Ж:{result['fat']}г | У:{result['carbs']}г\n\n"
            f"💬 {result.get('comment','')}\n\n"
            f"📊 Всего за день: *{total_cal} ккал*",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(e)
        await update.message.reply_text("😕 Напиши точнее, например: *борщ*, *гречка с котлетой*, *яблоко*", parse_mode="Markdown")


async def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("✅ Бот запущен!")
    await app.run_polling()


if __name__ == "__main__":
    import nest_asyncio
    nest_asyncio.apply()
    asyncio.get_event_loop().run_until_complete(main())
