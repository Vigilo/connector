# vim: set fileencoding=utf-8 sw=4 ts=4 et :

"""
Extends pubsub clients to compute Socket message
"""

from __future__ import absolute_import

from twisted.internet import reactor, protocol
from twisted.protocols.basic import LineReceiver
from twisted.words.protocols.jabber.jid import JID
from twisted.words.xish import domish
from wokkel.pubsub import PubSubClient, Item
from wokkel.generic import parseXml
from wokkel import xmppim

from vigilo.connector import converttoxml 
from vigilo.connector.store import DbRetry
from vigilo.common.gettext import translate
_ = translate(__name__)
from vigilo.common.logging import get_logger
LOGGER = get_logger(__name__)

import os

MESSAGEONETOONE = 'oneToOne'


class Forwarder(LineReceiver):
    """ Protocol used for each line received from the socket """
    
    delimiter = '\n'

    def lineReceived(self, line):
        """ redefinition of the lineReceived function"""
        
        if len(line) == 0:
            # empty line -> can't parse it
            return

        # already XML or not ?
        if line[0] != '<':
            xml = converttoxml.text2xml(line)
        else:
            xml = parseXml(line)

        if xml is None:
            # Couldn't parse this line
            return
        if xml.name == MESSAGEONETOONE:
            self.factory.sendOneToOneXml(xml)
        else:
            self.factory.publishXml(xml)



class SocketToNodeForwarder(PubSubClient):
    """
    Receives messages on the socket and passes them to the xmpp bus,
    Forward socket to Node.
    """

    def __init__(self, socket_filename, dbfilename, dbtable, 
                 nodetopublish, service):
        """
        Instancie un connecteur socket vers BUS XMPP.

        @param pipe_filename: le nom du fichier pipe qui accueillra les 
        messages XMPP
        @type pipe_filename: C{str}
        @param dbfilename: le nom du fichier permettant la sauvegarde des 
        messages en cas de problème d'éciture sur le pipe
        @type dbfilename: C{str}
        @param dbtable: Le nom de la table SQL dans ce fichier.
        @type dbtable: C{str}
        @param nodetopublish: dictionnaire pour la correspondance type de message 
                              noeud PubSub de destination.
        @type nodetopublish: C{dict}
        @param service: The publish subscribe service that keeps the node.
        @type service: L{JID}
        """
        PubSubClient.__init__(self)
        self.retry = DbRetry(dbfilename, dbtable)
        self.__backuptoempty = os.path.exists(dbfilename)

        self.__factory = protocol.ServerFactory()

        self.__factory.protocol = Forwarder
        self.__factory.publishXml = self.publishXml
        self.__factory.sendOneToOneXml = self.sendOneToOneXml
        self.__connector = reactor.listenUNIX(socket_filename, self.__factory)
        self.__service = service
        self.__nodetopublish = nodetopublish


    def sendQueuedMessages(self):
        """
        Called to send Message previously stored
        """
        if self.__backuptoempty:
            self.__backuptoempty = False
            # XXX Ce code peut potentiellement boucler indéfiniment...
            while True:
                msg = self.retry.unstore()
                if msg is None:
                    break
                else:
                    xml = parseXml(msg)
                    if xml.name == MESSAGEONETOONE:
                        if self.sendOneToOneXml(xml) is not True:
                            # we loose the ability to send message again
                            self.__backuptoempty = True
                            break
                    else:
                        if self.publishXml(xml) is not True:
                            # we loose the ability to send message again
                            self.__backuptoempty = True
                            break

            self.retry.vacuum()


    def connectionInitialized(self):
        """ redefinition of the function for flushing backup message """
        PubSubClient.connectionInitialized(self)
        # There's probably a way to configure it (on_sub vs on_sub_and_presence)
        # but the spec defaults to not sending subscriptions without presence.
        self.send(xmppim.AvailablePresence())
        LOGGER.info(_('ConnectionInitialized'))
        self.sendQueuedMessages()


    def sendOneToOneXml(self, xml):
        """ 
        function to send a XML msg to a particular jabber user
        @param xml: le message a envoyé sous forme XML 
        @type xml: twisted.words.xish.domish.Element
        """
        # we need to send it to a particular receiver 
        # il faut l'envoyer vers un destinataire en particulier
        msg = domish.Element((None, "message"))
        msg["to"] = xml['to']
        msg["from"] = self.parent.jid.userhostJID().full()
        msg["type"] = 'chat'
        body = xml.firstChildElement()
        msg.addElement("body", content=body)
        # if not connected store the message
        if self.xmlstream is None:
            LOGGER.error(_('Message from Socket impossible to forward' + \
                           ' (no connection to XMPP server), the mess' + \
                           'age is stored for later reemission'))
            self.retry.store(xml.toXml().encode('utf8'))
            self.__backuptoempty = True 
        else:
            self.send(msg)
            return True



    def publishXml(self, xml):
        """ 
        function to publish a XML msg to node 
        @param xml: le message a envoyé sous forme XML 
        @type xml: twisted.words.xish.domish.Element
        """
        # if not connected store the message
        if self.xmlstream is None:
            LOGGER.error(_('Message from Socket impossible to forward' + \
                           ' (no connection to XMPP server), the mess' + \
                           'age is stored for later reemission'))
            self.retry.store(xml.toXml().encode('utf8'))
            return

        def eb(e, xml):
            """errback"""
            LOGGER.error(_("errback publishStrXml %s") % e.__str__())
            self.retry.store(xml.toXml().encode('utf8'))
            self.__backuptoempty = True 

        item = Item(payload=xml)
        node = self.__nodetopublish[xml.name]
        try :
            result = self.publish(self.__service, node, [item])
            result.addErrback(eb, xml)
            return True
        except AttributeError :
            LOGGER.error(_('Message from Socket impossible to forward' + \
                           ' (no connection to XMPP server), the mess' + \
                           'age is stored for later reemission'))
            self.retry.store(xml.toXml().encode('utf8'))
            self.__backuptoempty = True
