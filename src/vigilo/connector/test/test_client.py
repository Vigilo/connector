# -*- coding: utf-8 -*-
# pylint: disable-msg=W0212,R0903,R0904,C0111,W0613
# Copyright (C) 2006-2012 CS-SI
# License: GNU GPL v2 <http://www.gnu.org/licenses/gpl-2.0.html>

import unittest

# ATTENTION: ne pas utiliser twisted.trial, car nose va ignorer les erreurs
# produites par ce module !!!
#from twisted.trial import unittest
from nose.twistedtools import reactor, deferred

import mock
from twisted.internet import protocol, tcp, defer
from configobj import ConfigObj

from vigilo.connector.client import MultipleServerMixin
#from vigilo.connector.client import MultipleServersXmlStreamFactory
from vigilo.connector.client import client_factory, oneshotclient_factory
from vigilo.connector.client import VigiloClient

class MultipleServerConnector(MultipleServerMixin, tcp.Connector):
    pass

class MSCTestCase(unittest.TestCase):
    """Teste L{MultipleServerConnector}"""

    def setUp(self):
        self.rcf = protocol.ReconnectingClientFactory()

    def tearDown(self):
        self.rcf.stopTrying()

    def test_pickServer_first(self):
        c = MultipleServerConnector(None, None, None, 30, None,
                                    reactor=reactor)
        c.setMultipleParams([("test1", 5222), ("test2", 5222)], tcp.Connector)
        c.pickServer()
        self.assertEqual(c.host, "test1")

    def test_change_host(self):
        # reconnexion manuelle
        self.rcf.stopTrying()
        c = MultipleServerConnector(None, None, self.rcf, 30, None,
                                    reactor=reactor)
        c.setMultipleParams([("test1", 5222), ("test2", 5222)], tcp.Connector)

        for attemptsLeft in range(3, 0, -1):
            self.assertEqual(c._attemptsLeft, attemptsLeft)
            c.connect()
            c.connectionFailed(None)
            self.rcf.stopTrying()
            self.assertEqual(c.host, "test1")

        self.assertEqual(c._attemptsLeft, 3)
        c.connect()
        c.connectionFailed(None)
        self.rcf.stopTrying()
        self.assertEqual(c.host, "test2")



class VCTestCase(unittest.TestCase):

    def setUp(self):
        self.settings = ConfigObj()
        self.settings["bus"] = {
                "user": "test",
                "password": "test",
                }

    #@mock.patch("twisted.internet.reactor.stop")
    #@mock.patch("twisted.internet.reactor.run")
    @mock.patch("twisted.internet.reactor.connectTCP")
    #def test_host_no_port(self, mockedConnectTCP, mockedRun, mockedStop):
    def test_host_no_port(self, mockedConnectTCP):
        self.settings["bus"]["host"] = "testhost"
        vc = client_factory(self.settings)
        vc._getConnection()
        self.assertEqual(mockedConnectTCP.call_count, 1)
        self.assertEqual(mockedConnectTCP.call_args[0][:2], ("testhost", 5670))

    #@mock.patch("twisted.internet.reactor.stop")
    #@mock.patch("twisted.internet.reactor.run")
    @mock.patch("twisted.internet.reactor.connectTCP")
    #def test_host_and_port(self, mockedConnectTCP, mockedRun, mockedStop):
    def test_host_and_port(self, mockedConnectTCP):
        self.settings["bus"]["host"] = "testhost:5333"
        vc = client_factory(self.settings)
        vc._getConnection()
        self.assertEqual(mockedConnectTCP.call_count, 1)
        self.assertEqual(mockedConnectTCP.call_args[0][:2], ("testhost", 5333))



class OSCTestCase(unittest.TestCase):
    """
    Teste les méthodes de connexion en fonction de la configuration fournie
    """

    def setUp(self):
        self.settings = ConfigObj()
        self.settings["bus"] = {
                "user": "test",
                "password": "test",
                }
        self.settings["connector"] = {
                "lock_file": "/nonexistant",
                }

    @mock.patch("twisted.internet.reactor.stop")
    @mock.patch("twisted.internet.reactor.run")
    @mock.patch("twisted.internet.reactor.connectTCP")
    def test_host_no_port(self, mockedConnectTCP, mockedRun, mockedStop):
        self.settings["bus"]["host"] = "testhost"
        osc = oneshotclient_factory(self.settings)
        osc.create_lockfile = mock.Mock()
        osc.create_lockfile.return_value = False
        osc.run()
        self.assertEqual(mockedConnectTCP.call_count, 1)
        self.assertEqual(mockedConnectTCP.call_args[0][:2], ("testhost", 5670))

    @mock.patch("twisted.internet.reactor.stop")
    @mock.patch("twisted.internet.reactor.run")
    @mock.patch("twisted.internet.reactor.connectTCP")
    def test_host_and_port(self, mockedConnectTCP, mockedRun, mockedStop):
        self.settings["bus"]["host"] = "testhost:5333"
        osc = oneshotclient_factory(self.settings)
        osc.create_lockfile = mock.Mock()
        osc.create_lockfile.return_value = False
        osc.run()
        self.assertEqual(mockedConnectTCP.call_count, 1)
        self.assertEqual(mockedConnectTCP.call_args[0][:2], ("testhost", 5333))



class VigiloClientTestCase(unittest.TestCase):


    @deferred(timeout=30)
    def test_send(self):
        c = VigiloClient(None, None, None)
        c.channel = mock.Mock()
        c.channel.basic_publish.side_effect = \
                lambda *a, **kw: defer.succeed(None)
        d = c.send("exch", "key", "msg")
        def check(r):
            self.assertTrue(c.channel.basic_publish.called)
            #print c.channel.basic_publish.call_args_list
            args = c.channel.basic_publish.call_args_list[0][1]
            print args
            self.assertTrue("delivery-mode" in args["content"].properties)
            self.assertEqual(args["content"].properties["delivery-mode"], 2)
            self.assertEqual(args["content"].body, "msg")
            self.assertEqual(args["routing_key"], "key")
            self.assertEqual(args["exchange"], "exch")
            self.assertEqual(args["immediate"], False)
        d.addCallback(check)
        return d


    @deferred(timeout=30)
    def test_send_non_persistent(self):
        c = VigiloClient(None, None, None)
        c.channel = mock.Mock()
        c.channel.basic_publish.side_effect = \
                lambda *a, **kw: defer.succeed(None)
        d = c.send("exch", "key", "msg", persistent=False)
        def check(r):
            self.assertTrue(c.channel.basic_publish.called)
            #print c.channel.basic_publish.call_args_list
            args = c.channel.basic_publish.call_args_list[0][1]
            print args
            self.assertTrue("delivery-mode" in args["content"].properties)
            self.assertEqual(args["content"].properties["delivery-mode"], 1)
            self.assertEqual(args["immediate"], True)
        d.addCallback(check)
        return d
