import os
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters

TOKEN = os.environ["TOKEN"]

STICKER_COMPRA = "CAACAgQAAyEFAATmBptiAAIbc2pCtW0Cin0rkU6CFSGyVqWmQYbMAAILIQACaEkIUnVRn_2NEtPVPAQ"
STICKER_BIENVENIDA = "CAACAgQAAyEFAATmBptiAAIbdGpCtXLR4nqSl707gZNKRYI7MUZOAAJBIAACRh8JUh_nOBSMnXM1PAQ"

async def leer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    msg = update.message
    usuario = msg.from_user

    nombre = usuario.full_name if usuario else ""
    username = usuario.username if usuario else ""
    texto = msg.text or msg.caption or ""

    print("=" * 60)
    print("ID:", usuario.id)
    print("Nombre:", nombre)
    print("Username:", username)
    print("Es bot:", usuario.is_bot)
    print("Texto:", texto)
    print("=" * 60)

    # Compra detectada
    if "IWRU Buy!" in texto:
        print(">>> COMPRA DETECTADA <<<")
        await msg.reply_sticker(STICKER_COMPRA)
        return

    # Nuevo usuario
    if "New human detected" in texto:
        print(">>> NUEVO HUMANO <<<")
        await msg.reply_sticker(STICKER_BIENVENIDA)
        return

app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(MessageHandler(filters.ALL, leer))

print("======================================")
print("      IWRU BOT INICIADO")
print("======================================")

app.run_polling()
