# vim: set fileencoding=utf-8 sw=4 ts=4 et :
# Copyright (C) 2011-2011 CS-SI
# License: GNU GPL v2 <http://www.gnu.org/licenses/gpl-2.0.html>

"""
Gestionnaires de messages
"""

from __future__ import absolute_import

import os
from collections import deque
from platform import python_version_tuple

try:
    import json
except ImportError:
    import simplejson as json

from zope.interface import implements

from twisted.internet import defer
from twisted.internet.interfaces import IConsumer, IPushProducer
from twisted.application.service import Service
from twisted.python.failure import Failure

import txamqp

from vigilo.connector.interfaces import IBusHandler, IBusProducer
from vigilo.connector.store import DbRetry

from vigilo.common.gettext import translate
_ = translate(__name__)
from vigilo.common.logging import get_logger, get_error_message
LOGGER = get_logger(__name__)



class BusHandler(object):
    """Gestionnaire de base à brancher à un client du bus"""

    implements(IBusHandler)


    def __init__(self):
        self.client = None

    def setClient(self, client):
        self.client = client
        self.client.addHandler(self)



class QueueSubscriber(BusHandler):
    """Abonnement à une file d'attente"""

    implements(IBusProducer, IBusHandler)


    def __init__(self, queue_name):
        BusHandler.__init__(self)
        self.queue_name = queue_name
        self._bindings = []
        self.consumer = None
        self._channel = None
        self._queue = None
        self._do_consume = True
        self.ready = defer.Deferred()


    def connectionInitialized(self):
        self._channel = self.client.channel
        d = self._create()
        d.addCallback(lambda _x: self._bind())
        d.addCallback(lambda _x: self._subscribe())
        return d

    def connectionLost(self, reason):
        self._channel = None
        self._queue = None
        self.ready = defer.Deferred()


    def bindToExchange(self, exchange, routing_key=None):
        if routing_key is None:
            routing_key = self.queue_name
        self._bindings.append( (exchange, routing_key) )


    def _create(self):
        if not self._channel:
            return defer.succeed(None)
        d = self._channel.queue_declare(queue=self.queue_name,
                    durable=True, exclusive=False, auto_delete=False)
        return d

    def _bind(self):
        dl = []
        for exchange, routing_key in self._bindings:
            d = self.client.channel.queue_bind(queue=self.queue_name,
                    exchange=exchange, routing_key=routing_key)
            dl.append(d)
        return defer.DeferredList(dl)

    def _subscribe(self):
        if not self._channel:
            return
        d = self._channel.basic_consume(queue=self.queue_name,
                                        consumer_tag=self.queue_name)
        d.addCallback(lambda reply: self.client.getQueue(reply.consumer_tag))
        def store_queue(queue):
            self._queue = queue
            return queue
        d.addCallback(store_queue)
        d.addCallback(self.ready.callback)
        return d


    def resumeProducing(self):
        if not self._queue:
            return defer.fail(Exception(
                        _("Can't resume producing: not connected yet")))
        d = self._queue.get()
        def cb(msg):
            if self.client.log_traffic:
                qname, msgid, unknown, exch, rkey = msg.fields
                LOGGER.debug("RECEIVED from %s on %s with key %s: %s"
                             % (exch, qname, rkey, msg.content.body))
            return self.consumer.write(msg)
        def eb(f):
            f.trap(txamqp.queue.Closed) # déconnexion pendant le get()
        d.addCallbacks(cb, eb)
        return d


    # Proxies

    def ack(self, msg, multiple=False):
        return self._channel.basic_ack(msg.delivery_tag, multiple=multiple)

    def send(self, exchange, routing_key, message):
        return self.client.send(exchange, routing_key, message)



class MessageHandler(BusHandler):
    """Gère la réception des messages. Peut aussi agir comme un IPushProducer"""

    implements(IConsumer, IPushProducer, IBusHandler)


    def __init__(self):
        BusHandler.__init__(self)
        self.producer = None
        self.keepProducing = True
        # Stats
        self._messages_forwarded = 0


    def write(self, msg):
        content = json.loads(msg.content.body)
        if "messages" in content and content["messages"]:
            d = self._processList(content["messages"])
        else:
            d = defer.maybeDeferred(self.processMessage, content)
        d.addCallbacks(self.processingSucceeded, self.processingFailed,
                       callbackArgs=(msg, ))
        if self.keepProducing:
            d.addBoth(lambda _x: self.producer.resumeProducing())
        return d


    @defer.inlineCallbacks
    def _processList(self, msglist):
        while msglist:
            msg = msglist.pop(0)
            yield defer.maybeDeferred(self.processMessage, msg)


    def processMessage(self, msg):
        raise NotImplementedError()


    def processingSucceeded(self, _ignored, msg):
        self.producer.ack(msg)

    def processingFailed(self, error):
        LOGGER.error(error)


    def pauseProducing(self):
        self.keepProducing = False

    def resumeProducing(self):
        self.keepProducing = True
        self.producer.resumeProducing()


    def getStats(self):
        """Récupère des métriques de fonctionnement du connecteur"""
        stats = {
            "forwarded": self._messages_forwarded,
            }
        return defer.succeed(stats)


    def subscribe(self, queue_name, bindings=None):
        # attention, pas d'injection de deps, faire le vrai boulot dans
        # registerProducer()
        subscriber = QueueSubscriber(queue_name)
        subscriber.setClient(self.client)
        if bindings is None:
            bindings = []
        for exchange, routing_key in bindings:
            subscriber.bindToExchange(exchange, routing_key)
        self.registerProducer(subscriber, False)

    def unsubscribe(self):
        return self.unregisterProducer()

    def registerProducer(self, producer, streaming):
        #assert streaming == False # on ne prend que des PullProducers
        self.producer = producer
        self.producer.consumer = self
        self.producer.ready.addCallback(
                lambda _x: self.producer.resumeProducing())

    def unregisterProducer(self):
        self.producer = None



class BusPublisher(BusHandler):
    """Gère la publication de messages"""

    implements(IConsumer, IBusHandler)


    def __init__(self, publications={}, batch_send_perf=1):
        BusHandler.__init__(self)
        self.name = self.__class__.__name__
        self.producer = None
        self._is_streaming = True
        self._publications = publications
        self._initialized = False
        # Stats
        self._messages_forwarded = 0
        self._messages_sent = 0
        # Accumulation des messages de perf
        self.batch_send_perf = batch_send_perf
        self._batch_perf_queue = deque()


    def connectionInitialized(self):
        """
        Lancée à la connexion (ou re-connexion).
        Redéfinie pour pouvoir vider les messages en attente.
        """
        self._initialized = True
        # les stats sont des COUNTERs, on peut réinitialiser
        self._messages_sent = 0
        if self.producer is not None:
            self.producer.resumeProducing()


    def connectionLost(self, reason):
        """
        Lancée à la perte de la connexion au bus. Permet d'arrêter d'envoyer
        les messages en attente.
        """
        self._initialized = False
        if self.producer is not None:
            self.producer.pauseProducing()
        if reason is None:
            reason = Failure(Exception())
        LOGGER.info(_('Lost connection to the XMPP bus (reason: %s)'),
                    get_error_message(reason))


    def isConnected(self):
        """
        Teste si on est connecté au bus
        """
        return self._initialized


    def getStats(self):
        """Récupère des métriques de fonctionnement du connecteur"""
        stats = {
            "forwarded": self._messages_forwarded,
            "sent": self._messages_sent,
            }
        return defer.succeed(stats)


    def registerProducer(self, producer, streaming):
        self.producer = producer
        self._is_streaming = streaming
        self.producer.consumer = self
        if self.isConnected():
            self.producer.resumeProducing()

    def unregisterProducer(self):
        self.producer.pauseProducing()
        self.producer = None


    def write(self, data):
        d = self.sendMessage(data)
        def doneSending(result):
            if self.producer is not None and not self._is_streaming:
                self.producer.resumeProducing()
            return result
        d.addBoth(doneSending)
        return d


    def sendMessage(self, msg):
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
            msg = json.loads(msg)
        # accumulation des messages de perf
        msg = self._accumulate_perf_msgs(msg)
        if msg is None:
            return defer.succeed(None)
        msg_text = json.dumps(msg)

        if msg["type"] in self._publications:
            exchange = self._publications[msg["type"]]
        else:
            exchange = msg["type"]

        routing_key = msg.get("routing_key", msg["type"])
        persistent = msg.get("persistent", True)
        result = self.client.send(exchange, str(routing_key), msg_text, persistent)
        return result


    def _accumulate_perf_msgs(self, msg):
        if self.batch_send_perf <= 1 or msg["type"] != "perf":
            return msg # on est pas concerné
        self._batch_perf_queue.append(msg)
        if len(self._batch_perf_queue) < self.batch_send_perf:
            return None
        batch_msg = {"type": "perf",
                     "messages": list(self._batch_perf_queue)}
        self._batch_perf_queue.clear()
        #LOGGER.info("Sent a batch perf message with %d messages",
        #            self.batch_send_perf)
        return batch_msg



class BackupProvider(Service):
    """
    Ajoute à un PushProducer la possibilité d'être mis en pause. Les données
    vont alors dans une file d'attente mémoire qui est sauvegardée sur le
    disque dans une base.
    """

    implements(IPushProducer, IConsumer)


    def __init__(self, dbfilename=None, dbtable=None, max_queue_size=None):
        self.producer = None
        self.consumer = None
        self.paused = True
        # File d'attente mémoire
        self.max_queue_size = max_queue_size
        self.queue = None
        self._build_queue()
        self._processing_queue = False
        # Base de backup
        if dbfilename is None or dbtable is None:
            self.retry = None
        else:
            self.retry = DbRetry(dbfilename, dbtable)
        # Stats
        self.stat_names = {
                "queue": "queue",
                "backup_in_buf": "backup_in_buf",
                "backup_out_buf": "backup_out_buf",
                "backup": "backup",
                }


    def _build_queue(self):
        if self.max_queue_size is not None:
            self.queue = deque(maxlen=self.max_queue_size)
        else:
            # sur python < 2.6, il n'y a pas de maxlen
            self.queue = deque()


    def startService(self):
        d = self.retry.initdb()
        if self.producer is not None:
            d.addCallback(lambda _x: self.producer.startService())
        return d


    def stopService(self):
        if self.producer is not None:
            d = self.producer.stopService()
        else:
            d = defer.succeed(None)
        d.addCallback(lambda _x: self._saveToDb())
        d.addCallback(lambda _x: self.retry.flush())
        return d


    def registerProducer(self, producer, streaming):
        assert streaming == True # Ça n'a pas de sens avec un PullProducer
        self.producer = producer
        self.producer.consumer = self

    def unregisterProducer(self):
        #self.producer.pauseProducing() # A priori il sait pas faire
        self.producer = None


    def getStats(self):
        """Récupère des métriques de fonctionnement"""
        stats = {
            self.stat_names["queue"]: len(self.queue),
            self.stat_names["backup_in_buf"]: len(self.retry.buffer_in),
            self.stat_names["backup_out_buf"]: len(self.retry.buffer_out),
            }
        backup_size_d = self.retry.qsize()
        def add_backup_size(backup_size):
            stats[ self.stat_names["backup"] ] = backup_size
            return stats
        backup_size_d.addCallbacks(add_backup_size,
                                   lambda e: add_backup_size("U"))
        return backup_size_d


    def write(self, data):
        self.queue.append(data)
        self.processQueue()


    def pauseProducing(self):
        self.paused = True
        d = self._saveToDb()
        d.addCallback(lambda _x: self.retry.flush())
        return d


    def resumeProducing(self):
        self.paused = False
        if self.retry.initialized.called:
            d = self.processQueue()
        else:
            d = self.retry.initialized
            d.addCallback(lambda _x: self.processQueue())
        return d


    def stopProducing(self):
        pass


    @defer.inlineCallbacks
    def processQueue(self):
        if self._processing_queue:
            return
        if self.paused:
            yield self._saveToDb()
            return

        self._processing_queue = True
        while True:
            msg = yield self._getNextMsg()
            if msg is None:
                break
            try:
                yield self.consumer.write(msg)
            except Exception, e:
                self._send_failed(e, msg)
        self._processing_queue = False


    def _send_failed(self, e, msg):
        """errback: remet le message en base"""
        errmsg = _('Unable to forward the message (%(reason)s).')
        LOGGER.error(errmsg % {
            "reason": get_error_message(e.value),
        })
        self.queue.append(msg)


    def _saveToDb(self):
        """Sauvegarde tous les messages de la file dans la base de backup"""
        def eb(f):
            LOGGER.error(_("Error trying to save a message to the backup "
                           "database: %s"), get_error_message(f.value))
        saved = []
        while len(self.queue) > 0:
            msg = self.queue.popleft()
            d = self.retry.put(json.dumps(msg))
            d.addErrback(eb)
            saved.append(d)
        return defer.gatherResults(saved)

    def _getNextMsg(self):
        """
        Récupère le prochain message à traiter, en commençant par essayer dans
        la base de backup
        """
        d = self.retry.pop()
        def get_from_queue(msg):
            if msg is not None:
                return json.loads(msg) # le backup est prioritaire
            # on dépile la file principale
            try:
                msg = self.queue.popleft()
            except IndexError:
                # plus de messages
                return None
            return msg
        d.addCallback(get_from_queue)
        return d


# Factories


def buspublisher_factory(settings, client=None):
    publications = {
            "aggr": "correlation",
            "delaggr": "correlation",
            "correvent": "correlation",
            }
    try:
        # copy: on modifie la hashmap dans status.py
        publications.update(settings.get('publications', {}))
    except KeyError:
        pass
    batch_send_perf = int(settings["bus"].get("batch_send_perf", 1))
    publisher = BusPublisher(publications, batch_send_perf)
    if client is not None:
        publisher.setClient(client)
    return publisher


def backupprovider_factory(settings, producer=None):
    # Max queue size
    try:
        max_queue_size = int(settings["connector"]["max_queue_size"])
    except KeyError:
        max_queue_size = 0
    except ValueError:
        LOGGER.warning(_("Can't understand the max_queue_size option, it "
                         "should be an integer (or 0 for no limit). "
                         "Current value: %s"), max_queue_size)
        max_queue_size = 0
    if max_queue_size <= 0:
        max_queue_size = None
    if (max_queue_size is not None and
                tuple(python_version_tuple()) < ('2', '6')):
        LOGGER.warning(_("The max_queue_size option is only available "
                         "on Python >= 2.6. Ignoring."))
        max_queue_size = None

    # Base de backup
    bkpfile = settings['connector'].get('backup_file', ":memory:")
    if bkpfile != ':memory:':
        if not os.path.exists(os.path.dirname(bkpfile)):
            msg = _("Directory not found: '%(dir)s'") % \
                    {'dir': os.path.dirname(bkpfile)}
            LOGGER.error(msg)
            raise OSError(msg)
        if not os.access(os.path.dirname(bkpfile), os.R_OK | os.W_OK | os.X_OK):
            msg = _("Wrong permissions on directory: '%(dir)s'") % \
                    {'dir': os.path.dirname(bkpfile)}
            LOGGER.error(msg)
            raise OSError(msg)
    bkptable = settings['connector'].get('backup_table_to_bus', "tobus")

    backup = BackupProvider(bkpfile, bkptable, max_queue_size)

    if producer is not None:
        backup.registerProducer(producer, True)

    return backup






#class PubSubForwarder(PubSubClient):
#    """
#    Traite des messages en provenance de ou à destination du bus.
#
#    @ivar _pending_replies: file des réponses à attendre de la part du serveur.
#        Quand un message est envoyé, son Deferred est ajouté dans cette file.
#        Quand elle est pleine (voir le paramètre de configuration
#        C{max_send_simult}), on doit attendre les réponses du serveurs, qui
#        vident la file en arrivant. Sur eJabberd, cela doit correspondre au
#        paramètre C{max_fsm_queue} (par défaut à 1000)
#    @type _pending_replies: C{Queue.Queue}
#    @ivar _nodetopublish: dictionnaire pour la correspondance type de message
#                          noeud PubSub de destination.
#    @type _nodetopublish: C{dict}
#    @ivar _service: Le service pubsub qui héberge le nœud de publication.
#    @type _service: C{twisted.words.protocols.jabber.jid.JID}
#    @ivar max_send_simult: le nombre de messages qu'on est autorisé à envoyer
#        en simultané avant de devoir s'arrêter pour écouter les réponses du bus
#    @type max_send_simult: C{int}
#    @ivar _process_as_domish: Détermine le format passé à L{processMessage} :
#        si C{False}, c'est une C{str}, si C{True}  c'est un C{domish.Element}.
#        Par défaut: C{True}.
#    @type _process_as_domish: C{bool}
#    """
#
#    def __init__(self, dbfilename=None, dbtable=None):
#        """
#        Instancie un connecteur vers le bus XMPP.
#
#        @param dbfilename: le nom du fichier permettant la sauvegarde des
#                           messages en cas de problème d'éciture sur le BUS
#        @type  dbfilename: C{str}
#        @param dbtable: Le nom de la table SQL pour la sauvegarde des messages.
#        @type  dbtable: C{str}
#        """
#        super(PubSubForwarder, self).__init__()
#        self.name = self.__class__.__name__
#        self._service = JID(settings['bus']['service'])
#        # copy: on modifie la hashmap dans status.py
#        self._nodetopublish = settings.get('publications', {}).copy()
#        self.max_queue_size = self._max_queue_size()
#        self._build_queue()
#        self._initialized = False
#        # Base de backup
#        if dbfilename is None or dbtable is None:
#            self.retry = None
#        else:
#            self.retry = DbRetry(dbfilename, dbtable)
#            self.retry.initdb()
#        self._task_process_queue = task.LoopingCall(self.processQueue)
#        # File d'attente des réponses
#        self.max_send_simult = 1
#        self._pending_replies = []
#        self._processing_queue = False
#        self._messages_forwarded = 0
#        self._process_as_domish = True
#        # Gestionnaire de présence
#        self.producer = None
#
#    def _build_queue(self):
#        if self.max_queue_size is not None:
#            self.queue = deque(maxlen=self.max_queue_size)
#        else:
#            # sur python < 2.6, il n'y a pas de maxlen
#            self.queue = deque()
#
#    def _max_queue_size(self):
#        max_queue_size = settings.get("connector", {}).get("max_queue_size", 0)
#        try:
#            max_queue_size = int(max_queue_size)
#        except ValueError:
#            LOGGER.warning(_("Can't understand the max_queue_size option, it "
#                             "should be an integer (or 0 for no limit). "
#                             "Current value: %s"), max_queue_size)
#            return None
#        if max_queue_size <= 0:
#            max_queue_size = None
#        if (max_queue_size is not None and
#                    tuple(python_version_tuple()) < ('2', '6')):
#            LOGGER.warning(_("The max_queue_size option is only available "
#                             "on Python >= 2.6. Ignoring."))
#            max_queue_size = None
#        return max_queue_size
#
#    def registerProducer(self, producer, streaming):
#        assert streaming == True # on ne sait pas gérer autre chose
#        self.producer = producer
#
#    def unregisterProducer(self):
#        self.producer = None
#
#    def connectionInitialized(self):
#        """
#        Lancée à la connexion (ou re-connexion).
#        Redéfinie pour pouvoir vider les messages en attente.
#        """
#        super(PubSubForwarder, self).connectionInitialized()
#        self._initialized = True
#        if not self._task_process_queue.running:
#            if self.retry is None:
#                d = defer.succeed(None)
#            else:
#                d = self.retry.initdb()
#            def start_task(r):
#                if not self._task_process_queue.running:
#                    self._task_process_queue.start(5)
#            d.addCallback(start_task)
#        self._messages_forwarded = 0
#
#    def connectionLost(self, reason):
#        """
#        Lancée à la perte de la connexion au bus. Permet d'arrêter d'envoyer
#        les messages en attente.
#        """
#        super(PubSubForwarder, self).connectionLost(reason)
#        self._initialized = False
#        LOGGER.info(_('Lost connection to the XMPP bus (reason: %s)'), reason)
#        if self.retry is not None:
#            self.retry.flush()
#
#    def isConnected(self):
#        """
#        Teste si on est connecté à notre destination (par exemple: le bus, un
#        pipe, un socket, etc...)
#        """
#        raise NotImplementedError()
#
#    def getStats(self):
#        """Récupère des métriques de fonctionnement du connecteur"""
#        stats = {
#            "forwarded": self._messages_forwarded,
#            "queue": len(self.queue),
#            }
#        if self.retry is None:
#            return defer.succeed(stats)
#        else:
#            stats["backup_in_buf"] = len(self.retry.buffer_in)
#            stats["backup_out_buf"] = len(self.retry.buffer_out)
#            backup_size_d = self.retry.qsize()
#            def add_backup_size(backup_size):
#                stats["backup"] = backup_size
#                return stats
#            backup_size_d.addCallbacks(add_backup_size,
#                                       lambda e: add_backup_size("U"))
#            return backup_size_d
#
#    def _send_failed(self, e, msg):
#        """errback: remet le message en base"""
#        errmsg = _('Unable to forward the message (%(reason)s)')
#        if isinstance(e.value, error.StanzaError) and \
#                e.value.condition == "not-acceptable":
#            LOGGER.error(errmsg % {"reason": e.getErrorMessage()})
#            return # pas de sauvegarde, sinon on boucle
#        if self.retry is not None:
#            errmsg += _('. it has been stored for later retransmission')
#        LOGGER.error(errmsg % {"reason": e.getErrorMessage()})
#        if self.retry is not None:
#            self.retry.put(msg)
#
#    def forwardMessage(self, msg):
#        """
#        Envoi du message sur le bus, en respectant le nombre max d'envois
#        simultanés.
#        @param msg: le message à envoyer
#        """
#        if (self.producer is not None and self.max_queue_size is not None
#                and len(self.queue) >= (self.max_queue_size * 0.99) ):
#            self.producer.pauseProducing()
#        if isinstance(msg, domish.Element):
#            msg = msg.toXml().encode("utf-8")
#        self.queue.append(msg)
#        reactor.callLater(0, self.processQueue)
#
#    def processQueue(self):
#        """
#        Envoie les messages en attente, en commançant par le backup s'il en
#        contient. On respecte aussi le nombre max de messages simultanés
#        acceptés par le bus.
#
#        @note: U{http://stackoverflow.com/questions/776631/using-twisteds-twisted-web-classes-how-do-i-flush-my-outgoing-buffers}
#        """
#        if self._processing_queue:
#            return
#        # Gestion du cas déconnecté
#        if not self.isConnected():
#            return self._save_to_db()
#        # On dépile
#        self._processing_queue = True
#        d = self._processQueue()
#        next_call_delay = 0
#        def eb(f):
#            # vérouillée, il faut attendre un peu
#            f.trap(sqlite3.OperationalError)
#            next_call_delay = 0.5
#        d.addErrback(eb)
#        def cb_final(is_work_left):
#            self._processing_queue = False
#            if is_work_left:
#                reactor.callLater(next_call_delay, self.processQueue)
#        d.addBoth(cb_final)
#        return d
#
#    @defer.inlineCallbacks
#    def _processQueue(self):
#        """
#        Boucle principale de dépilement. On s'arrête si on perd la connexion.
#        N'envoie pas trop de messages à la fois pour ne pas bloquer le reactor.
#        @return: un booléen qui indique s'il reste du travail (et donc s'il
#            faut relancer la fonction)
#        @rtype:  C{bool}
#        """
#        work_left = False
#        # Pas trop de messages à la fois pour ne pas bloquer le reactor.
#        processed_max = 4096
#        processed = 0
#        while self.isConnected() and processed < processed_max:
#            msg = yield self._get_next_msg()
#            if msg is None:
#                break # rien à faire
#            work_left = True
#            # envoi
#            processed += 1
#            self._messages_forwarded += 1
#            result = self.processMessage(msg)
#            if result is None:
#                continue # pas besoin d'attendre
#            if self.max_send_simult <= 1:
#                yield result # pas d'envoi simultané
#            else:
#                self._pending_replies.append(result)
#                if len(self._pending_replies) >= self.max_send_simult:
#                    if self.max_send_simult >= 100:
#                        LOGGER.info(_('Batch sent, waiting for %d replies '
#                                      'from the bus'),
#                                    len(self._pending_replies))
#                    break # on fait une pause pour écouter les réponses
#        if self._pending_replies and processed < processed_max:
#            # on s'est arrêté pour écouter les réponses
#            yield self.waitForReplies()
#        if work_left:
#            defer.returnValue(True)
#        else:
#            defer.returnValue(False)
#
#    def _save_to_db(self):
#        """Sauvegarde tous les messages de la file dans la base de backup"""
#        if self.retry is None:
#            return
#        def eb(f):
#            LOGGER.error(_("Error trying to save a message to the backup "
#                           "database: %s"), f.getErrorMessage())
#        saved = []
#        while len(self.queue) > 0:
#            self._messages_forwarded += 1
#            msg = self.queue.popleft()
#            if isinstance(msg, domish.Element):
#                msg = msg.toXml().encode("utf-8")
#            d = self.retry.put(msg)
#            d.addErrback(eb)
#            saved.append(d)
#        return defer.DeferredList(saved)
#
#    def _get_next_msg(self):
#        """
#        Récupère le prochain message à traiter, en commençant par essayer dans
#        la base de backup
#        """
#        if self.retry is not None:
#            d = self.retry.pop()
#        else:
#            d = defer.succeed(None)
#        def get_from_queue(msg):
#            if msg is not None:
#                return msg # le backup est prioritaire
#            # rien dans le backup, on vérifie s'il faut reprendre la réception
#            if (self.producer is not None and self.max_queue_size is not None
#                    and len(self.queue) <= (self.max_queue_size * 0.10) ):
#                self.producer.resumeProducing()
#            # on dépile la file principale
#            try:
#                msg = self.queue.popleft()
#            except IndexError:
#                # plus de messages
#                return None
#            if self._process_as_domish and isinstance(msg, basestring):
#                msg = parseXml(msg)
#            return msg
#        d.addCallback(get_from_queue)
#        return d
#
#    def processMessage(self, msg):
#        """
#        Traite un message, par exemple en l'envoyant sur le bus.
#        Ne sera pas lancé plus de L{max_send_simult} fois sans attendre les
#        réponses.
#        @param msg: message à traiter
#        @type  msg: C{str} ou C{twisted.words.xish.domish.Element}
#        @return: le C{Deferred} avec la réponse, ou C{None} si cela n'a pas
#            lieu d'être (message envoyé en push)
#        """
#        raise NotImplementedError()
#
#    def waitForReplies(self):
#        """
#        Attente des réponses de la part du bus. Les réponses sont dans
#        L{_pending_replies}, et sont dépilées au fur et à mesure de leur
#        arrivée.
#
#        Note: l'implémentation n'utilise pas {defer.inlineDeferred} car on va
#        déjà faire appel à cette méthode par un C{yield} dans
#        L{processQueue}, donc on a pas le droit de I{yielder} nous-même.
#
#        @return: un Deferred qui se déclenche quand toutes les réponses sont
#            arrivées
#        @rtype:  C{Deferred}
#        """
#        d = defer.DeferredList(self._pending_replies)
#        def purge_pending(r): # pylint:disable-msg=W0613
#            del self._pending_replies[:]
#        d.addCallback(purge_pending)
#        return d
#
#    def stop(self):
#        if self.retry is not None:
#            return self.retry.flush()
#
#
#class PubSubSender(PubSubForwarder):
#    """
#    Gère des messages à destination du bus
#    """
#
#    def __init__(self, dbfilename=None, dbtable=None):
#        super(PubSubSender, self).__init__(dbfilename, dbtable)
#        self._messages_sent = 0
#        # Envoi simultanés sur le bus
#        max_send_simult = int(settings['bus'].get('max_send_simult', 1000))
#        # marge de sécurité de 20%
#        self.max_send_simult = int(max_send_simult * 0.8)
#        # accumulation des messages de perf
#        self.batch_send_perf = int(settings["bus"].get("batch_send_perf", 1))
#        self._batch_perf_queue = deque()
#        if "perf" in self._nodetopublish:
#            self._nodetopublish["perfs"] = self._nodetopublish["perf"]
#
#    def connectionInitialized(self):
#        super(PubSubSender, self).connectionInitialized()
#        self._messages_sent = 0 # c'est un COUNTER, on peut réinitialiser
#
#    def connectionLost(self, reason):
#        if self.retry is not None and self._batch_perf_queue:
#            for msg in self._batch_perf_queue:
#                self.retry.put(msg)
#        super(PubSubSender, self).connectionLost(reason)
#
#    def getStats(self):
#        """Récupère des métriques de fonctionnement du connecteur"""
#        d = super(PubSubSender, self).getStats()
#        def add_messages_sent(stats):
#            stats["sent"] = self._messages_sent
#            return stats
#        d.addCallback(add_messages_sent)
#        return d
#
#    def isConnected(self):
#        """
#        Teste si on est connecté au bus
#        """
#        return self._initialized
#
#    def processMessage(self, msg):
#        """
#        Traite un message en l'envoyant sur le bus.
#        Ne sera pas lancé plus de L{max_send_simult} fois sans attendre les
#        réponses.
#        @param msg: message à traiter
#        @type  msg: C{str} ou C{twisted.words.xish.domish.Element}
#        @return: le C{Deferred} avec la réponse, ou C{None} si cela n'a pas
#            lieu d'être (message envoyé en push)
#        """
#        self._messages_sent += 1
#        if isinstance(msg, basestring):
#            msg = parseXml(msg)
#        if msg.name == MESSAGEONETOONE:
#            # pas de réponse du bus pour ce type de messages (push)
#            return self.sendOneToOneXml(msg)
#        # accumulation des messages de perf
#        msg = self._accumulate_perf_msgs(msg)
#        if msg is None:
#            return None
#        result = self.publishXml(msg)
#        return result
#
#    def _accumulate_perf_msgs(self, msg):
#        if self.batch_send_perf <= 1 or msg.name != "perf":
#            return msg # on est pas concerné
#        self._batch_perf_queue.append(msg)
#        if len(self._batch_perf_queue) < self.batch_send_perf:
#            return None
#        batch_msg = domish.Element((NS_PERF, "perfs"))
#        for msg in self._batch_perf_queue:
#            batch_msg.addChild(msg)
#        self._batch_perf_queue.clear()
#        #LOGGER.info("Sent a batch perf message with %d messages",
#        #            self.batch_send_perf)
#        return batch_msg
#
#    def sendOneToOneXml(self, xml):
#        """
#        Envoi d'un message à un utilisateur particulier.
#        @note: il n'y a pas de réponse du bus à attendre, donc pas de
#            C{Deferred} retourné
#        @param xml: le message a envoyer sous forme XML
#        @type xml: twisted.words.xish.domish.Element
#        """
#        # Préparation du message
#        msg = domish.Element((None, "message"))
#        msg["to"] = xml['to']
#        msg["from"] = self.parent.jid.userhostJID().full()
#        msg["type"] = 'chat'
#        msg.addElement("body", content=xml.firstChildElement())
#        # Tentative d'envoi du message
#        try:
#            self.xmlstream.send(msg)
#        except AttributeError:
#            self._send_failed(
#                Failure(XMPPNotConnectedError()),
#                xml.toXml().encode('utf8')
#            )
#        # Suppression de l'objet contenant le
#        # message pour limiter l'occupation mémoire.
#        finally:
#            del msg
#
#    def publishXml(self, xml):
#        """
#        function to publish a XML msg to node
#        @param xml: le message à envoyer sous forme XML
#        @type xml: twisted.words.xish.domish.Element
#        """
#        if xml.name not in self._nodetopublish:
#            LOGGER.error(_("No destination node configured for messages "
#                           "of type '%s'. Skipping."), xml.name)
#            return defer.succeed(True)
#        node = self._nodetopublish[xml.name]
#        item = Item(payload=xml)
#        try:
#            result = self.publish(self._service, node, [item])
#        except AttributeError:
#            result = defer.fail(XMPPNotConnectedError())
#        finally:
#            del item
#        result.addErrback(self._send_failed, xml.toXml().encode('utf8'))
#        return result
#
#
#class PubSubListener(PubSubForwarder):
#    """
#    Gère des messages en provenance du bus
#    """
#    # pylint:disable-msg=W0223
#
#    def connectionInitialized(self):
#        super(PubSubListener, self).connectionInitialized()
#        # Réceptionner les messages directs ("one-to-one")
#        self.xmlstream.addObserver("/message[@type='chat']", self.chatReceived)
#
#    def _max_queue_size(self):
#        """
#        On ajoute 10% à la valeur par défaut, parce que c'est le gestionnaire
#        de présence qui doit s'occuper de ça en priorité : il nous rendra
#        indisponible sur le bus si la limite est atteinte, donc on met un peu
#        de marge pour éviter de perdre des messages pour rien.
#        """
#        max_queue_size = super(PubSubListener, self)._max_queue_size()
#        if max_queue_size is not None:
#            max_queue_size += 0.1 * max_queue_size
#        return max_queue_size
#
#    def chatReceived(self, msg):
#        """
#        Fonction de traitement des messages de discussion reçus.
#        @param msg: Message à traiter.
#        @type  msg: C{twisted.words.xish.domish.Element}
#        """
#        # les données dont on a besoin sont juste en dessous
#        for data in msg.body.elements():
#            #LOGGER.debug('Chat message to forward: %s',
#            #             data.toXml().encode('utf8'))
#            if data.name == "perfs":
#                for msg in data.elements():
#                    self.forwardMessage(msg)
#            else:
#                self.forwardMessage(data)
#
#    def itemsReceived(self, event):
#        """
#        Fonction de traitement des événements XMPP reçus.
#        @param event: Événement XMPP à traiter.
#        @type  event: C{twisted.words.xish.domish.Element}
#        """
#        for item in event.items:
#            if item.name != 'item':
#                # The alternative is 'retract', which we silently ignore
#                # We receive retractations in FIFO order,
#                # ejabberd keeps 10 items before retracting old items.
#                continue
#            for data in item.elements():
#                #LOGGER.debug('Published message to forward: %s' %
#                #             data.toXml().encode('utf8'))
#                if data.name == "perfs":
#                    for msg in data.elements():
#                        self.forwardMessage(msg)
#                else:
#                    self.forwardMessage(data)