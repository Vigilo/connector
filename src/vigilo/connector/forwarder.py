# vim: set fileencoding=utf-8 sw=4 ts=4 et :

"""
Classe de base pour les composants d'un connecteur.
"""

from __future__ import absolute_import

from collections import deque

from twisted.internet import reactor, defer, task
from twisted.python.failure import Failure
from twisted.words.xish import domish
from twisted.words.protocols.jabber.jid import JID
from wokkel.pubsub import PubSubClient, Item
from wokkel.generic import parseXml
from wokkel import xmppim

from vigilo.pubsub.xml import NS_PERF
from vigilo.connector import MESSAGEONETOONE
from vigilo.connector.store import DbRetry
from vigilo.common.gettext import translate
_ = translate(__name__)
from vigilo.common.conf import settings
settings.load_module(__name__)
from vigilo.common.logging import get_logger
LOGGER = get_logger(__name__)


class NotConnectedError(Exception):
    def __str__(self):
        return _('no connection')

class XMPPNotConnectedError(NotConnectedError):
    def __str__(self):
        return _('no connection to the XMPP server')

class PubSubForwarder(PubSubClient):
    """
    Traite des messages en provenance de ou à destination du bus.

    @ivar _pending_replies: file des réponses à attendre de la part du serveur.
        Quand un message est envoyé, son Deferred est ajouté dans cette file.
        Quand elle est pleine (voir le paramètre de configuration
        C{max_send_simult}), on doit attendre les réponses du serveurs, qui
        vident la file en arrivant. Sur eJabberd, cela doit correspondre au
        paramètre C{max_fsm_queue} (par défaut à 1000)
    @type _pending_replies: C{Queue.Queue}
    @ivar _nodetopublish: dictionnaire pour la correspondance type de message
                          noeud PubSub de destination.
    @type _nodetopublish: C{dict}
    @ivar _service: Le service pubsub qui héberge le nœud de publication.
    @type _service: C{twisted.words.protocols.jabber.jid.JID}
    @ivar max_send_simult: le nombre de messages qu'on est autorisé à envoyer
        en simultané avant de devoir s'arrêter pour écouter les réponses du bus
    @type max_send_simult: C{int}
    """

    def __init__(self, dbfilename=None, dbtable=None):
        """
        Instancie un connecteur vers le bus XMPP.

        @param dbfilename: le nom du fichier permettant la sauvegarde des
                           messages en cas de problème d'éciture sur le BUS
        @type  dbfilename: C{str}
        @param dbtable: Le nom de la table SQL pour la sauvegarde des messages.
        @type  dbtable: C{str}
        """
        super(PubSubForwarder, self).__init__()
        self.name = self.__class__.__name__
        self._service = JID(settings['bus']['service'])
        self._nodetopublish = settings.get('publications', {})
        self.queue = deque()
        # Base de backup
        if dbfilename is None or dbtable is None:
            self.retry = None
        else:
            self.retry = DbRetry(dbfilename, dbtable)
            self.retry.initdb()
        self._task_process_queue = task.LoopingCall(self.processQueue)
        # File d'attente des réponses
        self.max_send_simult = 1
        self._pending_replies = []
        self._processing_queue = False
        self._messages_forwarded = 0

    def connectionInitialized(self):
        """
        Lancée à la connexion (ou re-connexion).
        Redéfinie pour pouvoir vider les messages en attente.
        """
        super(PubSubForwarder, self).connectionInitialized()
        LOGGER.info(_('Connected to the XMPP bus'))
        if not self._task_process_queue.running:
            if self.retry is None:
                d = defer.succeed(None)
            else:
                d = self.retry.initdb()
            def start_task(r):
                if not self._task_process_queue.running:
                    self._task_process_queue.start(5)
            d.addCallback(start_task)
        self._messages_forwarded = 0

    def connectionLost(self, reason):
        """
        Lancée à la perte de la connexion au bus. Permet d'arrêter d'envoyer
        les messages en attente.
        """
        super(PubSubForwarder, self).connectionLost(reason)
        LOGGER.info(_('Lost connection to the XMPP bus (reason: %s)'), reason)
        if self.retry is not None:
            self.retry.flush()

    def isConnected(self):
        """
        Teste si on est connecté à notre destination (par exemple: le bus, un
        pipe, un socket, etc...)
        """
        raise NotImplementedError()

    def getStats(self):
        """Récupère des métriques de fonctionnement du connecteur"""
        stats = {
            "forwarded": self._messages_forwarded,
            "queue": len(self.queue),
            }
        if self.retry is None:
            return defer.succeed(stats)
        else:
            stats["backup_in_buf"] = len(self.retry.buffer_in)
            stats["backup_out_buf"] = len(self.retry.buffer_out)
            backup_size_d = self.retry.qsize()
            def add_backup_size(backup_size):
                stats["backup"] = backup_size
                return stats
            backup_size_d.addCallback(add_backup_size)
            return backup_size_d

    def _send_failed(self, e, msg):
        """errback: remet le message en base"""
        errmsg = _('Unable to forward the message (%(reason)s)')
        if self.retry is not None:
            errmsg += _('. it has been stored for later retransmission')
        LOGGER.error(errmsg % {"reason": e.getErrorMessage()})
        if self.retry is not None:
            self.retry.put(msg)

    def forwardMessage(self, msg):
        """
        Envoi du message sur le bus, en respectant le nombre max d'envois
        simultanés.
        @param msg: le message à envoyer
        """
        self.queue.append(msg)
        reactor.callLater(0, self.processQueue)

    @defer.inlineCallbacks
    def processQueue(self):
        """
        Envoie les messages en attente, en commançant par le backup s'il en
        contient. On respecte aussi le nombre max de messages simultanés
        acceptés par le bus.

        @note: U{http://stackoverflow.com/questions/776631/using-twisteds-twisted-web-classes-how-do-i-flush-my-outgoing-buffers}
        """
        if self._processing_queue:
            return
        self._processing_queue = True
        # Gestion du cas déconnecté
        if not self.isConnected():
            # on sauvegarde les messages
            while len(self.queue) > 0:
                msg = self.queue.popleft()
                if not isinstance(msg, basestring):
                    msg = msg.toXml().encode("utf-8")
                yield self.retry.put(msg)
            self._processing_queue = False
            return
        # Vérification qu'il y a bien quelque chose à faire
        if self.retry is not None:
            backup_size = yield self.retry.qsize()
        else:
            backup_size = 0
        if len(self.queue) == 0 and backup_size == 0:
            self._processing_queue = False
            return # rien à faire
        # Boucle principale de dépilement
        while self.isConnected(): # arrêt si on perd la connexion
            # on commence par essayer le backup
            if self.retry is not None:
                msg = yield self.retry.pop()
            else:
                msg = None
            if msg is None:
                # rien dans le backup, on essaye la file principale
                try:
                    msg = self.queue.popleft()
                except IndexError:
                    break # rien à faire
            # envoi
            self._messages_forwarded += 1
            result = self.processMessage(msg)
            if result is None:
                continue # pas besoin d'attendre
            if self.max_send_simult <= 1:
                yield result # pas d'envoi simultané
            else:
                self._pending_replies.append(result)
                if len(self._pending_replies) >= self.max_send_simult:
                    if self.max_send_simult >= 100:
                        LOGGER.info(_('Batch sent, waiting for %d replies '
                                      'from the bus'),
                                    len(self._pending_replies))
                    break # on fait une pause pour écouter les réponses
        if self._pending_replies:
            yield self.waitForReplies()
        self._processing_queue = False
        # on relance : si la file est vide, on quittera au début
        reactor.callLater(0, self.processQueue)

    def processMessage(self, msg):
        """
        Traite un message, par exemple en l'envoyant sur le bus.
        Ne sera pas lancé plus de L{max_send_simult} fois sans attendre les
        réponses.
        @param msg: message à traiter
        @type  msg: C{str} ou C{twisted.words.xish.domish.Element}
        @return: le C{Deferred} avec la réponse, ou C{None} si cela n'a pas
            lieu d'être (message envoyé en push)
        """
        raise NotImplementedError()

    def waitForReplies(self):
        """
        Attente des réponses de la part du bus. Les réponses sont dans
        L{_pending_replies}, et sont dépilées au fur et à mesure de leur
        arrivée.

        Note: l'implémentation n'utilise pas {defer.inlineDeferred} car on va
        déjà faire appel à cette méthode par un C{yield} dans
        L{processQueue}, donc on a pas le droit de I{yielder} nous-même.

        @return: un Deferred qui se déclenche quand toutes les réponses sont
            arrivées
        @rtype:  C{Deferred}
        """
        d = defer.DeferredList(self._pending_replies)
        def purge_pending(r): # pylint:disable-msg=W0613
            del self._pending_replies[:]
        d.addCallback(purge_pending)
        return d


class PubSubSender(PubSubForwarder):
    """
    Gère des messages à destination du bus
    """

    def __init__(self, dbfilename=None, dbtable=None):
        super(PubSubSender, self).__init__(dbfilename, dbtable)
        self._messages_sent = 0
        # Envoi simultanés sur le bus
        max_send_simult = int(settings['bus'].get('max_send_simult', 1000))
        # marge de sécurité de 20%
        self.max_send_simult = int(max_send_simult * 0.8)
        # accumulation des messages de perf
        self.batch_send_perf = int(settings["bus"].get("batch_send_perf", 1))
        self._batch_perf_queue = deque()
        if "perf" in self._nodetopublish:
            self._nodetopublish["perfs"] = self._nodetopublish["perf"]

    def connectionInitialized(self):
        super(PubSubSender, self).connectionInitialized()
        self._messages_sent = 0 # c'est un COUNTER, on peut réinitialiser

    def getStats(self):
        """Récupère des métriques de fonctionnement du connecteur"""
        d = super(PubSubSender, self).getStats()
        def add_messages_sent(stats):
            stats["sent"] = self._messages_sent
            return stats
        d.addCallback(add_messages_sent)
        return d

    def isConnected(self):
        """
        Teste si on est connecté au bus
        """
        return self.xmlstream is not None

    def processMessage(self, msg):
        """
        Traite un message en l'envoyant sur le bus.
        Ne sera pas lancé plus de L{max_send_simult} fois sans attendre les
        réponses.
        @param msg: message à traiter
        @type  msg: C{str} ou C{twisted.words.xish.domish.Element}
        @return: le C{Deferred} avec la réponse, ou C{None} si cela n'a pas
            lieu d'être (message envoyé en push)
        """
        self._messages_sent += 1
        if isinstance(msg, basestring):
            msg = parseXml(msg)
        if msg.name == MESSAGEONETOONE:
            # pas de réponse du bus pour ce type de messages (push)
            return self.sendOneToOneXml(msg)
        # accumulation des messages de perf
        msg = self._accumulate_perf_msgs(msg)
        if msg is None:
            return None
        result = self.publishXml(msg)
        return result

    def _accumulate_perf_msgs(self, msg):
        if self.batch_send_perf <= 1 or msg.name != "perf":
            return msg # on est pas concerné
        self._batch_perf_queue.append(msg)
        if len(self._batch_perf_queue) < self.batch_send_perf:
            return None
        batch_msg = domish.Element((NS_PERF, "perfs"))
        for msg in self._batch_perf_queue:
            batch_msg.addChild(msg)
        self._batch_perf_queue.clear()
        #LOGGER.info("Sent a batch perf message with %d messages",
        #            self.batch_send_perf)
        return batch_msg

    def sendOneToOneXml(self, xml):
        """
        Envoi d'un message à un utilisateur particulier.
        @note: il n'y a pas de réponse du bus à attendre, donc pas de
            C{Deferred} retourné
        @param xml: le message a envoyé sous forme XML
        @type xml: twisted.words.xish.domish.Element
        """
        # il faut l'envoyer vers un destinataire en particulier
        msg = domish.Element((None, "message"))
        msg["to"] = xml['to']
        msg["from"] = self.parent.jid.userhostJID().full()
        msg["type"] = 'chat'
        body = xml.firstChildElement()
        msg.addElement("body", content=body)
        # if not connected store the message
        if not self.isConnected():
            self._send_failed(Failure(XMPPNotConnectedError()),
                              xml.toXml().encode('utf8'))
        else:
            self.xmlstream.send(msg)

    def publishXml(self, xml):
        """
        function to publish a XML msg to node
        @param xml: le message a envoyé sous forme XML
        @type xml: twisted.words.xish.domish.Element
        """
        if xml.name not in self._nodetopublish:
            LOGGER.error(_("No destination node configured for messages "
                           "of type '%s'. Skipping."), xml.name)
            return defer.succeed(True)
        node = self._nodetopublish[xml.name]
        item = Item(payload=xml)
        try:
            result = self.publish(self._service, node, [item])
        except AttributeError:
            result = defer.fail(XMPPNotConnectedError())
        finally:
            del item
        result.addErrback(self._send_failed, xml.toXml().encode('utf8'))
        return result


class PubSubListener(PubSubForwarder):
    """
    Gère des messages en provenance du bus
    """
    # pylint:disable-msg=W0223

    def connectionInitialized(self):
        super(PubSubListener, self).connectionInitialized()
        # Réceptionner les messages directs ("one-to-one")
        self.xmlstream.addObserver("/message[@type='chat']", self.chatReceived)

    def chatReceived(self, msg):
        """
        Fonction de traitement des messages de discussion reçus.
        @param msg: Message à traiter.
        @type  msg: C{twisted.words.xish.domish.Element}
        """
        # les données dont on a besoin sont juste en dessous
        for data in msg.body.elements():
            #LOGGER.debug('Chat message to forward: %s',
            #             data.toXml().encode('utf8'))
            if data.name == "perfs":
                for msg in data.elements():
                    self.forwardMessage(msg)
            else:
                self.forwardMessage(data)

    def itemsReceived(self, event):
        """
        Fonction de traitement des événements XMPP reçus.
        @param event: Événement XMPP à traiter.
        @type  event: C{twisted.words.xish.domish.Element}
        """
        for item in event.items:
            if item.name != 'item':
                # The alternative is 'retract', which we silently ignore
                # We receive retractations in FIFO order,
                # ejabberd keeps 10 items before retracting old items.
                continue
            for data in item.elements():
                #LOGGER.debug('Published message to forward: %s' %
                #             data.toXml().encode('utf8'))
                if data.name == "perfs":
                    for msg in data.elements():
                        self.forwardMessage(msg)
                else:
                    self.forwardMessage(data)

