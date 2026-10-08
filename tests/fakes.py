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
from roborock.data import HomeData, HomeDataDevice, HomeDataProduct, Reference, RoborockCategory, RRiot, UserData
from roborock.protocol import create_mqtt_decoder, create_mqtt_encoder, md5hex
from roborock.roborock_message import RoborockMessage, RoborockMessageProtocol

EMAIL_CODE = "123456"
DUID = "fake-duid"
LOCAL_KEY = "fake_localkey_16b"
USER = RRiot(u="user123", s="pass123", h="unknown123", k="qiCNieZa", r=Reference(r="US", a="", l="", m=""))


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
    async def read_varint(reader):
        value, shift = 0, 0
        while True:
            byte = (await reader.readexactly(1))[0]
            value |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return value
            shift += 7

    @staticmethod
    def varint(value):
        out = bytearray()
        while True:
            byte, value = value & 0x7F, value >> 7
            out.append(byte | (0x80 if value else 0))
            if not value:
                return bytes(out)

    @staticmethod
    def skip_properties(body, at):
        length, shift = 0, 0
        while True:
            byte = body[at]
            at += 1
            length |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return at + length
            shift += 7

    @staticmethod
    def matches(pattern, topic):
        want, got = pattern.split("/"), topic.split("/")
        for i, part in enumerate(want):
            if part == "#":
                return True
            if i >= len(got) or part not in ("+", got[i]):
                return False
        return len(want) == len(got)

    async def handle(self, reader, writer):
        self.subscribers[writer] = []
        try:
            while True:
                first = (await reader.readexactly(1))[0]
                body = await reader.readexactly(await self.read_varint(reader))
                kind = first >> 4
                if kind == 1:  # CONNECT
                    writer.write(bytes([0x20, 3, 0, 0, 0]))  # accepted, no properties
                elif kind == 8:  # SUBSCRIBE
                    at = self.skip_properties(body, 2)
                    codes = bytearray()
                    while at < len(body):
                        size = int.from_bytes(body[at:at + 2], "big")
                        self.subscribers[writer].append(body[at + 2:at + 2 + size].decode())
                        at += 2 + size + 1
                        codes.append(0)
                    writer.write(bytes([0x90]) + self.varint(3 + len(codes)) + body[:2] + b"\x00" + bytes(codes))
                elif kind == 10:  # UNSUBSCRIBE
                    at = self.skip_properties(body, 2)
                    codes = bytearray()
                    while at < len(body):
                        size = int.from_bytes(body[at:at + 2], "big")
                        topic = body[at + 2:at + 2 + size].decode()
                        if topic in self.subscribers[writer]:
                            self.subscribers[writer].remove(topic)
                        at += 2 + size
                        codes.append(0)
                    writer.write(bytes([0xB0]) + self.varint(3 + len(codes)) + body[:2] + b"\x00" + bytes(codes))
                elif kind == 3:  # PUBLISH
                    size = int.from_bytes(body[:2], "big")
                    topic = body[2:2 + size].decode()
                    at = 2 + size
                    qos = (first >> 1) & 3
                    if qos:
                        writer.write(bytes([0x40, 2]) + body[at:at + 2])
                        at += 2
                    payload = body[self.skip_properties(body, at):]
                    out = topic.encode()
                    packet = len(out).to_bytes(2, "big") + out + b"\x00" + payload
                    for other, filters in self.subscribers.items():
                        if any(self.matches(f, topic) for f in filters):
                            other.write(bytes([0x30]) + self.varint(len(packet)) + packet)
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
        self.unknown_requests = []

    async def start(self):
        app = web.Application()
        app.router.add_post("/api/v1/sendEmailCode", self.send_code)
        app.router.add_post("/api/v1/loginWithCode", self.login)
        app.router.add_get("/api/v1/getHomeDetail", self.home_detail)
        for path in ("/user/homes/{id}", "/v2/user/homes/{id}", "/v3/user/homes/{id}"):
            app.router.add_get(path, self.homes)
        app.router.add_route("*", "/{tail:.*}", self.unknown)
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
        rriot = RRiot(u=USER.u, s=USER.s, h=USER.h, k=USER.k,
                      r=Reference(r="US", a=self.url, l=self.url, m=f"tcp://127.0.0.1:{self.mqtt_port}"))
        user = UserData(uid=1, tokentype="token_type", token="abc123", rruid="abc123", region="us",
                        countrycode="1", country="US", nickname="tester", rriot=rriot)
        return web.json_response({"code": 200, "msg": "success", "data": user.as_dict()})

    async def home_detail(self, request):
        return web.json_response({"code": 200, "msg": "success", "data": {"rrHomeId": 1, "id": 1, "name": "Home"}})

    async def homes(self, request):
        product = HomeDataProduct(id="product-1", name="Roborock Vacuum", model="roborock.vacuum.fake",
                                  category=RoborockCategory.VACUUM)
        device = HomeDataDevice(duid=DUID, name="Fakey", local_key=LOCAL_KEY, product_id=product.id,
                                sn="FAKE-SERIAL", pv="1.0")
        home = HomeData(id=1, name="Home", devices=[device], products=[product])
        return web.json_response({"api": None, "code": 200, "success": True, "status": "ok",
                                  "result": home.as_dict()})

    async def unknown(self, request):
        self.unknown_requests.append(f"{request.method} {request.path_qs}")
        return web.json_response({"code": 404, "msg": "unknown", "data": None}, status=404)


def fresh_state():
    return {
        "state": 8, "battery": 87, "error_code": 0, "in_cleaning": 0, "dock_error_status": 0,
        "clean_time": 100, "clean_area": 1000, "volume": 30, "lock_status": 0, "led": 1,
        "dnd": [22, 0, 7, 0], "msg_ver": 2, "fan_power": 102, "water_box_mode": 200,
    }


@dataclass
class Vacuum:
    """A robot that answers Roborock's encrypted MQTT requests and keeps simple state."""

    broker_port: int
    state: dict = field(default_factory=fresh_state)
    requests: list = field(default_factory=list)  # (method, params) of every request, in order

    def user_topic(self):
        hashed = md5hex(USER.u + ":" + USER.k)[2:10]
        return f"{USER.u}/{hashed}/{DUID}"

    async def start(self):
        self.task = asyncio.create_task(self.run())
        await asyncio.sleep(0)

    async def stop(self):
        self.task.cancel()

    async def run(self):
        decode, encode = create_mqtt_decoder(LOCAL_KEY), create_mqtt_encoder(LOCAL_KEY)
        async with aiomqtt.Client("127.0.0.1", self.broker_port, protocol=aiomqtt.ProtocolVersion.V5) as client:
            await client.subscribe(f"rr/m/i/{self.user_topic()}")
            async for message in client.messages:
                for incoming in decode(message.payload):
                    request = json.loads(json.loads(incoming.payload)["dps"]["101"])
                    self.requests.append((request["method"], request.get("params")))
                    reply = self.answer(request["method"], request.get("params"))
                    inner = {"id": request["id"], **reply}
                    payload = {"dps": {"102": json.dumps(inner)}, "t": int(time.time())}
                    outgoing = RoborockMessage(protocol=RoborockMessageProtocol.RPC_RESPONSE,
                                               payload=json.dumps(payload).encode(), version=b"1.0",
                                               seq=incoming.seq, timestamp=int(time.time()))
                    await client.publish(f"rr/m/o/{self.user_topic()}", encode(outgoing))

    def answer(self, method, params):
        s = self.state
        match method:
            case "get_status":
                return {"result": [{k: s[k] for k in ("msg_ver", "state", "battery", "error_code", "in_cleaning",
                                                       "dock_error_status", "clean_time", "clean_area", "lock_status",
                                                       "fan_power", "water_box_mode")}]}
            case "get_network_info":
                return {"result": {"ip": "127.0.0.1", "ssid": "fake-wifi", "mac": "aa:bb:cc:dd:ee:ff",
                                   "bssid": "aa:bb:cc:dd:ee:ff", "rssi": -50}}
            case "app_get_init_status":
                return {"result": [{"local_info": {"location": "us", "bom": "A.03.0069", "featureset": 1,
                                                    "language": "en", "name": "fake"},
                                    "feature_info": [111, 112], "new_feature_info": 0,
                                    "new_feature_info_str": "0000000000002000", "new_feature_info_2": 8192}]}
            case "get_sound_volume":
                return {"result": [s["volume"]]}
            case "change_sound_volume":
                s["volume"] = params[0]
                return {"result": ["ok"]}
            case "get_child_lock_status":
                return {"result": [{"lock_status": s["lock_status"]}]}
            case "set_child_lock_status":
                s["lock_status"] = params["lock_status"]
                return {"result": ["ok"]}
            case "get_led_status":
                return {"result": [s["led"]]}
            case "set_led_status":
                s["led"] = params[0]
                return {"result": ["ok"]}
            case "get_dnd_timer":
                return {"result": [{"start_hour": s["dnd"][0], "start_minute": s["dnd"][1],
                                    "end_hour": s["dnd"][2], "end_minute": s["dnd"][3], "enabled": 1}]}
            case "set_dnd_timer":
                s["dnd"] = params
                return {"result": ["ok"]}
            case "get_room_mapping":
                return {"result": [[1, "100", 1], [2, "200", 15]]}
            case "find_me" | "app_charge" | "app_stop" | "app_pause" | "app_start" | "app_segment_clean" \
                    | "resume_segment_clean" | "resume_zoned_clean" | "app_set_dryer_status" | "resolve_error":
                self.act(method, params)
                return {"result": ["ok"]}
        return {"result": "unknown_method"}  # what real robots answer for a method they lack

    def act(self, method, params):
        s = self.state
        match method:
            case "app_start" | "app_segment_clean" | "resume_segment_clean" | "resume_zoned_clean":
                s.update(state=5, error_code=0)
            case "app_pause":
                s["state"] = 10
            case "app_stop":
                s["state"] = 3
            case "app_charge":
                s["state"] = 8
            case "resolve_error":
                s["error_code"] = 0


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
