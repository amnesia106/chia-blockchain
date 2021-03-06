import asyncio
import time
import math
import aiosqlite
import traceback
from random import Random
from src.types.peer_info import PeerInfo, TimestampedPeerInfo
from src.util.path import path_from_root, mkdir
from src.server.outbound_message import (
    Delivery,
    OutboundMessage,
    Message,
    NodeType,
)
from src.server.address_manager import ExtendedPeerInfo, AddressManager
from src.server.address_manager_store import AddressManagerStore
from src.protocols import (
    introducer_protocol,
    full_node_protocol,
)
from secrets import randbits
from src.util.hash import std_hash
from typing import Dict, Optional, AsyncGenerator
from src.util.ints import uint64

OutboundMessageGenerator = AsyncGenerator[OutboundMessage, None]


class FullNodeDiscovery:
    def __init__(
        self,
        server,
        root_path,
        global_connections,
        target_outbound_count,
        peer_db_path,
        introducer_info,
        peer_connect_interval,
        log,
    ):
        self.server = server
        assert self.server is not None
        self.message_queue = asyncio.Queue()
        self.is_closed = False
        self.global_connections = global_connections
        self.target_outbound_count = target_outbound_count
        self.peer_db_path = path_from_root(root_path, peer_db_path)
        self.introducer_info = PeerInfo(
            introducer_info["host"],
            introducer_info["port"],
        )
        self.peer_connect_interval = peer_connect_interval
        self.log = log
        self.relay_queue = None

    async def initialize_address_manager(self):
        mkdir(self.peer_db_path.parent)
        self.connection = await aiosqlite.connect(self.peer_db_path)
        self.address_manager_store = await AddressManagerStore.create(self.connection)
        if not await self.address_manager_store.is_empty():
            self.address_manager = await self.address_manager_store.deserialize()
        else:
            await self.address_manager_store.clear()
            self.address_manager = AddressManager()

    async def start_tasks(self):
        self.process_messages_task = asyncio.create_task(self._process_messages())
        random = Random()
        self.connect_peers_task = asyncio.create_task(self._connect_to_peers(random))
        self.serialize_task = asyncio.create_task(self._periodically_serialize(random))

    async def _close_common(self):
        self.is_closed = True
        self.connect_peers_task.cancel()
        self.process_messages_task.cancel()
        self.serialize_task.cancel()
        await self.connection.close()

    def add_message(self, message, data):
        self.message_queue.put_nowait((message, data))

    async def _process_messages(self):
        connection_time_pretest: Dict = {}
        while not self.is_closed:
            try:
                message, peer_info = await self.message_queue.get()
                if peer_info is None or not peer_info.port:
                    continue
                if message == "make_tried":
                    await self.address_manager.mark_good(peer_info, True)
                    await self.address_manager.connect(peer_info)
                elif message == "mark_attempted":
                    await self.address_manager.attempt(peer_info, True)
                elif message == "mark_attempted_soft":
                    await self.address_manager.attempt(peer_info, False)
                elif message == "update_connection_time":
                    if peer_info.host not in connection_time_pretest:
                        connection_time_pretest[peer_info.host] = time.time()
                    if time.time() - connection_time_pretest[peer_info.host] > 60:
                        await self.address_manager.connect(peer_info)
                        connection_time_pretest[peer_info.host] = time.time()
                elif message == "new_inbound_connection":
                    timestamped_peer_info = TimestampedPeerInfo(
                        peer_info.host,
                        peer_info.port,
                        uint64(int(time.time())),
                    )
                    await self.address_manager.add_to_new_table(
                        [timestamped_peer_info], peer_info, 0
                    )
                    await self.address_manager.mark_good(peer_info, True)
                    if self.relay_queue is not None:
                        self.relay_queue.put_nowait((timestamped_peer_info, 1))
            except Exception as e:
                self.log.error(f"Exception in process message: {e}")

    def _num_needed_peers(self) -> int:
        diff = self.target_outbound_count
        diff -= self.global_connections.count_outbound_connections()
        return diff if diff >= 0 else 0

    """
    Uses the Poisson distribution to determine the next time
    when we'll initiate a feeler connection.
    (https://en.wikipedia.org/wiki/Poisson_distribution)
    """

    def _poisson_next_send(self, now, avg_interval_seconds, random):
        return now + (
            math.log(random.randrange(1 << 48) * -0.0000000000000035527136788 + 1)
            * avg_interval_seconds
            * -1000000.0
            + 0.5
        )

    async def _introducer_client(self):
        async def on_connect() -> OutboundMessageGenerator:
            msg = Message("request_peers", introducer_protocol.RequestPeers())
            yield OutboundMessage(NodeType.INTRODUCER, msg, Delivery.RESPOND)

        await self.server.start_client(self.introducer_info, on_connect)
        # If we are still connected to introducer, disconnect
        for connection in self.global_connections.get_connections():
            if connection.connection_type == NodeType.INTRODUCER:
                self.global_connections.close(connection)

    async def _connect_to_peers(self, random):
        next_feeler = self._poisson_next_send(time.time() * 1000 * 1000, 240, random)
        empty_tables = False
        local_peerinfo: Optional[
            PeerInfo
        ] = await self.global_connections.get_local_peerinfo()
        last_timestamp_local_info: uint64 = uint64(int(time.time()))
        while not self.is_closed:
            # We don't know any address, connect to the introducer to get some.
            size = await self.address_manager.size()
            if size == 0 or empty_tables:
                await self._introducer_client()
                await asyncio.sleep(min(10, self.peer_connect_interval))
                empty_tables = False
                continue

            # Only connect out to one peer per network group (/16 for IPv4).
            groups = []
            connected = self.global_connections.get_full_node_peerinfos()
            for conn in self.global_connections.get_outbound_connections():
                peer = conn.get_peer_info()
                group = peer.get_group()
                if group not in groups:
                    groups.append(group)

            # Feeler Connections
            #
            # Design goals:
            # * Increase the number of connectable addresses in the tried table.
            #
            # Method:
            # * Choose a random address from new and attempt to connect to it if we can connect
            # successfully it is added to tried.
            # * Start attempting feeler connections only after node finishes making outbound
            # connections.
            # * Only make a feeler connection once every few minutes.

            is_feeler = False
            has_collision = False
            if self._num_needed_peers() == 0:
                if time.time() * 1000 * 1000 > next_feeler:
                    next_feeler = self._poisson_next_send(
                        time.time() * 1000 * 1000, 240, random
                    )
                    is_feeler = True

            await self.address_manager.resolve_tried_collisions()
            tries = 0
            now = time.time()
            got_peer = False
            addr: Optional[PeerInfo] = None
            max_tries = 50
            if len(groups) < 3:
                max_tries = 10
            elif len(groups) <= 5:
                max_tries = 25
            while not got_peer and not self.is_closed:
                sleep_interval = min(15, self.peer_connect_interval)
                sleep_interval = min(sleep_interval, 1 + len(groups) * 3)
                await asyncio.sleep(sleep_interval)
                tries += 1
                if tries > max_tries:
                    addr = None
                    empty_tables = True
                    break
                info: Optional[
                    ExtendedPeerInfo
                ] = await self.address_manager.select_tried_collision()
                if info is None:
                    info = await self.address_manager.select_peer(is_feeler)
                else:
                    has_collision = True
                if info is None:
                    if not is_feeler:
                        empty_tables = True
                    break
                # Require outbound connections, other than feelers, to be to distinct network groups.
                addr = info.peer_info
                if not is_feeler and addr.get_group() in groups:
                    addr = None
                    continue
                if addr in connected:
                    addr = None
                    continue
                # only consider very recently tried nodes after 30 failed attempts
                if now - info.last_try < 3600 and tries < 30:
                    continue
                if (
                    time.time() - last_timestamp_local_info > 1800
                    or local_peerinfo is None
                ):
                    local_peerinfo = await self.global_connections.get_local_peerinfo()
                    last_timestamp_local_info = uint64(int(time.time()))
                if local_peerinfo is not None and addr == local_peerinfo:
                    continue
                got_peer = True

            disconnect_after_handshake = is_feeler
            if self._num_needed_peers() == 0:
                disconnect_after_handshake = True
                empty_tables = False
            initiate_connection = (
                self._num_needed_peers() > 0 or has_collision or is_feeler
            )
            if addr is not None and initiate_connection:
                asyncio.create_task(
                    self.server.start_client(
                        addr, None, None, disconnect_after_handshake
                    )
                )
            sleep_interval = 5 + len(groups) * 5
            sleep_interval = min(sleep_interval, self.peer_connect_interval)
            await asyncio.sleep(sleep_interval)

    async def _periodically_serialize(self, random: Random):
        while not self.is_closed:
            serialize_interval = random.randint(15 * 60, 30 * 60)
            await asyncio.sleep(serialize_interval)
            async with self.address_manager.lock:
                await self.address_manager_store.serialize(self.address_manager)

    async def _respond_peers_common(self, request, peer_src, is_full_node):
        # Check if we got the peers from a full node or from the introducer.
        peers_adjusted_timestamp = []
        for peer in request.peer_list:
            if peer.timestamp < 100000000 or peer.timestamp > time.time() + 10 * 60:
                # Invalid timestamp, predefine a bad one.
                current_peer = TimestampedPeerInfo(
                    peer.host,
                    peer.port,
                    uint64(int(time.time() - 5 * 24 * 60 * 60)),
                )
            else:
                current_peer = peer
            if not is_full_node:
                current_peer = TimestampedPeerInfo(
                    peer.host,
                    peer.port,
                    uint64(0),
                )
            peers_adjusted_timestamp.append(current_peer)

        if is_full_node:
            await self.address_manager.add_to_new_table(
                peers_adjusted_timestamp, peer_src, 2 * 60 * 60
            )
        else:
            await self.address_manager.add_to_new_table(
                peers_adjusted_timestamp, None, 0
            )


class FullNodePeers(FullNodeDiscovery):
    def __init__(
        self,
        server,
        root_path,
        global_connections,
        max_inbound_count,
        target_outbound_count,
        peer_db_path,
        introducer_info,
        peer_connect_interval,
        log,
    ):
        super().__init__(
            server,
            root_path,
            global_connections,
            target_outbound_count,
            peer_db_path,
            introducer_info,
            peer_connect_interval,
            log,
        )
        self.global_connections.max_inbound_count = max_inbound_count
        self.relay_queue = asyncio.Queue()
        self.lock = asyncio.Lock()
        self.neighbour_known_peers = {}
        self.key = randbits(256)

    async def start(self):
        await self.initialize_address_manager()
        self.global_connections.set_full_node_peers_callback(self.add_message)
        self.self_advertise_task = asyncio.create_task(
            self._periodically_self_advertise()
        )
        self.address_relay_task = asyncio.create_task(self._address_relay())
        await self.start_tasks()

    async def close(self):
        await self._close_common()
        self.self_advertise_task.cancel()
        self.address_relay_task.cancel()

    async def _periodically_self_advertise(self):
        while not self.is_closed:
            try:
                await asyncio.sleep(24 * 3600)
                # Clean up known nodes for neighbours every 24 hours.
                async with self.lock:
                    for neighbour in list(self.neighbour_known_peers.keys()):
                        self.neighbour_known_peers[neighbour].clear()
                # Self advertise every 24 hours.
                peer = await self.global_connections.get_local_peerinfo()
                if peer is None:
                    continue
                timestamped_peer = [
                    TimestampedPeerInfo(
                        peer.host,
                        peer.port,
                        uint64(int(time.time())),
                    )
                ]
                outbound_message = OutboundMessage(
                    NodeType.FULL_NODE,
                    Message(
                        "respond_peers_full_node",
                        full_node_protocol.RespondPeers(timestamped_peer),
                    ),
                    Delivery.BROADCAST,
                )
                if self.server is not None:
                    self.server.push_message(outbound_message)
            except Exception as e:
                self.log.error(f"Exception in self advertise: {e}")
                self.log.error(f"Traceback: {traceback.format_exc()}")

    async def add_peers_neighbour(self, peers, neighbour_info):
        neighbour_data = (neighbour_info.host, neighbour_info.port)
        async with self.lock:
            for peer in peers:
                if neighbour_data not in self.neighbour_known_peers:
                    self.neighbour_known_peers[neighbour_data] = set()
                if peer.host not in self.neighbour_known_peers[neighbour_data]:
                    self.neighbour_known_peers[neighbour_data].add(peer.host)

    async def request_peers(self, peer_info):
        try:
            conns = self.global_connections.get_outbound_connections()
            is_outbound = False
            for conn in conns:
                conn_peer_info = conn.get_peer_info()
                if conn_peer_info == peer_info:
                    is_outbound = True
                    break

            # Prevent a fingerprint attack: do not send peers to inbound connections.
            # This asymmetric behavior for inbound and outbound connections was introduced
            # to prevent a fingerprinting attack: an attacker can send specific fake addresses
            # to users' AddrMan and later request them by sending getaddr messages.
            # Making nodes which are behind NAT and can only make outgoing connections ignore
            # the request_peers message mitigates the attack.
            if is_outbound:
                return
            peers = await self.address_manager.get_peers()
            await self.add_peers_neighbour(peers, peer_info)
            outbound_message = OutboundMessage(
                NodeType.FULL_NODE,
                Message(
                    "respond_peers_full_node",
                    full_node_protocol.RespondPeers(peers),
                ),
                Delivery.RESPOND,
            )
            yield outbound_message
            outbound_message2 = OutboundMessage(
                NodeType.WALLET,
                Message(
                    "respond_peers_full_node",
                    full_node_protocol.RespondPeers(peers),
                ),
                Delivery.RESPOND,
            )
            yield outbound_message2
        except Exception as e:
            self.log.error(f"Request peers exception: {e}")

    async def respond_peers(self, request, peer_src, is_full_node):
        await self._respond_peers_common(request, peer_src, is_full_node)
        if is_full_node:
            await self.add_peers_neighbour(request.peer_list, peer_src)
            if len(request.peer_list) == 1 and self.relay_queue is not None:
                peer = request.peer_list[0]
                if peer.timestamp > time.time() - 60 * 10:
                    self.relay_queue.put_nowait((peer, 2))

    async def _address_relay(self):
        while not self.is_closed:
            try:
                relay_peer, num_peers = await self.relay_queue.get()
                # https://en.bitcoin.it/wiki/Satoshi_Client_Node_Discovery#Address_Relay
                connections = self.global_connections.get_full_node_connections()
                hashes = []
                cur_day = int(time.time()) // (24 * 60 * 60)
                for connection in connections:
                    peer_info = connection.get_peer_info()
                    cur_hash = int.from_bytes(
                        bytes(
                            std_hash(
                                self.key.to_bytes(32, byteorder="big")
                                + peer_info.get_key()
                                + cur_day.to_bytes(3, byteorder="big")
                            )
                        ),
                        byteorder="big",
                    )
                    hashes.append((cur_hash, connection))
                hashes.sort(key=lambda x: x[0])
                for index, (_, connection) in enumerate(hashes):
                    if index >= num_peers:
                        break
                    peer_info = connection.get_peer_info()
                    pair = (peer_info.host, peer_info.port)
                    async with self.lock:
                        if (
                            pair in self.neighbour_known_peers
                            and relay_peer.host in self.neighbour_known_peers[pair]
                        ):
                            continue
                        if pair not in self.neighbour_known_peers:
                            self.neighbour_known_peers[pair] = set()
                        self.neighbour_known_peers[pair].add(relay_peer.host)
                    if connection.node_id is None:
                        continue
                    msg = OutboundMessage(
                        NodeType.FULL_NODE,
                        Message(
                            "respond_peers_full_node",
                            full_node_protocol.RespondPeers([relay_peer]),
                        ),
                        Delivery.SPECIFIC,
                        connection.node_id,
                    )
                    self.server.push_message(msg)
            except Exception as e:
                self.log.error(f"Exception in address relay: {e}")
                self.log.error(f"Traceback: {traceback.format_exc()}")


class WalletPeers(FullNodeDiscovery):
    def __init__(
        self,
        server,
        root_path,
        global_connections,
        target_outbound_count,
        peer_db_path,
        introducer_info,
        peer_connect_interval,
        log,
    ):
        super().__init__(
            server,
            root_path,
            global_connections,
            target_outbound_count,
            peer_db_path,
            introducer_info,
            peer_connect_interval,
            log,
        )

    async def start(self):
        await self.initialize_address_manager()
        self.global_connections.set_wallet_callback(self.add_message)
        await self.start_tasks()

    async def ensure_is_closed(self):
        if self.is_closed:
            return
        await self._close_common()

    async def respond_peers(self, request, peer_src, is_full_node):
        await self._respond_peers_common(request, peer_src, is_full_node)
