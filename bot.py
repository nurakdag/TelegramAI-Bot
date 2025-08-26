import asyncio
import json
import os
import random
import time
import logging
from datetime import datetime, timezone
from telegram.ext import ContextTypes, MessageHandler, filters
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TimedOut, NetworkError, Forbidden, BadRequest
from telegram.ext import Application, CommandHandler, ContextTypes
import google.generativeai as genai

import pytz

# Logging ayarları
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

from db import (
    init_db,
    upsert_user, set_optout, get_user, due_users, mark_sent,
    upsert_group, set_group_active, due_groups, mark_group_sent
)

# -----------------------------------------------------------------------------
# ENV & AYARLAR
# -----------------------------------------------------------------------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN in environment. Set it in .env")
if not GEMINI_API_KEY:
    raise RuntimeError("Missing GEMINI_API_KEY in environment. Set it in .env")

# Gemini AI'ı yapılandır
genai.configure(api_key=GEMINI_API_KEY)
# Yeni model adı: gemini-1.5-flash (daha hızlı ve ücretsiz)
# Alternatif: gemini-1.5-pro (daha güçlü ama limitli)
model = genai.GenerativeModel('gemini-1.5-flash')

MIN_DAYS = float(os.getenv("MIN_DAYS", "2"))
MAX_DAYS = float(os.getenv("MAX_DAYS", "3"))
PER_MINUTE_LIMIT = int(os.getenv("PER_MINUTE_LIMIT", "20"))
SLEEP_MS = int(os.getenv("SLEEP_BETWEEN_SENDS_MS", "100"))
TZ = os.getenv("TZ", "UTC")

# AI yanıt ayarları
AI_RESPONSE_CHANCE = float(os.getenv("AI_RESPONSE_CHANCE", "0.3"))  # %30 ihtimal
MIN_MESSAGE_LENGTH = int(os.getenv("MIN_MESSAGE_LENGTH", "10"))  # En az 10 karakter
RESPONSE_COOLDOWN = int(os.getenv("RESPONSE_COOLDOWN", "300"))  # 5 dakika cooldown

# Son yanıt zamanlarını takip et (grup bazında)
last_response_times = {}

# Mesajları güvenli yükleme
try:
    with open("messages.json", "r", encoding="utf-8") as f:
        MESSAGES = json.load(f)
    if not isinstance(MESSAGES, list) or not MESSAGES:
        raise ValueError("messages.json must be a non-empty JSON array of strings")
except FileNotFoundError:
    MESSAGES = ["👋 Merhaba! Bot aktif çalışıyor."]
    logger.warning("messages.json bulunamadı, varsayılan mesaj kullanılıyor")

# -----------------------------------------------------------------------------
# GEMINI AI FONKSİYONLARI
# -----------------------------------------------------------------------------
async def generate_ai_response(message_text: str, chat_title: str = "", user_name: str = "") -> str:
    """Gemini AI ile mesaja yanıt üret"""
    try:
        # Prompt oluştur
        context = f"""Sen Türkçe konuşan, dostane ve yardımsever bir Telegram bot asistanısın.

Grup: {chat_title}
Kullanıcı: {user_name}
Mesaj: "{message_text}"

Kurallat:
- Kısa ve doğal yanıtlar ver (max 200 karakter)
- Türkçe yanıtla
- Dostane ve samimi ol
- Gereksiz teknik detay verme
- Emoji kullanabilirsin ama fazla abartma
- Eğer soru sorulursa yardımcı olmaya çalış
- Eğer sohbet ediyorlarsa sohbete katıl

Yanıt:"""

        response = await asyncio.get_event_loop().run_in_executor(
            None, 
            lambda: model.generate_content(context)
        )
        
        if response.text:
            # Yanıtı temizle ve kısalt
            ai_text = response.text.strip()
            if len(ai_text) > 200:
                ai_text = ai_text[:197] + "..."
            return ai_text
        else:
            return ""
            
    except Exception as e:
        logger.error(f"Gemini AI error: {e}")
        return ""

def should_respond_to_message(message_text: str, chat_id: int) -> bool:
    """Mesaja yanıt verilip verilmeyeceğini belirle"""
    # Çok kısa mesajları ignore et
    if len(message_text.strip()) < MIN_MESSAGE_LENGTH:
        return False
    
    # Komutları ignore et
    if message_text.startswith('/'):
        return False
    
    # Cooldown kontrolü
    now = time.time()
    if chat_id in last_response_times:
        if now - last_response_times[chat_id] < RESPONSE_COOLDOWN:
            return False
    
    # Rastgele yanıt ihtimali
    return random.random() < AI_RESPONSE_CHANCE

# -----------------------------------------------------------------------------
# YARDIMCI FONKSİYONLAR
# -----------------------------------------------------------------------------
def seconds_between_days(min_days: float, max_days: float) -> int:
    delta_days = random.uniform(min_days, max_days)
    return int(delta_days * 24 * 3600)

def format_ts(ts: int) -> str:
    if not ts:
        return "—"
    try:
        tz = pytz.timezone(TZ)
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(tz)
        return dt.strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception as e:
        logger.error(f"Tarih formatlama hatası: {e}")
        return "Hata"

async def send_message_safely(app: Application, chat_id: int, text: str):
    """Rate limit ve geçici hatalara dayanıklı gönderim."""
    max_retries = 3
    retry_count = 0
    
    while retry_count < max_retries:
        try:
            await app.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            return True
        except RetryAfter as e:
            logger.warning(f"Rate limit hit for {chat_id}, waiting {e.retry_after}s")
            await asyncio.sleep(e.retry_after + 1)
            retry_count += 1
        except Forbidden:
            logger.info(f"Bot blocked by user/group {chat_id}")
            set_optout(chat_id, True)  # User için
            set_group_active(chat_id, False)  # Group için
            return False
        except BadRequest as e:
            logger.error(f"Bad request for {chat_id}: {e}")
            return False
        except (TimedOut, NetworkError) as e:
            logger.warning(f"Network error for {chat_id}: {e}")
            await asyncio.sleep(2)
            retry_count += 1
        except Exception as e:
            logger.error(f"Unexpected error for {chat_id}: {e}")
            return False
    
    return False

# -----------------------------------------------------------------------------
# MESAJ HANDLERs
# -----------------------------------------------------------------------------
async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Grup mesajlarını işle ve AI yanıtı ver"""
    try:
        message = update.message
        chat = update.effective_chat
        user = update.effective_user
        
        # Sadece grup/süpergruplarda çalış
        if chat.type not in ("group", "supergroup"):
            return
            
        # Bot'un kendi mesajını ignore et
        if user.id == context.bot.id:
            return
            
        message_text = message.text or ""
        if not message_text:
            return
            
        # Yanıt verilip verilmeyeceğini kontrol et
        if not should_respond_to_message(message_text, chat.id):
            return
            
        # AI yanıtı üret
        user_name = user.first_name or user.username or "Anonim"
        chat_title = chat.title or "Grup"
        
        ai_response = await generate_ai_response(
            message_text=message_text,
            chat_title=chat_title,
            user_name=user_name
        )
        
        if ai_response:
            # Yanıt gönder
            success = await send_message_safely(context.application, chat.id, ai_response)
            if success:
                last_response_times[chat.id] = time.time()
                logger.info(f"AI response sent to group {chat.id}: {ai_response[:50]}...")
                
    except Exception as e:
        logger.error(f"Error in handle_group_message: {e}")

async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Özel mesajları işle ve AI yanıtı ver"""
    try:
        message = update.message
        chat = update.effective_chat
        user = update.effective_user
        
        # Sadece özel mesajlarda çalış
        if chat.type != "private":
            return
            
        message_text = message.text or ""
        if not message_text:
            return
            
        # Komutları ignore et (diğer handler'lar işleyecek)
        if message_text.startswith('/'):
            return
            
        # AI yanıtı üret
        user_name = user.first_name or user.username or "Anonim"
        
        ai_response = await generate_ai_response(
            message_text=message_text,
            chat_title="Özel Mesaj",
            user_name=user_name
        )
        
        if ai_response:
            await send_message_safely(context.application, chat.id, ai_response)
            logger.info(f"AI response sent to user {user.id}: {ai_response[:50]}...")
        else:
            # AI yanıt üretemezse varsayılan mesaj
            await update.message.reply_text(
                "🤖 Anlayamadım, daha detaylı yazabilir misin? "
                "Yardım için /help yazabilirsin."
            )
                
    except Exception as e:
        logger.error(f"Error in handle_private_message: {e}")

# -----------------------------------------------------------------------------
# KOMUTLAR
# -----------------------------------------------------------------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        u = update.effective_user
        upsert_user(
            chat_id=u.id,
            username=u.username or "",
            first=u.first_name or "",
            last=u.last_name or "",
        )
        await update.message.reply_text(
            "✅ Hoş geldin! Ben AI destekli bir botum.\n\n"
            "🤖 Benimle sohbet edebilir, sorular sorabilirsin\n"
            "📱 Gruplarda da aktifim - bazen konuşmalara katılırım\n"
            "📬 Ayrıca düzenli güncellemeler de gönderiyorum\n\n"
            "Komutlar:\n"
            "/stop - Güncellemeleri durdur\n"
            "/status - Durumunu gör\n"
            "/help - Yardım al"
        )
        logger.info(f"New user subscribed: {u.id}")
    except Exception as e:
        logger.error(f"Error in start_cmd: {e}")
        await update.message.reply_text("❌ Bir hata oluştu, lütfen tekrar deneyin.")

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        u = update.effective_user
        set_optout(u.id, True)
        await update.message.reply_text(
            "🛑 Düzenli güncellemeler durduruldu.\n\n"
            "💬 Yine de benimle sohbet edebilirsin!\n"
            "🔄 Güncellemeleri tekrar başlatmak için /start yaz."
        )
        logger.info(f"User unsubscribed: {u.id}")
    except Exception as e:
        logger.error(f"Error in stop_cmd: {e}")
        await update.message.reply_text("❌ Bir hata oluştu.")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_data = get_user(update.effective_user.id)
        if not user_data:
            await update.message.reply_text(
                "📝 Henüz kayıtlı değilsin.\n"
                "Başlamak için /start yazabilirsin."
            )
            return
        
        msg = (
            f"📊 **Durum Raporu**\n\n"
            f"🟢 Güncelleme Durumu: {'Aktif' if user_data['opted_out']==0 else 'Durduruldu'}\n"
            f"📅 Son Gönderim: {format_ts(user_data['last_sent_ts'] or 0)}\n"
            f"⏰ Sıradaki Gönderim: {format_ts(user_data['next_due_ts'] or 0)}\n"
            f"📈 Mesaj Sayısı: {user_data['msg_index'] or 0}\n\n"
            f"🤖 AI Sohbet: Aktif\n"
            f"💬 Benimle istediğin zaman sohbet edebilirsin!"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error in status_cmd: {e}")
        await update.message.reply_text("❌ Durum bilgisi alınamadı.")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
🤖 **AI Bot Yardım**

**Özellikler:**
• 🧠 Gemini AI ile akıllı sohbet
• 📬 Düzenli içerik güncellemeleri  
• 👥 Grup sohbetlerine katılım
• 🎯 Kişiselleştirilmiş yanıtlar

**Komutlar:**
/start - Botu başlat ve abone ol
/stop - Güncellemeleri durdur
/status - Durum bilgin
/help - Bu yardım mesajı

**Grup Komutları:**
/groupstart - Grupta aktif ol
/groupstop - Gruptan çık
/groupstatus - Grup durumu

**Nasıl Çalışır:**
• Özel mesajlarında her şeye yanıt veririm
• Gruplarda %30 ihtimalle konuşmalara katılırım
• Düzenli olarak güncellemeler gönderirim

Soru/sorun için @your_username ile iletişime geç!
"""
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

# Grup komutları
async def groupstart_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat = update.effective_chat
        if chat.type not in ("group", "supergroup"):
            await update.message.reply_text("Bu komutu bir GRUP içinde çalıştırın.")
            return
        upsert_group(chat.id, chat.title or "")
        await update.message.reply_text(
            "✅ Grup aboneliği aktif!\n\n"
            "🤖 Artık sohbetlere katılabilirim\n"
            "📬 Düzenli güncellemeler gönderilecek\n"
            "🎯 İlginç konuşmalarda yanıtlar verebilirim"
        )
        logger.info(f"Group subscribed: {chat.id}")
    except Exception as e:
        logger.error(f"Error in groupstart_cmd: {e}")
        await update.message.reply_text("❌ Grup aboneliği başlatılamadı.")

async def groupstop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat = update.effective_chat
        if chat.type not in ("group", "supergroup"):
            await update.message.reply_text("Bu komutu bir GRUP içinde çalıştırın.")
            return
        set_group_active(chat.id, False)
        await update.message.reply_text("🛑 Grup aboneliği durduruldu.")
        logger.info(f"Group unsubscribed: {chat.id}")
    except Exception as e:
        logger.error(f"Error in groupstop_cmd: {e}")

async def groupstatus_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat = update.effective_chat
        if chat.type not in ("group", "supergroup"):
            await update.message.reply_text("Bu komutu bir GRUP içinde çalıştırın.")
            return
        await update.message.reply_text(
            "ℹ️ **Grup Durumu**\n\n"
            "🤖 AI sohbet aktif\n"
            "📬 Düzenli güncelleme durumu kontrol edilebilir\n"
            "🎯 /groupstart ile aboneliği başlatabilirsiniz",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in groupstatus_cmd: {e}")

# -----------------------------------------------------------------------------
# DRIP WORKER'LAR
# -----------------------------------------------------------------------------
async def drip_worker(app: Application):
    """DM aboneleri için periyodik gönderim."""
    logger.info("DM drip worker started")
    per_minute = PER_MINUTE_LIMIT
    window_start = time.time()
    sent_this_window = 0

    while True:
        try:
            now = int(time.time())
            if time.time() - window_start >= 60:
                window_start = time.time()
                sent_this_window = 0

            rows = due_users(now_ts=now, limit=per_minute)
            if not rows:
                await asyncio.sleep(30)
                continue

            for row in rows:
                if sent_this_window >= per_minute:
                    to_wait = 60 - (time.time() - window_start)
                    if to_wait > 0:
                        await asyncio.sleep(to_wait)
                    window_start = time.time()
                    sent_this_window = 0

                idx = (row["msg_index"] or 0) % len(MESSAGES)
                text = MESSAGES[idx]

                success = await send_message_safely(app, row["chat_id"], text)
                if success:
                    sent_this_window += 1
                    next_due = now + seconds_between_days(MIN_DAYS, MAX_DAYS)
                    mark_sent(row["chat_id"], next_due_ts=next_due, new_index=(idx + 1) % len(MESSAGES))

                await asyncio.sleep(SLEEP_MS / 1000.0)

            await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"Error in drip_worker: {e}")
            await asyncio.sleep(30)

async def group_drip_worker(app: Application):
    """Gruplar için periyodik gönderim."""
    logger.info("Group drip worker started")
    
    while True:
        try:
            now = int(time.time())
            rows = due_groups(now_ts=now, limit=20)
            if not rows:
                await asyncio.sleep(30)
                continue

            for row in rows:
                idx = (row["msg_index"] or 0) % len(MESSAGES)
                text = MESSAGES[idx]
                
                success = await send_message_safely(app, row["chat_id"], text)
                if success:
                    next_due = now + seconds_between_days(MIN_DAYS, MAX_DAYS)
                    mark_group_sent(row["chat_id"], next_due_ts=next_due, new_index=(idx + 1) % len(MESSAGES))
                else:
                    set_group_active(row["chat_id"], False)

                await asyncio.sleep(SLEEP_MS / 1000.0)

            await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"Error in group_drip_worker: {e}")
            await asyncio.sleep(30)

# -----------------------------------------------------------------------------
# STARTUP & MAIN
# -----------------------------------------------------------------------------
async def schedule_workers(context: ContextTypes.DEFAULT_TYPE):
    app = context.application
    app.create_task(drip_worker(app))
    app.create_task(group_drip_worker(app))

async def on_startup(app: Application):
    try:
        init_db()
        logger.info("Database initialized")
        app.job_queue.run_once(schedule_workers, when=0)
        logger.info("Workers scheduled")
        logger.info("🤖 AI Bot ready!")
    except Exception as e:
        logger.error(f"Startup error: {e}")
        raise

def main():
    try:
        # AIORateLimiter'ı kaldırdık - bu versiyonlarda sorun çıkarıyor
        application = Application.builder().token(BOT_TOKEN).build()

        # Komut handler'ları
        application.add_handler(CommandHandler("start", start_cmd))
        application.add_handler(CommandHandler("stop", stop_cmd))
        application.add_handler(CommandHandler("status", status_cmd))
        application.add_handler(CommandHandler("help", help_cmd))
        
        application.add_handler(CommandHandler("groupstart", groupstart_cmd))
        application.add_handler(CommandHandler("groupstop", groupstop_cmd))
        application.add_handler(CommandHandler("groupstatus", groupstatus_cmd))

        # Mesaj handler'ları - Öncelik önemli!
        application.add_handler(MessageHandler(
            filters.TEXT & filters.ChatType.GROUPS, 
            handle_group_message
        ))
        application.add_handler(MessageHandler(
            filters.TEXT & filters.ChatType.PRIVATE, 
            handle_private_message
        ))

        application.post_init = on_startup

        logger.info("🚀 AI Bot starting...")
        application.run_polling(close_loop=False)
        
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise

if __name__ == "__main__":
    main()