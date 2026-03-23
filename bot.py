import os
import telebot
import requests

# 🔑 PON AQUÍ TUS CLAVES
TELEGRAM_TOKEN = os.getenv ("8578641725:AAGdF_6RFMBioy6NnUx5g9h4HPn9gGqGJzg")
OPENROUTER_API_KEY = os.getenv ("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")

bot = telebot.TeleBot(TELEGRAM_TOKEN)

def preguntar_ia(mensaje):
    url = "https://openrouter.ai/api/v1/chat/completions"
    
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }

    data = {
        "model": "openrouter/free",
        "messages": [
            {
                "role": "system",
                "content": "Eres un asistente personal estilo pana, ayudas con tareas, dinero, motivación y consejos."
            },
            {
                "role": "user",
                "content": mensaje
            }
        ]
    }

    try:
        response = requests.post(url, headers=headers, json=data)
        print(response.text)  # 🔍 para ver errores en la terminal
        
        resultado = response.json()
        
        return resultado['choices'][0]['message']['content']
    
    except Exception as e:
        print("ERROR IA:", e)
        return "Hubo un problema con la IA 😢"

@bot.message_handler(func=lambda message: True)
def responder(message):
    try:
        respuesta = preguntar_ia(message.text)
        bot.reply_to(message, respuesta)
    except Exception as e:
        print("ERROR BOT:", e)
        bot.reply_to(message, "Error bro, intenta de nuevo 👀")

# 🔥 ESTA ES LA CLAVE PARA EVITAR EL ERROR 409
bot.infinity_polling(timeout=10, long_polling_timeout=5)
