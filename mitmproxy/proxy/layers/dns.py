from curses import nonl
from dataclasses import dataclass
import enum
import ipaddress
import socket
import struct
from typing import Callable, Dict, List, Tuple, Union

from mitmproxy import dns, platform
from mitmproxy import connection
from mitmproxy.proxy import commands, events, layer
from mitmproxy.proxy.context import Context
from mitmproxy.proxy.utils import expect
from mitmproxy.utils import human


@dataclass
class DnsRequestHook(commands.StartHook):
    """
    A DNS query has been received.
    """

    name = "resolve"
    flow: dns.DNSFlow


@dataclass
class DnsResponseHook(commands.StartHook):
    """
    A DNS response has been received or set.
    """

    name = "resolved"
    flow: dns.DNSFlow


@dataclass
class DnsErrorHook(commands.StartHook):
    """
    A DNS error has occurred.
    """
    flow: dns.DNSFlow


class DnsMode(enum.Enum):
    Simple = "simple"
    Forward = "forward:"
    Transparent = "transparent"


class DnsResolveError(Exception):
    def __init__(self, response_code: dns.ResponseCode):
        assert response_code is not dns.ResponseCode.NOERROR
        self.response_code = response_code


class DNSLayer(layer.Layer):
    """
    Layer that handles resolving DNS queries.
    """

    flows: Dict[int, dns.DNSFlow]
    mode: DnsMode

    def __init__(self, context: Context):
        super().__init__(context)
        self.flows = dict()

    @classmethod
    def simple_resolve(cls, questions: List[dns.Question]) -> List[dns.ResourceRecord]:
        answers = []

        def resolve_by_name(family: socket.AddressFamily, ip: Callable[[str], Union[ipaddress.IPv4Address, ipaddress.IPv6Address]]) -> None:
            nonlocal answers, question
            try:
                addrinfos = socket.getaddrinfo(host=question.name, port=0, family=family)
            except socket.gaierror as e:
                if e.errno == socket.EAI_NODATA:
                    raise DnsResolveError(dns.ResponseCode.NXDOMAIN)
                else:
                    raise DnsResolveError(dns.ResponseCode.SERVFAIL)
            for addrinfo in addrinfos:
                _, _, _, _, (addr, ) = addrinfo
                answers.append(dns.ResourceRecord(
                    name=question.name,
                    type=question.type,
                    class_=question.class_,
                    ttl=dns.ResourceRecord.DEFAULT_TTL,
                    data=ip(addr).packed,
                ))

        def resolve_by_addr(suffix: str, ip: Callable[[List[str]], Union[ipaddress.IPv4Address, ipaddress.IPv6Address]]) -> bool:
            nonlocal answers, question
            if not question.name.lower().endswith(suffix.lower()):
                return False
            try:
                addr = ip(question.name[0:-len(suffix)].split(".")[::-1])
            except ValueError:
                raise DnsResolveError(dns.ResponseCode.FORMERR)
            try:
                name, _, _ = socket.gethostbyaddr(str(addr))
            except socket.herror:
                raise DnsResolveError(dns.ResponseCode.NXDOMAIN)
            except socket.gaierror:
                raise DnsResolveError(dns.ResponseCode.SERVFAIL)
            answers.append(dns.ResourceRecord(
                name=question.name,
                type=question.type,
                class_=question.class_,
                ttl=dns.ResourceRecord.DEFAULT_TTL,
                data=dns.ResourceRecord.pack_domain_name(name),
            ))
            return True

        for question in questions:
            if question.class_ is not dns.Class.IN:
                raise DnsResolveError(dns.ResponseCode.NOTIMP)
            if question.type is dns.Type.A:
                resolve_by_name(socket.AddressFamily.AF_INET, ipaddress.IPv4Address)
            elif question.type is dns.Type.AAAA:
                resolve_by_name(socket.AddressFamily.AF_INET6, ipaddress.IPv6Address)
            elif question.type is dns.Type.PTR:
                known_family = (
                    resolve_by_addr(".in-addr.arpa", lambda x: ipaddress.IPv4Address(".".join(x)))
                    or
                    resolve_by_addr(".ip6.arpa", lambda x: ipaddress.IPv6Address(bytes.fromhex("".join(x))))
                )
                if not known_family:
                    raise DnsResolveError(dns.ResponseCode.FORMERR)
            else:
                raise DnsResolveError(dns.ResponseCode.NOTIMP)
        return answers

    def handle_request(self, msg: dns.Message) -> layer.CommandGenerator[None]:
        flow = dns.DNSFlow(self.context.client, self.context.server, True)
        flow.request = msg
        if self.mode is DnsMode.Transparent:  # get the query's original destination
            assert platform.original_addr is not None
            socket = yield commands.GetSocket(self.context.client)
            try:
                flow.server_conn = connection.Server(platform.original_addr(socket), protocol=connection.ConnectionProtocol.UDP)
            except Exception as e:
                yield commands.Log(f"Transparent DNS mode failed: {e!r}", level="warn")
                return
        yield DnsRequestHook(flow)  # give hooks a chance to produce a response
        if not flow.response:
            if self.mode is DnsMode.Simple:
                try:
                    if not msg.query:
                        raise DnsResolveError(dns.ResponseCode.REFUSED)  # we received an answer from the _client_
                    if msg.op_code is not dns.OpCode.QUERY:
                        raise DnsResolveError(dns.ResponseCode.NOTIMP)  # inverse queries and others are not supported
                    flow.response = msg.succeed(DNSLayer.simple_resolve(msg.questions))
                except DnsResolveError as e:
                    flow.response = msg.fail(e.response_code)
            else:
                if flow.server_conn.state is connection.ConnectionState.CLOSED:  # we need an upstream connection
                    err = yield commands.OpenConnection(flow.server_conn)
                    if err:
                        flow.error = str(err)
                        yield DnsErrorHook(flow)
                        return  # cannot recover from this
                self.flows[msg.id] = flow
                yield commands.SendData(flow.server_conn, msg.packed)
                return  # we need to wait for the server's response
        yield DnsResponseHook(flow)
        yield commands.SendData(self.context.client, flow.response.packed)

    def handle_response(self, msg: dns.Message, server_conn: connection.Connection) -> layer.CommandGenerator[None]:
        flow = self.flows[msg.id]
        if flow.server_conn is server_conn:
            del self.flows[msg.id]
            flow.response = msg
            yield DnsResponseHook(flow)
            yield commands.SendData(self.context.client, flow.response.packed)
            if self.mode is DnsMode.Transparent:  # always close transparent connections
                yield commands.CloseConnection(flow.server_conn)
        else:
            flow = dns.DNSFlow(self.context.client, server_conn, True)
            flow.response = msg
            flow.error = f"Received response to message {msg.id} sent to {human.format_address(flow.server_conn)}."
            yield DnsErrorHook(flow)

    @expect(events.Start)
    def start(self, _) -> layer.CommandGenerator[None]:
        mode: str = self.context.options.dns_mode
        if mode == DnsMode.Simple.name:
            self.mode = DnsMode.Simple
        elif mode == DnsMode.Transparent.name:
            self.mode = DnsMode.Transparent
        elif mode.startswith(DnsMode.Forward.name):
            self.mode = DnsMode.Forward
            addr = mode[0:-len(DnsMode.Forward.name)]
            # TODO
        else
            yield commands.Log(f"Invalid DNS '{mode}', disabling further message handling.", level="error")
            self._handle_event = self.done

    @expect(events.DataReceived, events.ConnectionClosed)
    def query(self, event: events.Event) -> layer.CommandGenerator[None]:
        assert isinstance(event, events.ConnectionEvent)

        if isinstance(event, events.DataReceived):
            from_client = event.connection == self.context.client
            try:
                msg = dns.Message.unpack(event.data)
            except struct.error as e:
                flow = dns.DNSFlow(self.context.client, self.context.server if from_client else event.connection, True)
                flow.error = str(e)
                yield DnsErrorHook(flow)
                return
            if msg.id in self.flows:
                if from_client:  # duplicate ID, remove the old flow with an error and create a new one
                    flow = self.flows[msg.id]
                    del self.flows[msg.id]
                    flow.error = f"Received duplicate request for id {msg.id}."
                    yield DnsErrorHook(flow)
                    yield from self.handle_request(msg)
                else:
                    yield from self.handle_response(msg, event.connection)
            else:
                if from_client:
                    yield from self.handle_request(msg)
                else:
                    flow = dns.DNSFlow(self.context.client, self.context.server, True)
                    flow.response = msg
                    flow.error = f"Received response for unknown message {msg.id}."
                    yield DnsErrorHook(flow)
            
        elif isinstance(event, events.ConnectionClosed):
            pass  # TODO

        else:
            raise AssertionError(f"Unexpected event: {event}")

    @expect(events.DataReceived, events.ConnectionClosed)
    def done(self, _) -> layer.CommandGenerator[None]:
        yield from ()

    _handle_event = start
