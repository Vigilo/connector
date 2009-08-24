# vim: set fileencoding=utf-8 sw=4 ts=4 et :
from __future__ import absolute_import

"""
Extends pubsub clients to compute Node message.
"""

import twisted.internet.protocol
from twisted.internet import reactor
from twisted.protocols.basic import LineReceiver

from vigilo.common.logging import get_logger
from vigilo.pubsub import  NodeSubscriber
import logging 
from vigilo.connector.stock import unstockmessage, stockmessage,\
        initializeDB, sqlitevacuumDB
import os

import time

LOGGER = get_logger(__name__)

class NodeToSocketForwarder(NodeSubscriber, twisted.internet.protocol.Protocol):
    """
    Receives messages on the xmpp bus, and passes them to the socket.
    Forward Node to socket.
    """

    def __init__(self, subscription, socket_filename, file_filename):
        self.__filename = file_filename
        initializeDB(self.__filename)
        self.__subscription = subscription
        self.__backuptoempty = os.path.exists(file_filename) 
        # using ReconnectingClientFactory using a backoff retry 
        # (it try again and again with a delay incrising between attempt)
        factory = twisted.internet.protocol.ReconnectingClientFactory()
        factory.buildProtocol = lambda addr: self
        #creation socket
        connector = reactor.connectUNIX(socket_filename, factory, timeout=3, checkPID=0)
        self.__connector = connector
        NodeSubscriber.__init__(self, [subscription])


    def itemsReceived(self, event):
        # See ItemsEvent
        #event.sender
        #event.recipient
        if event.nodeIdentifier != self.__subscription.node:
            return
        #event.headers
        for item in event.items:
            # Item is a domish.IElement and a domish.Element
            # Serialize as XML before queueing,
            # or we get harmless stderr pollution  × 5 lines:
            # Exception RuntimeError: 'maximum recursion depth exceeded in __subclasscheck__' in <type 'exceptions.AttributeError'> ignored
            # Stderr pollution caused by http://bugs.python.org/issue5508
            # and some touchiness on domish attribute access.
            if item.name != 'item':
                # The alternative is 'retract', which we silently ignore
                # We receive retractations in FIFO order,
                # ejabberd keeps 10 items before retracting old items.
                continue
            it = [ it for it in item.elements() if item.name == "item" ]
            if self.__connector.state == 'connected':
                # TODO unstock previous message if needed
                if self.__backuptoempty:
                    while not unstockmessage(self.__filename, self.__connector.transport.write):
                        None
                    self.__backuptoempty = False
                    sqlitevacuumDB(self.__filename)

                for i in it:
                    LOGGER.debug('Message from BUS to forward: %s', i.toXml().encode('utf8'))
                    self.__connector.transport.write(i.toXml().encode('utf8') + '\n\n')
            else:
                for i in it:
                    LOGGER.debug('Message from BUS impossible to forward (socket close ?): %s', i.toXml().encode('utf8'))
                    # TODO stock the message for a futur reemission
                    stockmessage(self.__filename, i.toXml().encode('utf8'))
                    self.__backuptoempty = True

