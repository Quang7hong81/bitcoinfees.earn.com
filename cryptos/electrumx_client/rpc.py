#!/usr/bin/env python3
#
# Copyright (c) 2016, Neil Booth
#
# All rights reserved.
#
# See the file "LICENCE" for information about the copyright
# and warranty status of this software.

'''Script to send RPC commands to a running ElectrumX server.'''

#https://github.com/kyuupichan/electrumx/blob/master/docs/PROTOCOL.rst

import asyncio
import threading
from queue import Queue
import json
import random
import os
import ssl
from .connection import TCPConnection
from ..utils import user_dir
from .. import constants
from functools import partial
from datetime import datetime, timedelta

from .jsonrpc import JSONSession, JSONRPCv2

class RPCResponseExecption(Exception):
    pass

class RPCClient(JSONSession):

    def __init__(self):
        super().__init__(version=JSONRPCv2)
        self.max_send = 0
        self.max_buffer_size = 5*10**6
        self.result = {}
        self.queue = Queue

    async def wait_for_response(self, id_):
        await self.items_events[id_].wait()
        await self.process_pending_items()
        self.items_events[id_].clear()
        del self.items_events[id_]
        return self.result.pop(id_)

    def send_rpc_request(self, method, params, callback=None):
        callback = callback or self.handle_response
        handler = partial(callback, method, params)
        return self.send_request(handler, method, params)

    def handle_response(self, method, params, id_, data, error):
        self.result[id_] = {'data': data, 'error': error, 'method': method, 'params': params}


def read_json(path, default):
    if not os.path.isabs(path):
        path = os.path.join(os.path.dirname(__file__), 'servers', path)
    try:
        with open(path, 'r') as f:
            r = json.loads(f.read())
    except:
        r = default
    return r


class ElectrumXClient:

    def __init__(self, server_file="bitcoin.json", servers=(), host=None, port=50002, use_ssl=True, timeout=15,
                 max_servers=5, protocol_version=(constants.PROTOCOL_VERSION, constants.PROTOCOL_VERSION),
                 client_name=constants.CLIENT_NAME, loop=None, config_path=None):
        self.use_ssl = use_ssl
        self.cache = {'fees': {}}
        self.client_name = client_name
        self.protocol_version = protocol_version
        self.config_path = config_path or user_dir(self.client_name)
        if loop:
            self.loop = loop
        else:
            self.loop = asyncio.get_event_loop()
        self.timeout = timeout
        self.listen_thread = threading.Thread(target=self.run_listen_loop)
        self.listen_loop = None
        self.failed_hosts = []
        self.max_servers = max_servers
        if not servers:
            servers = read_json(server_file, {})
        self.servers = {host: servers[host] for host in servers.keys() if self.server_is_usable(servers[host])}
        self.host = host
        self.port = port
        self.rpc_client = None
        if not self.host:
            self.host, self.port = self.choose_random_server()
        self.connect_to_server()

    def server_is_usable(self, server):
        if self.use_ssl and not 's' in server.keys():
            return False
        elif not self.use_ssl and not 't' in server.keys():
            return False
        return server.get('usable', True)

    def run_listen_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def create_listen_thread(self):
        if not self.listen_thread.is_alive():
            self.listen_thread.start()

    def choose_random_server(self):
        host = random.choice(list(self.servers.keys()))
        try:
            if self.use_ssl:
                return host, self.servers[host]['s']
            return host, self.servers[host]['t']
        except KeyError:
            del self.servers[host]
            return self.choose_random_server()

    def connect_to_server(self):
        print(self.host, self.port)
        try:
            transport, self.rpc_client = TCPConnection(RPCClient, self.host, self.port, self.use_ssl, self.loop,
                                                       self.config_path).create_connection()
            if not transport or not self.rpc_client:
                self.change_server()
            self.rpc_client.connection_made(transport)
            try:
                result = self.server_version()
                self.server_electrumx_version = result[0]
                self.server_protocol_version = result[1]
            except RPCResponseExecption:
                self.change_server()
        except (OSError, ssl.SSLError):
            self.change_server()

    def change_server(self):
        if self.transport:
            self.rpc_client.close_connection()
            self.transport = None
        if self.host not in self.failed_hosts:
            self.failed_hosts.append(self.host)
        if len(self.failed_hosts) >= self.max_servers:
            raise Exception("Attempted to connect to %s servers but failed" % len(self.failed_hosts))
        while self.host in self.failed_hosts:
            self.host, self.port = self.choose_random_server()
        self.connect_to_server()

    def get_coroutines(self, requests):
        coroutines = []
        for request in requests:
            method, params = request
            try:
                id_ = self.rpc_client.send_rpc_request(method, params)
                try:
                    coro = self.rpc_client.wait_for_response(id_)
                    coroutines.append(asyncio.wait_for(coro, self.timeout))
                except asyncio.TimeoutError:
                    self.change_server()
                    return self.rpc_multiple_send_and_wait(requests)
            except OSError:
                self.change_server()
                return self.rpc_multiple_send_and_wait(requests)
        return coroutines

    def rpc_multiple_send_and_wait(self, requests):
        coroutines = self.get_coroutines(requests)
        values = self.loop.run_until_complete(asyncio.gather(*coroutines))
        self.failed_hosts = []
        return values

    def _block_header(self, *heights):
        method = 'blockchain.block.get_header'
        return [(method, (height,)) for height in heights]

    def block_header(self, *heights):
        requests = self._block_header(*heights)
        return self.rpc_multiple_send_and_wait(requests)

    def _get_merkle(self, *txs):
        method = 'blockchain.transaction.get_merkle'
        return [(method, (tx['tx_hash'], tx['height'])) for tx in txs]

    def get_merkle(self, *txs):
        requests = self._get_merkle(*txs)
        return self.rpc_multiple_send_and_wait(requests)

    def get_all_merkle_data(self, *txs):
        block_header_requests = self._block_header(*[tx['height'] for tx in txs])
        get_merkle_requests = self._get_merkle(*txs)
        results = self.rpc_multiple_send_and_wait(block_header_requests + get_merkle_requests)
        merkles = []
        for result in results:
            if 'merkle' in result.keys():
                block_header = next(r for r in results if r['height'] == results['block_height'])
                result['merkle_root'] = block_header['merkle_root']
                merkles.append(result)
        return merkles

    def run_command(self, request):
        request = [request]
        result = self.rpc_multiple_send_and_wait(request)[0]
        if result['error']:
            raise RPCResponseExecption(result['error'])
        return result['data']

    def _estimate_fee(self, numblocks):
        return 'blockchain.estimatefee', (numblocks,)

    def estimate_fee(self, numblocks):
        return self.run_command(self._estimate_fee(numblocks))

    def estimate_fee_cached(self, numblocks, cache=10):
        now = datetime.now()
        if numblocks in self.cache['fees'] and self.cache['fees'][numblocks]['expiry'] <= now:
            return self.cache['fees'][numblocks]['fee']
        fee = self.estimate_fee(numblocks)
        interval = timedelta(minutes=cache)
        self.cache['fees'][numblocks] = {'fee': fee, 'expiry': now + interval}
        return fee

    def _relay_fee(self):
        return 'blockchain.relayfee', ()

    def relay_fee(self):
        return self.run_command(self._relay_fee())

    def _broadcast_transaction(self, raw_tx):
        return 'blockchain.transaction.broadcast', (raw_tx,)

    def broadcast_transaction(self, raw_tx):
        return self.run_command(self.broadcast_transaction(raw_tx))

    def _server_donation_address(self):
        return 'server.donation_address', ()

    def server_donation_address(self):
        return self.run_command(self._server_donation_address())

    def _server_banner(self):
        return 'server.banner', ()

    def server_banner(self):
        return self.run_command(self._server_banner())

    def _server_version(self, protocol_version=None, client_name=None):
        return 'server.version', (client_name or self.client_name, protocol_version or self.protocol_version)

    def server_version(self, protocol_version=None, client_name=None):
        return self.run_command(self._server_version(protocol_version=protocol_version, client_name=client_name))

    def _server_features(self):
        return 'server.features', ()

    def server_features(self):
        return self.run_command(self.server_features())

    def _subscribe_to_peers(self):
        return 'server.peers.subscribe', ()

    def subcribe_to_peers(self):
        return self.run_command(self.subcribe_to_peers())

    def _get_balance(self, scripthash):
        return "blockchain.scripthash.get_balance", (scripthash,)

    def get_balance(self, addrs_scripthashes):
        requests = [self._get_balance(scripthash) for scripthash in addrs_scripthashes.keys()]
        results = self.rpc_multiple_send_and_wait(requests)
        balances = []
        for result in results:
            if result['error']:
                raise RPCResponseExecption(result['error'])
            b = result['data']
            scripthash = result['params'][0]
            addr = addrs_scripthashes[scripthash]
            b['address'] = addr
            b['total'] = b['confirmed'] + b['unconfirmed']
            balances.append(b)
        return balances

    def _get_unspent(self, scripthash):
        return "blockchain.scripthash.listunspent", (scripthash,)

    def unspent(self, addrs_scripthashes):
        requests = [self._get_unspent(scripthash) for scripthash in addrs_scripthashes.keys()]
        results = self.rpc_multiple_send_and_wait(requests)
        unspents = []
        for i, result in enumerate(results):
            if result['error']:
                raise RPCResponseExecption(result['error'])
            unspent_for_addr = result['data']
            scripthash = result['params'][0]
            addr = addrs_scripthashes[scripthash]
            for u in unspent_for_addr:
                u['address'] = addr
                unspents.append(u)
        return unspents

    def _get_mempool(self, scripthash):
        return "blockchain.scripthash.get_mempool", (scripthash,)

    def get_mempool(self, addrs_scripthashes):
        requests = [self._get_mempool(scripthash) for scripthash in addrs_scripthashes.keys()]
        results = self.rpc_multiple_send_and_wait(requests)
        txs = []
        for i, result in enumerate(results):
            if result['error']:
                raise RPCResponseExecption(result['error'])
            unspent_for_addr = result['data']
            scripthash = result['params'][0]
            addr = addrs_scripthashes[scripthash]
            for tx in unspent_for_addr:
                tx['address'] = addr
                txs.append(tx)
        return txs

    def _get_history(self, scripthash):
        return "blockchain.scripthash.get_history", (scripthash,)

    def history(self, addrs_scripthashes):
        requests = [self._get_history(scripthash) for scripthash in addrs_scripthashes.keys()]
        results = self.rpc_multiple_send_and_wait(requests)
        txs = []
        for i, result in enumerate(results):
            if result['error']:
                raise RPCResponseExecption(result['error'])
            txs_for_addr = result['data']
            scripthash = result['params'][0]
            addr = addrs_scripthashes[scripthash]
            for tx in txs_for_addr:
                tx['address'] = addr
                txs.append(tx)
        return txs

    def _get_tx(self, tx_hash):
        return "blockchain.transaction.get", (tx_hash,)

    def get_txs(self, *tx_hashes):
        requests = [self._get_tx(tx_hash) for tx_hash in tx_hashes]
        return self.rpc_multiple_send_and_wait(requests)

    def rpc_subscribe(self, requests, callback):
        ids = []
        for request in requests:
            method, params = request
            ids.append(self.rpc_client.send_rpc_request(method, params, callback))
        return ids

    def _subscribe_to_scripthash(self, scripthash):
        return 'blockchain.scripthash.subscribe', (scripthash,)

    def subscribe_to_scripthashes(self, addrs_scripthashes, callback):
        requests = [self._subscribe_to_scripthash(scripthash) for scripthash in addrs_scripthashes.keys()]
        def handle_scripthash_notify(method, params, id_, data, error):
            if error:
                raise Exception(error)
            scripthash = data[0]
            address = addrs_scripthashes[scripthash]
            callback(address, data[1])
        return self.rpc_subscribe(requests, handle_scripthash_notify)

    def _subscribe_to_block_headers(self):
        return 'blockchain.headers.subscribe', ()

    def subscribe_to_block_headers(self, callback):
        request = self._subscribe_to_block_headers()
        def handle_block_headers_notify(method, params, id_, data, error):
            if error:
                raise Exception(error)
            callback(data)
        return self.rpc_subscribe([request], handle_block_headers_notify)