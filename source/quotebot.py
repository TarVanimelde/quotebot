"""Simple quote bot written using discord.py that seeks to emulate the
Quotes bot from the irc Rizon server."""

import argparse
import csv
from datetime import datetime
import os
import random
import re
import time

import discord

ADD_QUOTE = re.compile(r"(\+quote|\.quote add) (.+)", re.DOTALL | re.IGNORECASE)
DELETE_QUOTE = re.compile(r"(-quote|\.quote del) (\d+)", re.IGNORECASE)
FIND_MESSAGE_QUOTE = re.compile(r"\.(quote find|quote with) (.+)", re.DOTALL | re.IGNORECASE)
FIND_AUTHOR_QUOTE = re.compile(r"\.(quote author|quote by) (.+)", re.DOTALL | re.IGNORECASE)
GET_QUOTE = re.compile(r"\.(quote get|quote search) (\d+)", re.IGNORECASE)
RANDOM_QUOTE = re.compile(r"\.quote random", re.IGNORECASE)
MOST_RECENT_QUOTE = re.compile(r"\.quote last", re.IGNORECASE)

def format_timestamp(timestamp):
    """Formats an input timestamp (as from time.time() or similar) as
    %d %B %Y, %H:%M:%S (e.g., 17 March 2019, 13:12:11)"""
    time_as_object = datetime.fromtimestamp(float(timestamp))
    formatted_time = time_as_object.strftime("%d %B %Y, %H:%M:%S")
    return formatted_time

class QuoteBot(discord.Client):
    """Event handling for Discord."""
    def __init__(self, quotebot_owner_id=-1, quote_store_path="quotes.txt"):
        """Starts the quote bot (and its superclass), initializing the underlying quote store.
        Keywords arguments:
        quotebot_owner_id -- the userid of regular account owner in charge of the bot (default -1)
        quote_store_path -- the path to the file containing the quote store (default quotes.txt)"""
        self.quote_store = QuoteDB(quote_store_path)
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
                result = 'Quote #{} has been deleted, just like your dreams.'.format(quote_id)
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
    args = parser.parse_args()
    token = args.token
    owner_id = args.owner
    quotes_store = args.quotes
    client = QuoteBot(owner_id, quotes_store)
    client.run(token)

if __name__ == "__main__":
    main()
