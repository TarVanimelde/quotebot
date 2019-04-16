import argparse
import csv
from datetime import datetime
import discord
import os
import random
import re
import time

add_quote = re.compile(r"(\+quote|\.quote add) (.+)", re.DOTALL | re.IGNORECASE)
delete_quote = re.compile(r"(-quote|\.quote del) (\d+)", re.IGNORECASE)
find_message_quote = re.compile(r"\.(quote find|quote with) (.+)", re.DOTALL | re.IGNORECASE)
find_author_quote = re.compile(r"\.(quote author|quote by) (.+)", re.DOTALL | re.IGNORECASE)
get_quote = re.compile(r"\.quote get (\d+)", re.IGNORECASE)
random_quote = re.compile(r"\.quote random", re.IGNORECASE)
most_recent_quote = re.compile(r"\.quote last", re.IGNORECASE)

class QuoteBot(discord.Client):
    def __init__(self, quotebot_owner_id = -1, quote_store_path = ""):
        self.quote_store = QuoteDB(quote_store_path)
        self.quotebot_owner_id = quotebot_owner_id
        super(QuoteBot, self).__init__()

    async def on_ready(self):
        print('Logged in as ', self.user)
            
    async def on_message(self, message):
        if message.author == self.user:
            return
        if message.author.bot:
            return # Bots are not allowed to use this service.
        if isinstance(message.author, discord.User) and message.author.id != self.quotebot_owner_id:
            return # Ignore private messages that aren't from the bot owner.

        if add_quote.match(message.content):
            if  message.author.id != self.quotebot_owner_id and not message.author.top_role.permissions.send_messages:
                await message.channel.send('User has insufficient permissions for this action.')
                return
            match = add_quote.match(message.content)
            quote_message = match.group(2)
            timestamp = time.time()
            author = message.author.display_name
            id = self.quote_store.add_quote(quote_message, timestamp, author)
            await message.channel.send('Added #{} to the database.'.format(id))
        elif delete_quote.match(message.content):
            if message.author.id != self.quotebot_owner_id and not message.author.top_role.permissions.kick_members:
                await message.channel.send('User has insufficient permissions for this action.')
                return
            match = delete_quote.match(message.content)
            id = int(match.group(2))
            try:
                self.quote_store.delete_quote(id)
            except Exception:
                await message.channel.send('Quote #{} is not in the database.'.format(id))
            else:
                await message.channel.send('Quote #{} has been deleted, just like your dreams.'.format(id))
        elif find_message_quote.match(message.content):
            match = find_message_quote.match(message.content)
            quote_message = match.group(2)
            matching_ids = self.quote_store.find_message_quotes(quote_message)
            if len(matching_ids) == 0:
                await message.channel.send('No quotes that contain the search in the database.')
            elif len(matching_ids) == 1:
                id = matching_ids[0]
                result = self._get_quote(id)
                await message.channel.send(result)
            else:
                ids_as_string = ', '.join(str(s) for s in matching_ids)
                result = 'Quotes that contain the search include {}.'.format(ids_as_string)
                await message.channel.send(result)
        elif find_author_quote.match(message.content):
            match = find_author_quote.match(message.content)
            author = match.group(2)
            matching_ids = self.quote_store.find_author_quotes(author)
            if len(matching_ids) == 0:
                await message.channel.send('No quotes authored by {} in the database.'.format(author))
            elif len(matching_ids) == 1:
                id = matching_ids[0]
                result = self._get_quote(id)
                await message.channel.send(result)
            else:
                ids_as_string = ', '.join(str(s) for s in matching_ids)
                result = 'Quotes authored by {} include {}.'.format(author, ids_as_string)
                await message.channel.send(result)
        elif get_quote.match(message.content):
            match = get_quote.match(message.content)
            id = int(match.group(1))
            result = self._get_quote(id)
            await message.channel.send(result)
        elif random_quote.match(message.content):
            try:
                random_id = self.quote_store.get_random_id()
            except Exception:
                await message.channel.send('No quotes in the database.')
            else:
                result = self._get_quote(random_id)
                await message.channel.send(result)
        elif most_recent_quote.match(message.content):
            try:
                most_recent_id = self.quote_store.get_most_recent_id()
            except Exception:
                await message.channel.send('No quotes in the database.')
            else:
                result = self._get_quote(most_recent_id)
                await message.channel.send(result)
        else:
            pass # Irrelevant message.

    def _format_timestamp(self, timestamp):
        time_as_object = datetime.fromtimestamp(float(timestamp))
        formatted_time = time_as_object.strftime("%d %B %Y, %H:%M:%S")
        return formatted_time

    def _get_quote(self, id):
        try:
            message, author, timestamp = self.quote_store.get_quote(id)
        except Exception:
            return 'Quote #{} is not in the database.'.format(id)
        else:
            formatted_time = self._format_timestamp(timestamp)
            result = '#{} added by {} at {}:\n```{}```'.format(id, author, formatted_time, message)
            return result
class QuoteDB:
    def __init__(self, store_path = "quotes.txt"):
        self.store_path = store_path
        self.load_quotes()

    def load_quotes(self):
        if not os.path.exists(self.store_path):
            with open(self.store_path, 'w+'): pass
        elif os.path.isdir(self.store_path):
            print('Invalid quotes file: ', self.store_path)
            #throw error? todo
        with open(self.store_path, encoding='utf-8') as quote_file:
            self.ids_to_messages = {}
            reader = csv.reader(quote_file, delimiter=' ', quotechar = "|", quoting = csv.QUOTE_MINIMAL)
            for row in reader:
                if len(row) == 0:
                    continue
                id = int(row[0])
                author = row[1]
                timestamp = float(row[2])
                message = row[3]
                self.ids_to_messages[id] = (message, author, timestamp)
 
    def store_quotes(self):
        with open(self.store_path, 'w', encoding='utf-8') as store:
            writer = csv.writer(store, delimiter = ' ', quotechar = "|", quoting = csv.QUOTE_MINIMAL)
            for id, (message, author, timestamp) in self.ids_to_messages.items():
                writer.writerow([id, author, timestamp, message])
    
    def generate_next_id(self):
        if len(self.ids_to_messages) == 0:
            return 1
        last_id = list(self.ids_to_messages.keys())[-1]
        return last_id + 1

    def add_quote(self, message, timestamp, author):
        id = self.generate_next_id()
        self.ids_to_messages[id] = (message, author, timestamp)
        self.store_quotes()
        return id
    
    def delete_quote(self, id):
        if id in self.ids_to_messages:
            del self.ids_to_messages[id]
            self.store_quotes()
        else:
            raise Exception('Quote #{} not found in the database.'.format(id))
    def find_message_quotes(self, message_substring):
        message_substring = message_substring.lower()
        matching_ids = []
        for id, (message, _, _) in self.ids_to_messages.items():
            if message_substring in message.lower():
                matching_ids.append(id)
        return matching_ids
    
    def find_author_quotes(self, author):
        author = author.lower()
        matching_ids = []
        for id, (_, message_author, _) in self.ids_to_messages.items():
            if author == message_author.lower():
                matching_ids.append(id)
        return matching_ids

    def get_quote(self, id):
        if id not in self.ids_to_messages.keys():
            raise Exception('Invalid quote ID.')
        message, author, timestamp = self.ids_to_messages[id]
        return message, author, timestamp

    def get_random_id(self):
        if len(self.ids_to_messages) == 0:
            raise Exception('No quotes in the database.')
        id = random.choice(list(self.ids_to_messages.keys()))
        return id
    
    def get_most_recent_id(self):
        if len(self.ids_to_messages) == 0:
            raise Exception('No quotes in the database.')
        id = list(self.ids_to_messages.keys())[-1]
        return id

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-t", "--token", type=str, required=True, help="The token for your discord bot.")
    parser.add_argument("-o", "--owner", type=int, required=True, help="The quote bot owner's discord id.")
    parser.add_argument("-q", "--quotes", type=str, required=False, help="The path for the text file to store your quotes.", default="quotes.txt")
    args = parser.parse_args()
    token = args.token
    owner_id = args.owner
    quotes_store = args.quotes
    client = QuoteBot(owner_id, quotes_store)
    client.run(token)