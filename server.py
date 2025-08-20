import asyncio
import json
import math
import random
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
import subprocess

def get_ip_mac_mapping():
    """Return a dict of IP -> MAC for devices connected via wlan0 only."""
    output = subprocess.check_output("arp -n", shell=True).decode()
    mapping = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 5 and ':' in parts[2] and parts[-1] == "wlan0":
            ip = parts[0]
            mac = parts[2]
            mapping[ip] = mac
    return mapping



app = FastAPI()

app.mount("/static", StaticFiles(directory="static"), name="static")

PLAYER_SIZE = 15
WORLD_WIDTH = 4000
WORLD_HEIGHT = 3000
BULLET_SPEED = 30
BULLET_RADIUS = 4
ASTEROID_COUNT = 50
ASTEROID_MIN_SIZE = 15
ASTEROID_MAX_SIZE = 45
WIN_SCORE = 10


players = {}
bullets = {}
asteroids = {}
connections = {}

def clamp(value, minv, maxv):
    return max(minv, min(value, maxv))

def bounce(p1, p2):
    dx = p1["x"] - p2["x"]
    dy = p1["y"] - p2["y"]
    dist = math.hypot(dx, dy)
    if dist == 0:
        return
    overlap = PLAYER_SIZE * 2 - dist
    if overlap > 0:
        nx = dx / dist
        ny = dy / dist

        p1["x"] += nx * overlap / 2
        p1["y"] += ny * overlap / 2
        p2["x"] -= nx * overlap / 2
        p2["y"] -= ny * overlap / 2

        v1n = p1["vx"] * nx + p1["vy"] * ny
        v2n = p2["vx"] * nx + p2["vy"] * ny
        p1["vx"] += (v2n - v1n) * nx
        p1["vy"] += (v2n - v1n) * ny
        p2["vx"] += (v1n - v2n) * nx
        p2["vy"] += (v1n - v2n) * ny

def bounce_asteroid(p, ast):
    dx = p["x"] - ast["x"]
    dy = p["y"] - ast["y"]
    dist = math.hypot(dx, dy)
    if dist == 0:
        return
    overlap = PLAYER_SIZE + ast["size"] - dist
    if overlap > 0:
        nx = dx / dist
        ny = dy / dist
        p["x"] += nx * overlap
        p["y"] += ny * overlap

        dot = p["vx"] * nx + p["vy"] * ny
        p["vx"] -= 2 * dot * nx
        p["vy"] -= 2 * dot * ny

def bounce_bullet_asteroid(b, ast):
    dx = b["x"] - ast["x"]
    dy = b["y"] - ast["y"]
    dist = math.hypot(dx, dy)
    if dist == 0:
        return False
    return dist < BULLET_RADIUS + ast["size"]

def create_asteroids():
    asts = {}
    for _ in range(ASTEROID_COUNT):
        aid = str(uuid.uuid4())
        asts[aid] = {
            "id": aid,
            "x": random.uniform(0, WORLD_WIDTH),
            "y": random.uniform(0, WORLD_HEIGHT),
            "size": random.uniform(ASTEROID_MIN_SIZE, ASTEROID_MAX_SIZE),
        }
    return asts

class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        cid = str(uuid.uuid4())
        self.active_connections[cid] = websocket
        print(f"New connection: {cid}")
        return cid

    def disconnect(self, cid: str):
        if cid in self.active_connections:
            print(f"Disconnected: {cid}")
            del self.active_connections[cid]

    async def send_personal_message(self, message: str, cid: str):
        if cid in self.active_connections:
            await self.active_connections[cid].send_text(message)

    async def broadcast(self, message: str):
        to_remove = []
        for cid, connection in self.active_connections.items():
            try:
                await connection.send_text(message)
            except Exception as e:
                print(f"Failed to send to {cid}: {e}")
                to_remove.append(cid)
        for cid in to_remove:
            self.disconnect(cid)

manager = ConnectionManager()

async def game_loop():
    print("Game loop started")
    while True:
        for p in players.values():
            if not p["alive"]:
                if "respawn_time" in p:
                    if asyncio.get_event_loop().time() >= p["respawn_time"]:
                        p["alive"] = True
                        p["x"] = random.uniform(PLAYER_SIZE, WORLD_WIDTH - PLAYER_SIZE)
                        p["y"] = random.uniform(PLAYER_SIZE, WORLD_HEIGHT - PLAYER_SIZE)
                        p["vx"] = 0
                        p["vy"] = 0
                        p["angle"] = 0
                        del p["respawn_time"]
                continue

            if p["turning_left"]:
                p["angle"] -= 0.12
            if p["turning_right"]:
                p["angle"] += 0.12
            if p["moving_forward"]:
                p["vx"] += math.cos(p["angle"]) * 0.5
                p["vy"] += math.sin(p["angle"]) * 0.5

            p["vx"] *= 0.95
            p["vy"] *= 0.95

            p["x"] += p["vx"]
            p["y"] += p["vy"]

            p["x"] = clamp(p["x"], PLAYER_SIZE, WORLD_WIDTH - PLAYER_SIZE)
            p["y"] = clamp(p["y"], PLAYER_SIZE, WORLD_HEIGHT - PLAYER_SIZE)

        plist = list(players.values())
        for i in range(len(plist)):
            for j in range(i + 1, len(plist)):
                if plist[i]["alive"] and plist[j]["alive"]:
                    bounce(plist[i], plist[j])

        for p in players.values():
            if not p["alive"]:
                continue
            for ast in asteroids.values():
                bounce_asteroid(p, ast)

        to_remove = []
        for bid, b in bullets.items():
            b["x"] += b["vx"]
            b["y"] += b["vy"]

            if b["x"] < 0 or b["x"] > WORLD_WIDTH or b["y"] < 0 or b["y"] > WORLD_HEIGHT:
                to_remove.append(bid)
                continue

            hit_asteroid = False
            for ast in asteroids.values():
                if bounce_bullet_asteroid(b, ast):
                    to_remove.append(bid)
                    hit_asteroid = True
                    break
            if hit_asteroid:
                continue

            for pid, p in players.items():
                if p["alive"] and pid != b["shooter"]:
                    dx = b["x"] - p["x"]
                    dy = b["y"] - p["y"]
                    dist = math.hypot(dx, dy)
                    if dist < PLAYER_SIZE + 10:
                        p["alive"] = False
                        p["respawn_time"] = asyncio.get_event_loop().time() + 10
                        to_remove.append(bid)
                        if b["shooter"] in players:
                            players[b["shooter"]]["score"] += 1
                        break

        for bid in to_remove:
            if bid in bullets:
                del bullets[bid]

        await asyncio.sleep(1 / 30)

def reset_game():
    global players, bullets

    # Reset all players
    for p in players.values():
        p['score'] = 0
        p['x'] = WORLD_WIDTH // 2
        p['y'] = WORLD_HEIGHT // 2
        p['vx'] = 0
        p['vy'] = 0
        p['angle'] = 0
        p['alive'] = True
        p['respawn_timer'] = 0

    # Clear bullets
    bullets.clear()


async def broadcast_loop():
    print("Broadcast loop started")
    while True:
        try:
            # Check for winner
            for player in players.values():
                if player["score"] >= WIN_SCORE:
                    # Announce winner
                    await manager.broadcast(json.dumps({
                        "type": "game_over",
                        "winner": player["username"]
                    }))
                    reset_game()
                    break

            state = {
                "type": "update",
                "players": players,
                "bullets": bullets,
                "asteroids": asteroids,
                "player_size": PLAYER_SIZE,
                "world_width": WORLD_WIDTH,
                "world_height": WORLD_HEIGHT
            }
            await manager.broadcast(json.dumps(state))
            await asyncio.sleep(1 / 30)
        except Exception as e:
            print("Broadcast error:", e)

@app.get("/")
async def get_index():
    print("Serving index.html")
    return FileResponse(Path("static") / "index.html")

@app.websocket("/ws")
async def websocket_handler(websocket: WebSocket):
    connection_id = await manager.connect(websocket)
    player_id = str(uuid.uuid4())
    print(f"Player connected with id {player_id}")

    try:
        data = await websocket.receive_text()
        init_data = json.loads(data)
        username = init_data.get("username", "Anonymous")        
        # 1. Capture client IP
        client_ip = websocket.client.host


        # 2. Get MAC from IP→MAC mapping on wlan0
        ip_mac_map = get_ip_mac_mapping()
        player_mac = ip_mac_map.get(client_ip)

        print(f"Username: {username} Mac: {player_mac} Ip: {client_ip}")

        players[player_id] = {
            "id": player_id,
            "username": username,
            "mac": player_mac,   # store MAC here
            "color": f"hsl({random.randint(0,360)}, 70%, 60%)",
            "x": random.uniform(PLAYER_SIZE, WORLD_WIDTH - PLAYER_SIZE),
            "y": random.uniform(PLAYER_SIZE, WORLD_HEIGHT - PLAYER_SIZE),
            "vx": 0,
            "vy": 0,
            "angle": 0,
            "alive": True,
            "score": 0,
            "moving_forward": False,
            "turning_left": False,
            "turning_right": False,
        }

        await manager.send_personal_message(json.dumps({
            "type": "init",
            "id": player_id,
            "players": players,
            "bullets": bullets,
            "asteroids": asteroids,
            "player_size": PLAYER_SIZE,
            "world_width": WORLD_WIDTH,
            "world_height": WORLD_HEIGHT,
        }), connection_id)

        await manager.broadcast(json.dumps({
            "type": "new_player",
            "player": players[player_id]
        }))

        while True:
            text = await websocket.receive_text()
            msg = json.loads(text)

            if msg["type"] == "move":
                p = players.get(player_id)
                if p and p["alive"]:
                    p["x"] = clamp(msg.get("x", p["x"]), PLAYER_SIZE, WORLD_WIDTH - PLAYER_SIZE)
                    p["y"] = clamp(msg.get("y", p["y"]), PLAYER_SIZE, WORLD_HEIGHT - PLAYER_SIZE)
                    p["vx"] = msg.get("vx", p["vx"])
                    p["vy"] = msg.get("vy", p["vy"])
                    p["angle"] = msg.get("angle", p["angle"])
                    p["moving_forward"] = msg.get("moving_forward", False)
                    p["turning_left"] = msg.get("turning_left", False)
                    p["turning_right"] = msg.get("turning_right", False)
            elif msg["type"] == "chat":
                # Broadcast the chat message to all connected clients
                chat_message = {
                    "type": "chat",
                    "user": players[player_id]["username"],
                    "text": msg.get("text", "")
                }
                await manager.broadcast(json.dumps(chat_message))



            elif msg["type"] == "ban":
                username_to_ban = msg["username"]
                
                # Look up MAC
                player_mac = None
                for p in players.values():
                    if p["username"] == username_to_ban:
                        player_mac = p.get("mac")
                        break

                print(f"Ban requested for Username: {username_to_ban}, MAC: {player_mac}")

                if player_mac:
                    try:
                        # Ban in FORWARD chain
                        subprocess.run(
                            ["sudo", "iptables", "-I", "FORWARD", "-m", "mac", "--mac-source", player_mac, "-j", "DROP"],
                            check=True
                        )
                        # Ban in INPUT chain
                        subprocess.run(
                            ["sudo", "iptables", "-I", "INPUT", "-m", "mac", "--mac-source", player_mac, "-j", "DROP"],
                            check=True
                        )
                        print(f"{username_to_ban} with MAC {player_mac} has been banned at the firewall level.")
                    except subprocess.CalledProcessError as e:
                        print(f"Failed to ban {username_to_ban}: {e}")
                else:
                    print(f"MAC address not found for {username_to_ban}")




            elif msg["type"] == "unban":
                username_to_unban = msg["username"]
                player_mac = next((p.get("mac") for p in players.values() if p["username"] == username_to_unban), None)
                print(f"Unban requested for Username: {username_to_unban}, MAC: {player_mac}")
                if player_mac:
                    # Get rule numbers dynamically
                    input_rules = subprocess.check_output(["sudo", "iptables", "-L", "INPUT", "--line-numbers"], text=True)
                    forward_rules = subprocess.check_output(["sudo", "iptables", "-L", "FORWARD", "--line-numbers"], text=True)

                    for chain, rules in [("INPUT", input_rules), ("FORWARD", forward_rules)]:
                        for line in rules.splitlines():
                            if player_mac.lower() in line.lower():
                                rule_num = line.split()[0]
                                subprocess.run(["sudo", "iptables", "-D", chain, rule_num])
                                print(f"Removed {chain} rule {rule_num} for {player_mac}")
                                break  # Remove only the first matching rule


            elif msg["type"] == "shoot":
                p = players.get(player_id)
                if p and p["alive"]:
                    angle = p["angle"]
                    bx = p["x"] + math.cos(angle) * (PLAYER_SIZE + BULLET_RADIUS + 2)
                    by = p["y"] + math.sin(angle) * (PLAYER_SIZE + BULLET_RADIUS + 2)
                    bvx = math.cos(angle) * BULLET_SPEED + p["vx"]
                    bvy = math.sin(angle) * BULLET_SPEED + p["vy"]

                    bullet_id = str(uuid.uuid4())
                    bullets[bullet_id] = {
                        "id": bullet_id,
                        "x": bx,
                        "y": by,
                        "vx": bvx,
                        "vy": bvy,
                        "shooter": player_id
                    }
    except WebSocketDisconnect:
        print(f"Player {player_id} disconnected")
        manager.disconnect(connection_id)
        if player_id in players:
            del players[player_id]
        await manager.broadcast(json.dumps({
            "type": "player_left",
            "id": player_id
        }))
    except Exception as e:
        print(f"Websocket error: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    global asteroids
    print("Initializing game")
    asteroids = create_asteroids()
    asyncio.create_task(game_loop())
    asyncio.create_task(broadcast_loop())
    yield

app.router.lifespan_context = lifespan

if __name__ == "__main__":
    import uvicorn
    print("Starting server on http://")
    uvicorn.run("server:app", host="0.0.0.0", reload=True)
