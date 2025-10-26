import logging
import os
import json
import sqlite3
from dotenv import load_dotenv
from db_setup import setup_database
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler, ConversationHandler

# Conversation states
(TYPE, NAME, YEAR, DESCRIPTION, URL, IMAGE, ADMIN_RATING, SITE_RATING, GENRES, EDIT_CHOICE, EDIT_FIELD) = range(11)

# Enable logging
load_dotenv()

LOGGING_ENABLED = os.getenv("LOGGING_ENABLED", "false").lower() == "true"
ADMIN_IDS = [int(admin_id) for admin_id in os.getenv("ADMIN_IDS", "").split(",") if admin_id]
PAGE_SIZE = 5
DB_FILE = "bot.db"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

if not LOGGING_ENABLED:
    logging.disable(logging.CRITICAL)

def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# --- Stack-based message management ---

async def pop_and_delete_messages(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    if context.user_data.get('message_stack'):
        message_ids = context.user_data['message_stack'].pop()
        for message_id in message_ids:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
            except Exception as e:
                logger.error(f"Failed to delete message {message_id}: {e}")

def push_new_message_level(context: ContextTypes.DEFAULT_TYPE):
    if 'message_stack' not in context.user_data:
        context.user_data['message_stack'] = []
    context.user_data['message_stack'].append([])

def add_message_to_stack(context: ContextTypes.DEFAULT_TYPE, message_id: int):
    if context.user_data.get('message_stack'):
        context.user_data['message_stack'][-1].append(message_id)

# --- Keyboard generators ---

def get_recommendations_keyboard(user_id: int) -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("📸 Фильмы", callback_data="category:films")],
        [InlineKeyboardButton("🎬 Сериалы", callback_data="category:series")],
        [InlineKeyboardButton("📕 Книги", callback_data="category:books")],
    ]
    if is_admin(user_id):
        keyboard.append([InlineKeyboardButton("🛠️ Админ панель", callback_data="admin:panel")])
    return InlineKeyboardMarkup(keyboard)

def get_genres_keyboard(category: str, selected_genres: list[str], viewed_filter: bool, user_id: int) -> InlineKeyboardMarkup:
    available_genres = get_available_genres(category, selected_genres, viewed_filter, user_id)
    keyboard = []
    
    viewed_text = f"[{'✅' if viewed_filter else '❌'}] Просмотрено"
    keyboard.append([InlineKeyboardButton(viewed_text, callback_data=f"genre:{category}:_viewed_")])

    for genre in available_genres:
        text = f"[{'✅' if genre in selected_genres else '❌'}] {genre.capitalize()}"
        keyboard.append([InlineKeyboardButton(text, callback_data=f"genre:{category}:{genre}")])
    keyboard.append([InlineKeyboardButton("🔎 Выбрать", callback_data=f"select:{category}")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back:recommendations")])
    return InlineKeyboardMarkup(keyboard)

def get_results_keyboard(category: str, page: int, total_pages: int) -> InlineKeyboardMarkup:
    keyboard = []
    row = []
    if page > 0:
        row.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"page:{category}:{page-1}"))
    if page < total_pages - 1:
        row.append(InlineKeyboardButton("Вперед ➡️", callback_data=f"page:{category}:{page+1}"))
    keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔎 К выбору жанров", callback_data=f"back:genres:{category}")])
    return InlineKeyboardMarkup(keyboard)

# --- Data functions ---

def count_filtered_objects(category: str, selected_genres: list[str], viewed_filter: bool, user_id: int) -> int:
    conn = get_db_connection()
    category_map = {
        "films": "фильм",
        "series": "сериал",
        "books": "книга"
    }
    obj_type = category_map.get(category)

    db_query = "SELECT COUNT(id) FROM objects WHERE obj_type = ?"
    params = [obj_type]

    if selected_genres:
        for genre in selected_genres:
            db_query += " AND id IN (SELECT object_id FROM object_genres WHERE genre_id = (SELECT id FROM genres WHERE name = ?))"
            params.append(genre)
    
    if viewed_filter:
        db_query += " AND id IN (SELECT object_id FROM user_views WHERE user_id = ?)"
        params.append(user_id)

    try:
        count = conn.execute(db_query, params).fetchone()
        if count is None:
            return 0
        return count[0]
    except sqlite3.OperationalError as e:
        logger.error(f"Database error in count_filtered_objects: {e}")
        return 0
    finally:
        conn.close()


def get_available_genres(category: str, selected_genres: list[str], viewed_filter: bool, user_id: int) -> list[str]:
    conn = get_db_connection()
    category_map = {
        "films": "фильм",
        "series": "сериал",
        "books": "книга"
    }
    obj_type = category_map.get(category)

    base_query = "SELECT id FROM objects WHERE obj_type = ?"
    params = [obj_type]

    if selected_genres:
        for i, genre in enumerate(selected_genres):
            base_query += f" AND id IN (SELECT object_id FROM object_genres og JOIN genres g ON og.genre_id = g.id WHERE g.name = ?)"
            params.append(genre)

    if viewed_filter:
        base_query += " AND id IN (SELECT object_id FROM user_views WHERE user_id = ?)"
        params.append(user_id)

    query = f"""
        SELECT DISTINCT g.name 
        FROM genres g
        JOIN object_genres og ON g.id = og.genre_id
        WHERE og.object_id IN ({base_query})
        ORDER BY g.name
    """
    
    try:
        genres = conn.execute(query, params).fetchall()
        return [row['name'] for row in genres]
    except sqlite3.OperationalError as e:
        logger.error(f"Database error in get_available_genres: {e}")
        return []
    finally:
        conn.close()

def get_category_genres(category: str) -> list[str]:
    conn = get_db_connection()

    query = """
        SELECT DISTINCT g.name 
        FROM genres g
        JOIN object_genres og ON g.id = og.genre_id
        JOIN objects o ON o.id = og.object_id
        WHERE o.obj_type = ?
        ORDER BY g.name
    """
    
    try:
        genres = conn.execute(query, [category]).fetchall()
        return [row['name'] for row in genres]
    except sqlite3.OperationalError as e:
        logger.error(f"Database error in get_category_genres: {e}")
        return []
    finally:
        conn.close()

async def send_paginated_results(context: ContextTypes.DEFAULT_TYPE, chat_id: int, category: str):
    page = context.user_data.get('results_page', 0)
    results = context.user_data.get('results_cache', [])
    total_pages = (len(results) + PAGE_SIZE - 1) // PAGE_SIZE
    
    start_index = page * PAGE_SIZE
    end_index = start_index + PAGE_SIZE
    page_results = results[start_index:end_index]

    push_new_message_level(context)

    if not page_results:
        keyboard = get_results_keyboard(category, page, total_pages)
        message = await context.bot.send_message(chat_id=chat_id, text="Ничего не найдено", reply_markup=keyboard)
        add_message_to_stack(context, message.message_id)
    else:
        for obj_dict in page_results:
            # Re-query genres for each object to display them
            conn = get_db_connection()
            genres_rows = conn.execute("""
                SELECT g.name FROM genres g
                JOIN object_genres og ON g.id = og.genre_id
                WHERE og.object_id = ?
            """, (obj_dict['id'],)).fetchall()
            
            user_id = chat_id  # Assuming chat_id is the user_id
            viewed_row = conn.execute("SELECT 1 FROM user_views WHERE user_id = ? AND object_id = ?", (user_id, obj_dict['id'])).fetchone()
            conn.close()
            
            obj_dict['obj_genres'] = [row['name'] for row in genres_rows]
            is_viewed = viewed_row is not None

            viewed_text = " [Просмотрено]" if is_viewed else ""
            keyboard_buttons = []
            if obj_dict.get("obj_url") and isinstance(obj_dict["obj_url"], str) and obj_dict["obj_url"].startswith(('http://', 'https://')):
                keyboard_buttons.append(InlineKeyboardButton("🔗 Ссылка", url=obj_dict["obj_url"]))
            if not is_viewed:
                keyboard_buttons.append(InlineKeyboardButton("🎯 Просмотрено", callback_data=f"view:{category}:{obj_dict['id']}"))
            else:
                keyboard_buttons.append(InlineKeyboardButton("🚫 Не просмотрено", callback_data=f"unview:{category}:{obj_dict['id']}"))
            if is_admin(user_id):
                keyboard_buttons.append(InlineKeyboardButton("✏️ Изменить", callback_data=f"edit:{category}:{obj_dict['id']}"))
                keyboard_buttons.append(InlineKeyboardButton("🗑️ Удалить", callback_data=f"delete:{category}:{obj_dict['id']}"))
            
            keyboard = InlineKeyboardMarkup([keyboard_buttons])

            caption_parts = []
            caption_parts.append(f"{obj_dict['obj_name']} ({obj_dict['obj_year']}){viewed_text}")
            caption_parts.append(f"Жанры: {', '.join(obj_dict['obj_genres'])}")
            if obj_dict.get('admin_rating'):
                caption_parts.append(f"Оценка администрации: {obj_dict['admin_rating']}")
            if obj_dict.get('site_rating'):
                caption_parts.append(f"Оценка с сайта: {obj_dict['site_rating']}")
            caption_parts.append(f"\n{obj_dict['obj_description']}")
            caption = "\n".join(caption_parts)

            try:
                message = await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=obj_dict["obj_image"],
                    caption=caption,
                    reply_markup=keyboard
                )
                add_message_to_stack(context, message.message_id)
            except Exception as e:
                logger.error(f"Failed to send photo for object {obj_dict['id']}: {e}")
                message = await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"{caption}\n\nНе удалось загрузить постер",
                    reply_markup=keyboard
                )
                add_message_to_stack(context, message.message_id)
        
        results_keyboard = get_results_keyboard(category, page, total_pages)
        message = await context.bot.send_message(chat_id=chat_id, text=f"Страница {page+1}/{total_pages}", reply_markup=results_keyboard)
        add_message_to_stack(context, message.message_id)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a message with a custom keyboard when the command /start is issued."""
    logger.info(f"User {update.effective_user.id} started the bot")
    context.user_data.clear()
    context.user_data['message_stack'] = []
    reply_keyboard = [["Список рекомендаций"]]
    await update.message.reply_text(
        "Привет! Я бот, который поможет тебе найти рекомендации. "
        "Нажми на кнопку ниже, чтобы начать.",
        reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True, resize_keyboard=True),
    )

async def show_recommendations(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays the recommendations menu."""
    user_id = update.effective_user.id
    chat_id = update.message.chat_id
    logger.info(f"User {user_id} requested recommendations")

    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
    except Exception as e:
        logger.error(f"Failed to delete user message {update.message.message_id}: {e}")
    
    while context.user_data.get('message_stack'):
        await pop_and_delete_messages(context, chat_id)

    push_new_message_level(context)
    keyboard = get_recommendations_keyboard(user_id)
    message = await context.bot.send_message(chat_id=chat_id, text="Выберите категорию:", reply_markup=keyboard)
    add_message_to_stack(context, message.message_id)


async def button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    logger.info(f"Update object: {update.to_dict()}")
    logger.info(f"Type of query: {type(query)}")
    logger.info(f"User {update.effective_user.id} pressed button with data: {query.data}")

    parts = query.data.split(':')
    action = parts[0]

    if action == 'admin' and parts[1] == 'add_object':
        if not is_admin(query.from_user.id):
            await query.answer("У вас нет прав.", show_alert=True)
            return ConversationHandler.END

        keyboard = [
            [InlineKeyboardButton("📸 Фильм", callback_data="add_type:фильм")],
            [InlineKeyboardButton("🎬 Сериал", callback_data="add_type:сериал")],
            [InlineKeyboardButton("📕 Книга", callback_data="add_type:книга")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.edit_text("Выберите тип объекта:", reply_markup=reply_markup)
        return TYPE

    if action == 'category':
        category = parts[1]
        context.user_data['current_category'] = category
        if 'selected_genres' not in context.user_data:
            context.user_data['selected_genres'] = {}
        if category not in context.user_data['selected_genres']:
            context.user_data['selected_genres'][category] = []
        if 'viewed_filter' not in context.user_data:
            context.user_data['viewed_filter'] = {}
        if category not in context.user_data['viewed_filter']:
            context.user_data['viewed_filter'][category] = False
        
        selected_genres = context.user_data['selected_genres'][category]
        viewed_filter = context.user_data['viewed_filter'][category]
        user_id = query.from_user.id
        num_objects = count_filtered_objects(category, selected_genres, viewed_filter, user_id)
        keyboard = get_genres_keyboard(category, selected_genres, viewed_filter, user_id)
        
        await pop_and_delete_messages(context, query.message.chat_id)
        
        await pop_and_delete_messages(context, query.message.chat_id)
        
        push_new_message_level(context)
        if num_objects == 0:
            message = await query.message.reply_text("По вашему запросу ничего не найдено. Попробуйте изменить фильтры.", reply_markup=keyboard)
        else:
            message = await query.message.reply_text(f"Примените фильтры, выбрано {num_objects}:", reply_markup=keyboard)
        add_message_to_stack(context, message.message_id)

    elif action == 'genre':
        category = parts[1]
        genre = parts[2]
        if 'selected_genres' not in context.user_data:
            context.user_data['selected_genres'] = {category: []}
        if 'viewed_filter' not in context.user_data:
            context.user_data['viewed_filter'] = {category: False}

        if genre == '_viewed_':
            context.user_data['viewed_filter'][category] = not context.user_data['viewed_filter'][category]
        elif genre in context.user_data['selected_genres'][category]:
            context.user_data['selected_genres'][category].remove(genre)
        else:
            context.user_data['selected_genres'][category].append(genre)
            
        selected_genres = context.user_data['selected_genres'][category]
        viewed_filter = context.user_data['viewed_filter'][category]
        user_id = query.from_user.id
        num_objects = count_filtered_objects(category, selected_genres, viewed_filter, user_id)
        keyboard = get_genres_keyboard(category, selected_genres, viewed_filter, user_id)
        if num_objects == 0:
            await query.edit_message_text(text="По вашему запросу ничего не найдено. Попробуйте изменить фильтры.", reply_markup=keyboard)
        else:
            await query.edit_message_text(text=f"Примените фильтры, выбрано {num_objects}:", reply_markup=keyboard)

    elif action == 'admin' and parts[1] == 'panel':
        if not is_admin(query.from_user.id):
            await query.answer("У вас нет прав.", show_alert=True)
            return
        
        keyboard = [
            [InlineKeyboardButton("🆕 Добавить объект", callback_data="admin:add_object")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back:recommendations")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.edit_text("Админ панель", reply_markup=reply_markup)

    elif action == 'delete':
        if not is_admin(query.from_user.id):
            await query.answer("У вас нет прав.", show_alert=True)
            return

        category = parts[1]
        object_id = int(parts[2])

        conn = get_db_connection()
        conn.execute("DELETE FROM objects WHERE id = ?", (object_id,))
        conn.execute("DELETE FROM object_genres WHERE object_id = ?", (object_id,))
        conn.execute("DELETE FROM user_views WHERE object_id = ?", (object_id,))
        conn.commit()
        conn.close()

        await query.answer("Объект удален.")

        # Refresh the results
        results = context.user_data.get('results_cache', [])
        context.user_data['results_cache'] = [obj for obj in results if obj['id'] != object_id]
        await pop_and_delete_messages(context, query.message.chat_id)
        await send_paginated_results(context, query.message.chat_id, category)

    elif action == 'select':
        category = parts[1]
        selected_genres = context.user_data.get('selected_genres', {}).get(category, [])
        viewed_filter = context.user_data.get('viewed_filter', {}).get(category, False)
        user_id = query.message.chat_id

        conn = get_db_connection()
        category_map = {
            "films": "фильм",
            "series": "сериал",
            "books": "книга"
        }
        obj_type = category_map.get(category)

        db_query = "SELECT * FROM objects WHERE obj_type = ?"
        params = [obj_type]

        if selected_genres:
            for genre in selected_genres:
                db_query += " AND id IN (SELECT object_id FROM object_genres WHERE genre_id = (SELECT id FROM genres WHERE name = ?))"
                params.append(genre)
        
        if viewed_filter:
            db_query += " AND id IN (SELECT object_id FROM user_views WHERE user_id = ?)"
            params.append(user_id)

        filtered_objects = conn.execute(db_query, params).fetchall()
        conn.close()

        context.user_data['results_cache'] = [dict(row) for row in filtered_objects]
        context.user_data['results_page'] = 0

        await pop_and_delete_messages(context, query.message.chat_id)
        
        await send_paginated_results(context, query.message.chat_id, category)

    elif action == 'page':
        category = parts[1]
        page = int(parts[2])
        user_id = query.message.chat_id
        context.user_data['results_page'] = page
        
        await pop_and_delete_messages(context, query.message.chat_id)
        
        await send_paginated_results(context, query.message.chat_id, category)

    elif action == 'view':
        category = parts[1]
        object_id = int(parts[2])
        user_id = query.from_user.id

        conn = get_db_connection()
        conn.execute("INSERT OR IGNORE INTO user_views (user_id, object_id) VALUES (?, ?)", (user_id, object_id))
        conn.commit()
        conn.close()

        await query.answer("Отмечено как просмотренное")

        results = context.user_data.get('results_cache', [])
        obj_dict = next((obj for obj in results if obj['id'] == object_id), None)

        if obj_dict:
            conn = get_db_connection()
            genres_rows = conn.execute("""
                SELECT g.name FROM genres g
                JOIN object_genres og ON g.id = og.genre_id
                WHERE og.object_id = ?
            """, (object_id,)).fetchall()
            conn.close()
            obj_dict['obj_genres'] = [row['name'] for row in genres_rows]

            viewed_text = " [Просмотрено]"
            keyboard_buttons = []
            if obj_dict.get("obj_url") and isinstance(obj_dict["obj_url"], str) and obj_dict["obj_url"].startswith(('http://', 'https://')):
                keyboard_buttons.append(InlineKeyboardButton("🔗 Ссылка", url=obj_dict["obj_url"]))
            keyboard_buttons.append(InlineKeyboardButton("🚫 Не просмотрено", callback_data=f"unview:{category}:{object_id}"))
            if is_admin(user_id):
                keyboard_buttons.append(InlineKeyboardButton("✏️ Изменить", callback_data=f"edit:{category}:{object_id}"))
                keyboard_buttons.append(InlineKeyboardButton("🗑️ Удалить", callback_data=f"delete:{category}:{object_id}"))
            keyboard = InlineKeyboardMarkup([keyboard_buttons])

            caption_parts = []
            caption_parts.append(f"{obj_dict['obj_name']} ({obj_dict['obj_year']}){viewed_text}")
            caption_parts.append(f"Жанры: {', '.join(obj_dict['obj_genres'])}")
            if obj_dict.get('admin_rating'):
                caption_parts.append(f"Оценка администрации: {obj_dict['admin_rating']}")
            if obj_dict.get('site_rating'):
                caption_parts.append(f"Оценка с сайта: {obj_dict['site_rating']}")
            caption_parts.append(f"\n{obj_dict['obj_description']}")
            caption = "\n".join(caption_parts)

            if query.message.photo:
                await query.edit_message_caption(caption=caption, reply_markup=keyboard)
            else:
                await query.edit_message_text(text=caption, reply_markup=keyboard)

    elif action == 'unview':
        category = parts[1]
        object_id = int(parts[2])
        user_id = query.from_user.id

        conn = get_db_connection()
        conn.execute("DELETE FROM user_views WHERE user_id = ? AND object_id = ?", (user_id, object_id))
        conn.commit()
        conn.close()

        await query.answer("Статус 'просмотрено' снят")

        results = context.user_data.get('results_cache', [])
        obj_dict = next((obj for obj in results if obj['id'] == object_id), None)

        if obj_dict:
            conn = get_db_connection()
            genres_rows = conn.execute("""
                SELECT g.name FROM genres g
                JOIN object_genres og ON g.id = og.genre_id
                WHERE og.object_id = ?
            """, (object_id,)).fetchall()
            conn.close()
            obj_dict['obj_genres'] = [row['name'] for row in genres_rows]

            viewed_text = ""
            keyboard_buttons = []
            if obj_dict.get("obj_url") and isinstance(obj_dict["obj_url"], str) and obj_dict["obj_url"].startswith(('http://', 'https://')):
                keyboard_buttons.append(InlineKeyboardButton("🔗 Ссылка", url=obj_dict["obj_url"]))
            keyboard_buttons.append(InlineKeyboardButton("🎯 Просмотрено", callback_data=f"view:{category}:{object_id}"))
            if is_admin(user_id):
                keyboard_buttons.append(InlineKeyboardButton("✏️ Изменить", callback_data=f"edit:{category}:{object_id}"))
                keyboard_buttons.append(InlineKeyboardButton("🗑️ Удалить", callback_data=f"delete:{category}:{object_id}"))
            keyboard = InlineKeyboardMarkup([keyboard_buttons])

            caption_parts = []
            caption_parts.append(f"{obj_dict['obj_name']} ({obj_dict['obj_year']}){viewed_text}")
            caption_parts.append(f"Жанры: {', '.join(obj_dict['obj_genres'])}")
            if obj_dict.get('admin_rating'):
                caption_parts.append(f"Оценка администрации: {obj_dict['admin_rating']}")
            if obj_dict.get('site_rating'):
                caption_parts.append(f"Оценка с сайта: {obj_dict['site_rating']}")
            caption_parts.append(f"\n{obj_dict['obj_description']}")
            caption = "\n".join(caption_parts)

            if query.message.photo:
                await query.edit_message_caption(caption=caption, reply_markup=keyboard)
            else:
                await query.edit_message_text(text=caption, reply_markup=keyboard)

    elif action == 'back':
        target_menu = parts[1]
        
        await pop_and_delete_messages(context, query.message.chat_id)

        if target_menu == 'recommendations':
            user_id = query.from_user.id
            push_new_message_level(context)
            keyboard = get_recommendations_keyboard(user_id)
            message = await context.bot.send_message(chat_id=query.message.chat_id, text="Выберите категорию:", reply_markup=keyboard)
            add_message_to_stack(context, message.message_id)

        elif target_menu == 'genres':
            category = parts[2]
            selected_genres = context.user_data.get('selected_genres', {}).get(category, [])
            viewed_filter = context.user_data.get('viewed_filter', {}).get(category, False)
            user_id = query.from_user.id
            num_objects = count_filtered_objects(category, selected_genres, viewed_filter, user_id)
            keyboard = get_genres_keyboard(category, selected_genres, viewed_filter, user_id)
            
            push_new_message_level(context)
            if num_objects == 0:
                message = await context.bot.send_message(chat_id=query.message.chat_id, text="По вашему запросу ничего не найдено. Попробуйте изменить фильтры.", reply_markup=keyboard)
            else:
                message = await context.bot.send_message(chat_id=query.message.chat_id, text=f"Примените фильтры, выбрано {num_objects}:", reply_markup=keyboard)
            add_message_to_stack(context, message.message_id)


async def go_to_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info(f"User {update.effective_user.id} requested main menu")
    while context.user_data.get('message_stack'):
        await pop_and_delete_messages(context, update.message.chat_id)
    await start(update, context)


async def add_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['new_object'] = {'type': query.data.split(':')[1]}
    await query.message.edit_text("Введите название объекта:")
    return NAME

async def add_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['new_object']['name'] = update.message.text
    await update.message.reply_text("Введите год выпуска:")
    return YEAR

async def add_year(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['new_object']['year'] = int(update.message.text)
    await update.message.reply_text("Введите описание:")
    return DESCRIPTION

async def add_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['new_object']['description'] = update.message.text
    await update.message.reply_text("Введите ссылку на объект:")
    return URL

async def add_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['new_object']['url'] = update.message.text
    await update.message.reply_text("Введите ссылку на изображение:")
    return IMAGE

async def add_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['new_object']['image'] = update.message.text
    await update.message.reply_text("Введите оценку администрации (число):")
    return ADMIN_RATING

async def add_admin_rating(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['new_object']['admin_rating'] = float(update.message.text.replace(",", "."))
    await update.message.reply_text("Введите оценку с сайта (число):")
    return SITE_RATING

async def add_site_rating(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['new_object']['site_rating'] = float(update.message.text.replace(",", "."))
    genre_names = get_category_genres(context.user_data['new_object']['type'])

    print(f"выбран тип {context.user_data['new_object']['type']}")
    print(f"gendes = {genre_names}")

    message = "Введите жанры через запятую:"
    if genre_names:
        message += f" (добавлены ранее: {', '.join(genre_names)})"
    await update.message.reply_text(message)
    return GENRES

async def add_genres(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    genres = [genre.strip() for genre in update.message.text.split(",")]
    context.user_data['new_object']['genres'] = genres

    new_obj = context.user_data['new_object']
    conn = get_db_connection()
    c = conn.cursor()

    try:
        c.execute("INSERT INTO objects (obj_type, obj_name, obj_year, obj_description, obj_url, obj_image, admin_rating, site_rating) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                  (new_obj['type'], new_obj['name'], new_obj['year'], new_obj['description'], new_obj['url'], new_obj['image'], new_obj['admin_rating'], new_obj['site_rating']))
        object_db_id = c.lastrowid

        for genre_name in new_obj['genres']:
            c.execute("INSERT OR IGNORE INTO genres (name) VALUES (?)", (genre_name,))
            c.execute("SELECT id FROM genres WHERE name = ?", (genre_name,))
            genre_id = c.fetchone()[0]
            c.execute("INSERT INTO object_genres (object_id, genre_id) VALUES (?, ?)", (object_db_id, genre_id))

        conn.commit()
        await update.message.reply_text("Объект успешно добавлен!")
    except sqlite3.OperationalError as e:
        logger.error(f"Database error in add_genres: {e}. Attempting to set up database.")
        setup_database()
        conn = get_db_connection()
        c = conn.cursor()
        try:
            c.execute("INSERT INTO objects (obj_type, obj_name, obj_year, obj_description, obj_url, obj_image, admin_rating, site_rating) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                      (new_obj['type'], new_obj['name'], new_obj['year'], new_obj['description'], new_obj['url'], new_obj['image'], new_obj['admin_rating'], new_obj['site_rating']))
            object_db_id = c.lastrowid

            for genre_name in new_obj['genres']:
                c.execute("INSERT OR IGNORE INTO genres (name) VALUES (?)", (genre_name,))
                c.execute("SELECT id FROM genres WHERE name = ?", (genre_name,))
                genre_id = c.fetchone()[0]
                c.execute("INSERT INTO object_genres (object_id, genre_id) VALUES (?, ?)", (object_db_id, genre_id))

            conn.commit()
            await update.message.reply_text("Объект успешно добавлен!")
        except sqlite3.OperationalError as e:
            logger.error(f"Database error after setup in add_genres: {e}")
            await update.message.reply_text("Произошла ошибка при добавлении объекта даже после инициализации базы данных. Пожалуйста, проверьте логи.")
        finally:
            conn.close()
    finally:
        if conn:
            conn.close()

    context.user_data.pop('new_object')
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:

    await update.message.reply_text("Добавление объекта отменено.")

    return ConversationHandler.END

async def edit_object(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.answer("У вас нет прав.", show_alert=True)
        return ConversationHandler.END
    _, category, object_id = query.data.split(':')
    context.user_data['edit_object_id'] = int(object_id)
    context.user_data['edit_category'] = category
    keyboard = [
        [InlineKeyboardButton("Название", callback_data="edit_field:obj_name")],
        [InlineKeyboardButton("Год", callback_data="edit_field:obj_year")],
        [InlineKeyboardButton("Описание", callback_data="edit_field:obj_description")],
        [InlineKeyboardButton("Ссылка", callback_data="edit_field:obj_url")],
        [InlineKeyboardButton("Изображение", callback_data="edit_field:obj_image")],
        [InlineKeyboardButton("Оценка админа", callback_data="edit_field:admin_rating")],
        [InlineKeyboardButton("Оценка сайта", callback_data="edit_field:site_rating")],
        [InlineKeyboardButton("Жанры", callback_data="edit_field:genres")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="edit_cancel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if query.message.photo:
        await query.message.edit_caption("Выберите поле для изменения:", reply_markup=reply_markup)
    else:
        await query.message.edit_text("Выберите поле для изменения:", reply_markup=reply_markup)
    return EDIT_CHOICE

async def edit_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    field = query.data.split(':')[1]
    context.user_data['edit_field'] = field
    object_id = context.user_data['edit_object_id']
    conn = get_db_connection()
    obj = conn.execute("SELECT * FROM objects WHERE id = ?", (object_id,)).fetchone()
    current_value = obj[field]
    if field == 'genres':
        genres_rows = conn.execute("""
            SELECT g.name FROM genres g
            JOIN object_genres og ON g.id = og.genre_id
            WHERE og.object_id = ?
        """, (object_id,)).fetchall()
        current_value = ', '.join([row['name'] for row in genres_rows])
    conn.close()
    message_text = f"Введите новое значение для поля '{field}'.\nТекущее значение: {current_value}"
    if query.message.photo:
        await query.message.edit_caption(message_text, reply_markup=None)
    else:
        await query.message.edit_text(message_text, reply_markup=None)
    return EDIT_FIELD

async def edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    new_value = update.message.text
    object_id = context.user_data['edit_object_id']
    field = context.user_data['edit_field']
    conn = get_db_connection()
    if field == 'genres':
        genres = [genre.strip() for genre in new_value.split(",")]
        c = conn.cursor()
        c.execute("DELETE FROM object_genres WHERE object_id = ?", (object_id,))
        for genre_name in genres:
            c.execute("INSERT OR IGNORE INTO genres (name) VALUES (?)", (genre_name,))
            c.execute("SELECT id FROM genres WHERE name = ?", (genre_name,))
            genre_id = c.fetchone()[0]
            c.execute("INSERT INTO object_genres (object_id, genre_id) VALUES (?, ?)", (object_id, genre_id))
    else:
        conn.execute(f"UPDATE objects SET {field} = ? WHERE id = ?", (new_value, object_id))

    conn.commit()
    conn.close()
    await update.message.reply_text("Поле успешно обновлено!")

    # Refresh cache and show results
    category = context.user_data['edit_category']
    results = context.user_data.get('results_cache', [])
    for obj in results:
        if obj['id'] == object_id:
            if field == 'genres':
                obj['obj_genres'] = [genre.strip() for genre in new_value.split(",")]
            else:
                obj[field] = new_value
            break
    await pop_and_delete_messages(context, update.message.chat_id)
    await send_paginated_results(context, update.message.chat_id, category)
    return ConversationHandler.END

async def cancel_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await pop_and_delete_messages(context, query.message.chat_id)
    category = context.user_data['edit_category']
    await send_paginated_results(context, query.message.chat_id, category)
    return ConversationHandler.END

def main() -> None:
    """Start the bot."""
    logger.info("Starting bot...")
    application = Application.builder().token(os.getenv("TELEGRAM_BOT_TOKEN")).build()

    application.add_handler(CommandHandler("start", start))

    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(button, pattern='^admin:add_object')],
        states={
            TYPE: [CallbackQueryHandler(add_type, pattern='^add_type')],
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_name)],
            YEAR: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_year)],
            DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_description)],
            URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_url)],
            IMAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_image)],
            ADMIN_RATING: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_admin_rating)],
            SITE_RATING: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_site_rating)],
            GENRES: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_genres)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )

    application.add_handler(conv_handler)

    edit_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_object, pattern='^edit')],
        states={
            EDIT_CHOICE: [CallbackQueryHandler(edit_choice, pattern='^edit_field')],
            EDIT_FIELD: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_field)],
        },
        fallbacks=[CallbackQueryHandler(cancel_edit, pattern='^edit_cancel')],
    )

    application.add_handler(edit_conv_handler)
    application.add_handler(MessageHandler(filters.Regex("^Список рекомендаций$"), show_recommendations))
    application.add_handler(MessageHandler(filters.Regex("^Главное меню$"), go_to_main_menu))
    application.add_handler(CallbackQueryHandler(button))

    application.run_polling()
    logger.info("Bot stopped.")

if __name__ == "__main__":
    main()