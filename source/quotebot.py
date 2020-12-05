"""Simple quote bot written using discord.py that seeks to emulate the
Quotes bot from the irc Rizon server."""

import aiohttp        
import aiofiles
import argparse
import csv
from datetime import datetime
import os
import os.path
import random
import re
import sqlite3
import time
import urllib

import discord

ADD_QUOTE = re.compile(r"(\+quote|\.quote add) (.+)", re.DOTALL | re.IGNORECASE)
DELETE_QUOTE = re.compile(r"(-quote|\.quote del) (\d+)", re.IGNORECASE)
FIND_MESSAGE_QUOTE = re.compile(r"\.(quote search|quote with) (.+)", re.DOTALL | re.IGNORECASE)
FIND_AUTHOR_QUOTE = re.compile(r"\.(quote author|quote by) (.+)", re.DOTALL | re.IGNORECASE)
GET_QUOTE = re.compile(r"\.(quote get|quote read) (\d+)", re.IGNORECASE)
RANDOM_QUOTE = re.compile(r"\.quote random", re.IGNORECASE)
MOST_RECENT_QUOTE = re.compile(r"\.quote last", re.IGNORECASE)
TOTAL_QUOTE = re.compile(r"\.quote total", re.IGNORECASE)
HELP_QUOTE = re.compile(r"\.quote help", re.IGNORECASE)

ADD_IMAGE = re.compile(r".qimg add (sfw|nsfw)( )*(.*)", re.IGNORECASE)
CHANGE_SAFETY = re.compile(r".qimg change (sfw|nsfw)( )*(\d+)", re.IGNORECASE)
DELETE_IMAGE = re.compile(r".qimg delete ( )*(\d+)", re.IGNORECASE)
GET_IMAGE = re.compile(r".qimg (get|view) (sfw|nsfw) (\d+)", re.DOTALL | re.IGNORECASE)
GET_IMAGE_TAGGED = re.compile(r".qimg tagged (sfw|nsfw) (.+)", re.DOTALL | re.IGNORECASE)
RANDOM_IMAGE = re.compile(r".qimg random (sfw|nsfw)( )*(.*)", re.DOTALL | re.IGNORECASE)


HELP_MESSAGE = """.quote help - sends this message to the user.
\n.quote add <quote> | +quote <quote> - adds the quote to the store if the user has sufficient permissions.
\n.quote del <id> | -quote <id> - removes the quote with the id from the store if the user has sufficient permissions.
\n.quote author <author name> | .quote by <author name> - returns quotes added to the store by the author.
\n.quote search <search terms> | .quote with <search terms> - searches the store for quotes matching the search terms.
\n.quote read <id> | .quote get <id> - returns the quote with the matching id.
\n.quote random - returns a random quote from the store.
\n.quote last - returns the last quote added to the store.
\n.quote total - returns the number of quotes in the store."""

def format_timestamp(timestamp):
    """Formats an input timestamp (as from time.time() or similar) as
    %d %B %Y, %H:%M:%S (e.g., 17 March 2019, 13:12:11)"""
    time_as_object = datetime.fromtimestamp(float(timestamp))
    formatted_time = time_as_object.strftime("%d %B %Y, %H:%M:%S")
    return formatted_time

class QuoteBot(discord.Client):
    """Event handling for Discord."""
    def __init__(self, quotebot_owner_id=-1, quote_store_path="quotes.txt", image_db_path="imagedb.sqlite", image_dir="images"):
        """Starts the quote bot (and its superclass), initializing the underlying quote store.
        Keywords arguments:
        quotebot_owner_id -- the userid of regular account owner in charge of the bot (default -1)
        quote_store_path -- the path to the file containing the quote store (default quotes.txt)"""
        self.quote_store = QuoteDB(quote_store_path)
        self.quotebot_owner_id = quotebot_owner_id
        self.image_db = ImageDB(image_db_path, image_dir)
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
            quote_message = match.group(2)
            timestamp = time.time()
            author = message.author.display_name
            quote_id = self.quote_store.add_quote(quote_message, timestamp, author)
            await message.channel.send('Added #{} to the store.'.format(quote_id))
        elif DELETE_QUOTE.match(message.content):
            if (message.author.id != self.quotebot_owner_id and
                    not message.author.top_role.permissions.kick_members):
                await message.channel.send('User has insufficient permissions for this action.')
                return
            match = DELETE_QUOTE.match(message.content)
            quote_id = int(match.group(2))
            try:
                self.quote_store.delete_quote(quote_id)
            except KeyError:
                result = 'Quote #{} is not in the store.'.format(quote_id)
                await message.channel.send(result)
            else:
                result = 'Quote #{} has been deleted.'.format(quote_id)
                await message.channel.send(result)
        elif FIND_MESSAGE_QUOTE.match(message.content):
            match = FIND_MESSAGE_QUOTE.match(message.content)
            quote_message = match.group(2)
            matching_ids = self.quote_store.find_message_quotes(quote_message)
            if not matching_ids:
                await message.channel.send('No quotes that contain the search in the store.')
            elif len(matching_ids) == 1:
                quote_id = matching_ids[0]
                result = self._get_quote(quote_id)
                await message.channel.send(result)
            else:
                ids_as_string = ', '.join(str(s) for s in matching_ids)
                result = 'Quotes that contain the search include {}.'.format(ids_as_string)
                await message.channel.send(result)
        elif FIND_AUTHOR_QUOTE.match(message.content):
            match = FIND_AUTHOR_QUOTE.match(message.content)
            author = match.group(2)
            matching_ids = self.quote_store.find_author_quotes(author)
            if not matching_ids:
                await message.channel.send('No quotes authored by {} in the store.'.format(author))
            elif len(matching_ids) == 1:
                quote_id = matching_ids[0]
                result = self._get_quote(quote_id)
                await message.channel.send(result)
            else:
                ids_as_string = ', '.join(str(s) for s in matching_ids)
                result = 'Quotes authored by {} include {}.'.format(author, ids_as_string)
                await message.channel.send(result)
        elif GET_QUOTE.match(message.content):
            match = GET_QUOTE.match(message.content)
            quote_id = int(match.group(2))
            result = self._get_quote(quote_id)
            await message.channel.send(result)
        elif RANDOM_QUOTE.match(message.content):
            try:
                random_id = self.quote_store.get_random_id()
            except ValueError:
                await message.channel.send('No quotes in the store.')
            else:
                result = self._get_quote(random_id)
                await message.channel.send(result)
        elif MOST_RECENT_QUOTE.match(message.content):
            try:
                most_recent_id = self.quote_store.get_most_recent_id()
            except ValueError:
                await message.channel.send('No quotes in the store.')
            else:
                result = self._get_quote(most_recent_id)
                await message.channel.send(result)
        elif TOTAL_QUOTE.match(message.content):
            num_quotes = self.quote_store.quote_count()
            await message.channel.send('{} quotes in the store.'.format(num_quotes))
        elif HELP_QUOTE.match(message.content):
            response = '```{}```'.format(HELP_MESSAGE)
            await message.author.send(response)
        elif ADD_IMAGE.match(message.content):
            match = ADD_IMAGE.match(message.content)
            safety_level = match.group(1).lower()
            tags = match.group(3).split()

            image_types = ["png", "jpeg", "gif", "jpg"]
            is_image = lambda filename: any(filename.lower().endswith(image_type) for image_type in image_types)
            images = list(map(lambda attachment: attachment.url, filter(lambda attachment: is_image(attachment.filename), message.attachments)))
            print(images)

            if not images:
                await message.channel.send('No images to save.')
                return
            timestamp = time.time()
            author = message.author.display_name
            for image in images:

                result = await self.image_db.add_image(image, safety_level, tags, author, timestamp)
                if result is None:
                    await message.channel.send(f'There was an error saving {image}.')
                else:
                    await message.channel.send(f'Added <{image}> as image #{result} to the database.')
        elif CHANGE_SAFETY.match(message.content):
            if (message.author.id != self.quotebot_owner_id and
                not message.author.top_role.permissions.kick_members):
                await message.channel.send('User has insufficient permissions for this action.')
                return
            match = CHANGE_SAFETY.match(message.content)
            safety_level = match.group(1).lower()
            id = int(match.group(3))
            self.image_db.change_safety(id, safety_level)

        elif DELETE_IMAGE.match(message.content):
            if (message.author.id != self.quotebot_owner_id and
                not message.author.top_role.permissions.kick_members):
                await message.channel.send('User has insufficient permissions for this action.')
                return
            match = DELETE_IMAGE.match(message.content)
            id = int(match.group(2))
            if not self.image_db.id_exists(id):
                await message.channel.send(f'No image with that id exists.')
                return
            result = self.image_db.delete_image(id)
            if result is None:
                await message.channel.send(f'Failed to delete image #{id}.')
            else:
                await message.channel.send(f'Image #{id} has been deleted.')
        elif GET_IMAGE.match(message.content):
            match = GET_IMAGE.match(message.content)
            safety_level = match.group(2).lower()
            is_nsfw = safety_level == 'nsfw'
            if is_nsfw and not message.channel.is_nsfw():
                await message.channel.send('NSFW images are not permitted in this channel.')
                return

            image_id = match.group(3)
            result = self.image_db.get_image(safety_level, image_id)
            if result is None:
                # Image with id either does not exist or is more nsfw than the safety level specified.
                await message.channel.send('No matching image with the given safety level and id found.')
                return
            (img_id, path, _, timestamp, author) = result
            if os.path.isfile(path):
                # Post the random image
                response_message = f'#{img_id} added by {author} at {format_timestamp(timestamp)}.'
                await message.channel.send(content=response_message, file=discord.File(path))
            else:
                # Found a random image path, but the image does not exist.
                print(result)
                await message.channel.send('Found a matching random image, but it could not be retrieved. Please contact administrator.')
        elif GET_IMAGE_TAGGED.match(message.content):
            match = GET_IMAGE_TAGGED.match(message.content)
            safety_level = match.group(2).lower()
            is_nsfw = safety_level == 'nsfw'
            if is_nsfw and not message.channel.is_nsfw():
                await message.channel.send('NSFW images are not permitted in this channel.')
                return
            tags = match.group(2).split()#TODO
        
        elif RANDOM_IMAGE.match(message.content):
            match = RANDOM_IMAGE.match(message.content)
            safety_level = match.group(1).lower()
            is_nsfw = safety_level == 'nsfw'
            if is_nsfw and not message.channel.is_nsfw():
                await message.channel.send('NSFW images are not permitted in this channel.')
                return
            
            tags = match.group(3).split()
            result = self.image_db.get_random_image(safety_level, tags)
            if result is None:
                # no matching random image
                if not tags:
                    await message.channel.send('No random images with the given safety level.')
                else:
                    await message.channel.send('No random images with the given safety level and tags.')
                return
            (img_id, path, _, timestamp, author) = result
            if os.path.isfile(path):
                # Post the random image
                response_message = f'#{img_id} added by {author} at {format_timestamp(timestamp)}.'
                await message.channel.send(content=response_message, file=discord.File(path))
            else:
                # Found a random image path, but the image does not exist.
                print(result)
                await message.channel.send('Found a matching random image, but it could not be retrieved. Please contact administrator.')
        else:
            pass # Irrelevant message.

            
    def _get_quote(self, quote_id):
        try:
            message, author, timestamp = self.quote_store.get_quote(quote_id)
        except KeyError:
            return 'Quote #{} is not in the store.'.format(quote_id)
        else:
            formatted_time = format_timestamp(timestamp)
            result = '#{} added by {} at {}:\n```{}```'.format(quote_id,
                                                               author,
                                                               formatted_time,
                                                               message)
            return result
class QuoteDB:
    """Provides functions for loading and storing quotes and their data.
    Changes are persisted through in a csv file.
    The csv file is stored as quote_id author timestamp message,
    is space-delimited, and has quotechar \"|\"  """
    def __init__(self, store_path="quotes.txt"):
        """Loads the quotes from the quote store into memory, creating
        a new file for the quote store if none exists.
        Keyword arguments:
        store_path -- The filesystem path to the quote store (default "quotes.txt")"""
        self.store_path = store_path
        self.load_quotes()

    def load_quotes(self):
        """Load quote data from store_path into memory.
        If store_path does not exist, then it is created.
        If the store_path points to a directory, an IsADirectoryError is raised."""
        if not os.path.exists(self.store_path):
            with open(self.store_path, 'w+'):
                pass
        elif os.path.isdir(self.store_path):
            print('Invalid quotes file: ', self.store_path)
            raise IsADirectoryError('Quote file \"{}\" is a directory'.format(self.store_path))
        with open(self.store_path, encoding='utf-8') as quote_file:
            self.ids_to_messages = {}
            reader = csv.reader(quote_file, delimiter=' ', quotechar="|", quoting=csv.QUOTE_MINIMAL)
            for row in reader:
                if not row:
                    continue
                quote_id = int(row[0])
                author = row[1]
                timestamp = float(row[2])
                message = row[3]
                self.ids_to_messages[quote_id] = (message, author, timestamp)

    def store_quotes(self):
        """Store a copy of the in-memory quotes to store_path as a csv file."""
        with open(self.store_path, 'w', encoding='utf-8') as store:
            writer = csv.writer(store, delimiter=' ', quotechar="|", quoting=csv.QUOTE_MINIMAL)
            for quote_id, (message, author, timestamp) in self.ids_to_messages.items():
                writer.writerow([quote_id, author, timestamp, message])

    def generate_next_id(self):
        """Generates an integer id that is currently not in use in the store."""
        if not self.ids_to_messages:
            return 1
        last_id = list(self.ids_to_messages.keys())[-1]
        return last_id + 1

    def add_quote(self, message, timestamp, author):
        """Add the quote information to the quote store.
        Return the id corresponding to the quote."""
        quote_id = self.generate_next_id()
        self.ids_to_messages[quote_id] = (message, author, timestamp)
        self.store_quotes()
        return quote_id

    def delete_quote(self, quote_id):
        """Removes quote information corresponding to the id from memory and the underlying storage.
        Raises a KeyError if the id is not in the store."""
        if quote_id in self.ids_to_messages:
            del self.ids_to_messages[quote_id]
            self.store_quotes()
        else:
            raise KeyError('Quote #{} not found in the store.'.format(quote_id))
    def find_message_quotes(self, message_substring):
        """Return a list of ids whose quote contains input as a substring (case-insensitive)."""
        message_substring = message_substring.lower()
        matching_ids = []
        for quote_id, (message, _, _) in self.ids_to_messages.items():
            if message_substring in message.lower():
                matching_ids.append(quote_id)
        return matching_ids

    def find_author_quotes(self, author):
        """Return a list of ids (integers) whose author matches the input (case-insensitive)."""
        author = author.lower()
        matching_ids = []
        for quote_id, (_, message_author, _) in self.ids_to_messages.items():
            if author == message_author.lower():
                matching_ids.append(quote_id)
        return matching_ids

    def get_quote(self, quote_id):
        """Return the message, author, and timestamp corresponding to the id as a tuple.
        If there is no matching id in the store, a KeyError is raised.."""
        if quote_id not in self.ids_to_messages.keys():
            raise KeyError('Quote #{} not found in the store.'.format(quote_id))
        message, author, timestamp = self.ids_to_messages[quote_id]
        return message, author, timestamp

    def get_random_id(self):
        """Return a randomly selected id in the quote store.
        If there are no quotes in the store, a ValueError is raised."""
        if not self.ids_to_messages:
            raise ValueError('No quotes in the store.')
        quote_id = random.choice(list(self.ids_to_messages.keys()))
        return quote_id

    def get_most_recent_id(self):
        """Return the most recently generated id in the quote store.
        If there are no quotes in the store, a ValueError is raised."""
        if not self.ids_to_messages:
            raise ValueError('No quotes in the store.')
        quote_id = list(self.ids_to_messages.keys())[-1]
        return quote_id
    
    def quote_count(self):
        return len(self.ids_to_messages)

class ImageDB:
    def __init__(self, image_db_path="imagedb.sqlite", image_store_dir="images"):
        self.image_dir = image_store_dir
        self.connection = None
        try:
            self.connection = sqlite3.connect(image_db_path)
        except Exception as e:
            print(f'Error {e} occurred while trying to connect to the image database.')

        create_images_table = """
        CREATE TABLE IF NOT EXISTS images (
        imageid INTEGER PRIMARY KEY AUTOINCREMENT,
        imagepath TEXT NOT NULL,
        safety INTEGER,
        timestamp REAL,
        author TEXT NOT NULL
        );
        """

        # create_tags_table = """
        # CREATE TABLE IF NOT EXISTS tags (
        # tagid INTEGER PRIMARY KEY AUTOINCREMENT,
        # title TEXT NOT NULL,
        # );
        # """

        # create_imagetags_table = """
        # CREATE TABLE IF NOT EXISTS imagetags (
        # imageid INTEGER,
        # tagid INTEGER,
        # );
        # """
        cursor = self.connection.cursor()
        try:
            cursor.execute(create_images_table)
            self.connection.commit()
        except Exception as e:
            print(f"The error '{e}' occurred")
        finally:
            cursor.close()

        # execute_query(self.connection, create_tags_table)
        # execute_query(self.connection, create_imagetags_table)

    def get_random_image(self, safety='sfw', tags=[]):
        safety_level = self.safety_to_level(safety)
        try:
            query = """SELECT * FROM images WHERE safety = ?"""
            cursor = self.connection.cursor()
            cursor.execute(query, (safety_level,))
            result = cursor.fetchone()
            cursor.close()
            if result is None:
                return None
            (_, path, _, _, _) = result
            if not os.path.exists(path):
                print(f'Expected an image at {path} and found none.')
                return None
            return result
        except Exception as e:
            print(f"The error '{e}' occurred in get_random_image.")
            return None
    
    def get_image(self, safety, id):
        safety_level = self.safety_to_level(safety)
        print(safety)
        try:
            query = """SELECT * FROM images WHERE imageid = ? AND safety = ?"""
            cursor = self.connection.cursor()
            cursor.execute(query, (id,safety_level))
            result = cursor.fetchone()
            print(result)
            cursor.close()
            if result is None:
                return None
            (_, path, _, _, _) = result
            if not os.path.exists(path):
                print(f'Expected an image at {path} and found none.')
                return None
            return result
        except Exception as e:
            print(f"The error '{e}' occurred in get_random_image.")
            return None

    async def add_image(self, image_url, safety, tags, author, timestamp):
        safety_level = self.safety_to_level(safety)
        tags = list(map(lambda tag : tag.lower(), tags))
        file_path = await self.save_image(image_url)
        image_data = (file_path, safety_level, timestamp, author)

        cursor = self.connection.cursor()
        cursor.execute('INSERT INTO images(imagepath, safety, timestamp, author) values (?, ?, ?, ?)', image_data)
        self.connection.commit()
        cursor.close()
        return cursor.lastrowid

    def delete_image(self, id):
        if not self.id_exists(id):
            return None
        try:
            query = f"""SELECT imagepath FROM images WHERE imageid = {id}"""
            cursor = self.connection.cursor()
            cursor.execute(query)
            result = cursor.fetchone()


            if not os.path.exists(result):
                print(f'Expected an image at {result} and found none.')
                return None
            os.unlink(result)

            query = f"""DELETE FROM images WHERE imageid = {id}"""
            cursor.execute(query)
            self.connection.commit()

            cursor.close()
            return True
        except Exception as e:
            print(f"The error '{e}' occurred in delete_image.")
            return None

    def change_safety(self, id, safety='nsfw'):
        if not self.id_exists(id):
            return None
        
        safety_level = self.safety_to_level(safety)

        data = (id, safety_level)
        sql = """UPDATE images SET safety = ? WHERE imageid = ?"""
        cursor = self.connection.cursor()
        cursor.execute(sql, data)
        result = None if cursor.rowcount < 1 else f"Successfully changed the safety level to {safety}"
        cursor.close()
        return result

    def id_exists(self, id):
        cursor = self.connection.cursor()
        cursor.execute(f"SELECT COUNT() FROM images WHERE imageid='{id}'")
        exists = cursor.fetchone()[0] > 0
        cursor.close()
        return exists

    def safety_to_level(self, safety):
        return 0 if safety.lower() == 'sfw' else 1

    async def save_image(self, image_url):
        if not os.path.isdir(self.image_dir):
            os.makedirs(self.image_dir)
        filename = os.path.basename(image_url)

        output_file = self.image_dir + os.path.sep + filename
        counter = 1
        while os.path.exists(output_file):
            output_file = self.image_dir + os.path.sep + str(counter) + "_" + filename
        
        async with aiohttp.ClientSession() as session:
            async with session.get(image_url) as resp:
                if resp.status == 200:
                    f = await aiofiles.open(output_file, mode='wb')
                    await f.write(await resp.read())
                    await f.close()
        return output_file
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
    parser.add_argument("-q", "--quotes",
                        type=str,
                        required=False,
                        help="The path for the text file to store your quotes.",
                        default="quotes.txt")
    parser.add_argument("-db",
                        type=str,
                        required=False,
                        help="The path to the image database file.",
                        default="imagedb.sqlite")
    parser.add_argument("-id", "--imgdir",
                        type=str,
                        required=False,
                        help="The path to the directory holding images",
                        default="images")
    args = parser.parse_args()
    token = args.token
    owner_id = args.owner
    quotes_store = args.quotes
    image_db_path = args.db
    image_dir = args.imgdir
    client = QuoteBot(owner_id, quotes_store, image_db_path, image_dir)
    client.run(token)

if __name__ == "__main__":
    main()
