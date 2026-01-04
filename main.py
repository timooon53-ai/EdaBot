from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from cfg import TOKEN
TOKEN = TOKEN

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Я бот 👋")

def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))

    print("Бот запущен")
    app.run_polling()

if __name__ == "__main__":
    main()
