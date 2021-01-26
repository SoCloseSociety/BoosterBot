#! Python3

from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackQueryHandler
from telegram.ext import ConversationHandler, JobQueue
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, error
from instagrapi import exceptions, Client

import os
import sys
import configparser
import sqlite3
import logging
import instagrapi
import datetime as dt
import threading


def load_settings() -> None:
    global settings
    if not os.path.exists('bot_settings.ini'):
        sys.exit('FAILED TO FIND SETTING FILE!')
    else:
        config = configparser.ConfigParser()
        config.read('bot_settings.ini')
        settings = config['BOT']


def initiate_db() -> None:
    conn = sqlite3.connect('bot_data.db')  # creates the file if doesn't exists, or connect if exists
    c = conn.cursor()  # set up a cursor to execute SQL commands
    with conn:
        c.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT,"
                  "profile_link TEXT, balance REAL, pending REAL, is_admin BOOL)")
        c.execute("CREATE TABLE IF NOT EXISTS follows (action_id INTEGER PRIMARY KEY, follower_id INTEGER,"
                  "follower_profile TEXT, followee_id INTEGER, followee_profile TEXT, status TEXT, follow_date TEXT)")


def is_admin(user_id) -> bool:
    if str(user_id) == str(settings['owner_id']):
        return True
    return False


def add_action(follower_id: int, follower_profile: str, followee_profile: str, followee_id: int, status: str) -> None:
    conn = sqlite3.connect("bot_data.db")
    c = conn.cursor()
    with conn:
        c.execute("INSERT INTO follows (follower_id, follower_profile, followee_id, followee_profile, "
                  "status, follow_date) VALUES (:follower_id, :follower_profile, :followee_id, :followee_profile, "
                  ":status, :follow_date)",
                  {'follower_id': follower_id, 'follower_profile': follower_profile, 'followee_id': followee_id,
                   'followee_profile': followee_profile, 'status': status, 'follow_date':
                       dt.datetime.now().strftime('%d/%m/%Y')})


def user_exists(user_id: int) -> bool:
    conn = sqlite3.connect('bot_data.db')  # creates the file if doesn't exists, or connect if exists
    c = conn.cursor()  # set up a cursor to execute SQL commands
    with conn:
        c.execute("SELECT * from users WHERE (user_id = :user_id)",
                  {'user_id': user_id})
    if len(c.fetchall()) > 0:
        return True
    return False


def has_profile_link(user_id: int) -> bool:
    conn = sqlite3.connect('bot_data.db')  # creates the file if doesn't exists, or connect if exists
    c = conn.cursor()  # set up a cursor to execute SQL commands
    with conn:
        c.execute("SELECT * from users WHERE (user_id = :user_id AND profile_link IS NOT NULL)",
                  {'user_id': user_id})
    if len(c.fetchall()) > 0:
        return True
    return False


def is_profile_private(instagram_bot, profile_name: str):
    try:
        user_id = instagram_bot.user_id_from_username(profile_name)
        return instagram_bot.user_info(user_id).is_private
    except instagrapi.exceptions.UserNotFound:
        return True


def is_follower(instagram_bot, follower_profile: str, followee_profile: str) -> bool:
    # we're checking if follower is following followee
    print('***Checking if {} is following {}***'.format(follower_profile, followee_profile))
    try:
        user_id = instagram_bot.user_id_from_username(followee_profile)
        print('Userid {}'.format(user_id))
        for user in instagram_bot.user_followers(user_id=user_id, amount=0).values():
            print(user.username)
            if user.username == follower_profile:
                return True
    except instagrapi.exceptions.UserNotFound:
        return False
    return False


def check_action(follower_id: int, followee_id: int) -> int:
    conn = sqlite3.connect("bot_data.db")
    c = conn.cursor()
    with conn:
        c.execute("SELECT follow_date from follows WHERE (follower_id = :follower_id and followee_id = :followee_id)"
                  " ORDER BY action_id DESC", {'follower_id': follower_id, 'followee_id': followee_id})
        latest_action = c.fetchone()
    if latest_action is not None:
        latest_action_date = dt.datetime.strptime(latest_action[0], '%d/%m/%Y')
        delta = dt.datetime.now() - latest_action_date
        return delta.days
    return -1


def update_session(user_id: int, update, context) -> None:
    job = context.job_queue.get_jobs_by_name(name='Session:{}'.format(user_id))
    if len(job) > 0:
        job[0].schedule_removal()
    context.job_queue.run_once(callback=end_session_job, when=SESSION_TIME, context=update.callback_query,
                               name="Session:{}".format(update.callback_query.from_user.id))


def balance_to_pending(user_id: int, amount: float) -> None:
    conn = sqlite3.connect("bot_data.db")
    c = conn.cursor()
    with conn:
        c.execute("SELECT balance, pending FROM users WHERE user_id = :user_id", {'user_id': user_id})
        data = c.fetchone()
        balance, pending = float(data[0]), float(data[1])
        balance -= amount
        pending += amount
        c.execute("UPDATE users SET balance = :new_balance, pending = :new_pending WHERE user_id = :user_id",
                  {'new_balance': balance, 'new_pending': pending, 'user_id': user_id})


def transfer_points(sending_user_id: int, receiving_user_id: int, amount: float) -> bool:
    print("transfering from {} to {}. {}$".format(sending_user_id, receiving_user_id, amount))
    conn = sqlite3.connect("bot_data.db")
    c = conn.cursor()
    with conn:
        c.execute("SELECT pending FROM users WHERE user_id = :user_id", {'user_id': sending_user_id})
        sending_user_pending = float(c.fetchone()[0]) - amount
        if sending_user_pending < 0:
            return False
        c.execute("SELECT balance FROM users WHERE user_id = :user_id", {'user_id': receiving_user_id})
        receiving_user_balance = float(c.fetchone()[0]) + amount
        c.execute("UPDATE users SET pending = :new_pending WHERE user_id = :user_id",
                  {'new_pending': sending_user_pending, 'user_id': sending_user_id})
        c.execute("UPDATE users SET balance = :new_balance WHERE user_id = :user_id",
                  {'new_balance': receiving_user_balance, 'user_id': receiving_user_id})
    return True


def start(update, context) -> None:
    if not user_exists(update.message.from_user.id):    # Check if the user is already in the DB
        conn = sqlite3.connect("bot_data.db")
        c = conn.cursor()
        with conn:
            c.execute("INSERT INTO users (user_id, username, balance, pending, is_admin) VALUES "
                      "(:user_id, :username, 0, 0, 0)", {'user_id': update.message.from_user.id,
                                                         'username': update.message.from_user.username})
    if has_profile_link(update.message.from_user.id):  # Check if the user has an instagram profile link updated
        text = '''How to use the bot ☝️
1️⃣ We send you the links to Instagram profiles
2️⃣ You follow them
3️⃣ In 10 minutes we verify the action and give you the 🍪 reward

Press Followed if you followed the user. If you don't want to - just Skip it.

More often you use the bot + more 🍪 you have = more followers!

You need to have at least 5🍪 to start to gain followers.'''
        menu = [[InlineKeyboardButton("I'm ready to follow", callback_data='start_following')]]
        reply_markup = InlineKeyboardMarkup(menu)
        context.bot.send_message(chat_id=update.message.chat_id, text=text, reply_markup=reply_markup)
    else:
        # Get the user's profile link
        row = [InlineKeyboardButton('Yes', callback_data='set_profile_link'),
               InlineKeyboardButton('YES!!!', callback_data='set_profile_link')]
        menu = [row]
        reply_markup = InlineKeyboardMarkup(menu)
        context.bot.send_message(chat_id=update.message.chat_id, text="Wanna get instagram followers?!",
                                 reply_markup=reply_markup)


def add_profile_conversation(update, context) -> int:
    query = update.callback_query
    registration_link = r"https://freebitco.in/?r=21441719&tag=telegram"
    message = '''
    First of all, we need to know your Instagram. 
We will verify that other users really followed you before you send 🍪 to them.

⚠️ It is really important that:
📍 Your account should be yours, otherwise someone else will receive your new followers
📍 Your account should be public, otherwise we can't verify that someone followed you
📍 Your account should be real because we just don't allow fakes 🙅‍♂️🙅‍♀️

Please send us the your Instagram *username*. 👇'''

    if update.message is not None:
        update.message.chat.send_message(message)
    else:
        context.bot.editMessageText(message, chat_id=query.message.chat_id,
                                    message_id=query.message.message_id)
    return ADDRESS


def add_profile(update, context) -> None:
    if not is_profile_private(instagram_bot=insta_bot, profile_name=update.message.text):
        conn = sqlite3.connect("bot_data.db")
        c = conn.cursor()
        with conn:
            c.execute("UPDATE users SET profile_link = :profile_link WHERE user_id = :user_id",
                      {'profile_link': update.message.text, 'user_id': update.message.from_user.id})
        context.bot.send_message(chat_id=update.message.chat_id, text='Your profile has been successfully added!'
                                                                      '\nSend /start to begin gaining followers')
    else:
        row = [InlineKeyboardButton('Retry', callback_data='set_profile_link'),
               InlineKeyboardButton('Cancel', callback_data='cancel')]
        menu = [row]
        reply_markup = InlineKeyboardMarkup(menu)
        context.bot.send_message(chat_id=update.message.chat_id,
                                 reply_markup=reply_markup, text='Profile must be valid and public.'
                                                                 '\nYou should send your instagram username')
    return ConversationHandler.END


def cancel(update, context) -> int:
    text = 'Bye! I hope we can talk again some day.'
    if update.message is None:
        query = update.callback_query
        context.bot.send_message(chat_id=query.message.chat_id, text=text)
    else:
        update.message.reply_text(text)
    return ConversationHandler.END


def get_profile_to_follow(update, context) -> None:
    thread = threading.Thread(target=get_profile_to_follow_thread, args=(update, context))
    thread.start()


def get_profile_to_follow_thread(update, context) -> None:
    query = update.callback_query
    current_user = query.from_user.id
    conn = sqlite3.connect("bot_data.db")
    c = conn.cursor()
    with conn:
        c.execute("SELECT profile_link, user_id FROM users WHERE (balance >= 5 AND user_id != :user_id) "
                  "ORDER BY RANDOM() ", {'user_id': current_user})
        profiles_to_follow = c.fetchall()
        c.execute("SELECT profile_link FROM users WHERE (user_id = :user_id)", {'user_id': current_user})
        current_profile = c.fetchone()[0]
        success = False
        for profile in profiles_to_follow:
            days_from_last_action = check_action(current_user, profile[1])
            if days_from_last_action > 5 or days_from_last_action == -1:
                if not is_follower(instagram_bot=insta_bot, follower_profile=current_profile,
                                   followee_profile=profile[0]):
                    # update_session(user_id=query.from_user.id, update=update, context=context)
                    balance_to_pending(profile[1], 1)
                    text = "📌 Like & Follow https://instagram.com/{} to get a 🍪".format(profile[0])
                    context.user_data['follower_id'] = current_user
                    context.user_data['follower_profile'] = current_profile
                    context.user_data['followee_id'] = profile[1]
                    context.user_data['followee_profile'] = profile[0]
                    menu = [[InlineKeyboardButton("Followed👍🏻", callback_data='confirm_follow')],
                            [InlineKeyboardButton("Skip⏩", callback_data='skip')]]
                    reply_markup = InlineKeyboardMarkup(menu)
                    context.bot.editMessageText(chat_id=query.message.chat_id, message_id=query.message.message_id,
                                                text=text, reply_markup=reply_markup)
                    success = True
                    break
        if not success:
            context.bot.editMessageText(chat_id=query.message.chat_id, message_id=query.message.message_id,
                                        text="Error: no new profiles to follow at the moment. Please come back later.")


def followed(update, context) -> None:
    thread = threading.Thread(target=followed_thread, args=(update, context))
    thread.start()


def followed_thread(update, context) -> None:
    text = "{} followed {}".format(context.user_data['follower_id'], context.user_data['followee_id'])
    logging.info(text)
    print("Checking if {}-{} has followed {}-{}".format(context.user_data['follower_id'],
                                                        context.user_data['follower_profile'],
                                                        context.user_data['followee_id'],
                                                        context.user_data['followee_profile']))
    add_action(follower_id=int(context.user_data['follower_id']), follower_profile=context.user_data['follower_profile']
               , followee_id=int(context.user_data['followee_id']),
               followee_profile=context.user_data['followee_profile'], status='pending')
    if is_follower(instagram_bot=insta_bot, followee_profile=context.user_data['followee_profile'],
                   follower_profile=context.user_data['follower_profile']):
        transfer_points(sending_user_id=int(context.user_data['followee_id']),
                        receiving_user_id=context.user_data['follower_id'], amount=1)
        try:
            context.bot.send_message(chat_id=context.user_data['followee_id'], text="You just gained a new follower!")
        except Exception:
            logging.error("Couldn't send follower confirmation to chat id: {}".format(context.user_data['followee_id']))
    get_profile_to_follow(update, context)


def skip(update, context) -> None:
    balance_to_pending(context.user_data['followee_id'], -1)
    add_action(int(context.user_data['follower_id']), int(context.user_data['followee_id']), 'skipped')
    get_profile_to_follow(update, context)


def end_session_job(context) -> None:
    job = context.job
    context.bot.delete_message(chat_id=job.context.chat_id, message_id=job.context.message_id)


def add_points(update, context) -> None:
    if is_admin(update.message.from_user.id):
        output = ""
        if len(context.args) == 2:
            user_id = int(context.args[0])
            if user_exists(user_id=user_id):
                conn = sqlite3.connect("bot_data.db")
                c = conn.cursor()
                with conn:
                    c.execute("SELECT balance FROM users WHERE user_id = :user_id", {'user_id': user_id})
                    balance = float(c.fetchone()[0])
                    balance += int(context.args[1])
                    c.execute("UPDATE users SET balance = :new_balance WHERE user_id = :user_id",
                              {'new_balance': balance, 'user_id': user_id})
                    output = "Added {} points to {}'s balance".format(int(context.args[1]), user_id)
            else:
                output = "Error: no such user in the DB."
        else:
            output = "Error: this command takes 2 arguments. /add_points USER_ID AMOUNT"
        context.bot.send_message(chat_id=update.message.chat_id, text=output)


def get_all_users(update, context) -> None:
    if is_admin(update.message.from_user.id):
        output = ""
        if len(context.args) == 0:
            conn = sqlite3.connect("bot_data.db")
            c = conn.cursor()
            with conn:
                c.execute("SELECT * FROM users")
            for user in c.fetchall():
                output += "ID: {}, Username: {}, Profile: {}, Balance: {}, Pending {}\n".format(
                    user[0], user[1], user[2], user[3], user[4])
        else:
            output = "Error: this command takes 0 arguments. /get_users"
        if output == "":
            output = "No users to display"
        context.bot.send_message(chat_id=update.message.chat_id, text=output)


def set_something(update, context) -> None:
    conn = sqlite3.connect("bot_data.db")
    c = conn.cursor()
    amount = float(context.args[2])
    what = context.args[1]
    user = context.args[0]
    with conn:
        c.execute("UPDATE users SET "+what+" = :amount WHERE user_id = :user_id",
                  {'amount': amount, 'user_id': user})


def get_balance(update, context) -> None:
    output = ""
    conn = sqlite3.connect("bot_data.db")
    c = conn.cursor()
    with conn:
        c.execute("SELECT * FROM users WHERE user_id = :user_id", {'user_id': update.message.from_user.id})
    user = c.fetchone()
    if len(user) > 0:
        output = "ID: {}, Username: {}, Profile: {}, Balance: {}\n".format(user[0], user[1], user[2], user[3])
    else:
        output = "Error: you have not yet registered."
    context.bot.send_message(chat_id=update.message.chat_id, text=output)


def get_all_actions(update, context) -> None:
    if is_admin(update.message.from_user.id):
        output = ""
        if len(context.args) == 0:
            conn = sqlite3.connect("bot_data.db")
            c = conn.cursor()
            with conn:
                c.execute("SELECT * FROM follows")
            for action in c.fetchall():
                output += "ID: {}, Follower: {}-{}, Followee: {}-{}, Status: {}, Date {}\n".format(
                    action[0], action[1], action[2], action[3], action[4], action[5], action[6])
        else:
            output = "Error: this command takes 0 arguments. /get_actions"
        if output == "":
            output = "No actions to display"
        context.bot.send_message(chat_id=update.message.chat_id, text=output)


def update_points(update, context) -> None:
    if is_admin(update.message.from_user.id):
        conn = sqlite3.connect("bot_data.db")
        c = conn.cursor()
        with conn:
            c.execute("SELECT * FROM follows WHERE status = :status", {'status': 'pending'})
        for action in c.fetchall():
            thread = threading.Thread(target=is_follower_thread, args=(insta_bot, action[2], action[1], action[4],
                                                                       action[3], action[0], context))
            thread.start()


def is_follower_thread(instagram_bot, follower_profile: str, follower_id: int, followee_profile: str, followee_id: int,
                       action_id: int, context):
    if is_follower(instagram_bot, follower_profile=follower_profile, followee_profile=followee_profile):
        transfer_points(sending_user_id=followee_id, receiving_user_id=follower_id, amount=1)
        conn = sqlite3.connect("bot_data.db")
        c = conn.cursor()
        with conn:
            c.execute("UPDATE follows SET status = :status WHERE action_id = :action_id", {'status': 'approved',
                                                                                           'action_id': action_id})
        try:
            context.bot.send_message(chat_id=followee_id, text="You just gained a new follower!")
        except Exception:
            logging.error("Couldn't send follower confirmation to chat id: {}".format(followee_id))


def main() -> None:
    global insta_bot
    load_settings()
    initiate_db()
    insta_bot = Client()
    insta_bot.login(settings['insta_username'], settings['insta_password'])
#     try:
#         insta_bot.login(settings['insta_username'], settings['insta_password'])
#     except exceptions.SentryBlock:
#         insta_bot.relogin()
    logging.basicConfig(format='[%(asctime)s] - %(message)s', datefmt='%d-%b-%y %H:%M:%S',
                        level=logging.INFO)  # initialize logging module and format. exclude debug messages
    updater = Updater(settings['TOKEN'], use_context=True)
    updater.dispatcher.add_handler(CommandHandler('start', start))
    updater.dispatcher.add_handler(CommandHandler('update_points', update_points))
    updater.dispatcher.add_handler(CommandHandler('set', set_something))
    updater.dispatcher.add_handler(CommandHandler('get_users', get_all_users))
    updater.dispatcher.add_handler(CommandHandler('get_actions', get_all_actions))
    updater.dispatcher.add_handler(CommandHandler('add_points', add_points))
    updater.dispatcher.add_handler(CommandHandler('balance', get_balance))
    updater.dispatcher.add_handler(CallbackQueryHandler(get_profile_to_follow, pattern='start_following'))
    updater.dispatcher.add_handler(CallbackQueryHandler(followed, pattern='confirm_follow'))
    updater.dispatcher.add_handler(CallbackQueryHandler(skip, pattern='skip'))
    add_profile_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_profile_conversation, pattern='set_profile_link')],
        states={
            ADDRESS: [MessageHandler(Filters.text & ~Filters.command, add_profile)],
        }, fallbacks=[CommandHandler('cancel', cancel), CallbackQueryHandler(cancel, pattern='cancel')], )
    updater.dispatcher.add_handler(add_profile_conv_handler)
    updater.start_polling()
    updater.idle()


if __name__ == '__main__':
    global settings
    global insta_bot
    ADDRESS = range(4)
    SESSION_TIME = 5    # in seconds
    main()
