import asyncio
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest import TestCase
from processes.chinamoney import process as p

DAY = date(2026, 11, 25)
START = date(2026, 11, 16)

def response(payload):
    return SimpleNamespace(json=lambda: payload, close=lambda: None)

LATEST = {'head': {'rep_code': '200'}, 'data': {'lastDate':'2026-11-25 9:15'},
          'records':[{'vrtEName':'EUR/CNY','price':'7.3271'}, {'vrtEName':'USD/CNY','price':'6.7088'}]}
HISTORY = {'data': {'searchlist':['EUR/CNY','USD/CNY']}, 'records':[
    {'date':'2026-11-24','values':['7.3008','6.7111']},
    {'date':'2026-11-25','values':['7.3271','6.7089']}]}

class ChinaMoneyTests(TestCase):
    def test_latest(self):
        frame=p.read_latest(response(LATEST),START,DAY)
        self.assertEqual(frame.iloc[0].value,Decimal('6.7088'))
        self.assertEqual(frame.iloc[0].date,'2026-11-25')
    def test_history(self):
        frame=p.read_history(response(HISTORY),START,DAY)
        self.assertEqual(list(frame.value),[Decimal('6.7111'),Decimal('6.7089')])
    def test_invalid(self):
        for value in ('---','NaN','0','-1'):
            with self.subTest(value=value),self.assertRaises(p.SourceError): p.rate(value)
        with self.assertRaises(p.SourceError): p.read_latest(response(LATEST),START,date(2026,11,24))
        with self.assertRaises(p.SourceError): p.read_history(response({'data':{'head':['EUR/CNY']},'records':[]}),START,DAY)
    def test_collection(self):
        class Client:
            def get(self,url,**kwargs):
                return response(HISTORY if url==p.HISTORY else LATEST)
        frame, body=asyncio.run(p.ChinaMoneyProcess().collect(Client(),DAY))
        self.assertEqual(len(frame),2)
        self.assertEqual(frame.iloc[-1].value,Decimal('6.7088'))
    def test_fallback(self):
        class Client:
            def get(self,url,**kwargs):
                if url==p.HISTORY: raise p.SourceError('offline history')
                return response(LATEST)
        frame, body=asyncio.run(p.ChinaMoneyProcess().collect(Client(),DAY))
        self.assertEqual(len(frame),1)
        self.assertIn('частичная выгрузка',body)

    def test_ssh_default_and_lifecycle(self):
        from unittest.mock import patch, MagicMock
        import os
        from common import auto_http, ssh_tunnel
        settings = {
            'VPN_SSH_HOST': 'example.org', 'VPN_SSH_USER': 'test',
            'VPN_SSH_KEY': '/example/key', 'VPN_SSH_PORT': '2222',
            'VPN_SSH_KNOWN_HOSTS': '/example/known_hosts',
        }
        with patch.dict(os.environ, settings, clear=True), \
             patch.object(ssh_tunnel, 'SSHTunnel') as tunnel, \
             patch.object(auto_http, 'HttpClient') as http:
            tunnel.return_value.__enter__.return_value = 'socks5h://127.0.0.1:40000'
            http.return_value.__enter__.return_value.get.side_effect = [response(HISTORY), response(LATEST)]
            with p.ChinaMoneyProcess().http_client() as client:
                self.assertEqual(client.mode, 'proxy')
                frame, _ = asyncio.run(p.ChinaMoneyProcess().collect(client, DAY))
                self.assertEqual(len(frame), 2)
                self.assertIsNone(client._direct)
            tunnel.assert_called_once()
            config = tunnel.call_args.args[0]
            self.assertEqual((config.host, config.user, config.key, config.port, config.mode),
                             ('example.org', 'test', '/example/key', 2222, 'dynamic'))
            self.assertEqual(config.known_hosts, '/example/known_hosts')
            self.assertEqual(http.call_count, 1)
            self.assertTrue(http.call_args.args[0].proxy_enabled)
            self.assertTrue(http.call_args.args[0].verify_tls)
            self.assertEqual(http.call_args.args[0].proxy_url, 'socks5h://127.0.0.1:40000')
            tunnel.return_value.__exit__.assert_called_once()
            http.return_value.__exit__.assert_called_once()

    def test_explicit_proxy_and_direct_mode(self):
        import os
        from unittest.mock import patch
        from common import auto_http, ssh_tunnel
        for mode in ('proxy', 'direct'):
            with self.subTest(mode=mode), patch.dict(os.environ, {
                'CHINAMONEY_NETWORK_MODE': mode,
                'CHINAMONEY_PROXY_URL': 'socks5h://example.org:1080',
            }, clear=True), patch.object(ssh_tunnel, 'SSHTunnel') as tunnel, \
                    patch.object(auto_http, 'HttpClient') as http:
                http.return_value.__enter__.return_value.get.return_value = response(LATEST)
                with p.ChinaMoneyProcess().http_client() as client:
                    client.get(p.LATEST)
                tunnel.assert_not_called()
                self.assertEqual(http.call_args.args[0].proxy_enabled, mode == 'proxy')
                self.assertEqual(http.call_args.args[0].proxy_url,
                                 'socks5h://example.org:1080' if mode == 'proxy' else '')
