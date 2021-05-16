import asyncio
import io
import json
import logging
import secrets
import traceback
import typing as t
from datetime import datetime
from functools import partial

import aioredis
from PIL import Image
from asyncpg import Connection
from fastapi import Cookie, FastAPI, HTTPException, Request, Response
from fastapi.security.utils import get_authorization_scheme_param
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from httpx import AsyncClient
from itsdangerous import URLSafeSerializer
from jose import JWTError, jwt
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import RedirectResponse

from pixels import constants
from pixels.canvas import Canvas
from pixels.models import AuthResult, AuthState, Pixel, User
from pixels.utils import docs, ratelimits

log = logging.getLogger(__name__)

app = FastAPI(
    title="Pixels API",
    description=docs.get_doc("overview"),
    version="0.0.1",
    docs_url=None,
    redoc_url=None,
)
app.mount("/static", StaticFiles(directory="pixels/static"), name="static")

templates = Jinja2Templates(directory="pixels/templates")

auth_s = URLSafeSerializer(secrets.token_hex(16))

# Global canvas reference
canvas: t.Optional[Canvas] = None

# Global Redis pool reference
redis_pool: t.Optional[aioredis.Redis] = None


@app.exception_handler(StarletteHTTPException)
async def my_exception_handler(request: Request, exception: StarletteHTTPException) -> Response:
    """Custom exception handler to render template for 404 error."""
    if exception.status_code == 404:
        return templates.TemplateResponse("not_found.html", {"request": request})
    return Response(
        status_code=exception.status_code,
        content=exception.detail
    )


@app.on_event("startup")
async def startup() -> None:
    """Create a asyncpg connection pool on startup and setup logging."""
    # We have to make a global canvas object as there is no way for us to send an object to the following requests
    # from this function.
    # The global here isn't too bad, having many Canvas objects in use isn't even an issue.
    global canvas

    # Setup logging
    format_string = "[%(asctime)s] [%(process)d] [%(levelname)s] %(name)s - %(message)s"
    date_format_string = "%Y-%m-%d %H:%M:%S %z"
    logging.basicConfig(
        format=format_string,
        datefmt=date_format_string,
        level=getattr(logging, constants.log_level.upper())
    )

    # Init DB and Redis Connections
    await constants.DB_POOL

    # Make redis_pool global so other endpoints can get access to it.
    global redis_pool
    redis_pool = await aioredis.create_redis_pool(constants.redis_url)
    constants.REDIS_FUTURE.set_result(redis_pool)

    # Start background tasks
    app.state.rate_cleaner = asyncio.create_task(ratelimits.start_cleaner(constants.DB_POOL))

    canvas = Canvas(redis_pool)  # Global
    await canvas.sync_cache(await constants.DB_POOL.acquire())


@app.on_event("shutdown")
async def shutdown() -> None:
    """Close down the app."""
    app.state.rate_limit_cleaner.cancel()
    await constants.DB_POOL.close()


@app.middleware("http")
async def setup_data(request: Request, callnext: t.Callable) -> Response:
    """Get a connection from the pool and a canvas reference for this request."""
    async with constants.DB_POOL.acquire() as connection:
        request.state.db_conn = connection
        request.state.canvas = canvas
        request.state.auth = await authorized(connection, request.headers.get("Authorization"))
        response = await callnext(request)
    request.state.db_conn = None
    request.state.canvas = None
    return response


def build_oauth_token_request(code: str) -> t.Tuple[dict, dict]:
    """Given a code, return a dict of query params needed to complete the oath flow."""
    query = dict(
        client_id=constants.client_id,
        client_secret=constants.client_secret,
        grant_type="authorization_code",
        code=code,
        redirect_uri=f"{constants.base_url}/callback",
        scope="identify",
    )
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    return query, headers


@app.get("/callback", include_in_schema=False)
async def auth_callback(request: Request) -> Response:
    """
    Create the user given the authorization code and output the token.

    This endpoint is only used as a redirect target from discord.
    """
    code = request.query_params["code"]
    try:
        async with AsyncClient() as client:
            token_params, token_headers = build_oauth_token_request(code)
            token = (await client.post(constants.token_url, data=token_params, headers=token_headers)).json()
            auth_header = {"Authorization": f"Bearer {token['access_token']}"}
            user = (await client.get(constants.user_url, headers=auth_header)).json()
            token = await reset_user_token(request.state.db_conn, user["id"])
    except KeyError:
        # ensure that users don't land on the show_pixel page,
        log.error(traceback.format_exc())
        raise HTTPException(401, "Unknown error while creating token")
    except PermissionError:
        raise HTTPException(401, "You are banned")

    # Redirect so that a user doesn't refresh the page and spam discord
    token = auth_s.dumps(token)
    redirect = RedirectResponse("/show_token", status_code=303)
    redirect.set_cookie(
        key='token',
        value=token,
        httponly=True,
        max_age=10,
        path='/show_token',
    )
    return redirect


@app.get("/show_token", include_in_schema=False)
async def show_token(request: Request, token: str = Cookie(None)) -> Response:  # noqa: B008
    """Take a token from URL and show it."""
    template_name = "cookie_disabled.html"
    context = {"request": request}

    if token:
        token = auth_s.loads(token)
        context["token"] = token
        template_name = "api_token.html"

    return templates.TemplateResponse(template_name, context)


async def authorized(conn: Connection, authorization: t.Optional[str]) -> AuthResult:
    """Attempt to authorize the user given a token and a database connection."""
    if authorization is None:
        return AuthResult(AuthState.NO_TOKEN, None)
    scheme, token = get_authorization_scheme_param(authorization)
    if scheme.lower() != "bearer":
        return AuthResult(AuthState.BAD_HEADER, None)
    try:
        token_data = jwt.decode(token, constants.jwt_secret)
    except JWTError:
        return AuthResult(AuthState.INVALID_TOKEN, None)
    else:
        user_id = token_data["id"]
        token_salt = token_data["salt"]
        user_state = await conn.fetchrow(
            "SELECT is_banned, is_mod, key_salt FROM users WHERE user_id = $1;", int(user_id),
        )
        if user_state is None or user_state["key_salt"] != token_salt:
            return AuthResult(AuthState.INVALID_TOKEN, None)
        elif user_state["is_banned"]:
            return AuthResult(AuthState.BANNED, int(user_id))
        elif user_state["is_mod"]:
            return AuthResult(AuthState.MODERATOR, int(user_id))
        else:
            return AuthResult(AuthState.USER, int(user_id))


async def reset_user_token(conn: Connection, user_id: str) -> str:
    """
    Ensure a user exists and create a new token for them.

    If the user already exists, their token is regenerated and the old is invalidated.
    """
    # returns None if the user doesn't exist and false if they aren't banned
    is_banned = await conn.fetchval("SELECT is_banned FROM users WHERE user_id = $1", int(user_id))
    if is_banned:
        raise PermissionError
    # 22 long string
    token_salt = secrets.token_urlsafe(16)
    is_mod = user_id in constants.mods
    async with conn.transaction():
        await conn.execute(
            """INSERT INTO users (user_id, key_salt, is_mod) VALUES ($1, $2, $3)
            ON CONFLICT (user_id) DO UPDATE SET key_salt=$2;""",
            int(user_id),
            token_salt,
            is_mod,
        )
    jwt_data = dict(id=user_id, salt=token_salt)
    return jwt.encode(jwt_data, constants.jwt_secret, algorithm="HS256")


# ENDPOINTS
@app.get("/", tags=["General Endpoints"])
async def docs(request: Request) -> Response:
    """Return the API docs."""
    template_name = "docs.html"
    return templates.TemplateResponse(template_name, {"request": request})


@app.get("/mod", tags=["Moderation Endpoints"])
async def mod_check(request: Request) -> dict:
    """Check if the authenticated user is a mod."""
    request.state.auth.raise_unless_mod()
    return {"Message": "Hello fellow moderator!"}


@app.post("/set_mod", tags=["Moderation Endpoints"])
async def set_mod(request: Request, user: User) -> dict:
    """Make another user a mod."""
    user_id = user.user_id
    request.state.auth.raise_unless_mod()
    conn = request.state.db_conn
    async with conn.transaction():
        user_state = await conn.fetchrow(
            "SELECT is_mod FROM users WHERE user_id = $1;", user_id,
        )
        if user_state is None:
            return {"Message": f"User with user_id {user_id} does not exist."}
        elif user_state['is_mod']:
            return {"Message": f"User with user_id {user_id} is already a mod."}

        await conn.execute(
            """
            UPDATE users SET is_mod = true WHERE user_id = $1;
        """,
            user_id,
        )
    return {"Message": f"Successfully set user with user_id {user_id} to mod"}


@app.post("/mod_ban", tags=["Moderation Endpoints"])
async def ban_users(request: Request, user_list: t.List[User]) -> dict:
    """Ban users from using the API."""
    request.state.auth.raise_unless_mod()

    conn = request.state.db_conn
    users = [user.user_id for user in user_list]

    # Should be fetched from cache whenever it is implemented.
    sql = "SELECT * FROM users WHERE user_id=any($1::bigint[])"
    records = await conn.fetch(sql, tuple(users))
    db_users = [record["user_id"] for record in records]

    non_db_users = set(users)-set(db_users)

    # Ref:
    # https://magicstack.github.io/asyncpg/current/faq.html#why-do-i-get-postgressyntaxerror-when-using-expression-in-1
    sql = "UPDATE users SET is_banned=TRUE where user_id=any($1::bigint[])"

    await conn.execute(
        sql, db_users
    )

    resp = {"Banned": db_users}
    if non_db_users:
        resp["Not Found"] = non_db_users

    return resp


@app.get("/pixel_history", tags=["Moderation Endpoints"])
async def pixel_history(
        request: Request,
        x: int = constants.x_query_validator,
        y: int = constants.y_query_validator
) -> dict:
    """GET the user who edited the pixel with the given co-ordinates."""
    request.state.auth.raise_unless_mod()

    conn = request.state.db_conn

    sql = """
    select user_id
    from pixel_history
    where x=$1
    and y=$2
    and not deleted
    order by pixel_history_id desc
    limit 1
    """
    record = await conn.fetchrow(sql, x, y)

    if not record:
        return {"Message": f"No user history for pixel ({x}, {y})"}

    user_id = record["user_id"]

    return {
        "user_id": user_id
    }


@app.get("/authorize", tags=["Authorization Endpoints"])
async def authorize() -> Response:
    """
    Redirect the user to discord authorization, the flow continues in /callback.

    Unlike other endpoints, you should open this one in the browser, since it redirects to a discord website.
    """
    return RedirectResponse(url=constants.auth_uri)


@app.get("/get_size", tags=["Canvas Endpoints"])
async def get_size(request: Request) -> dict:
    """Get the size of the pixels canvas."""
    return dict(width=constants.width, height=constants.height)


@app.get("/get_pixels", tags=["Canvas Endpoints"])
@ratelimits.UserRedis(requests=5, time_unit=10, cooldown=20)
async def get_pixels(request: Request) -> Response:
    """
    Get the current state of all pixels from the db.

    Requires a valid token in an Authorization header.
    """
    request.state.auth.raise_if_failed()
    # The cast to bytes here is needed by FastAPI ¯\_(ツ)_/¯
    return Response(bytes(await request.state.canvas.get_pixels()), media_type="application/octet-stream")


@app.post("/set_pixel", tags=["Canvas Endpoints"])
@ratelimits.UserRedis(requests=1, time_unit=constants.PIXEL_RATE_LIMIT, cooldown=300)
async def set_pixel(request: Request, pixel: Pixel) -> dict:
    """
    Create a new pixel at the specified position with the specified color.

    Override any pixel already at the same position.

    Requires a valid token in an Authorization header.

    missing Ratelimit
    """
    request.state.auth.raise_if_failed()
    log.info(f"{request.state.auth.user_id} is setting {pixel.x}, {pixel.y} to {pixel.rgb}")
    await request.state.canvas.set_pixel(request.state.db_conn, pixel.x, pixel.y, pixel.rgb, request.state.auth.user_id)
    return dict(message=f"added pixel at x={pixel.x},y={pixel.y} of color {pixel.rgb}")


@app.post("/webhook", tags=["Webhook Endpoints"])
async def webhook(request: Request) -> Response:
    """Send or update Discord webhook image."""
    request.state.auth.raise_unless_mod()

    last_message_id = await redis_pool.get("last-webhook-message")

    now = datetime.now()

    # Generate payload that will be sent in payload_json
    data = {
        "content": "",
        "embeds": [{
            "title": "Pixels State",
            "image": {
                "url": f"attachment://pixels_{now.timestamp()}.png"
            },
            "footer": {
                "text": "Last updated"
            },
            "timestamp": now.isoformat()
        }]
    }

    # Run Pillow stuff in executor because these actions are blocking
    loop = asyncio.get_event_loop()
    image = await loop.run_in_executor(
        None,
        partial(
            Image.frombytes,
            "RGB",
            (constants.width, constants.height),
            bytes(await request.state.canvas.get_pixels())
        )
    )

    # Increase size of image so this looks better in Discord
    image = await loop.run_in_executor(
        None,
        partial(
            image.resize,
            (constants.width * 10, constants.height * 10),
            Image.NEAREST
        )
    )

    # BytesIO gives file-like interface for saving
    # and later this is able to get actual content what will be sent.
    file = io.BytesIO()
    await loop.run_in_executor(None, partial(image.save, file, format="PNG"))

    # Name file to pixels.png
    files = {
        "file": (f"pixels_{now.timestamp()}.png", file.getvalue(), "image/png")
    }

    async with AsyncClient(timeout=None) as client:
        # If last message exists in cache, try to edit this
        if last_message_id is not None:
            data["attachments"] = []
            edit_resp = await client.patch(
                f"{constants.webhook_url}/messages/{int(last_message_id)}",
                data={"payload_json": json.dumps(data)},
                files=files
            )

            if edit_resp.status_code != 200:
                last_message_id = None

        # If no message is found in cache or message is missing, create new message
        if last_message_id is None:
            # If we are sending new message, don't specify attachments
            data.pop("attachments", None)
            # Username can be only set on sending
            data["username"] = "Pixels"
            create_resp = (await client.post(
                constants.webhook_url,
                data={"payload_json": json.dumps(data)},
                files=files
            )).json()

            await redis_pool.set("last-webhook-message", create_resp["id"])

    return Response(status_code=204)
