import asyncio
import logging
import os
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from io import BytesIO
from typing import Any, Dict, Literal, Optional, Tuple

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from PIL import Image, ImageDraw, ImageFont


CURRENCIES = ["RUB", "USD", "USN", "USDT", "AED", "AEN", "CNY"]

# Прямые пары из исходного ТЗ для логики инверсии курса.
# Логику расчета не меняем: внутренне currency_in = what we send, currency_out = what we receive.
DIRECT_PAIRS = {
    ("RUB", "USD"),
    ("RUB", "USN"),
    ("RUB", "USDT"),
    ("RUB", "AED"),
    ("RUB", "AEN"),
    ("RUB", "CNY"),
}

CURRENCY_LABELS = {
    "RUB": "🇷🇺 RUB",
    "USD": "🇺🇸 USD",
    "USN": "🪙 USN",
    "USDT": "₮ USDT",
    "AED": "🇦🇪 AED",
    "AEN": "🇦🇪 AEN",
    "CNY": "🇨🇳 CNY",
}

# Новый визуальный стиль итоговой таблицы: как на референсе.
TABLE_BG = (247, 247, 247)
WHITE = (255, 255, 255)
GRID = (223, 223, 223)
HIGHLIGHT = (182, 231, 205)
TEXT = (17, 17, 17)

RateMode = Literal["direct", "reverse", "custom"]


dp = Dispatcher(storage=MemoryStorage())


class CalcStates(StatesGroup):
    waiting_currency_in = State()
    waiting_currency_out = State()
    waiting_amount_side = State()
    waiting_amount = State()
    waiting_commission = State()
    waiting_rate = State()


def restart_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔄 Restart", callback_data="restart")]]
    )


def build_currency_keyboard(prefix: str, exclude: Optional[str] = None) -> InlineKeyboardMarkup:
    buttons = []
    for currency in CURRENCIES:
        if currency == exclude:
            continue
        buttons.append(
            InlineKeyboardButton(
                text=CURRENCY_LABELS[currency],
                callback_data=f"{prefix}:{currency}",
            )
        )

    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton(text="🔄 Restart", callback_data="restart")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_amount_side_keyboard(currency_in: str, currency_out: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"Amount in {currency_in}",
                    callback_data="amount_side:in",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"Amount in {currency_out}",
                    callback_data="amount_side:out",
                )
            ],
            [InlineKeyboardButton(text="🔄 Restart", callback_data="restart")],
        ]
    )


async def start_dialog(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(CalcStates.waiting_currency_in)
    await message.answer(
        "💱 <b>Currency IN</b>\n\nCurrency that we receive:",
        reply_markup=build_currency_keyboard("currency_in"),
        parse_mode="HTML",
    )


def parse_decimal(text: str) -> Decimal:
    normalized = text.strip().replace(" ", "").replace(",", ".")
    if not normalized:
        raise ValueError("Empty input")

    try:
        return Decimal(normalized)
    except InvalidOperation as exc:
        raise ValueError("Invalid number") from exc


def quantize_pattern(places: int) -> Decimal:
    return Decimal("1").scaleb(-places)


def format_decimal(value: Decimal, places: int = 2, strip_trailing: bool = False) -> str:
    quantized = value.quantize(quantize_pattern(places), rounding=ROUND_HALF_UP)
    text = f"{quantized:,.{places}f}".replace(",", " ")
    if strip_trailing and "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def format_rate_value(value: Decimal) -> str:
    if abs(value) >= Decimal("1"):
        return format_decimal(value, 4, strip_trailing=False)
    return format_decimal(value, 8, strip_trailing=True)


def load_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    candidates = (
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
            "DejaVuSans-Bold.ttf",
        ]
        if bold
        else [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
            "DejaVuSans.ttf",
        ]
    )

    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def get_rate_mode(currency_in: str, currency_out: str) -> RateMode:
    if (currency_in, currency_out) in DIRECT_PAIRS:
        return "direct"
    if (currency_out, currency_in) in DIRECT_PAIRS:
        return "reverse"
    return "custom"


def get_entered_rate_pair_label(currency_in: str, currency_out: str) -> str:
    rate_mode = get_rate_mode(currency_in, currency_out)
    if rate_mode == "reverse":
        return f"{currency_out}-{currency_in}"
    return f"{currency_in}-{currency_out}"


def resolve_rate(currency_in: str, currency_out: str, entered_rate: Decimal) -> Tuple[Decimal, RateMode]:
    if entered_rate <= 0:
        raise ValueError("Exchange rate must be greater than 0")

    rate_mode = get_rate_mode(currency_in, currency_out)
    if rate_mode == "direct":
        return entered_rate, "direct"
    if rate_mode == "reverse":
        return Decimal("1") / entered_rate, "reverse"

    # Для кросс-пар вне списка RUB-XXX используем курс как ввёл пользователь.
    return entered_rate, "custom"


def get_calc_currencies_from_ui(ui_currency_in: str, ui_currency_out: str) -> Tuple[str, str]:
    # Пользовательский интерфейс:
    # currency_in  = what we receive
    # currency_out = what we send
    # Внутренний расчет оставляем как раньше:
    # calc_currency_in  = what we send
    # calc_currency_out = what we receive
    return ui_currency_out, ui_currency_in


def calculate_result(
    amount_value: Decimal,
    currency_in: str,
    currency_out: str,
    entered_rate: Decimal,
    commission_pct: Decimal,
    input_is_currency_in: bool,
) -> Dict[str, Any]:
    if amount_value <= 0:
        raise ValueError("Amount must be greater than 0")
    if commission_pct < 0 or commission_pct >= 100:
        raise ValueError("Commission must be between 0 and 100")

    effective_rate, rate_mode = resolve_rate(currency_in, currency_out, entered_rate)
    commission_factor = Decimal("1") - (commission_pct / Decimal("100"))

    if commission_factor <= 0:
        raise ValueError("Commission leaves nothing to calculate")

    # Базовая логика расчета остается прежней:
    # Amount in currency in = (Amount in currency out) * exchange rate / (1 - commission)
    # where currency_in is the currency we send (internal)
    # and currency_out is the currency we receive (internal).
    if input_is_currency_in:
        amount_in = amount_value
        amount_out = amount_in * commission_factor / effective_rate
    else:
        amount_out = amount_value
        amount_in = amount_out * effective_rate / commission_factor

    return {
        "currency_in": currency_in,
        "currency_out": currency_out,
        "amount_in": amount_in,
        "amount_out": amount_out,
        "entered_rate": entered_rate,
        "entered_rate_pair": get_entered_rate_pair_label(currency_in, currency_out),
        "effective_rate": effective_rate,
        "commission_pct": commission_pct,
        "input_is_currency_in": input_is_currency_in,
        "rate_mode": rate_mode,
    }


def center_text(
    draw: ImageDraw.ImageDraw,
    box: Tuple[int, int, int, int],
    text: str,
    font: ImageFont.ImageFont,
    fill: Tuple[int, int, int],
) -> None:
    left, top, right, bottom = box
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    x = left + (right - left - text_w) / 2
    y = top + (bottom - top - text_h) / 2 - 4
    draw.text((x, y), text, font=font, fill=fill)


def prepare_display_rows(result: Dict[str, Any]) -> list[tuple[str, str, str, bool, bool]]:
    to_currency = result["currency_out"]
    from_currency = result["currency_in"]
    to_amount = result["amount_out"]
    from_amount = result["amount_in"]
    rate_value = result["effective_rate"]
    before_margin = to_amount * rate_value
    pair_label = f"{to_currency}{from_currency}"

    return [
        ("TO AMOUNT IN", to_currency, format_decimal(to_amount, 2, strip_trailing=False), True, True),
        ("FX RATE", pair_label, format_rate_value(rate_value), False, True),
        ("BEFORE MARGIN", from_currency, format_decimal(before_margin, 2, strip_trailing=False), False, False),
        (
            "CONTRACT MARGIN",
            "%",
            f"{format_decimal(result['commission_pct'], 2, strip_trailing=False)}%",
            False,
            True,
        ),
        ("FROM AMOUNT IN", from_currency, format_decimal(from_amount, 2, strip_trailing=False), True, False),
    ]


def create_result_image(result: Dict[str, Any]) -> bytes:
    rows = prepare_display_rows(result)

    width = 1700
    row_height = 105
    outer_margin = 28
    table_width = width - 2 * outer_margin
    height = outer_margin * 2 + row_height * len(rows)

    img = Image.new("RGB", (width, height), WHITE)
    draw = ImageDraw.Draw(img)

    label_font_bold = load_font(50, bold=True)
    label_font_regular = load_font(46, bold=False)
    ccy_font_bold = load_font(48, bold=True)
    ccy_font_regular = load_font(44, bold=False)
    value_font_bold = load_font(50, bold=True)
    value_font_regular = load_font(46, bold=False)

    col1_w = int(table_width * 0.39)
    col2_w = int(table_width * 0.215)
    col3_w = table_width - col1_w - col2_w

    x0 = outer_margin
    x1 = x0 + col1_w
    x2 = x1 + col2_w
    x3 = x2 + col3_w

    table_top = outer_margin

    # Заливка строк и отдельных value cells.
    for idx, row in enumerate(rows):
        top = table_top + idx * row_height
        bottom = top + row_height

        # Базовая заливка всей строки.
        draw.rectangle((x0, top, x3, bottom), fill=TABLE_BG)

        # Подсветка value cells как в референсе: 1, 2 и 4 строки.
        _, _, _, _, highlight_value = row
        if highlight_value:
            draw.rectangle((x2, top, x3, bottom), fill=HIGHLIGHT)

    # Сетка.
    for i in range(len(rows) + 1):
        y = table_top + i * row_height
        draw.line((x0, y, x3, y), fill=GRID, width=2)
    for x in (x0, x1, x2, x3):
        draw.line((x, table_top, x, table_top + row_height * len(rows)), fill=GRID, width=2)

    # Текст.
    for idx, (label, ccy, value, emphasize, _) in enumerate(rows):
        top = table_top + idx * row_height
        bottom = top + row_height

        label_font = label_font_bold if emphasize else label_font_regular
        ccy_font = ccy_font_bold if emphasize else ccy_font_regular
        value_font = value_font_bold if emphasize else value_font_regular

        center_text(draw, (x0, top, x1, bottom), label, label_font, TEXT)
        center_text(draw, (x1, top, x2, bottom), ccy, ccy_font, TEXT)
        center_text(draw, (x2, top, x3, bottom), value, value_font, TEXT)

    buffer = BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await start_dialog(message, state)


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("❌ Calculation cancelled. Send /start to begin again.")


@dp.callback_query(F.data == "restart")
async def restart_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if callback.message:
        await start_dialog(callback.message, state)


@dp.callback_query(CalcStates.waiting_currency_in, F.data.startswith("currency_in:"))
async def currency_in_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    currency_in = callback.data.split(":", maxsplit=1)[1]

    if currency_in not in CURRENCIES:
        await callback.message.answer("❌ Unsupported currency. Send /start to try again.")
        await state.clear()
        return

    await state.update_data(currency_in=currency_in)
    await state.set_state(CalcStates.waiting_currency_out)

    await callback.message.edit_text(
        f"💸 <b>Currency OUT</b>\n\n"
        f"Selected Currency IN: <code>{currency_in}</code>\n"
        f"Currency that we send:",
        reply_markup=build_currency_keyboard("currency_out", exclude=currency_in),
        parse_mode="HTML",
    )


@dp.callback_query(CalcStates.waiting_currency_out, F.data.startswith("currency_out:"))
async def currency_out_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    currency_out = callback.data.split(":", maxsplit=1)[1]
    data = await state.get_data()
    currency_in = data.get("currency_in")

    if currency_out not in CURRENCIES or not currency_in:
        await callback.message.answer("❌ Session expired. Send /start to begin again.")
        await state.clear()
        return

    if currency_out == currency_in:
        await callback.answer("Currency OUT must be different", show_alert=True)
        return

    await state.update_data(currency_out=currency_out)
    await state.set_state(CalcStates.waiting_amount_side)

    await callback.message.edit_text(
        f"↕️ <b>Select the amount you want to enter</b>\n\n"
        f"<code>{currency_in} ↔ {currency_out}</code>",
        reply_markup=build_amount_side_keyboard(currency_in, currency_out),
        parse_mode="HTML",
    )


@dp.callback_query(CalcStates.waiting_amount_side, F.data.startswith("amount_side:"))
async def amount_side_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    side = callback.data.split(":", maxsplit=1)[1]
    data = await state.get_data()

    if side not in {"in", "out"}:
        await callback.message.answer("❌ Invalid option. Send /start to begin again.")
        await state.clear()
        return

    input_currency = data["currency_in"] if side == "in" else data["currency_out"]
    await state.update_data(input_side=side)
    await state.set_state(CalcStates.waiting_amount)

    await callback.message.edit_text(
        f"💵 <b>Enter the amount in {input_currency}</b>\n\n"
        f"Examples: <code>2100000</code> or <code>1000.50</code>",
        reply_markup=restart_keyboard(),
        parse_mode="HTML",
    )


@dp.message(CalcStates.waiting_amount)
async def process_amount(message: Message, state: FSMContext) -> None:
    if not message.text:
        await message.answer("❌ Send the amount as text. Example: 1000 or 1000.50")
        return

    try:
        amount = parse_decimal(message.text)
    except ValueError:
        await message.answer("❌ Invalid amount. Example: 1000 or 1000.50")
        return

    if amount <= 0:
        await message.answer("❌ Amount must be greater than 0.")
        return

    await state.update_data(amount=str(amount))
    await state.set_state(CalcStates.waiting_commission)

    await message.answer(
        "💳 <b>Enter commission in %</b>\n\n"
        "Examples: <code>0.1</code> or <code>0.35</code>\n"
        "<i>0.1 means 0.1%</i>",
        reply_markup=restart_keyboard(),
        parse_mode="HTML",
    )


@dp.message(CalcStates.waiting_commission)
async def process_commission(message: Message, state: FSMContext) -> None:
    if not message.text:
        await message.answer("❌ Send the commission as text. Example: 0.1 or 0.35")
        return

    try:
        commission_pct = parse_decimal(message.text)
    except ValueError:
        await message.answer("❌ Invalid commission. Example: 0.1 or 0.35")
        return

    if commission_pct < 0 or commission_pct >= 100:
        await message.answer("❌ Commission must be from 0 to less than 100.")
        return

    await state.update_data(commission_pct=str(commission_pct))
    await state.set_state(CalcStates.waiting_rate)

    data = await state.get_data()
    calc_currency_in, calc_currency_out = get_calc_currencies_from_ui(
        data["currency_in"], data["currency_out"]
    )
    rate_pair = get_entered_rate_pair_label(calc_currency_in, calc_currency_out)
    rate_mode = get_rate_mode(calc_currency_in, calc_currency_out)

    if rate_mode == "reverse":
        hint = (
            f"For reverse pair <code>{calc_currency_in}-{calc_currency_out}</code> enter the rate for "
            f"<code>{rate_pair}</code>. The bot will use <code>1/rate</code> automatically."
        )
    else:
        hint = "Examples: <code>0.0112</code> or <code>90</code>"

    await message.answer(
        f"📈 <b>Enter exchange rate for {rate_pair}</b>\n\n{hint}",
        reply_markup=restart_keyboard(),
        parse_mode="HTML",
    )


@dp.message(CalcStates.waiting_rate)
async def process_rate(message: Message, state: FSMContext) -> None:
    if not message.text:
        await message.answer("❌ Send the exchange rate as text. Example: 90")
        return

    try:
        rate = parse_decimal(message.text)
    except ValueError:
        await message.answer("❌ Invalid exchange rate. Example: 90")
        return

    if rate <= 0:
        await message.answer("❌ Exchange rate must be greater than 0.")
        return

    data = await state.get_data()
    calc_currency_in, calc_currency_out = get_calc_currencies_from_ui(
        data["currency_in"], data["currency_out"]
    )

    try:
        result = calculate_result(
            amount_value=Decimal(data["amount"]),
            currency_in=calc_currency_in,
            currency_out=calc_currency_out,
            entered_rate=rate,
            commission_pct=Decimal(data["commission_pct"]),
            input_is_currency_in=data["input_side"] == "out",
        )
    except ValueError as exc:
        await message.answer(f"❌ {exc}")
        return

    image_bytes = create_result_image(result)

    await message.answer_photo(
        BufferedInputFile(image_bytes, filename="calculation.png"),
        caption="✅ <b>Calculation complete</b>",
        parse_mode="HTML",
        reply_markup=restart_keyboard(),
    )
    await state.clear()


@dp.callback_query(
    F.data.startswith("currency_in:") | F.data.startswith("currency_out:") | F.data.startswith("amount_side:")
)
async def stale_callback(callback: CallbackQuery) -> None:
    await callback.answer("Session expired. Press Restart or send /start.", show_alert=False)


@dp.message()
async def fallback_message(message: Message) -> None:
    await message.answer("Send /start to begin a new calculation.")


async def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN environment variable is not set")

    logging.basicConfig(level=logging.INFO)
    bot = Bot(token=token)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
