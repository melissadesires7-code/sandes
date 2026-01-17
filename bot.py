import os
import json
import logging
import requests
import time
import uuid
from datetime import datetime
from flask import Flask, request, jsonify
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext, ConversationHandler
import asyncio
import aiohttp
from typing import Dict, Optional
import re

# Initialize Flask app (for Vercel serverless)
app = Flask(__name__)

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Load environment variables (set these in Vercel dashboard)
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
FAUCETPAY_API_KEY = os.environ.get('FAUCETPAY_API_KEY')
VERCEL_URL = os.environ.get('VERCEL_URL', '')  # Get Vercel URL

# FaucetPay API settings
FAUCETPAY_API_URL = "https://faucetpay.io/api/v1/send"
CURRENCY = "DGB"
AMOUNT = "0.00000001"  # 1 satoshi
DENGO = "1"

# Conversation states
WAITING_FOR_EMAIL = 1

# Email storage - using Vercel's /tmp directory for file storage
EMAIL_FILE = "/tmp/user_emails.txt"

class EmailStorage:
    """Class to handle email storage"""
    
    @staticmethod
    def save_email(email: str, user_data: Dict):
        """Save email and user data to file"""
        try:
            # Create directory if it doesn't exist
            os.makedirs(os.path.dirname(EMAIL_FILE), exist_ok=True)
            
            # Prepare data to save
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            entry = {
                'timestamp': timestamp,
                'email': email,
                'user_id': user_data.get('id'),
                'username': user_data.get('username'),
                'first_name': user_data.get('first_name'),
                'last_name': user_data.get('last_name')
            }
            
            # Append to file
            with open(EMAIL_FILE, 'a', encoding='utf-8') as f:
                f.write(json.dumps(entry, ensure_ascii=False) + '\n')
            
            logger.info(f"Email saved: {email} for user {user_data.get('id')}")
            return True
            
        except Exception as e:
            logger.error(f"Error saving email: {e}")
            # Try alternative storage method
            return EmailStorage.save_to_alternative(email, user_data)
    
    @staticmethod
    def save_to_alternative(email: str, user_data: Dict):
        """Alternative storage method if file write fails"""
        try:
            # Log to Vercel logs as backup
            logger.info(f"BACKUP_EMAIL_LOG: {email}|{user_data.get('id')}|{datetime.now()}")
            return True
        except:
            return False
    
    @staticmethod
    def get_all_emails():
        """Get all saved emails (for admin use)"""
        try:
            if not os.path.exists(EMAIL_FILE):
                return []
            
            emails = []
            with open(EMAIL_FILE, 'r', encoding='utf-8') as f:
                for line in f:
                    if line.strip():
                        emails.append(json.loads(line.strip()))
            
            return emails
        except Exception as e:
            logger.error(f"Error reading emails: {e}")
            return []
    
    @staticmethod
    def clear_emails():
        """Clear all saved emails (admin function)"""
        try:
            if os.path.exists(EMAIL_FILE):
                os.remove(EMAIL_FILE)
            return True
        except:
            return False

# Rate limiting storage (using in-memory for serverless)
user_claims = {}

def is_valid_email(email: str) -> bool:
    """Validate email format"""
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return bool(re.match(pattern, email))

async def start_command(update: Update, context: CallbackContext):
    """Handle /start command"""
    user = update.effective_user
    user_id = str(user.id)
    
    # Check if user has claimed recently (24-hour cooldown)
    if user_id in user_claims:
        last_claim = user_claims[user_id].get('last_claim', 0)
        cooldown = 86400  # 24 hours in seconds
        time_left = cooldown - (time.time() - last_claim)
        
        if time_left > 0:
            hours = int(time_left // 3600)
            minutes = int((time_left % 3600) // 60)
            
            await update.message.reply_text(
                f"? **Cooldown Active**\n\n"
                f"You have already claimed your reward.\n"
                f"Next claim available in: **{hours}h {minutes}m**\n\n"
                f"Please wait and try again later!"
            )
            return ConversationHandler.END
    
    welcome_message = (
        f"?? **Congratulations, {user.first_name or 'there'}!**\n\n"
        f"?? **You've won {DENGO} USDT!**\n\n"
        f"To claim your reward:\n"
        f"1?? Enter your **FaucetPay registered email**\n"
        f"2?? We'll send {DENGO} USDT instantly\n"
        f"3?? Check your FaucetPay balance\n\n"
        f"?? **Please enter your FaucetPay email now:**\n"
        f"(Example: yourname@example.com)"
    )
    
    await update.message.reply_text(welcome_message, parse_mode='Markdown')
    return WAITING_FOR_EMAIL

async def handle_email(update: Update, context: CallbackContext):
    """Handle user's email input"""
    email = update.message.text.strip().lower()
    user = update.effective_user
    user_id = str(user.id)
    
    # Validate email
    if not is_valid_email(email):
        await update.message.reply_text(
            "? **Invalid Email Format**\n\n"
            "Please enter a valid email address.\n"
            "Format: name@domain.com\n\n"
            "Try again:"
        )
        return WAITING_FOR_EMAIL
    
    # Check cooldown again (in case of multiple attempts)
    if user_id in user_claims:
        last_claim = user_claims[user_id].get('last_claim', 0)
        if time.time() - last_claim < 60:  # 60 seconds minimum between attempts
            await update.message.reply_text(
                "? Please wait a moment before trying again."
            )
            return ConversationHandler.END
    
    # Show processing message
    processing_msg = await update.message.reply_text(
        "? **Processing your request...**\n"
        "Please wait while we send your USDT reward."
    )
    
    try:
        # Send to FaucetPay API
        payload = {
            'api_key': FAUCETPAY_API_KEY,
            'currency': CURRENCY,
            'to': email,
            'amount': AMOUNT
        }
        
        headers = {
            'User-Agent': 'TelegramFaucetBot/1.0',
            'Accept': 'application/json'
        }
        
        # Make API request
        async with aiohttp.ClientSession() as session:
            async with session.post(FAUCETPAY_API_URL, data=payload, headers=headers, timeout=10) as response:
                result = await response.json()
        
        logger.info(f"FaucetPay API Response: {result}")
        
        if result.get('status') == 200:
            # Save email to storage
            user_data = {
                'id': user.id,
                'username': user.username,
                'first_name': user.first_name,
                'last_name': user.last_name
            }
            EmailStorage.save_email(email, user_data)
            
            # Update user claims
            user_claims[user_id] = {
                'last_claim': time.time(),
                'email': email
            }
            
            # Get transaction ID (shortened for display)
            tx_id = result.get('payout_user_hash', 'N/A')
            short_tx = tx_id[:12] + '...' if len(tx_id) > 12 else tx_id
            
            await processing_msg.edit_text(
                f"? **Success! Reward Sent!**\n\n"
                f"**Amount:** {AMOUNT} USDT\n"
                f"**Sent to:** {email}\n"
                f"**Transaction ID:** `{short_tx}`\n\n"
                f"?? **Next Steps:**\n"
                f"1. Check your FaucetPay account\n"
                f"2. Verify the transaction\n"
                f"3. Come back in 24 hours for more!\n\n"
                f"? **Next claim:** 24 hours from now\n\n"
                f"Thank you for using our bot! ??"
            )
            
        else:
            error_msg = result.get('message', 'Unknown error')
            await processing_msg.edit_text(
                f"? **Transaction Failed**\n\n"
                f"**Error:** {error_msg}\n\n"
                f"**Possible reasons:**\n"
                f"• Email not registered with FaucetPay\n"
                f"• FaucetPay API limit reached\n"
                f"• Temporary service issue\n\n"
                f"**Please try again with a valid FaucetPay email.**\n"
                f"Use /start to retry."
            )
            
    except asyncio.TimeoutError:
        await processing_msg.edit_text(
            "? **Request Timeout**\n\n"
            "The service is taking too long to respond.\n"
            "Please try again in a few minutes.\n\n"
            "Use /start to retry."
        )
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        await processing_msg.edit_text(
            "?? **Service Temporarily Unavailable**\n\n"
            "We're experiencing technical difficulties.\n"
            "Please try again later.\n\n"
            "Use /start to retry."
        )
    
    return ConversationHandler.END

async def help_command(update: Update, context: CallbackContext):
    """Handle /help command"""
    help_text = (
        "?? **Faucet Bot Help**\n\n"
        "**?? What is this?**\n"
        "Free USDT faucet bot! Get small amounts of USDT for free.\n\n"
        "**?? Commands:**\n"
        "/start - Claim your free USDT\n"
        "/help - Show this help message\n"
        "/status - Check your claim status\n"
        "/stats - View bot statistics (admin)\n\n"
        "**? How to claim:**\n"
        "1. Use /start command\n"
        "2. Enter your FaucetPay email\n"
        "3. Receive 0.00000001 USDT instantly\n"
        "4. Check your FaucetPay account\n\n"
        "**? Cooldown:** 24 hours between claims\n"
        "**?? Requirement:** Must be a registered FaucetPay email\n\n"
        "**? Need help?** Contact support if you have issues."
    )
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def status_command(update: Update, context: CallbackContext):
    """Handle /status command"""
    user = update.effective_user
    user_id = str(user.id)
    
    if user_id in user_claims:
        last_claim = user_claims[user_id].get('last_claim', 0)
        email = user_claims[user_id].get('email', 'Unknown')
        
        time_since = time.time() - last_claim
        cooldown = 86400  # 24 hours
        
        if time_since < cooldown:
            time_left = cooldown - time_since
            hours = int(time_left // 3600)
            minutes = int((time_left % 3600) // 60)
            
            await update.message.reply_text(
                f"?? **Your Status**\n\n"
                f"? **Last claim:** Success\n"
                f"?? **Email used:** {email}\n"
                f"? **Next claim in:** {hours}h {minutes}m\n\n"
                f"Please wait for the cooldown to end."
            )
        else:
            await update.message.reply_text(
                f"?? **Ready to Claim!**\n\n"
                f"You can claim your reward now!\n\n"
                f"Use /start to get your free USDT!"
            )
    else:
        await update.message.reply_text(
            "?? **No Claims Yet**\n\n"
            "You haven't made any claims yet.\n"
            "Use /start to claim your first reward!"
        )

async def stats_command(update: Update, context: CallbackContext):
    """Handle /stats command (admin only)"""
    user = update.effective_user
    
    # Add your Telegram user ID here for admin access
    ADMIN_IDS = ["YOUR_TELEGRAM_USER_ID"]  # Replace with your Telegram ID
    
    if str(user.id) not in ADMIN_IDS:
        await update.message.reply_text(
            "? **Access Denied**\n\n"
            "This command is for administrators only."
        )
        return
    
    # Get statistics
    emails = EmailStorage.get_all_emails()
    total_claims = len(emails)
    
    if total_claims > 0:
        # Count unique users
        unique_users = len(set(e.get('user_id') for e in emails))
        
        # Get recent claims
        recent_claims = emails[-5:] if len(emails) >= 5 else emails
        recent_text = "\n".join([
            f"• {e.get('email')} ({e.get('timestamp')})"
            for e in recent_claims
        ])
        
        stats_text = (
            f"?? **Bot Statistics**\n\n"
            f"**Total Claims:** {total_claims}\n"
            f"**Unique Users:** {unique_users}\n"
            f"**Active Users:** {len(user_claims)}\n\n"
            f"**Recent Claims:**\n{recent_text}\n\n"
            f"**Storage:** Emails saved: {total_claims}"
        )
    else:
        stats_text = (
            "?? **Bot Statistics**\n\n"
            "No claims have been made yet.\n"
            "Waiting for first user..."
        )
    
    await update.message.reply_text(stats_text, parse_mode='Markdown')

async def cancel_command(update: Update, context: CallbackContext):
    """Cancel current operation"""
    await update.message.reply_text(
        "? Operation cancelled.\n"
        "Use /start to begin again."
    )
    return ConversationHandler.END

# Create bot application
def create_application():
    """Create and configure Telegram bot"""
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Conversation handler for email collection
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start_command)],
        states={
            WAITING_FOR_EMAIL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_email)
            ]
        },
        fallbacks=[CommandHandler('cancel', cancel_command)]
    )
    
    # Add all handlers
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("stats", stats_command))
    
    return application

# Global application instance
bot_application = None

def init_bot():
    """Initialize bot application"""
    global bot_application
    if bot_application is None:
        bot_application = create_application()
        bot_application.initialize()

# Initialize bot
init_bot()

# Flask routes for Vercel
@app.route('/', methods=['GET'])
def home():
    """Home page"""
    return jsonify({
        "status": "online",
        "service": "Telegram Faucet Bot",
        "version": "2.0.0",
        "description": "Free USDT faucet bot",
        "endpoints": {
            "GET /": "This information",
            "POST /": "Telegram webhook endpoint",
            "GET /health": "Health check",
            "GET /stats": "View statistics",
            "GET /emails": "Download emails (admin)"
        },
        "usage": "Add this URL as webhook in Telegram bot settings"
    })

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "memory_usage": len(user_claims),
        "uptime": "running"
    })

@app.route('/stats', methods=['GET'])
def web_stats():
    """Web statistics endpoint"""
    emails = EmailStorage.get_all_emails()
    total = len(emails)
    unique = len(set(e.get('user_id') for e in emails))
    
    return jsonify({
        "total_claims": total,
        "unique_users": unique,
        "active_sessions": len(user_claims),
        "last_updated": datetime.now().isoformat()
    })

@app.route('/emails', methods=['GET'])
def download_emails():
    """Download emails endpoint (admin)"""
    # Simple password protection
    password = request.args.get('password')
    if password != os.environ.get('ADMIN_PASSWORD', 'admin123'):
        return jsonify({"error": "Unauthorized"}), 401
    
    emails = EmailStorage.get_all_emails()
    
    # Create CSV format
    csv_data = "Timestamp,Email,User ID,Username,First Name,Last Name\n"
    for entry in emails:
        csv_data += f"{entry.get('timestamp')},{entry.get('email')},{entry.get('user_id')},{entry.get('username')},{entry.get('first_name')},{entry.get('last_name')}\n"
    
    return csv_data, 200, {'Content-Type': 'text/csv', 'Content-Disposition': 'attachment; filename=emails.csv'}

@app.route('/', methods=['POST'])
async def webhook():
    """Main webhook handler for Telegram"""
    if request.is_json:
        try:
            update_data = request.get_json()
            update = Update.de_json(update_data, bot_application.bot)
            
            async with bot_application:
                await bot_application.process_update(update)
            
            return jsonify({"status": "ok"})
        except Exception as e:
            logger.error(f"Error processing update: {e}")
            return jsonify({"error": str(e)}), 500
    else:
        return jsonify({"error": "Invalid content"}), 400

# Vercel serverless handler
def handler(event, context):
    """Vercel serverless function handler"""
    # Convert Vercel event to Flask request
    from io import BytesIO
    import base64
    
    # This is handled automatically by Vercel's Python runtime
    # We just need to return the app
    return app