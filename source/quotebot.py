"""Simple quote bot written using discord.py that seeks to emulate the
Quotes bot from the irc Rizon server."""

import aiohttp        
import aiofiles
import argparse
import csv
from datetime import datetime
import os
import os.path
from pathlib import Path
import re
import sqlite3
import time

import discord

ADD_QUOTE = re.compile(r"(\+quote|\.quote add)((\s+(sfw|nsfw))? (.+))?", re.DOTALL | re.IGNORECASE)
DELETE_QUOTE = re.compile(r"(-quote|\.quote del) (\d+)", re.IGNORECASE)
FIND_MESSAGE_QUOTE = re.compile(r"\.(quote search|quote with)\s(.+)", re.DOTALL | re.IGNORECASE)
FIND_AUTHOR_QUOTE = re.compile(r"\.(quote author|quote by)\s(.+)", re.DOTALL | re.IGNORECASE)
GET_QUOTE = re.compile(r"\.(quote get|quote read)\s(\d+)", re.IGNORECASE)
RANDOM_QUOTE = re.compile(r"\.quote random\s*(i)?\s*(sfw|nsfw)?", re.IGNORECASE)
MOST_RECENT_QUOTE = re.compile(r"\.quote last", re.IGNORECASE)
SET_QUOTE = re.compile(r"\.quote set\s+(\d+)\s+(sfw|nsfw)", re.IGNORECASE)
TOTAL_QUOTE = re.compile(r"\.quote total", re.IGNORECASE)
HELP_QUOTE = re.compile(r"\.quote help", re.IGNORECASE)

HELP_MESSAGE = Path('help.txt').read_text()

def format_timestamp(timestamp):
    """Formats an input timestamp (as from time.time() or similar) as
    %d %B %Y, %H:%M:%S (e.g., 17 March 2019, 13:12:11)"""
    time_as_object = datetime.fromtimestamp(float(timestamp))
    formatted_time = time_as_object.strftime("%d %B %Y, %H:%M:%S")
    return formatted_time

class QuoteBot(discord.Client):
    """Event handling for Discord."""
    def __init__(self, quotebot_owner_id=-1, db_path="quotes.sql", image_dir="images"):
        """Starts the quote bot (and its superclass), initializing the underlying quote store.
        Keywords arguments:
        quotebot_owner_id -- the userid of regular account owner in charge of the bot (default -1)
        quote_store_path -- the path to the file containing the quote store (default quotes.txt)"""
        self.db = QuoteDB(db_path, image_dir)
        self.quotebot_owner_id = quotebot_owner_id
        super(QuoteBot, self).__init__()

    async def on_ready(self):
        """Prints basic logon info to the console after the bot logs in.
        Currently, that is just the bot's user name."""
        print('Logged in as ', self.user)

    def ignore_message(self, message):
        """Return whether the message should be not be run through the regexes.
        This will return true if the message is written by the bot, written by
        another bot, or if the message is in a private message from a user that
        is not the QuoteBot's owner."""
        if message.author == self.user:
            return True
        if message.author.bot:
            return True# Bots are not allowed to use this service.
        if (isinstance(message.author, discord.User)
                and message.author.id != self.quotebot_owner_id):
            return True# Ignore private messages that aren't from the bot owner.
        return False

    async def on_message(self, message):
        """Evaluate the message to determine if it is a command
        that the bot should respond to. If it is, an appropriate
        response is generated."""
        if self.ignore_message(message):
            return

        if ADD_QUOTE.match(message.content):
            if  (message.author.id != self.quotebot_owner_id and
                 not message.author.top_role.permissions.send_messages):
                await message.channel.send('User has insufficient permissions for this action.')
                return
            match = ADD_QUOTE.match(message.content)
            safety = match.group(4)
            quote_message = match.group(5)
            timestamp = time.time()
            author = message.author.display_name

            image_types = ["png", "jpeg", "gif", "jpg"]
            is_image = lambda filename: any(filename.lower().endswith(image_type) for image_type in image_types)
            images = list(map(lambda attachment: attachment.url, filter(lambda attachment: is_image(attachment.filename), message.attachments)))

            if safety is not None:
                safety = safety.lower()
            else:
                if images:
                    safety = "nsfw"
                else:
                    safety = "sfw"
            await self.add_quote(message.channel, quote_message, images, safety, author, timestamp)
        elif DELETE_QUOTE.match(message.content):
            if not self.has_maximum_permissions(message.author):
                await message.channel.send('User has insufficient permissions for this action.')
                return
            match = DELETE_QUOTE.match(message.content)
            quote_id = int(match.group(2))
            await self.delete_quote(message.channel, quote_id)
        elif FIND_MESSAGE_QUOTE.match(message.content):
            match = FIND_MESSAGE_QUOTE.match(message.content)
            search_term = match.group(2)
            await self.get_matching_quotes(message.channel, search_term)
        elif FIND_AUTHOR_QUOTE.match(message.content):
            match = FIND_AUTHOR_QUOTE.match(message.content)
            author = match.group(2)
            await self.get_author_quotes(message.channel, author)
        elif GET_QUOTE.match(message.content):
            match = GET_QUOTE.match(message.content)
            quote_id = int(match.group(2))
            await self.post_quote(message.channel, quote_id)
        elif MOST_RECENT_QUOTE.match(message.content):
            try:
                safety = 'nsfw' if message.channel.is_nsfw() else 'sfw'
                most_recent_id = self.db.get_most_recent_id(safety)
            except ValueError:
                await message.channel.send('No quotes in the store at or below the channel\'s safety level.')
            else:
                await self.post_quote(message.channel, most_recent_id)
        elif TOTAL_QUOTE.match(message.content):
            num_quotes = self.db.quote_count()
            await message.channel.send('{} quotes in the store.'.format(num_quotes))
        elif HELP_QUOTE.match(message.content):
            response = '```{}```'.format(HELP_MESSAGE)
            await message.author.send(response)
        elif SET_QUOTE.match(message.content):
            if not self.has_maximum_permissions(message.author):
                await message.channel.send('User has insufficient permissions for this action.')
                return
            match = SET_QUOTE.match(message.content)
            quote_id = int(match.group(1))
            safety = match.group(2).lower()
            self.db.change_safety(quote_id, safety)
            await message.channel.send(f'Changed the safety level of Quote #{quote_id}.')
        elif RANDOM_QUOTE.match(message.content):
            match = RANDOM_QUOTE.match(message.content)
            safety = match.group(2)
            get_image_quote = match.group(1) is not None
            if safety is None and get_image_quote and message.channel.is_nsfw():
                safety = 'nsfw' if message.channel.is_nsfw() else 'sfw'
            elif safety is None:
                safety = 'sfw'
            safety = safety.lower() 
            
            quote_id = self.db.random_quote_id(get_image_quote, safety)
            if quote_id is None:
                # no matching random image
                if get_image_quote:
                    await message.channel.send('No image quotes with the given safety level.')
                else:
                    await message.channel.send('No quotes with the given safety level.')
            else:
                await self.post_quote(message.channel, quote_id)
        else:
            pass # Irrelevant message.

    async def add_quote(self, channel, message, images, safety, author, timestamp):
        if not images:
            if message is None:
                return
            else:
                quote_id = await self.db.add_quote(message, None, safety, author, timestamp)
                await channel.send(f'Added #{quote_id} to the store.')
        else:
            if safety is None:
                if channel.is_nsfw():
                    safety = 'nsfw'
                else:
                    safety = 'sfw'
            for image in images:
                quote_id = await self.db.add_quote(message, image, safety, author, timestamp)
                if quote_id is None:
                    await channel.send(f'There was an error saving {image}.')
                else:
                    await channel.send(f'Added quote #{quote_id} to the store with image <{image}>.')

    async def delete_quote(self, channel, quote_id):
        try:
            self.db.delete_quote(quote_id)
        except KeyError:
            result = f'Quote #{quote_id} is not in the store.'
            await channel.send(result)
        else:
            result = f'Quote #{quote_id} has been deleted.'
            await channel.send(result)

    async def get_author_quotes(self, channel, author):
        matching_ids = self.db.find_author_quotes(author) #TODO: separate sfw/nsfw ids
        if not matching_ids:
            await channel.send(f'No quotes authored by {author} in the store.')
        elif len(matching_ids) == 1:
            quote_id = matching_ids[0]
            await self.post_quote(channel, quote_id)
        else:
            ids_as_string = ', '.join(str(s) for s in matching_ids)
            result = f'Quotes authored by {author} include {ids_as_string}.'
            await channel.send(result)

    async def get_matching_quotes(self, channel, search_term):
        matching_ids = self.db.find_message_quotes(search_term) #TODO: separate sfw/nsfw ids
        if not matching_ids:
            await channel.send('No quotes that contain the search in the store.')
        elif len(matching_ids) == 1:
            quote_id = matching_ids[0]
            await self.post_quote(channel, quote_id)
        else:
            ids_as_string = ', '.join(str(s) for s in matching_ids)
            result = f'Quotes that contain the search include {ids_as_string}.'
            await channel.send(result)
    
    def has_maximum_permissions(self, author):
        return author.id == self.quotebot_owner_id or author.top_role.permissions.kick_members

    async def post_quote(self, channel, quote_id):
        try:
            quote, image_path, safety, author, timestamp = self.db.get_quote(quote_id)
        except KeyError:
            await channel.send(f'Quote #{quote_id} is not in the store.')
            return
        else:
            formatted_time = format_timestamp(timestamp)
            response = f'#{quote_id} added by {author} at {formatted_time}'
            if quote is not None:
                response = response +  f':\n```{quote}```'
            else:
                response = response + '.'
            
            if image_path is None:
                await channel.send(response)
            else:
                is_nsfw = safety is None or safety == 'nsfw'
                if is_nsfw and not channel.is_nsfw():
                    await channel.send(f'NSFW images are not permitted in this channel, quote #{quote_id} was not posted.')
                    return
                        
                if os.path.isfile(image_path):
                    await channel.send(content=response, file=discord.File(image_path))
                else: 
                    print(image_path)
                    await channel.send('Found a matching image, but it could not be retrieved. Please contact the administrator.')

class QuoteDB:
    def __init__(self, database="quotes.sql", image_dir='images'):
        self.image_dir = image_dir
        self.connection = None
        try:
            self.connection = sqlite3.connect(database)
        except Exception as e:
            print(f'Error {e} occurred while trying to connect to the quote database.')

        create_quotes_table = """
        CREATE TABLE IF NOT EXISTS quotes (
        quoteid INTEGER PRIMARY KEY AUTOINCREMENT,
        message TEXT,
        image_name TEXT,
        safety INTEGER,
        author TEXT NOT NULL,
        timestamp REAL
        );
        """

    async def add_quote(self, quote, image_url, safety, author, timestamp):
        safety_level = self.safety_to_level(safety)
        filename = None
        if image_url is not None:
            filename = await self.save_image(image_url)
        image_data = (quote, filename, safety_level, timestamp, author)

        cursor = self.connection.cursor()
        cursor.execute('INSERT INTO quotes(message, image_name, safety, timestamp, author) values (?, ?, ?, ?, ?)', image_data)
        self.connection.commit()
        cursor.close()
        return cursor.lastrowid

    async def save_image(self, image_url):
        if not os.path.isdir(self.image_dir):
            os.makedirs(self.image_dir)
        filename = os.path.basename(image_url)

        output_file = self.image_dir + os.path.sep + filename
        counter = 1
        while os.path.exists(output_file):
            output_file = self.image_dir + os.path.sep + str(counter) + "_" + filename
            counter = counter + 1
        
        async with aiohttp.ClientSession() as session:
            async with session.get(image_url) as resp:
                if resp.status == 200:
                    f = await aiofiles.open(output_file, mode='wb')
                    await f.write(await resp.read())
                    await f.close()
        return filename
    
    def safety_to_level(self, safety):
        return 1 if safety is None or safety.lower() != 'sfw' else 0

    def delete_quote(self, quote_id):
        if not self.id_exists(quote_id):
            return None
        try:
            query = f"""SELECT image_name FROM quotes WHERE quoteid = {quote_id}"""
            cursor = self.connection.cursor()
            cursor.execute(query)
            image_name = cursor.fetchone()[0]

            if image_name is not None:
                # Delete the local image
                image_path = self.image_dir + os.sep + image_name
                if not os.path.exists(image_path):
                    print(f'Expected an image at {image_path} and found none.')
                    return None
                os.unlink(image_path)

            query = f"""DELETE FROM quotes WHERE quoteid = {quote_id}"""
            cursor.execute(query)
            self.connection.commit()

            cursor.close()
            return True
        except Exception as e:
            print(f"The error '{e}' occurred in delete_image.")
            return None

    def id_exists(self, quote_id):
        cursor = self.connection.cursor()
        cursor.execute(f"SELECT COUNT() FROM quotes WHERE quoteid='{quote_id}'")
        exists = cursor.fetchone()[0] > 0
        cursor.close()
        return exists

    def find_message_quotes(self, message_substring):
        """Return a list of ids whose quote contains input as a substring (case-insensitive)."""
        query = """SELECT quoteid from quotes WHERE message LIKE ?"""
        cursor = self.connection.cursor()
        cursor.execute(query, ('%'+message_substring+'%',))
        matching_ids = list(map(lambda result: result[0] , cursor.fetchall()))
        cursor.close()
        return matching_ids

    def find_author_quotes(self, author):
        """Return a list of ids (integers) whose author matches the input (case-insensitive)."""
        author = author.lower()
        query = """SELECT quoteid FROM quotes WHERE author LIKE ?"""
        cursor = self.connection.cursor()
        cursor.execute(query, (author,)) #"\'"
        matching_ids = list(map(lambda result: result[0] , cursor.fetchall()))
        cursor.close()
        return matching_ids

    def get_quote(self, quote_id):
        """Return the message, author, and timestamp corresponding to the id as a tuple.
        If there is no matching id in the store, a KeyError is raised.."""
        if not self.id_exists(quote_id):
            raise KeyError('Quote #{} not found in the store.'.format(quote_id))
        query = """SELECT message, image_name, safety, author, timestamp FROM quotes WHERE quoteid = ?"""
        cursor = self.connection.cursor()
        cursor.execute(query, (quote_id,))
        message, image_name, safety, author, timestamp = cursor.fetchone()
        image_path = None
        if image_name is not None:
            image_path = self.image_dir + os.sep + image_name
        cursor.close()
        return message, image_path, safety, author, timestamp

    def get_most_recent_id(self, safety):
        """Return the most recently generated id in the quote store.
        If there are no quotes in the store, a ValueError is raised."""
        safety_level = self.safety_to_level(safety)
        cursor = self.connection.cursor()
        cursor.execute("""SELECT MAX(quoteid) FROM quotes WHERE safety <= ?""", (safety_level,))
        result = cursor.fetchone()
        cursor.close()
        if result is None:
            raise ValueError('No quotes in the store.')
        return int(result[0])

    def quote_count(self):
        cursor = self.connection.cursor()
        cursor.execute("""SELECT COUNT(*) FROM quotes""")
        rowcount = cursor.fetchone()[0]
        cursor.close()
        return int(rowcount)

    def change_safety(self, quote_id, safety):
        safety_level = self.safety_to_level(safety)
        cursor = self.connection.cursor()
        cursor.execute("""UPDATE quotes SET safety = ? WHERE quoteid = ?""", (safety_level,quote_id))
        self.connection.commit()
        cursor.close()

    def random_quote_id(self, get_image_quote, safety='sfw'):
        safety_level = self.safety_to_level(safety)
        try:
            query = """SELECT quoteid FROM quotes WHERE safety = ? ORDER BY RANDOM() LIMIT 1"""
            if get_image_quote:
                query = """SELECT quoteid FROM quotes WHERE safety = ? AND image_name IS NOT NULL ORDER BY RANDOM() LIMIT 1"""
            cursor = self.connection.cursor()
            cursor.execute(query, (safety_level,))
            result = cursor.fetchone()
            cursor.close()
            if result is None:
                return None
            quoteid = result[0]
            return quoteid
        except Exception as e:
            print(f"The error '{e}' occurred in get_random_image.")
            return None
    
def main():
    """Point of entry for the quote bot."""
    parser = argparse.ArgumentParser()
    parser.add_argument("-t", "--token",
                        type=str,
                        required=True,
                        help="The token for your discord bot.")
    parser.add_argument("-o", "--owner",
                        type=int,
                        required=True,
                        help="The quote bot owner's discord id.")
    parser.add_argument("-db",
                        type=str,
                        required=False,
                        help="The path to the quote database.",
                        default="quotes.sql")
    parser.add_argument("-id", "--imgdir",
                        type=str,
                        required=False,
                        help="The path to the directory holding images",
                        default="images")
    args = parser.parse_args()
    token = args.token
    owner_id = args.owner
    db_path = args.db
    image_dir = args.imgdir
    client = QuoteBot(owner_id, db_path, image_dir)
    client.run(token)

if __name__ == "__main__":
    main()
