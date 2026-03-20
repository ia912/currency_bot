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

# Прямые пары из ТЗ.
# Для обратных пар бот просит ввести курс прямой пары и в формуле использует 1 / rate.
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

# Новый стиль картинки: светло-голубой фон и крупный жирный текст.
PAGE_BG = (226, 243, 255)
CARD_BG = (255, 255, 255)
HEADER_BG = (193, 226, 250)
ALT_ROW_BG = (237, 248, 255)
BORDER = (168, 204, 232)
TEXT = (23, 35, 47)
TEXT_SOFT = (69, 86, 102)

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
        "💱 <b>Currency IN</b>\n\nChoose the currency you give:",
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
        text = text.rstrip("0").
rstrip(".")
    return text


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

    # Основное равенство по твоему уточнению:
    # amount_in_currency_in = amount_in_currency_out * exchange_rate / (1 - commission)
    # Отсюда:
    # amount_out = amount_in * (1 - commission) / exchange_rate
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


def right_aligned_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    right_x: int,
    y: int,
    font: ImageFont.ImageFont,
    fill: Tuple[int, int, int],
) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    width = bbox[2] - bbox[0]
    draw.text((right_x - width, y), text, font=font, fill=fill)


def create_result_image(result: Dict[str, Any]) -> bytes:
    width, height = 1500, 1080
    img = Image.new("RGB", (width, height), PAGE_BG)
    draw = ImageDraw.Draw(img)

    title_font = load_font(60, bold=True)
    subtitle_font = load_font(34, bold=True)
    header_font = load_font(34, bold=True)
    cell_font = load_font(34, bold=True)
    value_font = load_font(38, bold=True)
card_left, card_top, card_right, card_bottom = 50, 45, width - 50, height - 45
    draw.rounded_rectangle(
        (card_left, card_top, card_right, card_bottom),
        radius=34,
        fill=CARD_BG,
        outline=BORDER,
        width=3,
    )

    currency_in = result["currency_in"]
    currency_out = result["currency_out"]
    input_side_label = (
        f"Entered amount: {currency_in}"
        if result["input_is_currency_in"]
        else f"Entered amount: {currency_out}"
    )

    draw.text((100, 90), f"{currency_in} → {currency_out}", font=title_font, fill=TEXT)
    draw.text((100, 168), input_side_label, font=subtitle_font, fill=TEXT_SOFT)

    table_left = 80
    table_top = 255
    table_right = width - 80
    row_height = 122
    row_gap = 16

    draw.rounded_rectangle(
        (table_left, table_top, table_right, table_top + row_height),
        radius=26,
        fill=HEADER_BG,
        outline=BORDER,
        width=3,
    )

    col1_x = table_left + 36
    col2_x = table_left + 560
    col3_right = table_right - 36

    draw.text((col1_x, table_top + 35), "FIELD", font=header_font, fill=TEXT)
    draw.text((col2_x, table_top + 35), "CCY", font=header_font, fill=TEXT)
    right_aligned_text(draw, "VALUE", col3_right, table_top + 35, header_font, TEXT)

    if result["input_is_currency_in"]:
        rows = [
            ("Amount in", currency_in, format_decimal(result["amount_in"], 2, strip_trailing=True)),
            (
                "Exchange rate",
                result["entered_rate_pair"],
                format_decimal(result["entered_rate"], 8, strip_trailing=True),
            ),
            ("Commission", "%", f"{format_decimal(result['commission_pct'], 4, strip_trailing=True)}%"),
            ("Amount out", currency_out, format_decimal(result["amount_out"], 2, strip_trailing=True)),
        ]
    else:
        rows = [
            ("Amount out", currency_out, format_decimal(result["amount_out"], 2, strip_trailing=True)),
            (
                "Exchange rate",
                result["entered_rate_pair"],
                format_decimal(result["entered_rate"], 8, strip_trailing=True),
            ),
            ("Commission", "%", f"{format_decimal(result['commission_pct'], 4, strip_trailing=True)}%"),
            ("Amount in", currency_in, format_decimal(result["amount_in"], 2, strip_trailing=True)),
        ]

    for index, (label, ccy, value) in enumerate(rows, start=1):
        top = table_top + row_height + row_gap + (index - 1) * (row_height + row_gap)
        bottom = top + row_height
        row_fill = ALT_ROW_BG if index % 2 == 0 else CARD_BG

        draw.rounded_rectangle(
            (table_left, top, table_right, bottom),
            radius=24,
            fill=row_fill,
            outline=BORDER,
            width=2,
        )

        draw.text((col1_x, top + 34), label, font=cell_font, fill=TEXT)
        draw.text((col2_x, top + 34), ccy, font=cell_font, fill=TEXT)
        right_aligned_text(draw, value, col3_right, top + 30, value_font, TEXT)

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
        await callback.message.answer("❌ Unsupported currency.Send /start to try again.")
        await state.clear()
        return

    await state.update_data(currency_in=currency_in)
    await state.set_state(CalcStates.waiting_currency_out)

    await callback.message.edit_text(
        f"💸 <b>Currency OUT</b>\n\n"
        f"Selected IN: <code>{currency_in}</code>\n"
        f"Choose the currency you receive:",
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
        f"<code>{currency_in} ↔️ {currency_out}</code>",
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
    currency_in = data["currency_in"]
    currency_out = data["currency_out"]
rate_pair = get_entered_rate_pair_label(currency_in, currency_out)
    rate_mode = get_rate_mode(currency_in, currency_out)

    if rate_mode == "reverse":
        hint = (
            f"For reverse pair <code>{currency_in}-{currency_out}</code> enter the rate for "
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

    try:
        result = calculate_result(
            amount_value=Decimal(data["amount"]),
            currency_in=data["currency_in"],
            currency_out=data["currency_out"],
            entered_rate=rate,
            commission_pct=Decimal(data["commission_pct"]),
            input_is_currency_in=data["input_side"] == "in",
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


if name == "__main__":
    asyncio.run(main())
