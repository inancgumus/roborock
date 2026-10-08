"""Fake Roborock servers that really listen on sockets.

The cloud speaks HTTP, the broker speaks MQTT 5 and the vacuum is an MQTT client
that answers Roborock's encrypted messages. Nothing is mocked. The real
roborock.py and the real python-roborock library talk to them over the network.
"""

import asyncio
import json
import threading
import time
from dataclasses import dataclass, field

import aiomqtt
from aiohttp import web
from paho.mqtt.client import topic_matches_sub
from paho.mqtt.properties import VariableByteIntegers
from roborock.data import (
    HomeData,
    HomeDataDevice,
    HomeDataProduct,
    Reference,
    RoborockCategory,
    RRiot,
    UserData,
)
from roborock.protocol import create_mqtt_decoder, create_mqtt_encoder, create_mqtt_params
from roborock.roborock_message import RoborockMessage, RoborockMessageProtocol

EMAIL_CODE = "123456"
DUID = "fake-duid"
LOCAL_KEY = "fake_localkey_16b"
USER = RRiot(
    u="user123",
    s="pass123",
    h="unknown123",
    k="qiCNieZa",
    r=Reference(r="US", a="", l="", m="tcp://127.0.0.1:1"),
)


class Broker:
    """A minimal MQTT 5 broker: connect, subscribe, publish and ping."""

    def __init__(self):
        self.subscribers: dict[asyncio.StreamWriter, list[str]] = {}

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self):
        self.server.close()
        for writer in list(self.subscribers):
            writer.close()
        await self.server.wait_closed()

    @staticmethod
    async def read_length(reader):
        value, shift = 0, 0
        while True:
            byte = (await reader.readexactly(1))[0]
            value |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return value
            shift += 7

    @staticmethod
    def after_properties(body, at):
        """Where the packet continues after the properties that start at `at`."""
        length, used = VariableByteIntegers.decode(body[at:])
        return at + used + length

    @staticmethod
    def topic_filters(body, option_bytes):
        """The topics in a SUBSCRIBE (one option byte each) or UNSUBSCRIBE packet."""
        at = Broker.after_properties(body, 2)
        while at < len(body):
            size = int.from_bytes(body[at : at + 2], "big")
            yield body[at + 2 : at + 2 + size].decode()
            at += 2 + size + option_bytes

    @staticmethod
    def packet(kind, content):
        return bytes([kind]) + VariableByteIntegers.encode(len(content)) + content

    @staticmethod
    def ack(kind, body, topics):
        """The answer to a SUBSCRIBE or UNSUBSCRIBE: its id, no properties, success per topic."""
        return Broker.packet(kind, body[:2] + b"\x00" + bytes(len(topics)))

    async def handle(self, reader, writer):
        self.subscribers[writer] = []
        try:
            while True:
                first = (await reader.readexactly(1))[0]
                body = await reader.readexactly(await self.read_length(reader))
                kind = first >> 4
                if kind == 1:  # CONNECT
                    writer.write(bytes([0x20, 3, 0, 0, 0]))  # accepted, no properties
                elif kind == 8:  # SUBSCRIBE
                    topics = list(self.topic_filters(body, option_bytes=1))
                    self.subscribers[writer] += topics
                    writer.write(self.ack(0x90, body, topics))
                elif kind == 10:  # UNSUBSCRIBE
                    topics = list(self.topic_filters(body, option_bytes=0))
                    keep = [topic for topic in self.subscribers[writer] if topic not in topics]
                    self.subscribers[writer] = keep
                    writer.write(self.ack(0xB0, body, topics))
                elif kind == 3:  # PUBLISH
                    size = int.from_bytes(body[:2], "big")
                    topic = body[2 : 2 + size].decode()
                    at = 2 + size
                    qos = (first >> 1) & 3
                    if qos:
                        writer.write(bytes([0x40, 2]) + body[at : at + 2])
                        at += 2
                    payload = body[self.after_properties(body, at) :]
                    forwarded = self.packet(0x30, body[: 2 + size] + b"\x00" + payload)
                    for other, filters in self.subscribers.items():
                        if any(topic_matches_sub(f, topic) for f in filters):
                            other.write(forwarded)
                elif kind == 12:  # PINGREQ
                    writer.write(bytes([0xD0, 0]))
                elif kind == 14:  # DISCONNECT
                    break
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self.subscribers.pop(writer, None)
            writer.close()


class Cloud:
    """The parts of Roborock's HTTP API that logging in and finding the vacuum use."""

    def __init__(self, mqtt_port):
        self.mqtt_port = mqtt_port
        self.codes_requested = []

    async def start(self):
        app = web.Application()
        app.router.add_post("/api/v1/sendEmailCode", self.send_code)
        app.router.add_post("/api/v1/loginWithCode", self.login)
        app.router.add_get("/api/v1/getHomeDetail", self.home_detail)
        for path in ("/user/homes/{id}", "/v2/user/homes/{id}", "/v3/user/homes/{id}"):
            app.router.add_get(path, self.homes)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"

    async def stop(self):
        await self.runner.cleanup()

    async def send_code(self, request):
        self.codes_requested.append(request.query["username"])
        return web.json_response({"code": 200, "msg": "success", "data": None})

    async def login(self, request):
        if request.query["verifycode"] != EMAIL_CODE:
            return web.json_response({"code": 2018, "msg": "invalid code", "data": None})
        where = Reference(r="US", a=self.url, l=self.url, m=f"tcp://127.0.0.1:{self.mqtt_port}")
        rriot = RRiot(u=USER.u, s=USER.s, h=USER.h, k=USER.k, r=where)
        user = UserData(
            uid=1,
            tokentype="token_type",
            token="abc123",
            rruid="abc123",
            region="us",
            countrycode="1",
            country="US",
            nickname="tester",
            rriot=rriot,
        )
        return web.json_response({"code": 200, "msg": "success", "data": user.as_dict()})

    async def home_detail(self, request):
        data = {"rrHomeId": 1, "id": 1, "name": "Home"}
        return web.json_response({"code": 200, "msg": "success", "data": data})

    async def homes(self, request):
        product = HomeDataProduct(
            id="product-1",
            name="Roborock Vacuum",
            model="roborock.vacuum.fake",
            category=RoborockCategory.VACUUM,
        )
        device = HomeDataDevice(
            duid=DUID,
            name="Fakey",
            local_key=LOCAL_KEY,
            product_id=product.id,
            sn="FAKE-SERIAL",
            pv="1.0",
        )
        home = HomeData(id=1, name="Home", devices=[device], products=[product])
        result = home.as_dict()
        reply = {"api": None, "code": 200, "success": True, "status": "ok", "result": result}
        return web.json_response(reply)


STATUS_KEYS = (
    "msg_ver",
    "state",
    "battery",
    "error_code",
    "in_cleaning",
    "dock_error_status",
    "clean_time",
    "clean_area",
    "lock_status",
    "fan_power",
    "water_box_mode",
)


def fresh_state():
    return {
        "state": 8,
        "battery": 87,
        "error_code": 0,
        "in_cleaning": 0,
        "dock_error_status": 0,
        "clean_time": 100,
        "clean_area": 1000,
        "volume": 30,
        "lock_status": 0,
        "led": 1,
        "dnd": [22, 0, 7, 0],
        "msg_ver": 2,
        "fan_power": 102,
        "water_box_mode": 200,
    }


@dataclass
class Vacuum:
    """A robot that answers Roborock's encrypted MQTT requests and keeps simple state.

    A method named `app_pause` is answered by `do_app_pause`. A method without one gets
    "unknown_method", which is what real robots answer for a method they lack.
    """

    broker_port: int
    state: dict = field(default_factory=fresh_state)
    requests: list = field(default_factory=list)  # (method, params) of every request, in order
    subscribed: asyncio.Event = field(default_factory=asyncio.Event)

    async def start(self):
        self.task = asyncio.create_task(self.run())
        await asyncio.wait_for(self.subscribed.wait(), 10)

    async def stop(self):
        self.task.cancel()

    async def run(self):
        decode, encode = create_mqtt_decoder(LOCAL_KEY), create_mqtt_encoder(LOCAL_KEY)
        topic = f"{USER.u}/{create_mqtt_params(USER).username}/{DUID}"
        protocol = aiomqtt.ProtocolVersion.V5
        async with aiomqtt.Client("127.0.0.1", self.broker_port, protocol=protocol) as client:
            await client.subscribe(f"rr/m/i/{topic}")
            self.subscribed.set()
            async for message in client.messages:
                for incoming in decode(message.payload):
                    request = json.loads(json.loads(incoming.payload)["dps"]["101"])
                    method, params = request["method"], request.get("params")
                    self.requests.append((method, params))
                    reply = {"id": request["id"], "result": self.answer(method, params)}
                    payload = {"dps": {"102": json.dumps(reply)}, "t": int(time.time())}
                    outgoing = RoborockMessage(
                        protocol=RoborockMessageProtocol.RPC_RESPONSE,
                        payload=json.dumps(payload).encode(),
                        version=b"1.0",
                        seq=incoming.seq,
                        timestamp=int(time.time()),
                    )
                    await client.publish(f"rr/m/o/{topic}", encode(outgoing))

    def answer(self, method, params):
        handler = getattr(self, f"do_{method}", None)
        return handler(params) if handler else "unknown_method"

    def update(self, **changes):
        self.state.update(changes)
        return ["ok"]

    def do_get_status(self, params):
        return [{key: self.state[key] for key in STATUS_KEYS}]

    def do_get_network_info(self, params):
        return {
            "ip": "127.0.0.1",
            "ssid": "fake-wifi",
            "mac": "aa:bb:cc:dd:ee:ff",
            "bssid": "aa:bb:cc:dd:ee:ff",
            "rssi": -50,
        }

    def do_app_get_init_status(self, params):
        local_info = {
            "location": "us",
            "bom": "A.03.0069",
            "featureset": 1,
            "language": "en",
            "name": "fake",
        }
        return [
            {
                "local_info": local_info,
                "feature_info": [111, 112],
                "new_feature_info": 0,
                "new_feature_info_str": "0000000000002000",
                "new_feature_info_2": 8192,
            }
        ]

    def do_get_sound_volume(self, params):
        return [self.state["volume"]]

    def do_change_sound_volume(self, params):
        return self.update(volume=params[0])

    def do_get_child_lock_status(self, params):
        return [{"lock_status": self.state["lock_status"]}]

    def do_set_child_lock_status(self, params):
        return self.update(lock_status=params["lock_status"])

    def do_get_led_status(self, params):
        return [self.state["led"]]

    def do_set_led_status(self, params):
        return self.update(led=params[0])

    def do_get_dnd_timer(self, params):
        start_hour, start_minute, end_hour, end_minute = self.state["dnd"]
        return [
            {
                "start_hour": start_hour,
                "start_minute": start_minute,
                "end_hour": end_hour,
                "end_minute": end_minute,
                "enabled": 1,
            }
        ]

    def do_set_dnd_timer(self, params):
        return self.update(dnd=params)

    def do_get_room_mapping(self, params):
        return [[1, "100", 1], [2, "200", 15]]

    def do_find_me(self, params):
        return self.update()

    def do_app_set_dryer_status(self, params):
        return self.update()

    def do_app_charge(self, params):
        return self.update(state=8)

    def do_app_stop(self, params):
        return self.update(state=3)

    def do_app_pause(self, params):
        return self.update(state=10)

    def do_resolve_error(self, params):
        return self.update(error_code=0)

    def do_app_start(self, params):
        return self.update(state=5, error_code=0)

    do_app_segment_clean = do_resume_segment_clean = do_resume_zoned_clean = do_app_start


class World:
    """The cloud, the broker and the vacuum, running on their own thread."""

    def start(self):
        self.ready = threading.Event()
        self.thread = threading.Thread(target=lambda: asyncio.run(self.main()), daemon=True)
        self.thread.start()
        assert self.ready.wait(20), "the fake servers did not start"
        return self

    async def main(self):
        self.stop_event = asyncio.Event()
        self.loop = asyncio.get_running_loop()
        self.broker = Broker()
        await self.broker.start()
        self.cloud = Cloud(self.broker.port)
        await self.cloud.start()
        self.vacuum = Vacuum(self.broker.port)
        await self.vacuum.start()
        self.ready.set()
        await self.stop_event.wait()
        await self.vacuum.stop()
        await self.cloud.stop()
        await self.broker.stop()

    def stop(self):
        self.loop.call_soon_threadsafe(self.stop_event.set)
        self.thread.join(10)
