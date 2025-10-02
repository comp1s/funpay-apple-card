import os
import logging
import re
import requests
import time
import uuid
from dotenv import load_dotenv

from FunPayAPI import Account
from FunPayAPI.updater.runner import Runner
from FunPayAPI.updater.events import NewOrderEvent, NewMessageEvent


load_dotenv()
FUNPAY_AUTH_TOKEN = os.getenv("FUNPAY_AUTH_TOKEN")
NSGIFT_LOGIN = os.getenv("NSGIFT_LOGIN")
NSGIFT_PASSWORD = os.getenv("NSGIFT_PASSWORD")
CATEGORY_ID = int(os.getenv("CATEGORY_ID", "1316"))
DEACTIVATE_CATEGORY_ID = int(os.getenv("DEACTIVATE_CATEGORY_ID", str(CATEGORY_ID)))

def _env_bool_raw(name: str):
    return os.getenv(name)

def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "y")

AUTO_REFUND_RAW = _env_bool_raw("AUTO_REFUND")
AUTO_DEACTIVATE_RAW = _env_bool_raw("AUTO_DEACTIVATE")

AUTO_REFUND = _env_bool("AUTO_REFUND", True)
AUTO_DEACTIVATE = _env_bool("AUTO_DEACTIVATE", True)

NSG_MIN_BALANCE_RAW = os.getenv("NSG_MIN_BALANCE")
try:
    NSG_MIN_BALANCE = float(NSG_MIN_BALANCE_RAW) if NSG_MIN_BALANCE_RAW is not None else 5.0
except Exception:
    NSG_MIN_BALANCE = 5.0


try:
    from colorama import init as colorama_init, Fore, Style
    colorama_init(autoreset=True)
except Exception:
    class _Dummy: RESET_ALL = ""
    class _Fore(_Dummy):
        RED = GREEN = YELLOW = CYAN = MAGENTA = BLUE = WHITE = ""
    class _Style(_Dummy):
        BRIGHT = NORMAL = ""
    Fore, Style = _Fore(), _Style()

class ColorFormatter(logging.Formatter):
    LEVEL_COLORS = {
        logging.DEBUG: Fore.BLUE,
        logging.INFO: Fore.CYAN,
        logging.WARNING: Fore.YELLOW,
        logging.ERROR: Fore.RED,
        logging.CRITICAL: Fore.MAGENTA + Style.BRIGHT,
    }
    def format(self, record):
        color = self.LEVEL_COLORS.get(record.levelno, "")
        message = super().format(record)
        return f"{color}{message}{Style.RESET_ALL}"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s:%(lineno)d | %(message)s"
)
for h in logging.getLogger().handlers:
    try:
        fmt = h.formatter._fmt if hasattr(h, "formatter") else "%(message)s"
        h.setFormatter(ColorFormatter(fmt))
    except Exception:
        pass

logger = logging.getLogger("AutoAppleCard")

TOKEN_DATA = {"token": None, "expiry": 0}
USER_STATES = {}
SERVICE_IDS_TR = {
    10: 33,
    15: 450,
    25: 34,
    30: 451,
    40: 377,
    50: 35,
    75: 452,
    100: 36,
    150: 453,
    200: 454,
    250: 37,
    300: 455,
    400: 456,
    500: 38,
    600: 457,
    700: 458,
    1000: 39,
    1250: 459,
    1500: 460
}

SERVICE_IDS_US = {
    2: 20,
    3: 21,
    4: 22,
    5: 23,
    6: 24,
    7: 25,
    8: 26,
    9: 27,
    10: 28,
    20: 29,
    25: 30,
    50: 31,
    100: 32
}

SERVICE_IDS_RU = {
    500: 40,
    600: 378,
    700: 379,
    1000: 41,
    1500: 42,
    2000: 380,
    2500: 381,
    5000: 382
}


def create_order(service_id: int, quantity: float = 1.0, data: str = ""):
    token = get_token()
    custom_id = str(uuid.uuid4())
    payload = {
        "service_id": service_id,
        "quantity": quantity,
        "custom_id": custom_id,
        "data": data
    }
    r = requests.post(
        "https://api.ns.gifts/api/v1/create_order",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
        timeout=30
    )
    if r.status_code != 200:
        raise Exception(f"Ошибка create_order: {r.text}")
    return custom_id



def pay_order(custom_id: str):
    token = get_token()
    payload = {"custom_id": custom_id}
    r = requests.post(
        "https://api.ns.gifts/api/v1/pay_order",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
        timeout=30
    )
    if r.status_code != 200:
        raise Exception(f"Ошибка pay_order: {r.text}")
    return r.json() 



def get_order(custom_id: str):
    token = get_token()
    payload = {"custom_id": custom_id}

    try:
        r = requests.post(
            "https://api.ns.gifts/api/v1/order_info",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
            timeout=30
        )
        if r.status_code == 200:
            return r.json()
        elif r.status_code == 422:
            raise Exception(f"Ошибка валидации custom_id: {r.text}")
        else:
            raise Exception(f"Ошибка get_order: {r.text}")
    except Exception as e:
        raise Exception(f"Ошибка get_order: {e}")


def _nice_refund(account: Account, chat_id, order_id, user_text: str):
    logger.info(Fore.YELLOW + f"↩️ Возврат по заказу {order_id}: {user_text}")
    if chat_id:
        account.send_message(chat_id, user_text + ("\n\nДеньги вернутся автоматически." if AUTO_REFUND else "\n\nСвяжитесь с админом для возврата."))
    try:
        account.refund(order_id)
        logger.warning(Fore.YELLOW + f"[FUNPAY] Возврат оформлен по заказу {order_id}.")
    except Exception as e:
        logger.error(Fore.RED + f"[FUNPAY] Не удалось оформить возврат {order_id}: {e}")


def deactivate_category(account: Account, category_id: int) -> int:
    deactivated = 0
    my_lots = None

    candidates = [
        ("get_my_subcategory_lots", lambda cid: account.get_my_subcategory_lots(cid)),
        ("get_my_lots", lambda cid: account.get_my_lots(cid) if hasattr(account, "get_my_lots") else None),
        ("get_my_lots_all", lambda cid: account.get_my_lots() if hasattr(account, "get_my_lots") else None),
    ]
    for name, fn in candidates:
        try:
            res = fn(category_id)
            if res:
                my_lots = res
                logger.debug(Fore.CYAN + f"[LOTS] Получили лоты через {name}, count={len(res) if hasattr(res,'__len__') else 'unknown'}")
                break
        except Exception as e:
            logger.debug(Fore.YELLOW + f"[LOTS] Метод {name} выбросил исключение: {e}")

    if my_lots is None:
        logger.error(Fore.RED + f"[LOTS] Не удалось получить список лотов для категории {category_id}.")
        return 0

    for lot in my_lots:
        lot_id = getattr(lot, "id", None) if not isinstance(lot, dict) else lot.get("id") or lot.get("lot_id")
        if not lot_id:
            continue

        field = None
        for fn_name in ("get_lot_fields", "get_lot_field", "get_lot", "get_lot_by_id"):
            try:
                fn = getattr(account, fn_name, None)
                if callable(fn):
                    field = fn(lot_id)
                    if field:
                        break
            except Exception:
                field = None

        if not field:
            logger.warning(Fore.YELLOW + f"[LOTS] Не удалось получить поля лота {lot_id}. Пропуск.")
            continue

        try:
            if isinstance(field, dict):
                field["active"] = False
            else:
                if hasattr(field, "active"):
                    setattr(field, "active", False)
                elif hasattr(field, "is_active"):
                    setattr(field, "is_active", False)
        except Exception as e:
            logger.debug(Fore.YELLOW + f"[LOTS] Не удалось установить active=False для {lot_id}: {e}")

        saved = False
        for sm in ("save_lot", "save_lot_field", "update_lot", "update_lot_field"):
            try:
                fn = getattr(account, sm, None)
                if callable(fn):
                    fn(field)
                    saved = True
                    logger.info(Fore.YELLOW + f"[LOTS] Деактивирован лот {lot_id} через {sm}")
                    deactivated += 1
                    break
            except Exception:
                pass

        if not saved:
            try:
                account.save_lot(field)
                logger.info(Fore.YELLOW + f"[LOTS] Деактивирован лот {lot_id} через fallback save_lot")
                deactivated += 1
            except Exception as e:
                logger.error(Fore.RED + f"[LOTS] Не удалось деактивировать лот {lot_id}: {e}")

    logger.warning(Fore.YELLOW + f"[LOTS] Всего деактивировано: {deactivated}")
    return deactivated


def _after_nsg_failure(account: Account, state: dict, err_text: str):
    chat_id = state.get("chat_id")
    order_id = state.get("order_id")

    if AUTO_REFUND:
        account.send_message(chat_id, "❌ Не удалось Оформить покупку карты.\n" + err_text + "\n\n🔁 Оформляю возврат средств…")
        try:
            account.refund(order_id)
            account.send_message(chat_id, "✅ Средства возвращены. Можно оформить заказ повторно позже.")
            logger.warning(Fore.YELLOW + f"[FUNPAY] Возврат оформлен по заказу {order_id}")
        except Exception as e:
            logger.error(Fore.RED + f"[FUNPAY] Возврат не удался по заказу {order_id}: {e}")
            account.send_message(chat_id, "❌ Не удалось выполнить автоматический возврат. Свяжитесь с админом.")
    else:
        account.send_message(chat_id, "❌ Не удалось Оформить покупку карты.\n" + err_text + "\n\n⚠️ Авто-возврат выключен. Напишите в чат для возврата.")

    bal = get_balance()
    if bal is None:
        logger.warning(Fore.YELLOW + "[BALANCE] Баланс NSGIFTS определить не удалось.")
        return

    logger.info(Fore.MAGENTA + f"[BALANCE] Текущий баланс NSGIFTS: {bal}")
    if bal < NSG_MIN_BALANCE:
        logger.warning(Fore.YELLOW + f"[BALANCE] Баланс NSGIFTS {bal} ниже порога {NSG_MIN_BALANCE}$.")
        if AUTO_DEACTIVATE:
            cnt = deactivate_category(account, DEACTIVATE_CATEGORY_ID)
            logger.warning(Fore.MAGENTA + f"[LOTS] Авто-деактивировано {cnt} лотов (subcategory {DEACTIVATE_CATEGORY_ID}).")
        else:
            logger.warning(Fore.MAGENTA + "[LOTS] AUTO_DEACTIVATE выключен — деактивацию лотов нужно сделать вручную.")



def handle_new_message(account: Account, message):
    user_id = getattr(message, "author_id", None)
    chat_id = getattr(message, "chat_id", None)
    text = (getattr(message, "text", "") or "").strip()

    logger.info(Fore.MAGENTA + f"📩 Сообщение от {user_id}: {text}")


def handle_new_order(account: Account, order):
    subcat = getattr(order, "subcategory", None) or getattr(order, "sub_category", None)
    subcat_id = getattr(subcat, "id", None)
    if subcat_id != CATEGORY_ID:
        logger.info(Fore.BLUE + f"[ORDER] Пропуск заказа {order.id} (subcategory {subcat_id} != {CATEGORY_ID})")
        return

    chat_id = getattr(order, "chat_id", None)
    buyer_id = getattr(order, "buyer_id", None)

    logger.info(Style.BRIGHT + Fore.WHITE + "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    logger.info(Style.BRIGHT + Fore.CYAN + f"🆕 Новый заказ #{getattr(order, 'id', 'unknown')} | Покупатель: {buyer_id}")
    title = getattr(order, "title", None)
    if title:
        logger.info(Fore.CYAN + f"📦 Товар: {title}")
    logger.info(Style.BRIGHT + Fore.WHITE + "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    try:
        parsed = extract_apple_card(getattr(order, "full_description", "") or getattr(order, "short_description", ""))
        if not parsed:
            logger.error(Fore.RED + f"❌ В заказе {order.id} не найден формат apple_card: <номинал> <валюта> в описании")
            account.send_message(chat_id, "❌ Ошибка: не удалось определить номинал карты. Укажите в описании: apple_card: <число> <TRY/USD/RUB>.")
            return

        nominal, currency = parsed

        if currency == "TRY":
            service_dict = SERVICE_IDS_TR
        elif currency == "USD":
            service_dict = SERVICE_IDS_US
        elif currency == "RUB":
            service_dict = SERVICE_IDS_RU
        else:
            logger.error(Fore.RED + f"❌ Валюта {currency} не поддерживается (заказ {order.id}).")
            account.send_message(chat_id, f"❌ Ошибка: валюта {currency} не поддерживается.")
            return

        service_id = service_dict.get(nominal)
        if not service_id:
            logger.error(Fore.RED + f"❌ Номинал {nominal} {currency} не найден.")
            account.send_message(chat_id, f"❌ Ошибка: неподдерживаемый номинал {nominal} {currency}.")
            return

        
        custom_id = create_order(service_id, 1.0)
        pay_order(custom_id)
        result = get_order(custom_id)

        codes = result.get("pins", [])

        if codes:
            text_lines = ["✅ Готово Вот код карты:"]
            for i, c in enumerate(codes, start=1):
                text_lines.append(f"{i}. {c}") 
            
            text_lines.append(f"✨ Номинал: {nominal} {currency}")
            text_lines.append(f"✨ Заказ #{getattr(order, 'id')} Выполнен!")
            text_lines.append(
                f"💬 Пожалуйста подтвердите заказ, и оставте отзыв: https://funpay.com/orders/{getattr(order, 'id')}/"
            )
            text = "\n".join(text_lines)
            
            account.send_message(chat_id, text)
            logger.info(Fore.GREEN + f"✅ Коды выданы покупателю {buyer_id}: {codes}")
        else:
            account.send_message(chat_id, "⏳ Код ещё в обработке. Попробуйте позже.")
            logger.warning(Fore.YELLOW + f"⚠ Нет кодов в ответе для заказа {custom_id}")
    except Exception as e:
        logger.error(Fore.RED + f"❌ Ошибка при заказе #{order.id}: {e}")
        state = {"chat_id": chat_id, "order_id": getattr(order, "id", None)}
        _after_nsg_failure(account, state, str(e))


def get_token():
    if TOKEN_DATA["token"] and time.time() < TOKEN_DATA["expiry"]:
        return TOKEN_DATA["token"]

    payload = {"email": NSGIFT_LOGIN, "password": NSGIFT_PASSWORD}

    try:
        response = requests.post(
            "https://api.ns.gifts/api/v1/get_token",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30
        )
    except Exception as e:
        raise Exception(f"❌ Ошибка сети при обращении к API: {e}")

    if response.status_code == 200:
        try:
            data = response.json()
        except Exception:
            raise Exception(f"❌ Сервер вернул невалидный JSON: {response.text}")

        token = data.get("access_token")
        if not token:
            raise Exception(f"❌ В ответе нет access_token: {data}")

        valid_thru = data.get("valid_thru", time.time() + 7200)

        TOKEN_DATA["token"] = token
        TOKEN_DATA["expiry"] = valid_thru if isinstance(valid_thru, (int, float)) else time.time() + 7200

        return TOKEN_DATA["token"]

    elif response.status_code == 401:
        raise Exception("❌ Неверный email или пароль для NSGifts.")
    elif response.status_code == 422:
        raise Exception(f"❌ Ошибка валидации полей: {response.text}")
    else:
        raise Exception(f"❌ Неожиданный ответ API ({response.status_code}): {response.text}")



def extract_apple_card(description: str = "") -> tuple[int, str] | None:
    text = (description or "").lower()
    m = re.search(r"apple_card[:=]\s*(\d{1,6})\s*(try|usd|rub)", text)
    if not m:
        return None
    nominal = int(m.group(1))
    currency = m.group(2).upper()
    return nominal, currency

def get_balance():
    try:
        token = get_token()
        response = requests.post("https://api.ns.gifts/api/v1/check_balance", headers={"Authorization": f"Bearer {token}"})
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, (int, float)):
                return data
            return data.get("balance", 0)
        return 0
    except Exception as e:
        logger.error(Fore.BLUE + f"❌ Ошибка при получении баланса: {e}")
        return 0
    
def whitelist_current_ip():
    try:
        token = get_token()
        ip = get_external_ip()
        if not ip:
            logger.error(Fore.RED + "❌ Не удалось определить внешний IP для whitelist.")
            return False

        r = requests.get(
            "https://api.ns.gifts/api/v1/ip-whitelist/list",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30
        )
        if r.status_code == 200:
            ip_list = r.json()
            if isinstance(ip_list, dict) and "data" in ip_list:
                ip_list = ip_list["data"]
            if isinstance(ip_list, list) and ip in ip_list:
                logger.info(Fore.GREEN + f"🌍 IP {ip} уже есть в whitelist.")
                return True

        payload = {"ip": ip}
        r = requests.post(
            "https://api.ns.gifts/api/v1/ip-whitelist/add",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30
        )
        if r.status_code == 200:
            return True
        else:
            logger.error(Fore.RED + f"❌ Ошибка при добавлении IP {ip}: {r.text}")
            return False
    except Exception as e:
        logger.error(Fore.RED + f"❌ Исключение при whitelist: {e}")
        return False



def get_external_ip():
    try:
        r = requests.get("https://api.ipify.org?format=json", timeout=10)
        if r.status_code == 200:
            return r.json().get("ip")
    except Exception as e:
        logger.error(Fore.RED + f"❌ Ошибка при получении внешнего IP: {e}")
    return None


def main():
    if not FUNPAY_AUTH_TOKEN:
        raise RuntimeError("FUNPAY_AUTH_TOKEN не найден в .env")
    if not NSGIFT_LOGIN:
        raise RuntimeError("NSGIFT_LOGIN не найден в .env")
    if not NSGIFT_PASSWORD:
        raise RuntimeError("NSGIFT_PASSWORD не найден в .env")
    

    whitelist_current_ip()

    account = Account(FUNPAY_AUTH_TOKEN)
    account.get()
    logger.info(Fore.GREEN + f"🔐 Авторизован как {getattr(account, 'username', '(unknown)')}")
    balance = get_balance()
    logger.info(Fore.GREEN + f"👾 Ваш баланс: {balance}$")
    logger.info(Fore.CYAN + f"⚙️ Настройки: Авто-Возврат: {'Включен ✅' if AUTO_REFUND else 'Выключен ❌'} , Авто-Деактивация-лотов: {'Включена ✅' if AUTO_DEACTIVATE else 'Выключена ❌'} , ⚠️ Минимальный-Баланс: {NSG_MIN_BALANCE}$")

    runner = Runner(account)
    logger.info(Style.BRIGHT + Fore.WHITE + "🚀 AutoAppleCard запущен. Ожидаю события...")

    for event in runner.listen(requests_delay=3.0):
        try:
            if isinstance(event, NewOrderEvent):
                order = account.get_order(event.order.id)
                handle_new_order(account, order)
            elif isinstance(event, NewMessageEvent):
                handle_new_message(account, event.message)
        except Exception:
            logger.exception(Fore.RED + "Ошибка в основном цикле")

if __name__ == "__main__":
    main()