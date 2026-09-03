"""
Модуль обработки обновлений (хэндлеры).
Содержит все роутеры для команд, текстовых сообщений и callback-запросов.
Вся бизнес-логика рендеринга и взаимодействия.
"""
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.exceptions import TelegramBadRequest
from datetime import timedelta

import config
import utils
import obsidian_parser as vault
# Импортируем необходимые объекты из bot.py
from bot import (
    user_settings, default_settings, save_user_settings,
    safe_edit, main_menu_kb, settings_menu_kb, upcoming_kb, tasks_nav_kb,
    settings_kb, display_settings_kb, kanban_settings_kb, tasks_settings_kb, projects_settings_kb,
    format_note_card, _status_icon, _source_icon_for, projects_menu_kb,
    SOURCE_ICON_BY_NAME, SOURCE_ICON, STATUS_BUTTONS, SOURCE_BUTTONS, extra_settings_kb
)

router = Router()

# Словарь для отслеживания активной Reply-клавиатуры (main/settings).
# Решает конфликт одинаковых текстов кнопок в разных меню без FSM.
user_menus = {}


# ==============================================================================
# 1. ХЭНДЛЕРЫ БАЗОВЫХ КОМАНД
# ==============================================================================
@router.message(Command("start"))
async def cmd_start(message: Message):
    """Запускает бота, инициализирует настройки пользователя и включает уведомления."""
    uid = message.from_user.id
    if uid not in user_settings: user_settings[uid] = default_settings()
    else: user_settings[uid]["is_active"] = True
    save_user_settings()
    user_menus[uid] = "main" # NEW: Фиксируем активное меню
    await message.answer("Привет! Я бот для Obsidian. Выбери статус задач внизу:", reply_markup=main_menu_kb())


@router.message(Command("stop"))
async def cmd_stop(message: Message):
    """Останавливает отправку уведомлений (ставит is_active = False)."""
    uid = message.from_user.id
    if uid not in user_settings: user_settings[uid] = default_settings()
    user_settings[uid]["is_active"] = False
    save_user_settings()
    await message.answer("🛑 Все уведомления остановлены. Чтобы возобновить, используй /start или /menu.")


@router.message(Command("menu"))
async def cmd_menu(message: Message):
    """Открывает главное меню."""
    user_menus[message.from_user.id] = "main" # NEW: Фиксируем активное меню
    await message.answer("🏠 Главное меню:", reply_markup=main_menu_kb())


@router.message(Command("help"))
async def cmd_help(message: Message):
    """Выводит справку по доступным командам."""
    await message.answer("ℹ️ <b>Справка по боту</b>\n\nЯ читаю твои заметки из папок Tasks и присылаю уведомления о дедлайнах.\n\nДоступные команды:\n/start - Запустить бота / включить уведомления\n/stop - Временно выключить уведомления\n/menu - Открыть главное меню\n/help - Показать эту справку", parse_mode="HTML")


# ==============================================================================
# 2. ХЭНДЛЕРЫ НАСТРОЕК
# ==============================================================================
# Используем переменные из config.py для текстов кнопок
@router.message(F.text == config.BTN_SETTINGS)
async def open_settings(message: Message):
    """Открывает меню настроек (Reply клавиатура)."""
    user_menus[message.from_user.id] = "settings" # NEW: Фиксируем активное меню
    await message.answer("⚙️ <b>Настройки</b>\nВыберите раздел:", reply_markup=settings_menu_kb(), parse_mode="HTML")


@router.message(F.text == config.BTN_NOTIFY)
async def show_notify_settings(message: Message):
    """Открывает inline-меню настройки интервалов уведомлений."""
    if message.from_user.id not in user_settings: user_settings[message.from_user.id] = default_settings()
    await message.answer("⏳ <b>Настройки уведомлений</b>\n\nВыбери, за сколько предупреждать о дедлайне.\n✅ — включено\n❌ — выключено", reply_markup=settings_kb(message.from_user.id), parse_mode="HTML")

# Открытие дополнительных настроек
@router.message(F.text == config.BTN_EXTRA_SETTINGS)
async def open_extra_settings(message: Message):
    """Открывает меню дополнительных настроек."""
    await message.answer("⚙️ <b>Дополнительные настройки</b>\n\nУправление режимами бота:", reply_markup=extra_settings_kb(message.from_user.id), parse_mode="HTML")

@router.callback_query(F.data == "back_to_settings_menu")
async def back_to_settings_menu(callback: CallbackQuery):
    """Возвращает из inline-настроек в меню настроек (Reply)."""
    user_menus[callback.from_user.id] = "settings" # NEW: Фиксируем активное меню
    try: await callback.message.delete()
    except TelegramBadRequest: pass
    await callback.message.answer("⚙️ <b>Настройки</b>\nВыберите раздел:", reply_markup=settings_menu_kb(), parse_mode="HTML")
    await callback.answer()


@router.message(F.text == config.BTN_MAIN_MENU)
async def back_to_main_menu(message: Message):
    """Возвращает из меню настроек в главное меню."""
    user_menus[message.from_user.id] = "main" # NEW: Фиксируем активное меню
    await message.answer("🏠 Главное меню:", reply_markup=main_menu_kb())


@router.callback_query(F.data == "back_to_notify_settings")
async def back_to_notify_settings(callback: CallbackQuery):
    """Возвращает из настроек отображения в меню уведомлений."""
    await safe_edit(callback, text="⏳ <b>Настройки уведомлений</b>\n\nВыбери, за сколько предупреждать о дедлайне.\n✅ — включено\n❌ — выключено", reply_markup=settings_kb(callback.from_user.id))
    await callback.answer()


# ==============================================================================
# 3. ПЕРЕКЛЮЧАТЕЛИ НАСТРОЕК (Дедлайны)
# ==============================================================================
@router.callback_query(F.data.in_(["set_sort_new", "set_sort_old"]))
async def set_sort_order(callback: CallbackQuery):
    """Устанавливает порядок дат (Сначала новые / Сначала старые) для дедлайнов."""
    uid = callback.from_user.id
    if uid not in user_settings: user_settings[uid] = default_settings()
    user_settings[uid]["sort_order"] = "new" if callback.data == "set_sort_new" else "old"
    save_user_settings()
    await safe_edit(callback, reply_markup=display_settings_kb(uid))
    await callback.answer()

@router.callback_query(F.data.in_(["set_dir_ttb", "set_dir_btt"]))
async def set_sort_dir(callback: CallbackQuery):
    """Устанавливает направление списка (Сверху вниз / Снизу вверх) для дедлайнов."""
    uid = callback.from_user.id
    if uid not in user_settings: user_settings[uid] = default_settings()
    user_settings[uid]["sort_dir"] = "ttb" if callback.data == "set_dir_ttb" else "btt"
    save_user_settings()
    await safe_edit(callback, reply_markup=display_settings_kb(uid))
    await callback.answer()

@router.callback_query(F.data == "tog_group")
async def toggle_group(callback: CallbackQuery):
    """Переключает группировку по источникам для дедлайнов."""
    uid = callback.from_user.id
    if uid not in user_settings: user_settings[uid] = default_settings()
    user_settings[uid]["group_by_source"] = not user_settings[uid].get("group_by_source", False)
    save_user_settings()
    await safe_edit(callback, reply_markup=display_settings_kb(uid))
    await callback.answer()


@router.callback_query(F.data == "tog_dyn_sort")
async def toggle_dynamic_sort(callback: CallbackQuery):
    """Переключает динамическую сортировку групп по срочности для дедлайнов."""
    uid = callback.from_user.id
    if uid not in user_settings: user_settings[uid] = default_settings()
    user_settings[uid]["dynamic_sort"] = not user_settings[uid].get("dynamic_sort", True)
    save_user_settings()
    await safe_edit(callback, reply_markup=display_settings_kb(uid))
    await callback.answer()

# ==============================================================================
# 3.1 ПЕРЕКЛЮЧАТЕЛИ НАСТРОЕК (Kanban)
# ==============================================================================
@router.callback_query(F.data.in_(["set_kanban_sort_new", "set_kanban_sort_old"]))
async def set_kanban_sort_order(callback: CallbackQuery):
    """Устанавливает порядок дат (Сначала новые / Сначала старые) для Kanban."""
    uid = callback.from_user.id
    if uid not in user_settings: user_settings[uid] = default_settings()
    user_settings[uid]["kanban_sort_order"] = "new" if callback.data == "set_kanban_sort_new" else "old"
    save_user_settings()
    await safe_edit(callback, reply_markup=kanban_settings_kb(uid))
    await callback.answer()

@router.callback_query(F.data.in_(["set_kanban_dir_ttb", "set_kanban_dir_btt"]))
async def set_kanban_sort_dir(callback: CallbackQuery):
    """Устанавливает направление списка (Сверху вниз / Снизу вверх) для Kanban."""
    uid = callback.from_user.id
    if uid not in user_settings: user_settings[uid] = default_settings()
    user_settings[uid]["kanban_sort_dir"] = "ttb" if callback.data == "set_kanban_dir_ttb" else "btt"
    save_user_settings()
    await safe_edit(callback, reply_markup=kanban_settings_kb(uid))
    await callback.answer()

@router.callback_query(F.data.in_(["set_kanban_group_boards", "set_kanban_group_notes", "tog_kanban_group"]))
async def set_kanban_primary_group(callback: CallbackQuery):
    """Устанавливает тип группировки Kanban (борды/заметки) и обрабатывает переключатель 'С группировкой'."""
    uid = callback.from_user.id
    if uid not in user_settings: user_settings[uid] = default_settings()

    if callback.data == "set_kanban_group_boards":
        user_settings[uid]["kanban_primary_group"] = "boards_first"
    elif callback.data == "set_kanban_group_notes":
        user_settings[uid]["kanban_primary_group"] = "notes_first"
    elif callback.data == "tog_kanban_group":
        current_group = user_settings[uid].get("kanban_primary_group", "none")
        user_settings[uid]["kanban_primary_group"] = "none" if current_group != "none" else "boards_first"

    save_user_settings()
    await safe_edit(callback, reply_markup=kanban_settings_kb(uid))
    await callback.answer()

@router.callback_query(F.data == "tog_kanban_dyn_sort")
async def toggle_kanban_dynamic_sort(callback: CallbackQuery):
    """Переключает динамическую сортировку групп по срочности для Kanban."""
    uid = callback.from_user.id
    if uid not in user_settings: user_settings[uid] = default_settings()
    user_settings[uid]["kanban_dynamic_sort"] = not user_settings[uid].get("kanban_dynamic_sort", True)
    save_user_settings()
    await safe_edit(callback, reply_markup=kanban_settings_kb(uid))
    await callback.answer()

# ==============================================================================
# 3.2 ПЕРЕКЛЮЧАТЕЛИ НАСТРОЕК (Tasks)
# ==============================================================================
@router.callback_query(F.data.in_(["set_tasks_sort_new", "set_tasks_sort_old"]))
async def set_tasks_sort_order(callback: CallbackQuery):
    """Устанавливает порядок дат (Сначала новые / Сначала старые) для Tasks."""
    uid = callback.from_user.id
    if uid not in user_settings: user_settings[uid] = default_settings()
    user_settings[uid]["tasks_sort_order"] = "new" if callback.data == "set_tasks_sort_new" else "old"
    save_user_settings()
    await safe_edit(callback, reply_markup=tasks_settings_kb(uid))
    await callback.answer()

@router.callback_query(F.data.in_(["set_tasks_dir_ttb", "set_tasks_dir_btt"]))
async def set_tasks_sort_dir(callback: CallbackQuery):
    """Устанавливает направление списка (Сверху вниз / Снизу вверх) для Tasks."""
    uid = callback.from_user.id
    if uid not in user_settings: user_settings[uid] = default_settings()
    user_settings[uid]["tasks_sort_dir"] = "ttb" if callback.data == "set_tasks_dir_ttb" else "btt"
    save_user_settings()
    await safe_edit(callback, reply_markup=tasks_settings_kb(uid))
    await callback.answer()

# ==============================================================================
# 3.3 ПЕРЕКЛЮЧАТЕЛИ НАСТРОЕК (Projects)
# ==============================================================================
@router.callback_query(F.data.in_(["set_projects_sort_new", "set_projects_sort_old"]))
async def set_projects_sort_order(callback: CallbackQuery):
    """Устанавливает порядок дат (Сначала новые / Сначала старые) для Projects."""
    uid = callback.from_user.id
    if uid not in user_settings: user_settings[uid] = default_settings()
    user_settings[uid]["projects_sort_order"] = "new" if callback.data == "set_projects_sort_new" else "old"
    save_user_settings()
    await safe_edit(callback, reply_markup=projects_settings_kb(uid))
    await callback.answer()

@router.callback_query(F.data.in_(["set_projects_dir_ttb", "set_projects_dir_btt"]))
async def set_projects_sort_dir(callback: CallbackQuery):
    """Устанавливает направление списка (Сверху вниз / Снизу вверх) для Projects."""
    uid = callback.from_user.id
    if uid not in user_settings: user_settings[uid] = default_settings()
    user_settings[uid]["projects_sort_dir"] = "ttb" if callback.data == "set_projects_dir_ttb" else "btt"
    save_user_settings()
    await safe_edit(callback, reply_markup=projects_settings_kb(uid))
    await callback.answer()

# ==============================================================================
# 4. ХЭНДЛЕРЫ ДЕДЛАЙНОВ И РЕНДЕР
# ==============================================================================
# Используем переменную из config.py
@router.message(F.text == config.BTN_DEADLINES)
async def handle_deadlines_msg(message: Message):
    """Обрабатывает кнопку '📅 Дедлайны' в зависимости от текущего меню (Конфликт-фри)."""
    uid = message.from_user.id
    if user_menus.get(uid) == "settings":
        # Если нажато из меню настроек, открываем настройки отображения
        await message.answer("📅 <b>Настройки дедлайнов</b>\n\nВыбери, как выводить дедлайны:", reply_markup=display_settings_kb(uid), parse_mode="HTML")
    else:
        # По умолчанию (из главного меню) выводим список
        await render_upcoming(message, "1", 0, is_callback=False)


@router.callback_query(F.data.startswith("up|"))
async def show_upcoming_clb(callback: CallbackQuery):
    """Обрабатывает нажатия inline-кнопок периода и пагинации для дедлайнов."""
    parts = callback.data.split("|")
    await render_upcoming(callback, parts[1], int(parts[2]), is_callback=True)


async def render_upcoming(event, period: str, page: int, is_callback: bool):
    """Рендерит список дедлайнов или просроченных событий с учетом сортировок и пагинации."""
    limit = config.PAGINATION_LIMIT
    offset = page * limit
    now = utils.get_now()

    is_overdue = (str(period) == "overdue")
    # Периоды на кнопках в дедлайнах
    period_titles = {"1": "1 день", "2": "2 дня", "4": "4 дня", "7": "Неделя", "14": "2 недели", "30": "Месяц", "90": "3 месяца"}

    if is_overdue:
        all_notes = vault.get_overdue_notes()
        title_text = "🔴 Просроченные"
    else:
        if period == "all":
            max_date = now + timedelta(days=365 * 10)
            title_text = "📅 Все будущие"
        else:
            # Убрано избыточное тернарное условие
            days = int(period)
            # Если включен режим "today", сдвигаем границу до 23:59 нужного дня для всех кнопок
            if getattr(config, 'DEADLINES_DAY_VIEW', '24h') == "today":
                # "1 день" = сегодня (0 дней вперед), "2 дня" = завтра (1 день вперед) и т.д.
                max_date = (now + timedelta(days=days - 1)).replace(hour=23, minute=59, second=0, microsecond=0)
            else:
                max_date = now + timedelta(days=days)
            title_text = f"📅 {period_titles.get(period, f'{days} дн.')}"
        all_notes = vault.get_upcoming_notes(max_date=max_date)

    uid = event.from_user.id
    s = user_settings.get(uid, default_settings())

    # Подготовка порядка источников для статической группировки
    enabled_sources = sorted([v for v in config.SOURCES.values() if v.get("enabled")], key=lambda x: x.get("order", 99))
    source_order_map = {v["name"]: v.get("order", 99) for v in enabled_sources}

    # Логика сортировки. Разворачиваем весь массив, чтобы свежие/старые попадали на 1-ю страницу.
    sort_order = s.get("sort_order", "new")
    if is_overdue:
        # Для просроченных: new (Сначала новые) = reverse=True (вчерашние наверху), old (Сначала старые) = reverse=False (древние наверху)
        reverse_base_sort = (sort_order == "new")
    else:
        # Для будущих: new (Сначала новые) = reverse=False (завтра наверху), old (Сначала старые) = reverse=True (через год наверху)
        reverse_base_sort = (sort_order == "old")

    sorted_notes = utils.sort_notes(
        all_notes,
        group_by_source=s.get("group_by_source", False) and config.SHOW_SOURCES,
        dynamic_sort=s.get("dynamic_sort", True),
        source_order_map=source_order_map,
        reverse_base_sort=reverse_base_sort
    )

    total_count = len(sorted_notes)
    notes = sorted_notes[offset: offset + limit]

    # Направление списка (Сверху вниз / Снизу вверх)
    if s.get("sort_dir", "btt") == "btt":
        notes.reverse()

    max_pages = max(0, (total_count - 1) // limit) if total_count > 0 else 0
    page_info = f" | Стр. {page + 1} из {max_pages + 1}" if total_count > 0 else ""
    text = f"<b>{title_text}</b>\n(Всего: {total_count}{page_info})\n\n"

    if not notes:
        text += "Пусто..."
    else:
        limit_reached = False
        current_src = None

        for n in notes:
            if s.get("group_by_source", False) and config.SHOW_SOURCES:
                if n['source'] != current_src:
                    current_src = n['source']
                    text += f"{SOURCE_ICON_BY_NAME.get(current_src, SOURCE_ICON)} <b>{current_src}</b>\n➖➖➖➖➖➖➖➖➖➖➖➖➖➖➖\n"

            card = format_note_card(n, show_source=not s.get("group_by_source", False) and config.SHOW_SOURCES)
            if len(text) + len(card) > 4000:
                text += "\n<blockquote>... (остальные заметки не поместились)</blockquote>\n"
                limit_reached = True
                break
            text += card
        if not limit_reached: text += "\n"

    kb = upcoming_kb(period, page, total_count, limit)
    if is_callback:
        try:
            await event.message.edit_text(text[:4096], reply_markup=kb, parse_mode="HTML")
        except TelegramBadRequest as e:
            if "not modified" not in str(e): raise e
        await event.answer()
    else:
        await event.answer(text[:4096], reply_markup=kb, parse_mode="HTML")


# ==============================================================================
# 5. ХЭНДЛЕРЫ ЗАДАЧ И РЕНДЕР СПИСКОВ
# ==============================================================================
# Унифицирована логика кнопок Kanban/Projects/Tasks с учетом текущего меню (как в Дедлайнах)
@router.message(F.text.in_(SOURCE_BUTTONS.keys()))
async def show_source_list(message: Message):
    """Выводит список по кнопке источника (Kanban, Tasks, Projects) или открывает настройки, если мы в меню настроек."""
    uid = message.from_user.id
    source = SOURCE_BUTTONS[message.text]

    # Если кнопка нажата из меню настроек, открываем соответствующие настройки
    if user_menus.get(uid) == "settings":
        if source == 'kanban':
            await message.answer("📋 <b>Настройки Kanban</b>\n\nВыбери, как выводить доски и заметки:", reply_markup=kanban_settings_kb(uid), parse_mode="HTML")
        elif source == 'tasks':
            await message.answer("📝 <b>Настройки Tasks/Todo</b>\n\nВыбери, как выводить задачи:", reply_markup=tasks_settings_kb(uid), parse_mode="HTML")
        elif source == 'projects':
            await message.answer("📁 <b>Настройки Projects</b>\n\nВыбери, как выводить проекты:", reply_markup=projects_settings_kb(uid), parse_mode="HTML")
        return

    # По умолчанию (из главного меню) выводим список
    if source == 'projects':
        # Сканируем папку Projects
        projects = vault.get_projects_list()
        if len(projects) == 1:
            # Если проект 1, открываем его сразу
            await render_tasks(message, "All", 0, False, source='projects', project_name=projects[0])
        elif len(projects) > 1:
            # Если проектов 2+, показываем меню выбора
            await message.answer("📁 Выберите проект:", reply_markup=projects_menu_kb(projects))
        else:
            await message.answer("В папке Projects пока нет подпапок с проектами.")
        return
    await render_tasks(message, source.capitalize(), 0, False, source=source)

# Обработка выбора проекта из меню
@router.callback_query(F.data.startswith("prjset|"))
async def select_project_clb(callback: CallbackQuery):
    """Открывает задачи выбранного проекта."""
    project_name = callback.data.split("|", 1)[1]
    await render_tasks(callback, "All", 0, True, source='projects', project_name=project_name)

# Возврат к меню выбора проектов
@router.callback_query(F.data == "back_to_prj_menu")
async def back_to_prj_menu(callback: CallbackQuery):
    """Возвращает к выбору проекта."""
    projects = vault.get_projects_list()
    await safe_edit(callback, text="📁 Выберите проект:", reply_markup=projects_menu_kb(projects))
    await callback.answer()

@router.callback_query(F.data.startswith("pg|"))
async def paginate_tasks(callback: CallbackQuery):
    """Обрабатывает пагинацию списка задач и переключение статусов Projects."""
    parts = callback.data.split("|")
    # Формат: pg|source|project_name|status|page
    source = parts[1]
    project_name = parts[2] if parts[2] != '_' else None
    status = parts[3]
    page = int(parts[4])
    await render_tasks(callback, status, page, True, source=source, project_name=project_name)


# ИЗМЕНЕНО: Добавлен параметр project_name
async def render_tasks(event, status: str, page: int, is_callback: bool, source: str = 'projects', project_name: str = None):
    """Рендерит список задач с учетом пагинации, источника и умной сборки текста."""
    limit = config.PAGINATION_LIMIT
    offset = page * limit
    # Если выбран статус "All", фильтр не применяем (показываем все)
    status_filter = status if source == 'projects' and status != 'All' else None

    # Передаем project_name в парсер
    all_notes = vault.get_notes(status_filter=status_filter, source=source, project_name=project_name)
    total_count = len(all_notes)

    uid = event.from_user.id
    s = user_settings.get(uid, default_settings())

    if source == 'kanban':
        sort_order = s.get("kanban_sort_order", "new")
        sort_dir = s.get("kanban_sort_dir", "btt")

        # ИСПРАВЛЕНО: Унифицировано с Дедлайнами. "new" = False (ближайшие внизу)
        reverse_base_sort = (sort_order == "old")

        kanban_group = s.get("kanban_primary_group", "none")
        kanban_dynamic_sort = s.get("kanban_dynamic_sort", True)  # НОВОЕ: Читаем настройку

        # Учет настройки "С группировкой" (если включена - kanban_group != "none")
        if kanban_group == "none":
            sorted_notes = utils.sort_notes(all_notes, reverse_base_sort=reverse_base_sort)
        else:
            vg_config = {
                "group_names_order": ["Kanban Boards", "Kanban Notes"] if kanban_group == "boards_first" else ["Kanban Notes", "Kanban Boards"],
                "key_func": lambda n: "Kanban Boards" if 'kanban-plugin' in n.get('raw_metadata', {}) else "Kanban Notes"
            }
            # Передаем флаг dynamic_sort в универсальную функцию сортировки
            sorted_notes = utils.sort_notes(
                all_notes,
                virtual_group_config=vg_config,
                reverse_base_sort=reverse_base_sort,
                dynamic_sort=kanban_dynamic_sort
            )
    elif source == 'tasks':
        sort_order = s.get("tasks_sort_order", "new")
        sort_dir = s.get("tasks_sort_dir", "btt")
        # Унифицировано с Дедлайнами. "new" = False (ближайшие внизу)
        reverse_base_sort = (sort_order == "old")
        sorted_notes = utils.sort_notes(all_notes, reverse_base_sort=reverse_base_sort)
    else:
        # Для Projects логика сортировки аналогична дедлайнам
        sort_order = s.get("projects_sort_order", "new")
        sort_dir = s.get("projects_sort_dir", "btt")
        reverse_base_sort = (sort_order == "old")
        sorted_notes = utils.sort_notes(all_notes, reverse_base_sort=reverse_base_sort)

    notes = sorted_notes[offset: offset + limit]

    # Направление списка для Kanban, Tasks и Projects
    if source in ['kanban', 'tasks', 'projects'] and sort_dir == "btt":
        notes.reverse()

    src_cfg = config.SOURCES.get(source, {})
    src_name = src_cfg.get("name", source.capitalize())
    src_icon = _source_icon_for(source)

    # --- УБРАНЫ ТЕКСТОВЫЕ ЯРЛЫКИ "Источник:" и "Статус:" (Эмодзи самодостаточны) ---
    # ИЗМЕНЕНО: Заголовок для конкретного проекта
    if source == 'projects':
        tail = f"{_status_icon(status)}{status}" if status != 'All' else "Все"
        if project_name: tail = f"{project_name} | {tail}"
    elif source == 'kanban': tail = "Канбан-доски"
    elif source == 'tasks': tail = "Задачи (Tasks)"
    else: tail = ""

    max_pages = max(0, (total_count - 1) // limit) if total_count > 0 else 0
    page_info = f" | Стр. {page + 1} из {max_pages + 1}" if total_count > 0 else ""
    title = f"<b>{src_icon} {src_name}"
    if tail: title += f" | {tail}"
    title += f"</b>\n(Всего: {total_count}{page_info})\n\n"

    if not notes:
        text = title + "Пусто..."
    else:
        text = title
        limit_reached = False
        current_vg = None

        for note in notes:
            # Рендер шапок виртуальных групп для Канбана (если не выбрано "без группировки")
            if source == 'kanban' and s.get("kanban_primary_group", "none") != "none":
                vg_name = "Kanban Boards" if 'kanban-plugin' in note.get('raw_metadata', {}) else "Kanban Notes"
                if vg_name != current_vg:
                    current_vg = vg_name
                    text += f"<b>{vg_name}</b>\n➖➖➖➖➖➖➖➖➖➖➖➖➖➖➖\n"

            card = format_note_card(note)
            if len(text) + len(card) > 4000:
                text += "\n<blockquote>... (остальные заметки не поместились)</blockquote>\n"
                limit_reached = True
                break
            text += card
        if not limit_reached: text += "\n"

    # Передаем project_name в клавиатуру
    kb = tasks_nav_kb(source, status, page, total_count, project_name=project_name, limit=limit)
    if is_callback:
        try: await event.message.edit_text(text[:4096], reply_markup=kb, parse_mode="HTML")
        except TelegramBadRequest as e:
            if "not modified" not in str(e): raise e
        await event.answer()
    else:
        await event.answer(text[:4096], reply_markup=kb, parse_mode="HTML")

# ==============================================================================
# 6. НАСТРОЙКИ ИНТЕРВАЛОВ
# ==============================================================================
@router.callback_query(F.data.startswith("tog_min_"))
async def toggle_minutes(callback: CallbackQuery):
    """Включает/выключает интервал уведомлений."""
    uid = callback.from_user.id
    mins = int(callback.data.split("_")[-1])
    if uid not in user_settings: user_settings[uid] = default_settings()
    if mins in user_settings[uid]["intervals"]: user_settings[uid]["intervals"].remove(mins)
    else: user_settings[uid]["intervals"].append(mins)
    save_user_settings()
    await safe_edit(callback, reply_markup=settings_kb(uid))
    await callback.answer()

@router.callback_query(F.data == "tog_start")
async def toggle_start(callback: CallbackQuery):
    """Включает/выключает уведомление 'В момент начала'."""
    uid = callback.from_user.id
    if uid not in user_settings: user_settings[uid] = default_settings()
    user_settings[uid]["at_start"] = not user_settings[uid]["at_start"]
    save_user_settings()
    await safe_edit(callback, reply_markup=settings_kb(uid))
    await callback.answer()

# Беззвучные уведомления
@router.callback_query(F.data == "tog_silent")
async def toggle_silent(callback: CallbackQuery):
    """Включает/выключает беззвучные уведомления."""
    uid = callback.from_user.id
    if uid not in user_settings: user_settings[uid] = default_settings()
    user_settings[uid]["silent_notifications"] = not user_settings[uid].get("silent_notifications", False)
    save_user_settings()
    await safe_edit(callback, reply_markup=settings_kb(uid))
    await callback.answer()

# НОВЫЙ ХЭНДЛЕР: Утренняя сводка
@router.callback_query(F.data == "tog_digest")
async def toggle_digest(callback: CallbackQuery):
    """Включает/выключает утреннюю сводку."""
    uid = callback.from_user.id
    if uid not in user_settings: user_settings[uid] = default_settings()
    user_settings[uid]["digest_enabled"] = not user_settings[uid].get("digest_enabled", False)
    save_user_settings()
    await safe_edit(callback, reply_markup=extra_settings_kb(uid))
    await callback.answer()

# НОВЫЙ ХЭНДЛЕР: Тихие часы
@router.callback_query(F.data == "tog_quiet")
async def toggle_quiet(callback: CallbackQuery):
    """Включает/выключает тихие часы."""
    uid = callback.from_user.id
    if uid not in user_settings: user_settings[uid] = default_settings()
    user_settings[uid]["quiet_hours"] = not user_settings[uid].get("quiet_hours", False)
    save_user_settings()
    await safe_edit(callback, reply_markup=extra_settings_kb(uid))
    await callback.answer()

@router.callback_query(F.data == "clear_intervals")
async def clear_intervals(callback: CallbackQuery):
    """Сбрасывает все интервалы уведомлений пользователя."""
    uid = callback.from_user.id
    if uid not in user_settings: user_settings[uid] = default_settings()
    user_settings[uid]["intervals"] = []
    user_settings[uid]["at_start"] = False
    save_user_settings()
    await safe_edit(callback, reply_markup=settings_kb(uid))
    await callback.answer("Все интервалы сброшены!", show_alert=False)