#!/usr/bin/env python3

import logging
import threading
import json
import discord
import asyncio
import uuid

from src import util
from src import messages
from src.commandhandler import node
from src.commandhandler import user
from src.commandhandler import common
from src.smartexplorer import WebExplorer

logger = logging.getLogger("bot")

class SmartNodeBotDiscord(object):

    def __init__(self, botToken, admin, password, db, nodeList):

        # Currently only used for markdown
        self.messenger = "discord"

        self.client = discord.Client()
        self.client.on_ready = self.on_ready
        self.client.on_message = self.on_message
        # Create a bot instance for async messaging
        self.token = botToken
        # Set the database of the pools/users/nodes
        self.database = db
        # Store and setup the nodeslist
        self.nodeList = nodeList
        self.nodeList.networkCB = self.networkCB
        self.nodeList.nodeChangeCB = self.nodeUpdateCB
        # Create the WebExplorer
        self.explorer = WebExplorer(self.balancesCB)
        self.balanceChecks = {}
        # Store the admin password
        self.password = password
        # Store the admin user
        self.admin = admin
        # Semphore to lock the balance check list.
        self.balanceSem = threading.Lock()

    ######
    # Starts the bot and block until the programm gets stopped.
    ######
    def start(self):
        self.client.run(self.token)

    ######
    # Send a message :text to a specific user :user
    ######
    async def sendMessage(self, user, text, split = '\n'):

        logger.info("sendMessage - Chat: {}, Text: {}".format(user,text))

        parts = messages.splitMessage(text, split, 2000)

        try:
            for part in parts:
                await self.client.send_message(user, part)
        except discord.errors.Forbidden:
            logging.error('sendMessage user blocked the bot')

            # Remove the user and the assigned nodes.
            self.database.deleteNodesForUser(user.id)
            self.database.deleteUser(user.id)

        except Exception as e:
            logging.error('sendMessage', exc_info=e)
        else:
            logger.info("sendMessage - OK!")

    async def on_ready(self):
        print('Logged in as')
        print(self.client.user.name)
        print(self.client.user.id)
        print('------')

    ######
    # Discord api coroutine which gets called when a new message has been
    # received in one of the channels or in a private chat with the bot.
    ######
    async def on_message(self,message):

        if message.author == self.client.user:
            # Just jump out if its the bots message.
            return

        # split the new messages by spaces
        parts = message.content.split()

        command = None
        args = None

        # If the first mention in the message is the bot itself
        # and there is a possible command in the message
        if len(message.mentions) and message.mentions[0] == self.client.user\
            and len(parts) > 1:
            command = parts[1]
            args = parts[2:]
        # If there are no mentions and we are in a private chat
        elif len(message.mentions) == 0 and not isinstance(message.author, discord.Member):
            command = parts[0]
            args = parts[1:]
        # If we got mentioned but no command is available in the message just send the help
        elif len(message.mentions) and message.mentions[0] == self.client.user and\
              len(parts) == 1:
              command = 'help'
        # No message of which the bot needs to know about.
        else:
            logger.debug("on_message - jump out {}".format(self.client.user))
            return

        # If we got here call the command handler to see if there is any action required now.
        await self.commandHandler(message, command.lower(), args)

    ######
    # Handles incomming splitted messages. Check if there are commands which require
    # any action. If so it calls the related methods and sends the response to
    # the author of the command message.
    ######
    async def commandHandler(self, message, command, args):

        logger.info("commandHandler - {}, command: {}, args: {}".format(message.author, command, args))

        # per default assume the message gets back from where it came
        receiver = message.author

        ####
        # List of available commands
        # Public = 0
        # DM-Only = 1
        # Admin only = 2
        ####
        commands = {
                    # Common commands
                    'info':0,
                    # User commmands
                    'me':1,'status':1,'reward':1,'network':1, 'timeout':1,
                    # Node commands
                    'add':1,'update':1,'remove':1,'nodes':1, 'detail':1, 'balance':1,
                    # Admin commands
                    'stats':2, 'broadcast':2
        }

        # If the command is DM only
        if command in commands and commands[command] == 1:

            if isinstance(message.author, discord.Member):
             await self.client.send_message(message.channel,\
             message.author.mention + ', the command `{}` is only available in private chat with me!'.format(command))
             await self.client.send_message(message.author,'Try it here!')
             return

        else:
            receiver = message.channel

        # If the command is admin only
        if command in commands and commands[command] == 2:

            # Admin command got fired in a public chat
            if isinstance(message.author, discord.Member):
                # Just send the unknown command message and jump out
                await self.sendMessage(receiver, (message.author.mention + ", " + common.unknown(self)))
                logger.info("Admin only, public")
                return

            # Admin command got fired from an unauthorized user
            if int(message.author.id) == int(self.admin) and\
                len(args) >= 1 and args[0] == self.password:
                receiver = message.author
            else:
                logger.info("Admin only, other")

                # Just send the unknown command message and jump out
                await self.sendMessage(receiver, (message.author.mention + ", " + common.unknown(self)))
                return

        ### Common command handler ###
        if command == 'info':
            response = common.info(self,message)
            await self.sendMessage(receiver, response)
        ### Node command handler ###
        elif command == 'add':
            response = node.nodeAdd(self,message,args)
            await self.sendMessage(receiver, response)
        elif command == 'update':
            response = node.nodeUpdate(self,message,args)
            await self.sendMessage(receiver, response)
        elif command == 'remove':
            response = node.nodeRemove(self,message,args)
            await self.sendMessage(receiver, response)
        elif command == 'nodes':
            response = node.nodes(self,message)
            await self.sendMessage(receiver, response)
        elif command == 'detail':
            response = node.detail(self,message)
            await self.sendMessage(receiver, response)
        elif command == 'balance':

            failed = None
            nodes = []

            for n in self.database.getNodes(message.author.id):
                nodes.append(self.nodeList.getNodeById(n['node_id']))

            check = self.explorer.balances(nodes)

            # Needed cause the balanceChecks dict also gets modified from other
            # threads.
            self.balanceSem.acquire()

            if check:
                self.balanceChecks[check] = message.author.id
            else:
                logger.info("Balance check failed instant.")
                failed = uuid.uuid4()
                self.balanceChecks[failed] = message.author.id

            # Needed cause the balanceChecks dict also gets modified from other
            # threads.
            self.balanceSem.release()

            if failed:
                self.balancesCB(failed,None)

        ### User command handler ###
        elif command == 'me':
            response = user.me(self,message)
            await self.sendMessage(receiver, response)
        elif command == 'status':
            response = user.status(self,message, args)
            await self.sendMessage(receiver, response)
        elif command == 'reward':
            response = user.reward(self,message, args)
            await self.sendMessage(receiver, response)
        elif command == 'timeout':
            response = user.timeout(self,message, args)
            await self.sendMessage(receiver, response)
        elif command == 'network':
            response = user.network(self,message, args)
            await self.sendMessage(receiver, response)

        ### Admin command handler ###
        elif command == 'stats':
            response = common.stats(self)
            await self.sendMessage(receiver, response)
        elif command == 'broadcast':
            response = common.broadcast(self,message,args)
            await self.sendMessage(receiver, response)

        # Help message
        elif command == 'help':
            await self.sendMessage(receiver, messages.help(self.messenger))

        # Could not match any command. Send the unknwon command message.
        else:
            await self.sendMessage(receiver, (message.author.mention + ", " + common.unknown(self)))

    ######
    # Unfortunately there is no better way to send messages to a user if you have
    # only their userId. Therefor this method searched the discord user object
    # in the global member list and returns is.
    ######
    def findMember(self, userId):

        for member in self.client.get_all_members():
            if int(member.id) == int(userId):
                return member

        logger.info ("Could not find the userId in the list?! {}".format(userId))

        return None


    ############################################################
    #                        Callbacks                         #
    ############################################################


    ######
    # Callback which get called when there is a new releases in the smartcash repo.
    #
    # Called by: Nothing yet, SmartGitHubUpdates later.
    #
    ######
    def updateCheckCallback(self, tag):

        for user in self.database.getUsers():
            self.sendMessage(user['id'], ("*Node update available*\n\n"
                                         "https://github.com/SmartCash/smartcash/releases/tag/{}").format(tag))


    ######
    # Callback for evaluating if someone in the database had an upcomming event
    # and send messages to all chats with activated notifications
    #
    # Called by: SmartNodeList
    #
    ######
    def nodeUpdateCB(self, update, n):

        for user in self.database.getUsers():

            userNode = self.database.getNode(n.id, user['id'])

            if userNode == None:
                continue

            logger.info("nodeChangeCB {}".format(n.payee))

            member = self.findUser(user['id'])

            if member:
                for response in node.nodeUpdated(self, update, user, userNode, n):
                    asyncio.run_coroutine_threadsafe(self.sendMessage(member, response), loop=self.client.loop)

    #####
    # Callback for evaluating if someone has enabled network notifications
    # and send messages to all relevant chats
    #
    # Called by: SmartNodeList
    #
    ######
    def networkCB(self, ids, added):

        response = common.networkUpdate(self, ids, added)

        for user in self.database.getUsers():

            if user['network_n']:

                member = self.findMember(user['id'])

                if member:
                    asyncio.run_coroutine_threadsafe(self.sendMessage(member, response), loop=self.client.loop)

    ######
    # Callback which gets called from the SmartNodeList when a balance request triggered by any user
    # is done. It sends the result to the related user.
    #
    # Called by: SmartExplorer
    #
    ######
    def balancesCB(self, check, results):

        # Needed cause the balanceChecks dict also gets modified from other
        # threads.
        self.balanceSem.acquire()

        if not check in self.balanceChecks:
            logger.error("Ivalid balance check received {} - count {}".format(check,len(results)))
            self.balanceSem.release()
            return

        userId = self.balanceChecks[check]
        self.balanceChecks.pop(check)

        # Needed cause the balanceChecks dict also gets modified from other
        # threads.
        self.balanceSem.release()

        response = node.balances(self, userId, results)

        member = self.findMember(userId)

        if member:
            asyncio.run_coroutine_threadsafe(self.sendMessage(member, response), loop=self.client.loop)
