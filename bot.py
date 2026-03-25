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

# Direct pairs:
# for these selected Currency IN -> Currency OUT pairs
# the entered exchange rate is used in the formula as-is.
# For reverse pairs the formula uses 1 / entered_rate.
DIRECT_PAIRS = {
    ("RUB", "USD"),
    ("RUB", "USN"),
    ("RUB", "USDT"),
    ("RUB", "AED"),
    ("RUB", "AEN"),
    ("RUB", "CNY"),
    ("AED", "USD"),
    ("AED", "USN"),
    ("AED", "USDT"),
    ("AED", "AEN"),
    ("AEN", "USD"),
    ("AEN", "USN"),
    ("AEN", "USDT"),
    ("CNY", "USD"),
    ("CNY", "USN"),
    ("CNY", "USDT"),
    ("CNY", "AED"),
    ("CNY", "AEN"),
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

SPECIAL_CROSS_PAIR_SETS = {
    frozenset(("RUB", "AED")),
    frozenset(("RUB", "AEN")),
}
DEFAULT_AEDUSD_RATE = Decimal("3.6725")

# Table colors
WHITE = (255, 255, 255)
ROW_BLUE = (214, 236, 255)
GRID = (210, 210, 210)
TEXT = (18, 18, 18)

RateMode = Literal["direct", "reverse", "custom"]

dp = Dispatcher(storage=MemoryStorage())


class CalcStates(StatesGroup):
    waiting_currency_in = State()
    waiting_currency_out = State()
    waiting_amount_side = State()
    waiting_amount = State()
    waiting_commission = State()
    waiting_rate = State()
    waiting_rubusd_rate = State()
    waiting_aedusd_choice = State()
    waiting_aedusd_custom = State()


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
            [InlineKeyboardButton(text=f"Amount in {currency_in}", callback_data="amount_side:in")],
            [InlineKeyboardButton(text=f"Amount in {currency_out}", callback_data="amount_side:out")],
            [InlineKeyboardButton(text="🔄 Restart", callback_data="restart")],
        ]
    )


def build_aedusd_choice_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="3.6725", callback_data="aedusd_choice:3.6725"),
                InlineKeyboardButton(text="Enter your rate", callback_data="aedusd_choice:custom"),
            ],
            [InlineKeyboardButton(text="🔄 Restart", callback_data="restart")],
        ]
    )


async def start_dialog(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(CalcStates.waiting_currency_in)
    await message.answer(
        "💱 <b>Currency IN</b>\n\nCurrency that we send:",
        reply_markup=build_currency_keyboard("currency_in"),
        parse_mode="HTML",
    )


def is_special_cross_pair(currency_in: str, currency_out: str) -> bool:
    return frozenset((currency_in, currency_out)) in SPECIAL_CROSS_PAIR_SETS


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
    text = f"{quantized:,.{places}f}"
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
            "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
            "DejaVuSans-Bold.ttf",
        ]
        if bold
        else [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf",
            "DejaVuSans.ttf",
        ]
    )

    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def get_rate_mode(currency_in: str, currency_out: str) -> RateMode:
    if (currency_in, currency_out) in DIRECT_PAIRS:
        return "direct"
    if (currency_out, currency_in) in DIRECT_PAIRS:
        return "reverse"
    return "custom"


def resolve_rate(currency_in: str, currency_out: str, entered_rate: Decimal) -> Tuple[Decimal, RateMode]:
    if entered_rate <= 0:
        raise ValueError("Exchange rate must be greater than 0")

    rate_mode = get_rate_mode(currency_in, currency_out)
    if rate_mode == "direct":
        return entered_rate, "direct"
    if rate_mode == "reverse":
        return Decimal("1") / entered_rate, "reverse"
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

    # Main equality:
    # amount in currency out = amount in currency in * (1 - commission) / exchange rate
    #
    # For direct pairs: use the entered rate as-is.
    # For reverse pairs: use 1 / entered_rate inside the formula.
    if input_is_currency_in:
        amount_in = amount_value
        before_margin = amount_in * commission_factor
        amount_out = before_margin / effective_rate
    else:
        amount_out = amount_value
        amount_in = amount_out * effective_rate / commission_factor
        before_margin = amount_out * effective_rate

    return {
        "currency_in": currency_in,
        "currency_out": currency_out,
        "amount_in": amount_in,
        "amount_out": amount_out,
        "before_margin": before_margin,
        "entered_rate": entered_rate,
        "effective_rate": effective_rate,
        "commission_pct": commission_pct,
        "input_is_currency_in": input_is_currency_in,
        "rate_mode": rate_mode,
    }


def fit_font(
    draw: ImageDraw.ImageDraw,
    text: str,
    box_width: int,
    box_height: int,
    start_size: int,
    min_size: int,
    bold: bool,
) -> ImageFont.ImageFont:
    size = start_size
    while size >= min_size:
        font = load_font(size, bold=bold)
        bbox = draw.textbbox((0, 0), text, font=font)
        width = bbox[2] - bbox[0]
        height = bbox[3] - bbox[1]
        if width <= box_width * 0.97 and height <= box_height * 0.82:
            return font
        size -= 4
    return load_font(min_size, bold=bold)


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
    y = top + (bottom - top - text_h) / 2 - 3
    draw.text((x, y), text, font=font, fill=fill, stroke_width=2, stroke_fill=fill)


def prepare_display_rows(result: Dict[str, Any]) -> list[tuple[str, str, str, bool]]:
    currency_in = result["currency_in"]
    currency_out = result["currency_out"]
    amount_in = result["amount_in"]
    amount_out = result["amount_out"]
    before_margin = result["before_margin"]

    if result.get("special_cross_pair"):
        return [
            ("TO AMOUNT IN", currency_out, format_decimal(amount_out, 2, strip_trailing=False), True),
            ("FX RATE", "RUBUSD", format_rate_value(result["rubusd_rate"]), False),
            ("FX RATE", "AEDUSD", format_rate_value(result["aedusd_rate"]), False),
            ("BEFORE MARGIN", currency_in, format_decimal(before_margin, 2, strip_trailing=False), False),
            ("CONTRACT MARGIN", "%", f"{format_decimal(result['commission_pct'], 2, strip_trailing=False)}%", False),
            ("FROM AMOUNT IN", currency_in, format_decimal(amount_in, 2, strip_trailing=False), True),
        ]

    pair_label = f"{currency_in}{currency_out}"
    return [
        ("TO AMOUNT IN", currency_out, format_decimal(amount_out, 2, strip_trailing=False), True),
        ("FX RATE", pair_label, format_rate_value(result["entered_rate"]), False),
        ("BEFORE MARGIN", currency_in, format_decimal(before_margin, 2, strip_trailing=False), False),
        ("CONTRACT MARGIN", "%", f"{format_decimal(result['commission_pct'], 2, strip_trailing=False)}%", False),
        ("FROM AMOUNT IN", currency_in, format_decimal(amount_in, 2, strip_trailing=False), True),
    ]


def create_result_image(result: Dict[str, Any]) -> bytes:
    rows = prepare_display_rows(result)

    width = 2300
    row_height = 220
    outer_margin = 36
    table_width = width - 2 * outer_margin
    height = outer_margin * 2 + row_height * len(rows)

    img = Image.new("RGB", (width, height), WHITE)
    draw = ImageDraw.Draw(img)

    col1_w = int(table_width * 0.40)
    col2_w = int(table_width * 0.22)
    col3_w = table_width - col1_w - col2_w

    x0 = outer_margin
    x1 = x0 + col1_w
    x2 = x1 + col2_w
    x3 = x2 + col3_w
    table_top = outer_margin

    blue_rows = {1, 3, 5} if len(rows) == 6 else {1, 3}

    for idx in range(len(rows)):
        top = table_top + idx * row_height
        bottom = top + row_height
        fill = ROW_BLUE if idx in blue_rows else WHITE
        draw.rectangle((x0, top, x3, bottom), fill=fill)

    table_bottom = table_top + row_height * len(rows)

    for i in range(len(rows) + 1):
        y = table_top + i * row_height
        draw.line((x0, y, x3, y), fill=GRID, width=3)
    for x in (x0, x1, x2, x3):
        draw.line((x, table_top, x, table_bottom), fill=GRID, width=3)

    for idx, (label, ccy, value, emphasize_amount) in enumerate(rows):
        top = table_top + idx * row_height
        bottom = top + row_height

        label_font = fit_font(
            draw,
            label,
            x1 - x0,
            row_height,
            start_size=124 if emphasize_amount else 116,
            min_size=84,
            bold=True,
        )
        ccy_font = fit_font(
            draw,
            ccy,
            x2 - x1,
            row_height,
            start_size=124 if emphasize_amount else 116,
            min_size=84,
            bold=True,
        )
        value_font = fit_font(
            draw,
            value,
            x3 - x2,
            row_height,
            start_size=142 if emphasize_amount else 132,
            min_size=96,
            bold=True,
        )

        center_text(draw, (x0, top, x1, bottom), label, label_font, TEXT)
        center_text(draw, (x1, top, x2, bottom), ccy, ccy_font, TEXT)
        center_text(draw, (x2, top, x3, bottom), value, value_font, TEXT)

    buffer = BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


async def send_result_photo(
    message: Message,
    state: FSMContext,
    entered_rate: Decimal,
    rubusd_rate: Optional[Decimal] = None,
    aedusd_rate: Optional[Decimal] = None,
) -> None:
    data = await state.get_data()

    try:
        result = calculate_result(
            amount_value=Decimal(data["amount"]),
            currency_in=data["currency_in"],
            currency_out=data["currency_out"],
            entered_rate=entered_rate,
            commission_pct=Decimal(data["commission_pct"]),
            input_is_currency_in=data["input_side"] == "in",
        )
    except ValueError as exc:
        await message.answer(f"❌ {exc}")
        return

    if rubusd_rate is not None and aedusd_rate is not None:
        result["special_cross_pair"] = True
        result["rubusd_rate"] = rubusd_rate
        result["aedusd_rate"] = aedusd_rate

    image_bytes = create_result_image(result)

    await message.answer_photo(
        BufferedInputFile(image_bytes, filename="calculation.png"),
        caption="✅ <b>Calculation complete</b>",
        parse_mode="HTML",
        reply_markup=restart_keyboard(),
    )
    await state.clear()


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
        f"Currency that we receive:",
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

    data = await state.get_data()
    if is_special_cross_pair(data["currency_in"], data["currency_out"]):
        await state.set_state(CalcStates.waiting_rubusd_rate)
        await message.answer(
            "📈 <b>Enter exchange rate for RUBUSD</b>\n\n"
            "Examples: <code>90</code> or <code>90.25</code>",
            reply_markup=restart_keyboard(),
            parse_mode="HTML",
        )
        return

    await state.set_state(CalcStates.waiting_rate)
    rate_label = f"{data['currency_in']}{data['currency_out']}"

    await message.answer(
        f"📈 <b>Enter exchange rate for {rate_label}</b>\n\n"
        "Examples: <code>0.0112</code> or <code>90</code>",
        reply_markup=restart_keyboard(),
        parse_mode="HTML",
    )


@dp.message(CalcStates.waiting_rubusd_rate)
async def process_rubusd_rate(message: Message, state: FSMContext) -> None:
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

    await state.update_data(rubusd_rate=str(rate))
    await state.set_state(CalcStates.waiting_aedusd_choice)

    await message.answer(
        "📈 <b>Select AEDUSD rate</b>",
        reply_markup=build_aedusd_choice_keyboard(),
        parse_mode="HTML",
    )


@dp.callback_query(CalcStates.waiting_aedusd_choice, F.data.startswith("aedusd_choice:"))
async def aedusd_choice_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    choice = callback.data.split(":", maxsplit=1)[1]

    if choice == "custom":
        await state.set_state(CalcStates.waiting_aedusd_custom)
        await callback.message.edit_text(
            "📈 <b>Enter exchange rate for AEDUSD</b>\n\n"
            "Example: <code>3.6725</code>",
            reply_markup=restart_keyboard(),
            parse_mode="HTML",
        )
        return

    try:
        aedusd_rate = parse_decimal(choice)
    except ValueError:
        await callback.message.answer("❌ Invalid AEDUSD rate. Press Restart and try again.")
        await state.clear()
        return

    data = await state.get_data()
    rubusd_rate = Decimal(data["rubusd_rate"])
    cross_rate = rubusd_rate / aedusd_rate

    await send_result_photo(
        callback.message,
        state,
        entered_rate=cross_rate,
        rubusd_rate=rubusd_rate,
        aedusd_rate=aedusd_rate,
    )


@dp.message(CalcStates.waiting_aedusd_choice)
async def waiting_aedusd_choice_message(message: Message) -> None:
    await message.answer("Use the AEDUSD buttons or press Restart.")


@dp.message(CalcStates.waiting_aedusd_custom)
async def process_aedusd_custom(message: Message, state: FSMContext) -> None:
    if not message.text:
        await message.answer("❌ Send the exchange rate as text. Example: 3.6725")
        return

    try:
        aedusd_rate = parse_decimal(message.text)
    except ValueError:
        await message.answer("❌ Invalid exchange rate. Example: 3.6725")
        return

    if aedusd_rate <= 0:
        await message.answer("❌ Exchange rate must be greater than 0.")
        return

    data = await state.get_data()
    rubusd_rate = Decimal(data["rubusd_rate"])
    cross_rate = rubusd_rate / aedusd_rate

    await send_result_photo(
        message,
        state,
        entered_rate=cross_rate,
        rubusd_rate=rubusd_rate,
        aedusd_rate=aedusd_rate,
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

    await send_result_photo(message, state, entered_rate=rate)


@dp.callback_query(
    F.data.startswith("currency_in:")
    | F.data.startswith("currency_out:")
    | F.data.startswith("amount_side:")
    | F.data.startswith("aedusd_choice:")
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
