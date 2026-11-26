"""Проверки общего VPN без сетевых соединений."""
import os
from unittest import TestCase
from unittest.mock import patch, MagicMock
from common.ssh_tunnel import tunnel_settings_from_env
from common import auto_http, http_fallback, ssh_tunnel

ENV = {'VPN_SSH_HOST': 'example.org', 'VPN_SSH_USER': 'vpnuser',
       'VPN_SSH_PORT': '2222', 'VPN_SSH_KEY': '/example/key',
       'VPN_SSH_KNOWN_HOSTS': '/example/known_hosts'}

class SharedVpnTests(TestCase):
    def test_config(self):
        with patch.dict(os.environ, ENV, clear=True):
            cfg = tunnel_settings_from_env('dynamic')
            self.assertEqual((cfg.host, cfg.user, cfg.port, cfg.key),
                             ('example.org', 'vpnuser', 2222, '/example/key'))
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, 'VPN_SSH_HOST'):
                tunnel_settings_from_env('dynamic')

    def test_forward(self):
        with patch.dict(os.environ, dict(ENV, VPN_REMOTE_PROXY_PORT='1080',
                                          VPN_REMOTE_PROXY_SCHEME='http'), clear=True):
            cfg = tunnel_settings_from_env('forward')
            self.assertEqual((cfg.remote_proxy_port, cfg.proxy_scheme), (1080, 'http'))
            self.assertEqual(tunnel_settings_from_env('dynamic').proxy_scheme, 'socks5h')

    def test_ueex(self):
        from processes.ueex import network
        from processes.ueex.config import load_settings
        with patch.dict(os.environ, ENV, clear=True), patch.object(network, 'SSHTunnel') as tunnel:
            settings = load_settings('/nonexistent/.env')
            tunnel.return_value.__enter__.return_value = 'socks5h://127.0.0.1:41001'
            with network.network_settings(settings) as current:
                self.assertEqual(current.http.proxy_url, 'socks5h://127.0.0.1:41001')
            self.assertEqual(tunnel.call_args.args[0].host, 'example.org')
            tunnel.return_value.__exit__.assert_called_once()
        with patch.dict(os.environ, dict(ENV, UEEX_TUNNEL_MODE='none'), clear=True):
            with self.assertRaisesRegex(ValueError, 'UEEX_PROXY_URL'):
                with network.network_settings(settings):
                    pass

    def test_belaes(self):
        with patch.dict(os.environ, dict(ENV, BELAES_NETWORK_MODE='vpn'), clear=True), \
             patch.object(ssh_tunnel, 'SSHTunnel') as tunnel, \
             patch.object(http_fallback, '_request_text', return_value='html') as request:
            tunnel.return_value.__enter__.return_value = 'socks5h://127.0.0.1:41002'
            self.assertEqual(http_fallback.fetch_text_with_fallback('https://example.org', 'BELAES'), 'html')
            self.assertEqual(tunnel.call_args.args[0].key, '/example/key')
            self.assertEqual(request.call_args.args[2], 'socks5h://127.0.0.1:41002')
            tunnel.return_value.__exit__.assert_called_once()

    def test_tskhinval(self):
        from processes.tskhinval.process import TskhinvalProcess
        with patch.dict(os.environ, dict(ENV, TSKHINVAL_NETWORK_MODE='vpn'), clear=True), \
             patch.object(ssh_tunnel, 'SSHTunnel') as tunnel, \
             patch.object(auto_http, 'HttpClient') as http:
            tunnel.return_value.__enter__.return_value = 'socks5h://127.0.0.1:41003'
            with TskhinvalProcess().http_client() as client:
                client.get('https://example.org')
                self.assertIsNone(client._direct)
            self.assertEqual(tunnel.call_args.args[0].host, 'example.org')
            tunnel.return_value.__exit__.assert_called_once()
