from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
import os
import json
import asyncio

from app.db import Base, engine, SessionLocal
from app.models import Question

app = FastAPI()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
Base.metadata.create_all(bind=engine)

ROOM_CODE = os.getenv("ROOM_CODE", "1234")
QUESTION_DURATION = int(os.getenv("QUESTION_DURATION", "15"))

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
ADMIN_COOKIE_NAME = "quizblast_admin_auth"

clients: list[WebSocket] = []
players: dict[str, int] = {}
answered_players: set[str] = set()
answer_counts = {"A": 0, "B": 0, "C": 0, "D": 0}

current_question_index = 0
quiz_started = False
question_open = False
auto_task = None


def is_admin_authenticated(request: Request) -> bool:
    token = request.cookies.get(ADMIN_COOKIE_NAME)
    return token == "ok"


def require_admin(request: Request):
    if not is_admin_authenticated(request):
        return RedirectResponse(url="/admin-login", status_code=303)
    return None


def db_get_questions():
    db = SessionLocal()
    try:
        rows = db.query(Question).order_by(Question.id.asc()).all()
        return rows
    finally:
        db.close()


def get_question_count() -> int:
    return len(db_get_questions())


def get_current_question():
    rows = db_get_questions()
    if not rows:
        return {
            "question_index": 0,
            "question": "Henüz soru yok",
            "options": ["-", "-", "-", "-"]
        }

    safe_index = min(current_question_index, len(rows) - 1)
    q = rows[safe_index]

    return {
        "id": q.id,
        "question_index": safe_index + 1,
        "question": q.question,
        "options": [q.option_a, q.option_b, q.option_c, q.option_d]
    }


def get_correct_letter():
    rows = db_get_questions()
    if not rows:
        return "A"

    safe_index = min(current_question_index, len(rows) - 1)
    q = rows[safe_index]
    return ["A", "B", "C", "D"][q.correct]


async def broadcast(payload: dict):
    dead = []
    for ws in clients:
        try:
            await ws.send_text(json.dumps(payload))
        except Exception:
            dead.append(ws)

    for ws in dead:
        if ws in clients:
            clients.remove(ws)


async def broadcast_leaderboard():
    await broadcast({
        "type": "leaderboard",
        "players": players
    })


async def broadcast_question():
    await broadcast({
        "type": "question",
        "data": get_current_question(),
        "duration": QUESTION_DURATION
    })


async def broadcast_answer_stats():
    await broadcast({
        "type": "answer_stats",
        "counts": answer_counts,
        "correct_answer": get_correct_letter()
    })


async def close_question():
    global question_open
    question_open = False

    await broadcast({
        "type": "question_closed",
        "correct_answer": get_correct_letter()
    })

    await broadcast_answer_stats()


async def auto_close_question():
    await asyncio.sleep(QUESTION_DURATION)
    if question_open:
        await close_question()


def reset_answer_state():
    global answered_players, answer_counts
    answered_players = set()
    answer_counts = {"A": 0, "B": 0, "C": 0, "D": 0}


@app.get("/")
def root():
    return FileResponse(os.path.join(BASE_DIR, "static/index.html"))


@app.get("/player")
def player():
    return FileResponse(os.path.join(BASE_DIR, "static/player.html"))


@app.get("/host")
def host():
    return FileResponse(os.path.join(BASE_DIR, "static/host.html"))


@app.get("/admin-login")
def admin_login_page():
    return FileResponse(os.path.join(BASE_DIR, "static/admin_login.html"))


@app.get("/admin")
def admin(request: Request):
    auth_redirect = require_admin(request)
    if auth_redirect:
        return auth_redirect

    return FileResponse(os.path.join(BASE_DIR, "static/admin.html"))


@app.get("/health")
def health():
    return {"ok": True}


@app.head("/")
def root_head():
    return {}


@app.get("/api/config")
def api_config():
    return {
        "room_code": ROOM_CODE,
        "question_duration": QUESTION_DURATION,
    }


@app.post("/api/admin-login")
async def api_admin_login(request: Request):
    body = await request.json()

    username = str(body.get("username", "")).strip()
    password = str(body.get("password", "")).strip()

    if username != ADMIN_USERNAME or password != ADMIN_PASSWORD:
        return JSONResponse(
            {"ok": False, "message": "Kullanıcı adı veya şifre hatalı"},
            status_code=401
        )

    response = JSONResponse({"ok": True})
    response.set_cookie(
        key=ADMIN_COOKIE_NAME,
        value="ok",
        httponly=True,
        samesite="lax",
        secure=False
    )
    return response


@app.post("/api/admin-logout")
def api_admin_logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(ADMIN_COOKIE_NAME)
    return response


@app.get("/api/questions")
def api_list_questions(request: Request):
    if not is_admin_authenticated(request):
        return JSONResponse({"ok": False, "message": "Yetkisiz erişim"}, status_code=401)

    rows = db_get_questions()
    return {
        "items": [
            {
                "id": q.id,
                "question": q.question,
                "options": [q.option_a, q.option_b, q.option_c, q.option_d],
                "correct": q.correct
            }
            for q in rows
        ]
    }


@app.post("/api/questions")
async def api_add_question(request: Request):
    if not is_admin_authenticated(request):
        return JSONResponse({"ok": False, "message": "Yetkisiz erişim"}, status_code=401)

    body = await request.json()

    question = str(body.get("question", "")).strip()
    options = body.get("options", [])
    correct = body.get("correct", None)

    if not question:
        return JSONResponse({"ok": False, "message": "Soru boş olamaz"}, status_code=400)

    if not isinstance(options, list) or len(options) != 4:
        return JSONResponse({"ok": False, "message": "4 seçenek gerekli"}, status_code=400)

    options = [str(x).strip() for x in options]
    if any(not x for x in options):
        return JSONResponse({"ok": False, "message": "Tüm seçenekler dolu olmalı"}, status_code=400)

    if correct not in [0, 1, 2, 3]:
        return JSONResponse({"ok": False, "message": "Doğru cevap 0-3 arası olmalı"}, status_code=400)

    db = SessionLocal()
    try:
        row = Question(
            question=question,
            option_a=options[0],
            option_b=options[1],
            option_c=options[2],
            option_d=options[3],
            correct=correct
        )
        db.add(row)
        db.commit()
        db.refresh(row)

        return {"ok": True, "id": row.id}
    finally:
        db.close()


@app.delete("/api/questions/{question_id}")
def api_delete_question(question_id: int, request: Request):
    if not is_admin_authenticated(request):
        return JSONResponse({"ok": False, "message": "Yetkisiz erişim"}, status_code=401)

    global current_question_index

    db = SessionLocal()
    try:
        row = db.query(Question).filter(Question.id == question_id).first()
        if not row:
            return JSONResponse({"ok": False, "message": "Soru bulunamadı"}, status_code=404)

        db.delete(row)
        db.commit()

        total = get_question_count()
        current_question_index = 0 if total == 0 else min(current_question_index, total - 1)

        return {"ok": True}
    finally:
        db.close()


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    global quiz_started, question_open, current_question_index, auto_task

    await websocket.accept()
    clients.append(websocket)

    try:
        await websocket.send_text(json.dumps({
            "type": "room_info",
            "room_code": ROOM_CODE,
            "quiz_started": quiz_started,
            "question_open": question_open,
            "question_duration": QUESTION_DURATION,
        }))

        await websocket.send_text(json.dumps({
            "type": "leaderboard",
            "players": players
        }))

        if quiz_started and question_open and get_question_count() > 0:
            await websocket.send_text(json.dumps({
                "type": "question",
                "data": get_current_question(),
                "duration": QUESTION_DURATION
            }))

        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            msg_type = data.get("type")

            if msg_type == "join":
                name = data.get("name", "").strip()
                room_code = data.get("room_code", "").strip()

                if room_code != ROOM_CODE:
                    await websocket.send_text(json.dumps({
                        "type": "info",
                        "message": "Oda kodu yanlış."
                    }))
                    continue

                if not name:
                    await websocket.send_text(json.dumps({
                        "type": "info",
                        "message": "İsim gerekli."
                    }))
                    continue

                if name not in players:
                    players[name] = 0

                await websocket.send_text(json.dumps({
                    "type": "join_success",
                    "name": name,
                    "room_code": ROOM_CODE
                }))

                await broadcast_leaderboard()

                if quiz_started and question_open and get_question_count() > 0:
                    await websocket.send_text(json.dumps({
                        "type": "question",
                        "data": get_current_question(),
                        "duration": QUESTION_DURATION
                    }))

            elif msg_type == "start_quiz":
                if get_question_count() == 0:
                    await websocket.send_text(json.dumps({
                        "type": "info",
                        "message": "Soru yok. Önce admin panelden soru ekleyin."
                    }))
                    continue

                quiz_started = True
                question_open = True
                current_question_index = 0
                reset_answer_state()

                await broadcast_question()

                if auto_task and not auto_task.done():
                    auto_task.cancel()
                auto_task = asyncio.create_task(auto_close_question())

            elif msg_type == "next_question":
                total = get_question_count()

                if total == 0:
                    continue

                if current_question_index < total - 1:
                    current_question_index += 1
                    question_open = True
                    reset_answer_state()

                    await broadcast_question()

                    if auto_task and not auto_task.done():
                        auto_task.cancel()
                    auto_task = asyncio.create_task(auto_close_question())
                else:
                    question_open = False
                    await broadcast({
                        "type": "quiz_finished"
                    })
                    await broadcast_leaderboard()

            elif msg_type == "restart_quiz":
                current_question_index = 0
                quiz_started = False
                question_open = False
                reset_answer_state()

                for player_name in players:
                    players[player_name] = 0

                await broadcast({
                    "type": "info",
                    "message": "Quiz sıfırlandı."
                })
                await broadcast_leaderboard()

            elif msg_type == "show_answer":
                if question_open and get_question_count() > 0:
                    await close_question()

            elif msg_type == "answer":
                if not question_open or get_question_count() == 0:
                    await websocket.send_text(json.dumps({
                        "type": "info",
                        "message": "Bu soru kapandı."
                    }))
                    continue

                player_name = data.get("name", "").strip()
                answer = data.get("answer", "").strip()

                if player_name not in players:
                    await websocket.send_text(json.dumps({
                        "type": "info",
                        "message": "Önce oyuna katıl."
                    }))
                    continue

                if player_name in answered_players:
                    await websocket.send_text(json.dumps({
                        "type": "info",
                        "message": "Bu soruya zaten cevap verdin."
                    }))
                    continue

                if answer not in ["A", "B", "C", "D"]:
                    continue

                answered_players.add(player_name)
                answer_counts[answer] += 1

                correct = get_correct_letter()
                is_correct = answer == correct

                if is_correct:
                    players[player_name] += 10

                await websocket.send_text(json.dumps({
                    "type": "answer_result",
                    "correct": is_correct,
                    "your_answer": answer,
                    "correct_answer": correct,
                    "score": players[player_name]
                }))

                await broadcast_leaderboard()

                await broadcast({
                    "type": "host_answer_info",
                    "player": player_name,
                    "answer": answer,
                    "correct": is_correct
                })

    except WebSocketDisconnect:
        if websocket in clients:
            clients.remove(websocket)